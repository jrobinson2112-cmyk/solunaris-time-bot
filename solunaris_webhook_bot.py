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

required_keys = [
    "DISCORD_TOKEN",
    "WEBHOOK_URL",
    "PLAYERS_WEBHOOK_URL",
    "NITRADO_TOKEN",
    "NITRADO_SERVICE_ID",
    "RCON_HOST",
    "RCON_PORT",
    "RCON_PASSWORD",
]
missing = [k for k in required_keys if not os.getenv(k)]
if missing:
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

# --- Time config ---
# Your measured values:
# Day: 10:30 -> 17:05 (395 in-game mins) took 31:13 (1873s) => 4.7417721519
# Night: 22:15 -> 03:15 (300 in-game mins) took 19:54 (1194s) => 3.98
DAY_SPM = 4.7417721519
NIGHT_SPM = 3.98

# If you want your older values back, uncomment these:
# DAY_SPM = 4.7666667
# NIGHT_SPM = 4.045

SUNRISE = 5 * 60 + 30   # 05:30
SUNSET = 17 * 60 + 30   # 17:30

DAY_COLOR = 0xF1C40F
NIGHT_COLOR = 0x5865F2

# Update time message every N in-game minutes
UPDATE_IG_MINUTES = 10

STATE_FILE = "state.json"

# Persist webhook message IDs so reboot doesn't post new ones
MESSAGE_IDS_FILE = "message_ids.json"

# Status polling rules
STATUS_POLL_SECONDS = 15
STATUS_FORCE_UPDATE_SECONDS = 10 * 60  # 10 minutes

# =====================
# DISCORD SETUP
# =====================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# =====================
# SHARED STATE
# =====================
message_ids = {
    "time": None,
    "players": None,
}
last_announced_day = None

# For "only update if changed" logic
_last_status_snapshot = None
_last_status_force_ts = 0.0

