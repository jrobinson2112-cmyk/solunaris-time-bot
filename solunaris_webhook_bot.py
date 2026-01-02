import os
import re
import time
import json
import asyncio
import socket
import struct
from typing import Optional, Tuple, Dict, List

import aiohttp
import discord
from discord import app_commands
from ftplib import FTP

# =========================================================
# CONFIG / ENV
# =========================================================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TIME_WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PLAYERS_WEBHOOK_URL = os.getenv("PLAYERS_WEBHOOK_URL")

NITRADO_TOKEN = os.getenv("NITRADO_TOKEN")
NITRADO_SERVICE_ID = os.getenv("NITRADO_SERVICE_ID")

RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = int(os.getenv("RCON_PORT", "11020"))
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

FTP_HOST = os.getenv("FTP_HOST")
FTP_PORT = int(os.getenv("FTP_PORT", "21"))
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")
FTP_LOG_DIR = os.getenv("FTP_LOG_DIR", "ShooterGame/Saved/Logs")

STATUS_VC_ID = int(os.getenv("STATUS_VC_ID", "0"))
PLAYER_CAP = int(os.getenv("PLAYER_CAP", "42"))

GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

# Time calibration speeds
DAY_SECONDS_PER_INGAME_MINUTE = 4.7666667
NIGHT_SECONDS_PER_INGAME_MINUTE = 4.045

# Day: 05:30->17:30
SUNRISE_MIN = 5 * 60 + 30
SUNSET_MIN  = 17 * 60 + 30

DAY_COLOR = 0xF1C40F
NIGHT_COLOR = 0x5865F2

# Loops
STATUS_POLL_SECONDS = 15
FORCE_UPDATE_SECONDS = 600   # 10 mins
FTP_SCAN_SECONDS = 30        # scan logs every 30s

# Files
STATE_FILE = "state.json"
WEBHOOKS_FILE = "webhooks.json"
MAP_FILE = "eos_map.json"
FTP_STATE_FILE = "ftp_state.json"

# =========================================================
# VALIDATE REQUIRED
# =========================================================
missing = []
for k, v in [
    ("DISCORD_TOKEN", DISCORD_TOKEN),
    ("WEBHOOK_URL", TIME_WEBHOOK_URL),
    ("PLAYERS_WEBHOOK_URL", PLAYERS_WEBHOOK_URL),
    ("NITRADO_TOKEN", NITRADO_TOKEN),
    ("NITRADO_SERVICE_ID", NITRADO_SERVICE_ID),
    ("RCON_HOST", RCON_HOST),
    ("RCON_PASSWORD", RCON_PASSWORD),
    ("FTP_HOST", FTP_HOST),
    ("FTP_USER", FTP_USER),
    ("FTP_PASS", FTP_PASS),
    ("STATUS_VC_ID", str(STATUS_VC_ID) if STATUS_VC_ID else None),
]:
    if not v:
        missing.append(k)

if missing:
    raise RuntimeError(f"Missing env var(s): {', '.join(missing)}")

NITRADO_SERVICE_ID = str(NITRADO_SERVICE_ID)

# =========================================================
# DISCORD SETUP
# =========================================================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# =========================================================
# JSON HELPERS
# =========================================================
def load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# Time calibration state
state = load_json(STATE_FILE, None)

# Webhook message ids
webhooks = load_json(WEBHOOKS_FILE, {"time_msg_id": None, "players_msg_id": None})

# Mapping: EOS_ID -> {"char": "...", "platform": "..."}
eos_map: Dict[str, Dict[str, str]] = load_json(MAP_FILE, {})

# FTP cursor state
ftp_state = load_json(FTP_STATE_FILE, {"file": None, "pos": 0})

# =========================================================
# TIME CALC (piecewise day/night)
# =========================================================
def is_day(minute_of_day: int) -> bool:
    return SUNRISE_MIN <= minute_of_day < SUNSET_MIN

def spm(minute_of_day: int) -> float:
    return DAY_SECONDS_PER_INGAME_MINUTE if is_day(minute_of_day) else NIGHT_SECONDS_PER_INGAME_MINUTE

