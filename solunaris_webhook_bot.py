import os
import re
import json
import time
import asyncio
import socket
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import tasks

import requests
from ftplib import FTP

STATE_FILE = "bot_state.json"


# -----------------------------
# Helpers / State
# -----------------------------
def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


STATE = load_state()


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    return v if v not in (None, "") else default


def must_env(name: str) -> str:
    v = env(name)
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v


def now_ts() -> int:
    return int(time.time())


# -----------------------------
# RCON (simple Source-style; works for many hosting panels)
# If your ASA RCON differs, keep your existing working RCON bits
# and just keep the parsing + bot logic below.
# -----------------------------
class SimpleRCON:
    """
    Minimal RCON client for "Source" style RCON.
    If your current setup already runs ListPlayers via RCON successfully,
    this should work.
    """
    SERVERDATA_AUTH = 3
    SERVERDATA_AUTH_RESPONSE = 2
    SERVERDATA_EXECCOMMAND = 2
    SERVERDATA_RESPONSE_VALUE = 0

    def __init__(self, host: str, port: int, password: str, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self.req_id = 0

    def _pack(self, req_id: int, req_type: int, body: str) -> bytes:
        data = body.encode("utf-8") + b"\x00\x00"
        size = 4 + 4 + len(data)
        return size.to_bytes(4, "little", signed=True) + req_id.to_bytes(
            4, "little", signed=True
        ) + req_type.to_bytes(4, "little", signed=True) + data

    def _recv_packet(self) -> Tuple[int, int, str]:
        # read size
        raw = self.sock.recv(4)
        if len(raw) < 4:
            raise ConnectionError("RCON: incomplete packet size")
        size = int.from_bytes(raw, "little", signed=True)
        payload = b""
        while len(payload) < size:
            chunk = self.sock.recv(size - len(payload))
            if not chunk:
                break
            payload += chunk
        if len(payload) < size:
            raise ConnectionError("RCON: incomplete packet payload")

        req_id = int.from_bytes(payload[0:4], "little", signed=True)
        req_type = int.from_bytes(payload[4:8], "little", signed=True)
        body = payload[8:-2].decode("utf-8", errors="replace")
        return req_id, req_type, body

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)

        self.req_id += 1
        self.sock.sendall(self._pack(self.req_id, self.SERVERDATA_AUTH, self.password))

        # auth response often returns 2 packets; consume until we see auth response
        authed = False
        for _ in range(4):
            rid, rtype, _ = self._recv_packet()
            if rtype == self.SERVERDATA_AUTH_RESPONSE:
                if rid == -1:
                    raise PermissionError("RCON auth failed (bad password)")
                authed = True
                break
        if not authed:
            # some servers respond differently; if we're here, still try
            pass

    def close(self) -> None:
        try:
            if self.sock:
                self.sock.close()
        finally:
            self.sock = None

    def command(self, cmd: str) -> str:
        if not self.sock:
            self.connect()

        self.req_id += 1
        rid = self.req_id
        self.sock.sendall(self._pack(rid, self.SERVERDATA_EXECCOMMAND, cmd))

        # read until we get our response id (may be multiple chunks)
        out = []
        for _ in range(20):
            pr_id, pr_type, body = self._recv_packet()
            if pr_id == rid and pr_type in (self.SERVERDATA_RESPONSE_VALUE, self.SERVERDATA_AUTH_RESPONSE):
                out.append(body)
                # heuristic: empty body often indicates end
                if body == "":
                    break
            else:
                # ignore unrelated packets
                pass
        return "".join(out).strip()


# -----------------------------
# FTP Log Parsing (for character names + in-game time)
# -----------------------------
JOIN_RE = re.compile(r"^(?P<char>.+?) \[UniqueNetId:(?P<id>[0-9a-fA-F]+)\s+Platform:(?P<plat>[A-Z0-9]+)\] joined this ARK!")
DAYTIME_RE = re.compile(r"Day\s+\d+,\s+\d{1,2}:\d{2}:\d{2}")

@dataclass
class PlayerEntry:
    display: str          # platform name (gamertag/steam/psn)
    unique_id: str        # EOS/UniqueNetId
    character: Optional[str] = None


