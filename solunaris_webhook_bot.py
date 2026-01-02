# solunaris_time_and_status_bot.py
# ------------------------------------------------------------
# Features
# - Webhook embed that EDITS the same message (no spam)
# - In-game time tracking with different day/night speeds (piecewise)
# - Slash commands:
#     /day      -> shows current Solunaris time
#     /settime  -> set Year/Day/Hour/Minute (role-gated)
#     /status   -> shows server status + players (query + optional RCON)
# - Posts a message at the START of each new in-game day to a chosen channel
# - Renames 2 channels:
#     1) Time channel:  ☀️/🌙 | Solunaris Time | hh:mm | Day X | Year Y
#     2) Status channel: 🟢/🔴 | Solunaris | P/42
#
# IMPORTANT NOTE ABOUT ARK STATUS:
# - Most hosting setups use:
#     Game port  (UDP): 5020  (example)
#     Query port (UDP): often same or +1 (e.g. 5021) depending on host
#     RCON port  (TCP): separate (your screenshot showed 11020)
# - If you only have 31.214.239.2:5020, that's usually GAME/QUERY, not RCON.
# - This script tries UDP "A2S_INFO/A2S_PLAYER" via the Steam query protocol to get players.
# - It also optionally tries Source RCON to confirm online + run ListPlayers (if you supply RCON vars).
#
# Railway variables to set:
#   DISCORD_TOKEN
#   WEBHOOK_URL
#   # Optional but recommended:
#   TIME_TEXT_CHANNEL_ID          (channel to RENAME for time display)
#   STATUS_TEXT_CHANNEL_ID        (channel to RENAME for status display)
#   NEW_DAY_ANNOUNCE_CHANNEL_ID   (channel to post "New Day" messages)
#
#   ARK_HOST                      (e.g. 31.214.239.2)
#   ARK_QUERY_PORT                (UDP port for Steam query; try 5020 or 5021)
#   ARK_RCON_PORT                 (TCP port for RCON; e.g. 11020)
#   ARK_RCON_PASSWORD             (ServerAdminPassword)
#
# If you do NOT set RCON vars, status works via UDP query only.
# ------------------------------------------------------------

import os
import time
import json
import asyncio
import aiohttp
import socket
import struct
from dataclasses import dataclass
from typing import Optional, Tuple

import discord
from discord import app_commands

# =====================
# CONFIG
# =====================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

# In-game minute lengths (real seconds per in-game minute)
DAY_SECONDS_PER_INGAME_MINUTE = 4.7666667
NIGHT_SECONDS_PER_INGAME_MINUTE = 4.045

# Day/night window (your later rule)
# Day: 05:30 -> 17:30
# Night: 17:30 -> 05:30
DAY_START_MIN = 5 * 60 + 30     # 05:30
DAY_END_MIN = 17 * 60 + 30      # 17:30

# Embed colors
DAY_COLOR = 0xF1C40F    # Yellow
NIGHT_COLOR = 0x5865F2  # Blue

# How often to poll for changes (we only PATCH if changed)
POLL_EVERY_SECONDS = 15.0

# Force refresh even if unchanged
FORCE_REFRESH_SECONDS = 600.0   # 10 minutes

# State persistence
STATE_FILE = "state.json"

# ARK status
ARK_HOST = os.getenv("ARK_HOST", "31.214.239.2")  # default to what you gave
ARK_QUERY_PORT = int(os.getenv("ARK_QUERY_PORT", "5020"))  # UDP query (try 5020, else try 5021)
ARK_PLAYER_CAP = 42

# Optional RCON
ARK_RCON_PORT = os.getenv("ARK_RCON_PORT")  # e.g. 11020
ARK_RCON_PASSWORD = os.getenv("ARK_RCON_PASSWORD")