def advance_piecewise(start_day: int, start_minute: int, elapsed_seconds: float) -> Tuple[int, int]:
    day = int(start_day)
    minute = float(start_minute)
    rem = float(elapsed_seconds)

    for _ in range(20000):
        if rem <= 0:
            break

        m_int = int(minute) % 1440
        cur_spm = spm(m_int)

        # next boundary
        if is_day(m_int):
            boundary_total = (day - 1) * 1440 + SUNSET_MIN
        else:
            if m_int < SUNRISE_MIN:
                boundary_total = (day - 1) * 1440 + SUNRISE_MIN
            else:
                boundary_total = day * 1440 + SUNRISE_MIN

        current_total = (day - 1) * 1440 + minute
        mins_to_boundary = boundary_total - current_total
        if mins_to_boundary < 0:
            mins_to_boundary = 0

        sec_to_boundary = mins_to_boundary * cur_spm

        if sec_to_boundary > 0 and rem >= sec_to_boundary:
            rem -= sec_to_boundary
            minute += mins_to_boundary
        else:
            minute += (rem / cur_spm) if cur_spm > 0 else 0
            rem = 0

        while minute >= 1440:
            minute -= 1440
            day += 1

    return day, int(minute) % 1440

def calc_time():
    if not state:
        return None

    elapsed = time.time() - float(state["real_epoch"])
    start_day = int(state["day"])
    start_year = int(state["year"])
    start_minute_of_day = int(state["hour"]) * 60 + int(state["minute"])

    day_num, minute_of_day = advance_piecewise(start_day, start_minute_of_day, elapsed)

    year = start_year
    while day_num > 365:
        day_num -= 365
        year += 1

    hour = minute_of_day // 60
    minute = minute_of_day % 60

    day_now = is_day(minute_of_day)
    emoji = "☀️" if day_now else "🌙"
    color = DAY_COLOR if day_now else NIGHT_COLOR
    cur_spm = DAY_SECONDS_PER_INGAME_MINUTE if day_now else NIGHT_SECONDS_PER_INGAME_MINUTE

    title = f"{emoji} | Solunaris Time | {hour:02d}:{minute:02d} | Day {day_num} | Year {year}"
    return title, color, cur_spm

# =========================================================
# SOURCE RCON (minimal)
# =========================================================
SERVERDATA_AUTH = 3
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_RESPONSE_VALUE = 0

class RCONClient:
    def __init__(self, host: str, port: int, password: str, timeout: float = 4.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self._rid = 10

    def _next_id(self):
        self._rid += 1
        return self._rid

    def _send(self, rid: int, ptype: int, body: str):
        payload = struct.pack("<ii", rid, ptype) + body.encode("utf-8") + b"\x00\x00"
        self.sock.sendall(struct.pack("<i", len(payload)) + payload)

    def _recv(self) -> Tuple[int, int, str]:
        raw_len = self.sock.recv(4)
        if len(raw_len) < 4:
            raise RuntimeError("RCON: short length")
        (size,) = struct.unpack("<i", raw_len)
        data = b""
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                break
            data += chunk
        if len(data) < 8:
            raise RuntimeError("RCON: short packet")
        rid, ptype = struct.unpack("<ii", data[:8])
        body = data[8:-2].decode("utf-8", errors="replace")
        return rid, ptype, body

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)
        rid = self._next_id()
        self._send(rid, SERVERDATA_AUTH, self.password)

        # auth response may include extra packets
        authed = False
        for _ in range(4):
            rrid, rtype, _ = self._recv()
            if rtype == SERVERDATA_AUTH_RESPONSE:
                authed = (rrid != -1)
                break
        if not authed:
            raise RuntimeError("RCON auth failed")

    def command(self, cmd: str) -> str:
        rid = self._next_id()
        self._send(rid, SERVERDATA_EXECCOMMAND, cmd)

        out = []
        end_time = time.time() + self.timeout
        while time.time() < end_time:
            try:
                rrid, rtype, body = self._recv()
                if rrid != rid:
                    continue
                if body:
                    out.append(body)
                else:
                    break
            except socket.timeout:
                break
        return "\n".join(out).strip()

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        finally:
            self.sock = None