class LogCache:
    def __init__(self):
        self.id_to_character: Dict[str, str] = {}
        self.last_ingame_time: Optional[str] = None
        self.last_sync_ts: int = 0

    def update_from_log_text(self, log_text: str) -> None:
        # build mapping from join lines
        for line in log_text.splitlines():
            m = JOIN_RE.search(line)
            if m:
                uid = m.group("id").lower()
                char = m.group("char").strip()
                # prefer latest
                self.id_to_character[uid] = char

        # find most recent "Day X, HH:MM:SS"
        last = None
        for line in reversed(log_text.splitlines()):
            m = DAYTIME_RE.search(line)
            if m:
                last = m.group(0)
                break
        if last:
            self.last_ingame_time = last

        self.last_sync_ts = now_ts()


LOGCACHE = LogCache()


def fetch_log_via_ftp() -> str:
    host = must_env("FTP_HOST")
    port = int(env("FTP_PORT", "21"))
    user = must_env("FTP_USER")
    password = must_env("FTP_PASS")
    path = env("FTP_LOG_PATH", "arksa/ShooterGame/Saved/Logs")
    filename = env("FTP_LOG_FILE", "ShooterGame.log")

    ftp = FTP()
    ftp.connect(host, port, timeout=10)
    ftp.login(user, password)

    # Nitrado paths sometimes need stepping folder by folder
    if path:
        for part in path.split("/"):
            if part:
                ftp.cwd(part)

    chunks: List[bytes] = []
    ftp.retrbinary(f"RETR {filename}", chunks.append)
    ftp.quit()

    data = b"".join(chunks)
    # log is usually UTF-8; fall back safely
    return data.decode("utf-8", errors="replace")


# -----------------------------
# Webhook message (edit-only)
# -----------------------------
def ensure_webhook_message_id(webhook_url: str) -> str:
    # stored per webhook URL so you can reuse code for multiple servers later
    key = f"webhook_msg_id::{webhook_url}"
    msg_id = STATE.get(key)
    if msg_id:
        return str(msg_id)

    # Create one initial message, then store its id
    resp = requests.post(webhook_url, json={"content": "", "embeds": [{
        "title": "Online Players",
        "description": "Initializing…",
        "color": 0x2ECC71
    }]}, timeout=10)

    resp.raise_for_status()
    data = resp.json()
    new_id = str(data["id"])
    STATE[key] = new_id
    save_state(STATE)
    return new_id


def edit_webhook_message(webhook_url: str, message_id: str, embeds: list) -> None:
    # Discord webhook edit endpoint:
    # PATCH {webhook_url}/messages/{message_id}
    url = webhook_url.rstrip("/") + f"/messages/{message_id}"
    resp = requests.patch(url, json={"content": "", "embeds": embeds}, timeout=10)
    resp.raise_for_status()


# -----------------------------
# Bot setup
# -----------------------------
INTENTS = discord.Intents.default()
client = discord.Client(intents=INTENTS)
tree = app_commands.CommandTree(client)

DISCORD_TOKEN = must_env("DISCORD_TOKEN")

RCON_HOST = must_env("RCON_HOST")
RCON_PORT = int(must_env("RCON_PORT"))
RCON_PASSWORD = must_env("RCON_PASSWORD")

PLAYERS_WEBHOOK_URL = must_env("PLAYERS_WEBHOOK_URL")

STATUS_CHANNEL_ID = int(env("STATUS_CHANNEL_ID", "0") or 0)
ANNOUNCEMENT_CHANNEL_ID = int(env("ANNOUNCEMENT_CHANNEL_ID", "0") or 0)

# how often to refresh the online player message
REFRESH_SECONDS = int(env("REFRESH_SECONDS", "60") or 60)


def rcon_list_players() -> str:
    r = SimpleRCON(RCON_HOST, RCON_PORT, RCON_PASSWORD, timeout=8)
    try:
        return r.command("ListPlayers")
    finally:
        r.close()


def parse_listplayers(text: str) -> List[PlayerEntry]:
    """
    Example lines you showed:
      0. kjrobinsonn, 0002027eea6e4d69bea553b53a343124
    """
    players: List[PlayerEntry] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # accept either "0." or "01)" style
        m = re.search(r"^\s*(?:\d+[\.\)]\s*)?([^,]+)\s*,\s*([0-9a-fA-F]{16,})\s*$", line)
        if not m:
            continue
        display = m.group(1).strip()
        uid = m.group(2).strip().lower()
        players.append(PlayerEntry(display=display, unique_id=uid))
    return players