# =====================
# FILE HELPERS
# =====================
def load_json_file(path: str, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json_file(path: str, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass

# Load persisted message IDs
message_ids.update(load_json_file(MESSAGE_IDS_FILE, {}) or {})
save_json_file(MESSAGE_IDS_FILE, message_ids)

# =====================
# STATE FILE
# =====================
def load_state():
    return load_json_file(STATE_FILE, None)

def save_state(s):
    save_json_file(STATE_FILE, s)

state = load_state()

# =====================
# TIME LOGIC (FAST + ACCURATE)
# =====================
def is_day(minute_of_day: int) -> bool:
    return SUNRISE <= minute_of_day < SUNSET

def spm_for(minute_of_day: int) -> float:
    return DAY_SPM if is_day(minute_of_day) else NIGHT_SPM

def advance_minutes_piecewise(start_day: int, start_minute_of_day: int, elapsed_real_seconds: float):
    """
    Advances in-game time using day/night speeds without looping per minute.
    Returns (new_day, new_minute_of_day_float).
    day is 1-based day-of-year as stored (we handle year rollover elsewhere).
    """
    day = int(start_day)
    minute = float(start_minute_of_day)  # can be fractional
    remaining = float(elapsed_real_seconds)

    # prevent any runaway loop
    for _ in range(20000):
        if remaining <= 0:
            break

        minute_int = int(minute) % 1440
        current_spm = spm_for(minute_int)

        # Determine next boundary in absolute minutes
        # Boundaries are at SUNRISE and SUNSET.
        if is_day(minute_int):
            # day -> next boundary is sunset same day
            boundary_day = day
            boundary_minute = SUNSET
        else:
            # night -> next boundary is sunrise (might be next day)
            if minute_int < SUNRISE:
                boundary_day = day
                boundary_minute = SUNRISE
            else:
                boundary_day = day + 1
                boundary_minute = SUNRISE

        # Convert current position and boundary to "absolute minutes"
        current_abs = (day - 1) * 1440 + minute
        boundary_abs = (boundary_day - 1) * 1440 + boundary_minute

        minutes_to_boundary = max(0.0, boundary_abs - current_abs)
        seconds_to_boundary = minutes_to_boundary * current_spm

        if seconds_to_boundary > 0 and remaining >= seconds_to_boundary:
            # Jump to boundary
            remaining -= seconds_to_boundary
            minute += minutes_to_boundary
        else:
            # Partial step within this segment
            minute += (remaining / current_spm) if current_spm > 0 else 0
            remaining = 0.0

        # Normalize over midnight
        while minute >= 1440:
            minute -= 1440
            day += 1

    return day, minute

def calculate_time():
    """
    Returns:
      (title, color, year, day, minute_of_day_int, current_spm)
    """
    if not state:
        return None

    elapsed = time.time() - state["epoch"]

    start_year = int(state["year"])
    start_day = int(state["day"])
    start_minute_of_day = int(state["hour"]) * 60 + int(state["minute"])

    day, minute_float = advance_minutes_piecewise(start_day, start_minute_of_day, elapsed)

    # Roll years (365-day year)
    year = start_year
    while day > 365:
        day -= 365
        year += 1

    minute_of_day = int(minute_float) % 1440
    hour = minute_of_day // 60
    minute = minute_of_day % 60

    day_now = is_day(minute_of_day)
    emoji = "☀️" if day_now else "🌙"
    color = DAY_COLOR if day_now else NIGHT_COLOR
    current_spm = DAY_SPM if day_now else NIGHT_SPM

    title = f"{emoji} | Solunaris Time | {hour:02d}:{minute:02d} | Day {day} | Year {year}"
    return title, color, year, day, minute_of_day, current_spm

def seconds_until_next_tick(day: int, minute_of_day: int) -> float:
    """
    Compute real seconds until the next UPDATE_IG_MINUTES boundary in in-game time,
    integrating across day/night boundaries if needed.
    """
    if UPDATE_IG_MINUTES <= 0:
        return 5.0

    # Next tick minute-of-day (in-game)
    next_min = ((minute_of_day // UPDATE_IG_MINUTES) + 1) * UPDATE_IG_MINUTES
    target_day = day
    if next_min >= 1440:
        next_min -= 1440
        target_day = day + 1

    # Integrate from (day, minute_of_day) to (target_day, next_min)
    cur_day = day
    cur_min = float(minute_of_day)
    target_abs = (target_day - 1) * 1440 + next_min
    cur_abs = (cur_day - 1) * 1440 + cur_min

    total_seconds = 0.0

    for _ in range(20000):
        if cur_abs >= target_abs:
            break

        cur_min_int = int(cur_min) % 1440
        current_spm = spm_for(cur_min_int)

        # next day/night boundary abs minute
        if is_day(cur_min_int):
            b_day = cur_day
            b_min = SUNSET
        else:
            if cur_min_int < SUNRISE:
                b_day = cur_day
                b_min = SUNRISE
            else:
                b_day = cur_day + 1
                b_min = SUNRISE

        boundary_abs = (b_day - 1) * 1440 + b_min
        segment_end = min(boundary_abs, target_abs)

        minutes_in_segment = max(0.0, segment_end - cur_abs)
        total_seconds += minutes_in_segment * current_spm

        cur_abs = segment_end
        # update cur_day/cur_min from cur_abs
        cur_day = int(cur_abs // 1440) + 1
        cur_min = float(cur_abs - (cur_day - 1) * 1440)

    # Small safety minimum to avoid tight loop
    return max(1.0, total_seconds)

# =====================
# NITRADO STATUS (COUNT)
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
    packet = (
        req_id.to_bytes(4, "little", signed=True)
        + ptype.to_bytes(4, "little", signed=True)
        + data
        + b"\x00"
    )
    size = len(packet)
    return size.to_bytes(4, "little", signed=True) + packet

async def rcon_command(command: str, timeout: float = 6.0) -> str:
    """
    Minimal Source RCON client.
    ptype: 3 = auth, 2 = exec command
    """
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(RCON_HOST, RCON_PORT), timeout=timeout
    )

    try:
        # auth
        writer.write(_rcon_make_packet(1, 3, RCON_PASSWORD))
        await writer.drain()

        # read some auth response bytes (doesn't always come neatly)
        try:
            _ = await asyncio.wait_for(reader.read(4096), timeout=timeout)
        except asyncio.TimeoutError:
            pass

        # send command
        writer.write(_rcon_make_packet(2, 2, command))
        await writer.drain()

        # read response chunks
        chunks = []
        end_time = time.time() + timeout
        while time.time() < end_time:
            try:
                part = await asyncio.wait_for(reader.read(4096), timeout=0.35)
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
            size = int.from_bytes(data[i : i + 4], "little", signed=True)
            i += 4
            if size < 10 or i + size > len(data):
                break
            pkt = data[i : i + size]
            i += size

            body = pkt[8:-2]
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
    ASA RCON ListPlayers typically returns lines like:
    0. Name, 0002xxxx... (UniqueNetId)
    We'll keep only the character name (left of comma).
    """
    players = []
    if not output:
        return players

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue

        # Strip "0. " prefix
        if ". " in line:
            line = line.split(". ", 1)[1].strip()

        # name before first comma
        if "," in line:
            name = line.split(",", 1)[0].strip()
        else:
            name = line.strip()

        if name and name.lower() not in ("executing", "listplayers", "done"):
            players.append(name)

    return players

# =====================
# WEBHOOK HELPER
# =====================
async def upsert_webhook(session: aiohttp.ClientSession, url: str, key: str, embed: dict):
    mid = message_ids.get(key)

    if mid:
        async with session.patch(f"{url}/messages/{mid}", json={"embeds": [embed]}) as r:
            if r.status == 404:
                message_ids[key] = None
                save_json_file(MESSAGE_IDS_FILE, message_ids)
                return await upsert_webhook(session, url, key, embed)
        return

    async with session.post(url + "?wait=true", json={"embeds": [embed]}) as r:
        data = await r.json()
        message_ids[key] = data["id"]
        save_json_file(MESSAGE_IDS_FILE, message_ids)

async def update_players_embed(session: aiohttp.ClientSession, force: bool = False):
    """
    Updates the players webhook embed using:
      - RCON ListPlayers for names
      - Nitrado for online status + fallback count
    Returns (emoji, count, online, names_list)
    """
    online_nitrado, nitrado_count = await get_server_status(session)

    # RCON list (names)
    names = []
    rcon_ok = True
    rcon_err = None
    try:
        out = await rcon_command("ListPlayers", timeout=6.0)
        names = parse_listplayers(out)
    except Exception as e:
        rcon_ok = False
        rcon_err = str(e)

    online = online_nitrado or rcon_ok
    count = len(names) if names else nitrado_count
    emoji = "🟢" if online else "🔴"

    # Build embed
    if names:
        lines = [f"{idx+1:02d}) {n}" for idx, n in enumerate(names[:50])]
        desc = f"**{count}/{PLAYER_CAP}** online\n\n" + "\n".join(lines)
    else:
        if not rcon_ok:
            desc = f"**{count}/{PLAYER_CAP}** online\n\n*(Could not fetch player names via RCON: {rcon_err})*"
        else:
            desc = f"**{count}/{PLAYER_CAP}** online\n\n*(No player list returned.)*"

    embed = {
        "title": "Online Players",
        "description": desc,
        "color": 0x2ECC71 if online else 0xE74C3C,
        "footer": {"text": f"Last update: {time.strftime('%H:%M:%S')}"},
    }

    # Only edit webhook if changed or forced
    snapshot = {
        "online": online,
        "count": count,
        "names": names[:50],
    }
    return emoji, count, online, names, embed, snapshot

# =====================
# LOOPS
# =====================
async def time_loop():
    """
    Updates time webhook only every UPDATE_IG_MINUTES in-game minutes.
    """
    global last_announced_day
    await client.wait_until_ready()

    async with aiohttp.ClientSession() as session:
        last_tick_bucket = None

        while True:
            t = calculate_time()
            if t:
                title, color, year, day, minute_of_day, _current_spm = t

                # bucket changes every UPDATE_IG_MINUTES
                bucket = (year, day, minute_of_day // UPDATE_IG_MINUTES)

                if bucket != last_tick_bucket:
                    embed = {"title": title, "color": color}
                    await upsert_webhook(session, WEBHOOK_URL, "time", embed)
                    last_tick_bucket = bucket

                # new day announcement
                absolute_day = year * 365 + day
                if last_announced_day is None:
                    last_announced_day = absolute_day
                elif absolute_day > last_announced_day:
                    ch = client.get_channel(ANNOUNCE_CHANNEL_ID)
                    if ch:
                        await ch.send(f"📅 **New Solunaris Day** — Day **{day}**, Year **{year}**")
                    last_announced_day = absolute_day

                # Sleep until the next in-game tick boundary (integrated across day/night)
                sleep_for = seconds_until_next_tick(day, minute_of_day)
            else:
                sleep_for = 5.0

            await asyncio.sleep(sleep_for)

async def status_loop():
    """
    Poll every 15s.
    Only update VC + webhook if something changed,
    but force an update every 10 minutes regardless.
    """
    global _last_status_snapshot, _last_status_force_ts

    await client.wait_until_ready()
    async with aiohttp.ClientSession() as session:
        while True:
            now = time.time()
            force = (now - _last_status_force_ts) >= STATUS_FORCE_UPDATE_SECONDS

            emoji, count, online, names, embed, snapshot = await update_players_embed(session)

            changed = (_last_status_snapshot != snapshot)

            if changed or force:
                # update VC
                vc = client.get_channel(STATUS_VC_ID)
                if vc:
                    try:
                        await vc.edit(name=f"{emoji} Solunaris | {count}/{PLAYER_CAP}")
                    except Exception:
                        pass

                # update players webhook
                await upsert_webhook(session, PLAYERS_WEBHOOK_URL, "players", embed)

                _last_status_snapshot = snapshot
                _last_status_force_ts = now

            await asyncio.sleep(STATUS_POLL_SECONDS)

# =====================
# COMMANDS
# =====================
@tree.command(name="settime", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(year="Year number", day="Day of year (1–365)", hour="Hour (0–23)", minute="Minute (0–59)")
async def settime(i: discord.Interaction, year: int, day: int, hour: int, minute: int):
    if not any(r.id == ADMIN_ROLE_ID for r in i.user.roles):
        await i.response.send_message("❌ No permission", ephemeral=True)
        return

    if year < 1 or day < 1 or day > 365 or hour < 0 or hour > 23 or minute < 0 or minute > 59:
        await i.response.send_message("❌ Invalid values", ephemeral=True)
        return

    global state
    state = {
        "epoch": time.time(),
        "year": int(year),
        "day": int(day),
        "hour": int(hour),
        "minute": int(minute),
    }
    save_state(state)
    await i.response.send_message("✅ Time set", ephemeral=True)

@tree.command(name="status", guild=discord.Object(id=GUILD_ID))
async def status(i: discord.Interaction):
    await i.response.defer(ephemeral=True)

    async with aiohttp.ClientSession() as session:
        # force an update bump of the players webhook when /status is used
        emoji, count, online, names, embed, snapshot = await update_players_embed(session)
        await upsert_webhook(session, PLAYERS_WEBHOOK_URL, "players", embed)

    if names:
        preview = "\n".join([f"- {n}" for n in names[:10]])
        extra = f"\n\n**Players:**\n{preview}"
        if len(names) > 10:
            extra += f"\n…and {len(names) - 10} more"
    else:
        extra = ""

    await i.followup.send(
        f"{emoji} **Solunaris** — {count}/{PLAYER_CAP} players{extra}",
        ephemeral=True,
    )

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