async def rcon_listplayers() -> List[Tuple[str, str]]:
    """
    Returns list of (platform_name, eos_id) from ListPlayers output.
    Example line: "0. Name, 0002abcd..."
    """
    def _do():
        rc = RCONClient(RCON_HOST, RCON_PORT, RCON_PASSWORD, timeout=4.0)
        try:
            rc.connect()
            raw = rc.command("ListPlayers")
        finally:
            rc.close()
        return raw

    raw = await asyncio.to_thread(_do)

    out = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        # remove leading "0." / "0)"
        s = re.sub(r"^\s*\d+\s*[\.\)]\s*", "", s)
        if "," in s:
            left, right = s.split(",", 1)
            platform = left.strip()
            eos = right.strip()
            if platform and eos:
                out.append((platform, eos))
        else:
            # no eos, just name
            out.append((s.strip(), ""))
    return out

# =========================================================
# FTP LOG MAPPING
# =========================================================

# Try to capture both EOS id + character + platform from logs.
# Your exact log wording may differ; this is best-effort and learns over time.
EOS_RX = re.compile(r"\b[0-9a-f]{32}\b", re.IGNORECASE)

# Candidate patterns (we store any char names we can find next to EOS ids)
PATTERNS = [
    # "... EOS: <id> ... Character: <char> ..."
    re.compile(r"(?P<eos>[0-9a-f]{32}).*?(?:Character|Survivor)\s*[:=]\s*(?P<char>[^,\n\r]+)", re.IGNORECASE),
    # "... CharacterName=<char> ... <eosid> ..."
    re.compile(r"(?:CharacterName|SurvivorName)\s*[:=]\s*(?P<char>[^,\n\r]+).*?(?P<eos>[0-9a-f]{32})", re.IGNORECASE),
    # "<char> ... <eosid>"
    re.compile(r"(?P<char>[A-Za-z0-9 _'\-]{2,32}).*?(?P<eos>[0-9a-f]{32})", re.IGNORECASE),
]

def ftp_get_latest_log(ftp: FTP) -> Optional[str]:
    ftp.cwd(FTP_LOG_DIR)
    files = ftp.nlst()
    # prefer .log, else anything
    logs = [f for f in files if f.lower().endswith(".log")]
    return logs[-1] if logs else (files[-1] if files else None)

def ftp_read_new_bytes(latest_file: str) -> bytes:
    global ftp_state
    with FTP() as ftp:
        ftp.connect(FTP_HOST, FTP_PORT, timeout=10)
        ftp.login(FTP_USER, FTP_PASS)
        ftp.cwd(FTP_LOG_DIR)

        try:
            size = ftp.size(latest_file) or 0
        except Exception:
            size = 0

        last_file = ftp_state.get("file")
        last_pos = int(ftp_state.get("pos", 0))

        if last_file != latest_file or last_pos > size:
            last_pos = 0
            ftp_state["file"] = latest_file
            ftp_state["pos"] = 0

        if size <= last_pos:
            return b""

        chunks: List[bytes] = []
        def _cb(data: bytes):
            chunks.append(data)

        ftp.sendcmd("TYPE I")
        ftp.retrbinary(f"RETR {latest_file}", _cb, rest=last_pos)

        new_data = b"".join(chunks)
        ftp_state["pos"] = last_pos + len(new_data)
        return new_data

def update_map_from_text(text: str):
    global eos_map
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue

        # find eos in line
        eos_match = EOS_RX.search(s)
        if not eos_match:
            continue
        eos = eos_match.group(0).lower()

        # attempt to find character name near it
        char = None
        for rx in PATTERNS:
            m = rx.search(s)
            if m:
                eos2 = m.group("eos").lower() if "eos" in m.groupdict() else eos
                if eos2 != eos:
                    continue
                c = (m.group("char") or "").strip()
                # basic sanity
                if 2 <= len(c) <= 32:
                    char = c
                    break

        if char:
            rec = eos_map.get(eos, {})
            rec["char"] = char
            eos_map[eos] = rec