def apply_character_names(players: List[PlayerEntry]) -> None:
    for p in players:
        p.character = LOGCACHE.id_to_character.get(p.unique_id)


def build_players_embed(players: List[PlayerEntry], max_players: int = 42) -> list:
    count = len(players)
    title = "Online Players"
    desc_lines = [f"**{count}/{max_players}** online"]

    if count == 0:
        desc_lines.append("\nNo one online.")
    else:
        desc_lines.append("")  # spacer

        # show character name if we have it, otherwise platform display
        for i, p in enumerate(players, start=1):
            name = p.character or p.display
            desc_lines.append(f"{i:02d}) {name}")

    if LOGCACHE.last_ingame_time:
        desc_lines.append("")
        desc_lines.append(f"**In-game time:** {LOGCACHE.last_ingame_time}")

    embeds = [{
        "title": title,
        "description": "\n".join(desc_lines),
        "color": 0x2ECC71,
        "footer": {"text": f"Last update: <t:{now_ts()}:R>"}
    }]
    return embeds


async def refresh_players_webhook() -> None:
    # update log cache occasionally so character mapping stays fresh
    # (cheap guard to avoid hammering FTP every minute)
    if now_ts() - LOGCACHE.last_sync_ts > 300:
        try:
            log_text = await asyncio.to_thread(fetch_log_via_ftp)
            LOGCACHE.update_from_log_text(log_text)
        except Exception:
            # ignore; RCON list still works
            pass

    raw = await asyncio.to_thread(rcon_list_players)
    players = parse_listplayers(raw)
    apply_character_names(players)

    msg_id = ensure_webhook_message_id(PLAYERS_WEBHOOK_URL)
    embeds = build_players_embed(players)
    await asyncio.to_thread(edit_webhook_message, PLAYERS_WEBHOOK_URL, msg_id, embeds)


@tasks.loop(seconds=REFRESH_SECONDS)
async def players_loop():
    try:
        await refresh_players_webhook()
    except Exception as e:
        print(f"[players_loop] error: {e}")


# -----------------------------
# Slash commands
# -----------------------------
@tree.command(name="status", description="Show server status and update the online players message.")
async def status_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=False, ephemeral=True)

    # force a webhook refresh and also sync log once (so names/time improves)
    try:
        try:
            log_text = await asyncio.to_thread(fetch_log_via_ftp)
            LOGCACHE.update_from_log_text(log_text)
        except Exception:
            pass

        await refresh_players_webhook()

        # Build a quick status message (simple: online if RCON responded)
        ingame = LOGCACHE.last_ingame_time or "Unknown"
        content = f"Server looks **online** (RCON OK)\nIn-game time: **{ingame}**"
        await interaction.followup.send(content, ephemeral=True)

        # Optional: also post a status message to your STATUS channel (if set)
        if STATUS_CHANNEL_ID:
            ch = client.get_channel(STATUS_CHANNEL_ID)
            if isinstance(ch, discord.TextChannel):
                await ch.send(f"Status update: Server **online** | In-game time: **{ingame}**")

    except Exception as e:
        await interaction.followup.send(f"Status check failed: `{e}`", ephemeral=True)


@tree.command(name="synctime", description="Sync in-game time from Nitrado logs (FTP) and update the online players message.")
async def synctime_cmd(interaction: discord.Interaction):
    await interaction.response.defer(thinking=False, ephemeral=True)
    try:
        log_text = await asyncio.to_thread(fetch_log_via_ftp)
        LOGCACHE.update_from_log_text(log_text)

        # after syncing time, bump the players webhook so it shows new time too
        await refresh_players_webhook()

        if LOGCACHE.last_ingame_time:
            await interaction.followup.send(
                f"Synced in-game time: **{LOGCACHE.last_ingame_time}**",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                "Synced logs, but couldn’t find a `Day X, HH:MM:SS` line yet.",
                ephemeral=True
            )
    except Exception as e:
        await interaction.followup.send(f"Synctime failed: `{e}`", ephemeral=True)


# -----------------------------
# Startup
# -----------------------------
@client.event
async def on_ready():
    print(f"Logged in as {client.user} (id={client.user.id})")
    try:
        synced = await tree.sync()
        print(f"Synced {len(synced)} commands.")
    except Exception as e:
        print(f"Slash sync error: {e}")

    if not players_loop.is_running():
        players_loop.start()


def main():
    client.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()