# Channel IDs to rename (optional)
TIME_TEXT_CHANNEL_ID = os.getenv("TIME_TEXT_CHANNEL_ID")
STATUS_TEXT_CHANNEL_ID = os.getenv("STATUS_TEXT_CHANNEL_ID")
NEW_DAY_ANNOUNCE_CHANNEL_ID = os.getenv("NEW_DAY_ANNOUNCE_CHANNEL_ID")

if not DISCORD_TOKEN or not WEBHOOK_URL:
    raise RuntimeError("Missing DISCORD_TOKEN or WEBHOOK_URL")

def _env_int(name: str) -> Optional[int]:
    v = os.getenv(name)
    if not v:
        return None
    try:
        return int(v)
    except Exception:
        return None

TIME_TEXT_CHANNEL_ID_INT = _env_int("TIME_TEXT_CHANNEL_ID")
STATUS_TEXT_CHANNEL_ID_INT = _env_int("STATUS_TEXT_CHANNEL_ID")
NEW_DAY_ANNOUNCE_CHANNEL_ID_INT = _env_int("NEW_DAY_ANNOUNCE_CHANNEL_ID")

ARK_RCON_PORT_INT = int(ARK_RCON_PORT) if ARK_RCON_PORT and ARK_RCON_PORT.isdigit() else None

# =====================
# DISCORD SETUP
# =====================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# =====================
# STATE
# =====================
def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(data):
    with open(STATE_FILE, "w") as f:
        json.dump(data, f)

state = load_state()

# webhook message id cache (also persisted)
webhook_message_id = None
WEBHOOK_STATE_FILE = "webhook_state.json"

def load_webhook_state():
    global webhook_message_id
    if not os.path.exists(WEBHOOK_STATE_FILE):
        return
    try:
        with open(WEBHOOK_STATE_FILE, "r") as f:
            data = json.load(f)
            webhook_message_id = data.get("message_id")
    except Exception:
        webhook_message_id = None

def save_webhook_state():
    try:
        with open(WEBHOOK_STATE_FILE, "w") as f:
            json.dump({"message_id": webhook_message_id}, f)
    except Exception:
        pass

load_webhook_state()

# =====================
# TIME CALCULATION (PIECEWISE DAY/NIGHT)
# =====================
def minute_of_day(hour: int, minute: int) -> int:
    return hour * 60 + minute

def is_day(minute_of_day_int: int) -> bool:
    # Day from 05:30 to 17:30
    return DAY_START_MIN <= minute_of_day_int < DAY_END_MIN

def seconds_per_minute(minute_of_day_int: int) -> float:
    return DAY_SECONDS_PER_INGAME_MINUTE if is_day(minute_of_day_int) else NIGHT_SECONDS_PER_INGAME_MINUTE

def next_boundary_total_minutes(day: int, minute_of_day_float: float) -> float:
    """
    Returns the next sunrise/sunset boundary as TOTAL in-game minutes since Day 1 midnight.
    """
    mod = int(minute_of_day_float) % 1440
    total_now = (day - 1) * 1440 + minute_of_day_float

    if is_day(mod):
        # next boundary is day end (17:30) same day
        boundary = (day - 1) * 1440 + DAY_END_MIN
        if boundary <= total_now:
            # already past; next would be next day's day end, but we should hit night boundary at day end anyway
            boundary = (day) * 1440 + DAY_END_MIN
        return boundary
    else:
        # next boundary is day start (05:30). If currently after day end, it's next day start.
        if mod < DAY_START_MIN:
            # before day start, boundary is today day start
            boundary = (day - 1) * 1440 + DAY_START_MIN
        else:
            # after day end, boundary is next day day start
            boundary = (day) * 1440 + DAY_START_MIN
        if boundary <= total_now:
            boundary += 1440
        return boundary