async def ftp_loop():
    while True:
        try:
            def _pick():
                with FTP() as ftp:
                    ftp.connect(FTP_HOST, FTP_PORT, timeout=10)
                    ftp.login(FTP_USER, FTP_PASS)
                    ftp.cwd(FTP_LOG_DIR)
                    files = ftp.nlst()
                    logs = [f for f in files if f.lower().endswith(".log")]
                    return logs[-1] if logs else (files[-1] if files else None)

            latest = await asyncio.to_thread(_pick)
            if latest:
                new_bytes = await asyncio.to_thread(ftp_read_new_bytes, latest)
                if new_bytes:
                    txt = new_bytes.decode("utf-8", errors="ignore")
                    update_map_from_text(txt)
                    save_json(MAP_FILE, eos_map)
                    save_json(FTP_STATE_FILE, ftp_state)
        except Exception:
            pass

        await asyncio.sleep(FTP_SCAN_SECONDS)

# =========================================================
# NITRADO STATUS
# =========================================================
async def nitrado_status(session: aiohttp.ClientSession) -> Tuple[bool, Optional[int]]:
    url = f"https://api.nitrado.net/services/{NITRADO_SERVICE_ID}/gameservers"
    headers = {"Authorization": f"Bearer {NITRADO_TOKEN}"}
    async with session.get(url, headers=headers, timeout=8) as resp:
        js = await resp.json()
    gs = js["data"]["gameserver"]
    status = (gs.get("status") or "").lower()
    online = status in ("started", "running", "online")
    q = gs.get("query") or {}
    cur = q.get("player_current") or q.get("players")
    try:
        cur = int(cur) if cur is not None else None
    except Exception:
        cur = None
    return online, cur

# =========================================================
# WEBHOOK UPDATE HELPERS
# =========================================================
async def upsert_webhook_embed(session: aiohttp.ClientSession, webhook_url: str, msg_id: Optional[str], embed: dict) -> str:
    if msg_id:
        async with session.patch(f"{webhook_url}/messages/{msg_id}", json={"embeds": [embed]}) as r:
            if r.status in (200, 204):
                return msg_id
            if r.status != 404:
                # non-404 failure; still try create
                pass

    async with session.post(webhook_url + "?wait=true", json={"embeds": [embed]}) as r:
        data = await r.json()
        return data["id"]

# =========================================================
# LOOPS
# =========================================================
async def time_loop():
    await client.wait_until_ready()
    async with aiohttp.ClientSession() as session:
        while True:
            if state:
                out = calc_time()
                if out:
                    title, color, cur_spm = out
                    embed = {"title": f"**{title}**", "color": color}
                    try:
                        webhooks["time_msg_id"] = await upsert_webhook_embed(
                            session, TIME_WEBHOOK_URL, webhooks.get("time_msg_id"), embed
                        )
                        save_json(WEBHOOKS_FILE, webhooks)
                    except Exception:
                        pass
                    await asyncio.sleep(float(cur_spm))
                    continue
            await asyncio.sleep(DAY_SECONDS_PER_INGAME_MINUTE)

