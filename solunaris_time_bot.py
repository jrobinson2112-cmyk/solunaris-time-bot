import os
import time
import json
import asyncio
import aiohttp
import discord
from discord import app_commands

# =====================
# ENV
# =====================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # time webhook
PLAYERS_WEBHOOK_URL = os.getenv("PLAYERS_WEBHOOK_URL")  # players webhook
NITRADO_TOKEN = os.getenv("NITRADO_TOKEN")
NITRADO_SERVICE_ID = os.getenv("NITRADO_SERVICE_ID")

RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = os.getenv("RCON_PORT")
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

required = [
    DISCORD_TOKEN, WEBHOOK_URL, PLAYERS_WEBHOOK_URL,
    NITRADO_TOKEN, NITRADO_SERVICE_ID,
    RCON_HOST, RCON_PORT, RCON_PASSWORD
]
if not all(required):
    missing = []
    for k in [
        "DISCORD_TOKEN", "WEBHOOK_URL", "PLAYERS_WEBHOOK_URL",
        "NITRADO_TOKEN", "NITRADO_SERVICE_ID",
        "RCON_HOST", "RCON_PORT", "RCON_PASSWORD"
    ]:
        if not os.getenv(k):
            missing.append(k)
    raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

RCON_PORT = int(RCON_PORT)

# =====================
# CONSTANTS
# =====================
GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076
STATUS_VC_ID = 1456615806887657606
ANNOUNCE_CHANNEL_ID = 1430388267446042666
PLAYER_CAP = 42

# ASA time tuning (seconds per in-game minute)
DAY_SPM = 4.7666667
NIGHT_SPM = 4.045
SUNRISE = 5 * 60 + 30
SUNSET = 17 * 60 + 30

DAY_COLOR = 0xF1C40F
NIGHT_COLOR = 0x5865F2

STATE_FILE = "state.json"

# How often to check things
PLAYERS_POLL_SECONDS = 15
TIME_CHECK_SECONDS = 2  # check often, but only POST/EDIT on round 10 minutes
VC_MIN_EDIT_INTERVAL = 60  # avoid 429 rate limits

# =====================
# DISCORD SETUP
# =====================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# =====================
# STATE (PERSISTED)
# =====================
def load_state_file():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f) or {}
    except Exception:
        return {}