def advance_minutes_piecewise(start_day: int, start_minute_of_day: int, elapsed_real_seconds: float) -> Tuple[int, int]:
    """
    Advance time where "seconds per in-game minute" differs depending on day/night,
    crossing boundaries exactly (smooth switching).
    Returns (day, minute_of_day_int).
    """
    day = int(start_day)
    minute_float = float(start_minute_of_day)
    remaining = float(elapsed_real_seconds)

    # Hard safety limit
    for _ in range(200000):
        if remaining <= 0:
            break

        mod = int(minute_float) % 1440
        spm = seconds_per_minute(mod)

        boundary_total = next_boundary_total_minutes(day, minute_float)
        current_total = (day - 1) * 1440 + minute_float
        mins_until_boundary = max(0.0, boundary_total - current_total)
        secs_until_boundary = mins_until_boundary * spm

        if secs_until_boundary > 0 and remaining >= secs_until_boundary:
            # jump to boundary exactly
            remaining -= secs_until_boundary
            minute_float += mins_until_boundary
        else:
            # partial move within this segment
            minute_float += remaining / spm
            remaining = 0.0

        # normalize
        while minute_float >= 1440.0:
            minute_float -= 1440.0
            day += 1

    return day, int(minute_float) % 1440

def roll_year(day: int, year: int) -> Tuple[int, int]:
    # Year rolls every 365 days; calibrated to provided day.
    while day > 365:
        day -= 365
        year += 1
    return day, year

def calculate_time() -> Optional[dict]:
    """
    Returns dict with:
      day, year, hour, minute, is_day, title, color, spm
    """
    global state
    if not state:
        return None

    elapsed_real = time.time() - float(state["real_epoch"])
    start_day = int(state["day"])
    start_year = int(state["year"])
    start_minute = int(state["hour"]) * 60 + int(state["minute"])

    day_now, mod_min = advance_minutes_piecewise(start_day, start_minute, elapsed_real)
    day_rolled, year_rolled = roll_year(day_now, start_year)

    hour = mod_min // 60
    minute = mod_min % 60

    dayflag = is_day(mod_min)
    emoji = "☀️" if dayflag else "🌙"
    color = DAY_COLOR if dayflag else NIGHT_COLOR
    spm = seconds_per_minute(mod_min)

    title = f"{emoji} | Solunaris Time | {hour:02d}:{minute:02d} | Day {day_rolled} | Year {year_rolled}"

    return {
        "day": day_rolled,
        "year": year_rolled,
        "hour": hour,
        "minute": minute,
        "minute_of_day": mod_min,
        "is_day": dayflag,
        "emoji": emoji,
        "color": color,
        "spm": spm,
        "title": title,
    }

# =====================
# STEAM QUERY (A2S) FOR ARK STATUS + PLAYERS (UDP)
# =====================
@dataclass
class QueryResult:
    online: bool
    players: Optional[int]
    max_players: Optional[int]
    name: Optional[str]
    error: Optional[str]

A2S_INFO_HEADER = b"\xFF\xFF\xFF\xFFTSource Engine Query\x00"
A2S_PLAYER_HEADER = b"\xFF\xFF\xFF\xFFU\xFF\xFF\xFF\xFF"  # request challenge

def _udp_request(host: str, port: int, payload: bytes, timeout: float = 2.0) -> bytes:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(payload, (host, port))
        data, _ = s.recvfrom(4096)
        return data

def query_a2s_info(host: str, port: int) -> QueryResult:
    try:
        data = _udp_request(host, port, A2S_INFO_HEADER, timeout=2.0)
        if not data.startswith(b"\xFF\xFF\xFF\xFFI"):
            return QueryResult(False, None, None, None, f"Unexpected INFO reply: {data[:10]!r}")

        # Parse minimal fields:
        # https://developer.valvesoftware.com/wiki/Server_queries#A2S_INFO
        # Format after header:
        # byte header(0x49), byte protocol, string name, string map, string folder, string game, short id,
        # byte players, byte max, ...
        # We'll parse up to players/max.
        offset = 5  # skip 4xFF + 'I'
        # protocol
        offset += 1

        def read_cstring(buf, idx):
            end = buf.index(b"\x00", idx)
            return buf[idx:end].decode("utf-8", errors="replace"), end + 1

        name, offset = read_cstring(data, offset)
        _map, offset = read_cstring(data, offset)
        _folder, offset = read_cstring(data, offset)
        _game, offset = read_cstring(data, offset)

        # short id
        offset += 2

        players = data[offset]
        max_players = data[offset + 1]

        return QueryResult(True, int(players), int(max_players), name, None)
    except Exception as e:
        return QueryResult(False, None, None, None, str(e))