async def status_and_players_loop():
    await client.wait_until_ready()
    guild = client.get_guild(GUILD_ID)

    last_status_key = None
    last_players_text = None
    last_force = 0.0

    async with aiohttp.ClientSession() as session:
        while True:
            now = time.time()
            force = (now - last_force) >= FORCE_UPDATE_SECONDS

            # status
            try:
                online, api_count = await nitrado_status(session)
            except Exception:
                online, api_count = False, None

            # players from RCON
            try:
                rcon_players = await rcon_listplayers()
            except Exception:
                rcon_players = []

            # count
            count = api_count if isinstance(api_count, int) else len(rcon_players)

            # rename status VC
            emoji = "🟢" if online else "🔴"
            vc_name = f"{emoji} Solunaris {count}/{PLAYER_CAP}"

            status_key = (online, count)

            if guild:
                ch = guild.get_channel(STATUS_VC_ID)
                if ch and isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
                    if force or status_key != last_status_key or ch.name != vc_name:
                        try:
                            await ch.edit(name=vc_name)
                        except Exception:
                            pass

            # build players list using mapping
            lines = []
            for i, (platform, eos) in enumerate(rcon_players, start=1):
                eos_l = eos.lower() if eos else ""
                char = eos_map.get(eos_l, {}).get("char")
                if char:
                    lines.append(f"{i:02d}) {char} - {platform}")
                else:
                    # fallback until mapping is learned
                    lines.append(f"{i:02d}) {platform}")

            header = f"**Solunaris**\nPlayers: **{len(rcon_players)}/{PLAYER_CAP}**\n"
            body = "\n".join(lines) if lines else "_No players online._"
            desc = header + body
            if len(desc) > 4000:
                desc = header + "\n".join(lines[:120]) + "\n…"

            players_text = desc

            if force or players_text != last_players_text:
                embed = {
                    "title": "Online Players",
                    "description": players_text,
                    "color": 0x2ECC71 if online else 0xE74C3C,
                }
                try:
                    webhooks["players_msg_id"] = await upsert_webhook_embed(
                        session, PLAYERS_WEBHOOK_URL, webhooks.get("players_msg_id"), embed
                    )
                    save_json(WEBHOOKS_FILE, webhooks)
                except Exception:
                    pass
                last_players_text = players_text

            if force or status_key != last_status_key:
                last_status_key = status_key
                last_force = now

            await asyncio.sleep(STATUS_POLL_SECONDS)

# =========================================================
# SLASH COMMANDS
# =========================================================
def has_admin_role(interaction: discord.Interaction) -> bool:
    try:
        return any(r.id == ADMIN_ROLE_ID for r in interaction.user.roles)
    except Exception:
        return False

@tree.command(name="day", description="Show current Solunaris time", guild=discord.Object(id=GUILD_ID))
async def cmd_day(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    out = calc_time()
    if not out:
        await interaction.followup.send("⏳ Time not set yet.", ephemeral=True)
        return
    title, _, _ = out
    await interaction.followup.send(title, ephemeral=True)

@tree.command(name="settime", description="Set Solunaris time", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(year="Year number", day="Day 1-365", hour="Hour 0-23", minute="Minute 0-59")
async def cmd_settime(interaction: discord.Interaction, year: int, day: int, hour: int, minute: int):
    await interaction.response.defer(ephemeral=True)
    if not has_admin_role(interaction):
        await interaction.followup.send("❌ No permission.", ephemeral=True)
        return
    if year < 1 or day < 1 or day > 365 or hour < 0 or hour > 23 or minute < 0 or minute > 59:
        await interaction.followup.send("❌ Invalid values.", ephemeral=True)
        return

    global state
    state = {
        "real_epoch": time.time(),
        "year": int(year),
        "day": int(day),
        "hour": int(hour),
        "minute": int(minute),
    }
    save_json(STATE_FILE, state)
    await interaction.followup.send(f"✅ Set to Day {day}, {hour:02d}:{minute:02d}, Year {year}", ephemeral=True)

@tree.command(name="status", description="Show Solunaris server status", guild=discord.Object(id=GUILD_ID))
async def cmd_status(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    async with aiohttp.ClientSession() as session:
        try:
            online, count = await nitrado_status(session)
        except Exception:
            online, count = False, None
    emoji = "🟢" if online else "🔴"
    txt = f"{emoji} Solunaris — Players: **{count if count is not None else '?'} / {PLAYER_CAP}**"
    await interaction.followup.send(txt, ephemeral=True)

# =========================================================
# STARTUP
# =========================================================
@client.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print("✅ Commands synced")

    client.loop.create_task(time_loop())
    client.loop.create_task(status_and_players_loop())
    client.loop.create_task(ftp_loop())

client.run(DISCORD_TOKEN)