def save_state_file(obj: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(obj, f)

_state_file = load_state_file()

# time anchor state
state = _state_file.get("time_state")  # dict or None

# webhook message ids (so webhook only edits, doesn't post new each time)
message_ids = _state_file.get("webhook_message_ids", {"time": None, "players": None})
if "time" not in message_ids:
    message_ids["time"] = None
if "players" not in message_ids:
    message_ids["players"] = None

# =====================
# RUNTIME STATE
# =====================
last_announced_absolute_day = None
last_time_bucket = None  # (year, day, minute_bucket_10)
last_vc_name = None
last_vc_edit_ts = 0.0

# =====================
# TIME LOGIC
# =====================
def is_day(minute_of_day: int) -> bool:
    return SUNRISE <= minute_of_day < SUNSET

def spm(minute_of_day: int) -> float:
    return DAY_SPM if is_day(minute_of_day) else NIGHT_SPM

def calculate_time_snapshot():
    """
    Returns:
      (title, color, year, day, hour, minute, minute_of_day)
    or None if time not set.
    """
    if not state:
        return None

    elapsed = time.time() - float(state["epoch"])
    minute_of_day = int(state["hour"]) * 60 + int(state["minute"])
    day = int(state["day"])
    year = int(state["year"])

    # Simulate minute-by-minute using correct day/night SPM.
    # (Fast enough for typical use; accurate across sunrise/sunset.)
    remaining = float(elapsed)
    while remaining > 0:
        remaining -= spm(minute_of_day)
        minute_of_day += 1
        if minute_of_day >= 1440:
            minute_of_day = 0
            day += 1
            if day > 365:
                day = 1
                year += 1

    hour = minute_of_day // 60
    minute = minute_of_day % 60
    emoji = "☀️" if is_day(minute_of_day) else "🌙"
    color = DAY_COLOR if is_day(minute_of_day) else NIGHT_COLOR
    title = f"{emoji} | Solunaris Time | {hour:02d}:{minute:02d} | Day {day} | Year {year}"
    return title, color, year, day, hour, minute, minute_of_day

# =====================
# NITRADO STATUS (ONLINE + COUNT FALLBACK)
# =====================
async def get_server_status(session: aiohttp.ClientSession):
    headers = {"Authorization": f"Bearer {NITRADO_TOKEN}"}
    url = f"https://api.nitrado.net/services/{NITRADO_SERVICE_ID}/gameservers"
    async with session.get(url, headers=headers) as r:
        data = await r.json()

    gs = data["data"]["gameserver"]
    status = str(gs.get("status", "")).lower()
    online = status in ("started", "running", "online")
    players = int(gs.get("query", {}).get("player_current", 0) or 0)
    return online, players

# =====================
# RCON (Source RCON)
# =====================
def _rcon_make_packet(req_id: int, ptype: int, body: str) -> bytes:
    data = body.encode("utf-8") + b"\x00"
    packet = req_id.to_bytes(4, "little", signed=True) + ptype.to_bytes(4, "little", signed=True) + data + b"\x00"
    size = len(packet)
    return size.to_bytes(4, "little", signed=True) + packet

async def rcon_command(command: str, timeout: float = 6.0) -> str:
    """
    Minimal Source RCON client.
      ptype: 3 = auth, 2 = exec command
    """
    reader, writer = await asyncio.wait_for(asyncio.open_connection(RCON_HOST, RCON_PORT), timeout=timeout)
    try:
        # auth
        writer.write(_rcon_make_packet(1, 3, RCON_PASSWORD))
        await writer.drain()

        raw = await asyncio.wait_for(reader.read(4096), timeout=timeout)
        if len(raw) < 12:
            raise RuntimeError("RCON auth failed (short response)")

        # command
        writer.write(_rcon_make_packet(2, 2, command))
        await writer.drain()

        chunks = []
        end_time = time.time() + timeout
        while time.time() < end_time:
            try:
                part = await asyncio.wait_for(reader.read(4096), timeout=0.3)
            except asyncio.TimeoutError:
                break
            if not part:
                break
            chunks.append(part)

        if not chunks:
            return ""

        data = b"".join(chunks)

        # Parse packets: [size][id][type][body]\x00\x00
        out = []
        i = 0
        while i + 4 <= len(data):
            size = int.from_bytes(data[i:i+4], "little", signed=True)
            i += 4
            if size < 10 or i + size > len(data):
                break
            pkt = data[i:i+size]
            i += size

            body = pkt[8:-2]  # remove id/type, strip \x00\x00
            txt = body.decode("utf-8", errors="ignore")
            if txt:
                out.append(txt)

        return "".join(out).strip()
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

def parse_listplayers(output: str):
    """
    Expected lines like:
      0. Name, 0002xxxxxxxx...
    Returns list of names.
    """
    players = []
    if not output:
        return players

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        if ". " in line:
            line = line.split(". ", 1)[1]
        if "," in line:
            name = line.split(",", 1)[0].strip()
        else:
            name = line.strip()

        if name and name.lower() not in ("executing", "listplayers", "done"):
            players.append(name)

    return players

# =====================
# WEBHOOK UPSERT (EDIT ONLY AFTER FIRST POST)
# =====================
async def upsert_webhook(session: aiohttp.ClientSession, url: str, key: str, embed: dict):
    """
    Edits an existing webhook message if we have its message_id.
    If missing or deleted, posts once and stores the id.
    """
    mid = message_ids.get(key)

    if mid:
        async with session.patch(f"{url}/messages/{mid}", json={"embeds": [embed]}) as r:
            if r.status == 404:
                # message deleted -> recreate once
                message_ids[key] = None
                _state_file["webhook_message_ids"] = message_ids
                save_state_file(_state_file)
                return await upsert_webhook(session, url, key, embed)
        return

    async with session.post(url + "?wait=true", json={"embeds": [embed]}) as r:
        data = await r.json()
        message_ids[key] = data["id"]
        _state_file["webhook_message_ids"] = message_ids
        save_state_file(_state_file)

# =====================
# PLAYERS UPDATE (RCON IS SOURCE OF TRUTH)
# =====================
async def update_players(session: aiohttp.ClientSession):
    """
    Uses RCON ListPlayers as the *primary* source of truth for count + names.
    Falls back to Nitrado count only if RCON fails.
    Returns (emoji, count, online_bool)
    """
    nitrado_online, nitrado_count = await get_server_status(session)

    names = []
    rcon_ok = True
    rcon_err = None
    try:
        out = await rcon_command("ListPlayers", timeout=6.0)
        names = parse_listplayers(out)
    except Exception as e:
        rcon_ok = False
        rcon_err = str(e)

    # ONLINE:
    # - prefer Nitrado's server status
    # - if nitrado says offline but rcon works, treat as online
    online = nitrado_online or rcon_ok

    # COUNT:
    # - If RCON is working, the count is len(names) (THIS is what keeps VC + channel matching)
    # - Only if RCON fails, fall back to nitrado_count
    if rcon_ok:
        count = len(names)
    else:
        count = nitrado_count

    emoji = "🟢" if online else "🔴"

    # description
    if rcon_ok:
        if names:
            lines = [f"{idx+1:02d}) {n}" for idx, n in enumerate(names[:50])]
            desc = f"**{count}/{PLAYER_CAP}** online\n\n" + "\n".join(lines)
        else:
            desc = f"**{count}/{PLAYER_CAP}** online\n\n*(No players online.)*"
    else:
        desc = f"**{count}/{PLAYER_CAP}** online\n\n*(RCON failed, using Nitrado count: {rcon_err})*"

    embed = {
        "title": "Online Players",
        "description": desc,
        "color": 0x2ECC71 if online else 0xE74C3C,
        "footer": {"text": f"Last update: {time.strftime('%H:%M:%S')}"}
    }
    await upsert_webhook(session, PLAYERS_WEBHOOK_URL, "players", embed)
    return emoji, count, online

async def maybe_update_vc(emoji: str, count: int):
    """
    Updates the VC channel name, but avoids rate limits:
    - only if changed
    - not more often than VC_MIN_EDIT_INTERVAL
    """
    global last_vc_name, last_vc_edit_ts

    vc = client.get_channel(STATUS_VC_ID)
    if not vc:
        return

    new_name = f"{emoji} Solunaris | {count}/{PLAYER_CAP}"
    now = time.time()

    if new_name == last_vc_name:
        return

    # throttle
    if now - last_vc_edit_ts < VC_MIN_EDIT_INTERVAL:
        return

    try:
        await vc.edit(name=new_name)
        last_vc_name = new_name
        last_vc_edit_ts = now
    except discord.HTTPException:
        # if discord rate limits or errors, just skip this tick
        return

# =====================
# LOOPS
# =====================
async def time_loop():
    """
    IMPORTANT: Only updates the time webhook every 10 in-game minutes,
    AND only when it's on a round 10 (minute == 00,10,20,30,...).
    """
    global last_announced_absolute_day, last_time_bucket

    await client.wait_until_ready()
    async with aiohttp.ClientSession() as session:
        while True:
            snap = calculate_time_snapshot()
            if snap:
                title, color, year, day, hour, minute, minute_of_day = snap

                # announce new day (only once per day)
                absolute_day = year * 365 + day
                if last_announced_absolute_day is None:
                    last_announced_absolute_day = absolute_day
                elif absolute_day > last_announced_absolute_day:
                    ch = client.get_channel(ANNOUNCE_CHANNEL_ID)
                    if ch:
                        await ch.send(f"📅 **New Solunaris Day** — Day **{day}**, Year **{year}**")
                    last_announced_absolute_day = absolute_day

                # update only on round 10 minutes
                minute_bucket_10 = minute_of_day // 10  # changes every 10 in-game minutes
                is_round_10 = (minute_of_day % 10 == 0)

                bucket = (year, day, minute_bucket_10)
                if is_round_10 and bucket != last_time_bucket:
                    embed = {"title": title, "color": color}
                    await upsert_webhook(session, WEBHOOK_URL, "time", embed)
                    last_time_bucket = bucket

            await asyncio.sleep(TIME_CHECK_SECONDS)

async def status_loop():
    await client.wait_until_ready()
    async with aiohttp.ClientSession() as session:
        while True:
            emoji, count, online = await update_players(session)
            await maybe_update_vc(emoji, count)
            await asyncio.sleep(PLAYERS_POLL_SECONDS)

# =====================
# COMMANDS
# =====================
@tree.command(name="settime", guild=discord.Object(id=GUILD_ID))
async def settime(i: discord.Interaction, year: int, day: int, hour: int, minute: int):
    if not any(r.id == ADMIN_ROLE_ID for r in i.user.roles):
        await i.response.send_message("❌ No permission", ephemeral=True)
        return

    global state, last_time_bucket
    state = {
        "epoch": time.time(),
        "year": int(year),
        "day": int(day),
        "hour": int(hour),
        "minute": int(minute),
    }

    _state_file["time_state"] = state
    _state_file["webhook_message_ids"] = message_ids
    save_state_file(_state_file)

    # reset bucket so next round-10 will post
    last_time_bucket = None

    await i.response.send_message("✅ Time set", ephemeral=True)

@tree.command(name="status", guild=discord.Object(id=GUILD_ID))
async def status(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        emoji, count, online = await update_players(session)
    await i.followup.send(f"{emoji} **Solunaris** — {count}/{PLAYER_CAP} players", ephemeral=True)

# =====================
# START
# =====================
@client.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    client.loop.create_task(time_loop())
    client.loop.create_task(status_loop())
    print("✅ Solunaris bot online")

client.run(DISCORD_TOKEN)