# =====================
# SOURCE RCON (TCP) OPTIONAL
# =====================
# Minimal Source RCON implementation (Valve protocol)
# Packet: <len:int32><id:int32><type:int32><body:bytes><00><00>
RCON_TYPE_AUTH = 3
RCON_TYPE_AUTH_RESP = 2
RCON_TYPE_EXEC = 2
RCON_TYPE_RESPONSE = 0

def _rcon_packet(req_id: int, ptype: int, body: str) -> bytes:
    b = body.encode("utf-8", errors="replace")
    # len includes id+type+body+2 nulls
    length = 4 + 4 + len(b) + 2
    return struct.pack("<iii", length, req_id, ptype) + b + b"\x00\x00"

def _rcon_recv(sock: socket.socket) -> Tuple[int, int, str]:
    raw_len = sock.recv(4)
    if len(raw_len) < 4:
        raise RuntimeError("RCON: short read (len)")
    (length,) = struct.unpack("<i", raw_len)
    payload = b""
    while len(payload) < length:
        chunk = sock.recv(length - len(payload))
        if not chunk:
            break
        payload += chunk
    if len(payload) < 8:
        raise RuntimeError("RCON: short read (payload)")
    req_id, ptype = struct.unpack("<ii", payload[:8])
    body = payload[8:-2].decode("utf-8", errors="replace")  # strip 2 nulls
    return req_id, ptype, body

def rcon_command(host: str, port: int, password: str, command: str, timeout: float = 3.0) -> str:
    req_id = 1234
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect((host, port))

        # auth
        s.sendall(_rcon_packet(req_id, RCON_TYPE_AUTH, password))
        # Some servers send an empty RESPONSE first; read until we see AUTH_RESP for our id
        authed = False
        for _ in range(5):
            rid, ptype, _body = _rcon_recv(s)
            if ptype == RCON_TYPE_AUTH_RESP:
                if rid == -1:
                    raise RuntimeError("RCON auth failed (bad password or blocked).")
                authed = True
                break
        if not authed:
            raise RuntimeError("RCON auth failed (no auth response).")

        # exec
        req_id2 = 5678
        s.sendall(_rcon_packet(req_id2, RCON_TYPE_EXEC, command))

        # response may come in parts; read a couple frames
        parts = []
        for _ in range(10):
            rid, ptype, body = _rcon_recv(s)
            if rid != req_id2:
                continue
            if ptype in (RCON_TYPE_RESPONSE, RCON_TYPE_EXEC):
                if body:
                    parts.append(body)
                # heuristic: stop if small/empty
                if len(body) == 0:
                    break
        return "".join(parts).strip()

def get_server_status() -> QueryResult:
    """
    Prefer Steam UDP query for players.
    Optionally confirm via RCON.
    """
    info = query_a2s_info(ARK_HOST, ARK_QUERY_PORT)

    # If UDP query says online, keep it.
    # If UDP query fails but RCON works, we can still mark online.
    if info.online:
        return info

    # Try RCON if configured
    if ARK_RCON_PORT_INT and ARK_RCON_PASSWORD:
        try:
            _ = rcon_command(ARK_HOST, ARK_RCON_PORT_INT, ARK_RCON_PASSWORD, "getchat", timeout=3.0)
            # online but unknown players
            return QueryResult(True, None, ARK_PLAYER_CAP, None, "UDP query failed; RCON ok")
        except Exception as e:
            return QueryResult(False, None, ARK_PLAYER_CAP, None, f"UDP query failed; RCON failed: {e}")

    return info

# =====================
# RENAME CHANNEL HELPERS
# =====================
_last_time_channel_name = None
_last_status_channel_name = None

async def set_channel_name(channel_id: int, new_name: str):
    ch = client.get_channel(channel_id)
    if ch is None:
        # try fetch
        try:
            ch = await client.fetch_channel(channel_id)
        except Exception:
            return
    try:
        await ch.edit(name=new_name)
    except Exception:
        # missing perms / rate limited / invalid chars etc.
        pass

# =====================
# WEBHOOK MESSAGE (EDIT SAME MESSAGE)
# =====================
_last_embed_title = None
_last_embed_color = None
_last_force_refresh_at = 0.0

def make_embed(title: str, color: int) -> dict:
    # Use description for bigger/bolder feeling (Discord embed titles already strong)
    # We'll keep title for the channel-style string.
    return {
        "title": title,
        "color": color,
        "description": f"**{title}**",  # bold + visually larger
    }

async def upsert_webhook_embed(session: aiohttp.ClientSession, title: str, color: int, force: bool = False):
    global webhook_message_id, _last_embed_title, _last_embed_color, _last_force_refresh_at

    changed = (title != _last_embed_title) or (color != _last_embed_color)
    now = time.time()
    forced_due = now >= _last_force_refresh_at

    if not force and not changed and not forced_due:
        return

    embed = make_embed(title, color)

    try:
        if webhook_message_id:
            await session.patch(
                f"{WEBHOOK_URL}/messages/{webhook_message_id}",
                json={"embeds": [embed]},
                timeout=aiohttp.ClientTimeout(total=5),
            )
        else:
            async with session.post(
                WEBHOOK_URL + "?wait=true",
                json={"embeds": [embed]},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                data = await resp.json()
                webhook_message_id = data["id"]
                save_webhook_state()

        _last_embed_title = title
        _last_embed_color = color
        _last_force_refresh_at = now + FORCE_REFRESH_SECONDS
    except Exception:
        # If message was deleted, reset id and try next loop
        webhook_message_id = None
        save_webhook_state()

# =====================
# NEW DAY ANNOUNCER
# =====================
_last_announced_day_key = None  # (year, day)

async def announce_new_day(time_info: dict):
    global _last_announced_day_key
    if not NEW_DAY_ANNOUNCE_CHANNEL_ID_INT:
        return

    key = (time_info["year"], time_info["day"])
    if _last_announced_day_key is None:
        _last_announced_day_key = key
        return

    if key != _last_announced_day_key:
        _last_announced_day_key = key
        ch = client.get_channel(NEW_DAY_ANNOUNCE_CHANNEL_ID_INT)
        if ch is None:
            try:
                ch = await client.fetch_channel(NEW_DAY_ANNOUNCE_CHANNEL_ID_INT)
            except Exception:
                return
        try:
            await ch.send(f"📅 **A new Solunaris day has begun!** Day **{time_info['day']}** — Year **{time_info['year']}**")
        except Exception:
            pass

# =====================
# BACKGROUND LOOPS
# =====================
async def time_webhook_loop():
    await client.wait_until_ready()
    global _last_time_channel_name

    async with aiohttp.ClientSession() as session:
        while True:
            info = calculate_time()
            if info:
                # webhook
                await upsert_webhook_embed(session, info["title"], info["color"])

                # rename time channel (optional)
                if TIME_TEXT_CHANNEL_ID_INT:
                    new_name = info["title"]
                    if new_name != _last_time_channel_name:
                        _last_time_channel_name = new_name
                        await set_channel_name(TIME_TEXT_CHANNEL_ID_INT, new_name)

                # new day message
                await announce_new_day(info)

            await asyncio.sleep(POLL_EVERY_SECONDS)

async def status_loop():
    await client.wait_until_ready()
    global _last_status_channel_name

    last_status_string = None
    last_force = 0.0

    while True:
        qr = get_server_status()
        online = qr.online
        players = qr.players if qr.players is not None else None
        maxp = qr.max_players if qr.max_players is not None else ARK_PLAYER_CAP

        dot = "🟢" if online else "🔴"
        ptxt = f"{players}/{maxp}" if players is not None else f"?/{maxp}"
        status_string = f"{dot} | Solunaris | {ptxt}"

        # Update channel name only on change, but force every 10 minutes
        now = time.time()
        force = now - last_force >= FORCE_REFRESH_SECONDS

        if STATUS_TEXT_CHANNEL_ID_INT and (force or status_string != last_status_string):
            last_status_string = status_string
            last_force = now
            await set_channel_name(STATUS_TEXT_CHANNEL_ID_INT, status_string)

        await asyncio.sleep(POLL_EVERY_SECONDS)

# =====================
# PERMISSIONS (ROLE CHECK)
# =====================
def has_admin_role(user: discord.Member) -> bool:
    try:
        return any(r.id == ADMIN_ROLE_ID for r in user.roles)
    except Exception:
        return False

# =====================
# SLASH COMMANDS
# =====================
@tree.command(
    name="day",
    description="Show current Solunaris time",
    guild=discord.Object(id=GUILD_ID),
)
async def day_cmd(interaction: discord.Interaction):
    info = calculate_time()
    if not info:
        await interaction.response.send_message("⏳ Time not set yet. Use /settime.", ephemeral=True)
        return
    await interaction.response.send_message(info["title"], ephemeral=True)

@tree.command(
    name="settime",
    description="Set Solunaris time (admin role only)",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(
    year="Year number",
    day="Day of year (1–365)",
    hour="Hour (0–23)",
    minute="Minute (0–59)",
)
async def settime_cmd(interaction: discord.Interaction, year: int, day: int, hour: int, minute: int):
    # Role gated (no Discord administrator perm needed)
    member = interaction.user
    if not isinstance(member, discord.Member):
        try:
            member = await interaction.guild.fetch_member(interaction.user.id)
        except Exception:
            member = interaction.user

    if not has_admin_role(member):
        await interaction.response.send_message("❌ You must have the required admin role to use /settime.", ephemeral=True)
        return

    if year < 1 or day < 1 or day > 365 or hour < 0 or hour > 23 or minute < 0 or minute > 59:
        await interaction.response.send_message("❌ Invalid values.", ephemeral=True)
        return

    global state
    state = {
        "real_epoch": time.time(),
        "year": int(year),
        "day": int(day),
        "hour": int(hour),
        "minute": int(minute),
    }
    save_state(state)

    # respond quickly (avoids "application did not respond")
    await interaction.response.send_message(
        f"✅ Set to **Day {day}**, **{hour:02d}:{minute:02d}**, **Year {year}**",
        ephemeral=True,
    )

@tree.command(
    name="status",
    description="Show server status + players online",
    guild=discord.Object(id=GUILD_ID),
)
async def status_cmd(interaction: discord.Interaction):
    qr = get_server_status()
    dot = "🟢" if qr.online else "🔴"
    cap = qr.max_players if qr.max_players is not None else ARK_PLAYER_CAP
    ptxt = f"{qr.players}/{cap}" if qr.players is not None else f"?/{cap}"
    msg = f"{dot} **Solunaris is {'ONLINE' if qr.online else 'OFFLINE'}** — Players: **{ptxt}**"
    if qr.error:
        msg += f"\n`{qr.error}`"
    await interaction.response.send_message(msg, ephemeral=True)

# =====================
# STARTUP
# =====================
@client.event
async def on_ready():
    try:
        await tree.sync(guild=discord.Object(id=GUILD_ID))
        print("✅ Commands synced to guild")
    except Exception as e:
        print(f"❌ Command sync failed: {e}")

    # Start loops
    client.loop.create_task(time_webhook_loop())
    client.loop.create_task(status_loop())

    print("✅ Bot ready")

client.run(DISCORD_TOKEN)