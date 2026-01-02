import os
import time
import json
import re
import asyncio
import aiohttp
import socket
import struct
import discord
from discord import app_commands

# ============================================================
# REQUIRED ENV VARS
# ============================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Time webhook (Solunaris time embed)
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# Nitrado API (for auto-sync from logs)
NITRADO_TOKEN = os.getenv("NITRADO_TOKEN")
NITRADO_SERVICE_ID = os.getenv("NITRADO_SERVICE_ID", "17997739")
NITRADO_LOG_FILE_PATH = os.getenv(
    "NITRADO_LOG_FILE_PATH",
    "arksa/ShooterGame/Saved/Logs/ShooterGame.log"
)

# Players list webhook (separate channel)
PLAYERS_WEBHOOK_URL = os.getenv("PLAYERS_WEBHOOK_URL")

# ============================================================
# DISCORD CONFIG (your IDs)
# ============================================================
GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

# Optional: post a message at the start of each new in-game day
# Put the channel ID here as an env var if you want it
NEW_DAY_CHANNEL_ID = os.getenv("NEW_DAY_CHANNEL_ID")  # e.g. "123456789012345678"

# Status voice channel ID (the VC you want renamed)
STATUS_VC_ID = os.getenv("STATUS_VC_ID")  # e.g. "123456789012345678"

# ============================================================
# ARK SERVER QUERY CONFIG
# ============================================================
ARK_IP = os.getenv("ARK_IP", "31.214.239.2")
ARK_QUERY_PORT = int(os.getenv("ARK_QUERY_PORT", "5020"))
PLAYER_CAP = int(os.getenv("PLAYER_CAP", "42"))

# ============================================================
# IN-GAME TIME SPEED CONFIG
# ============================================================
# Your measured minute lengths
DAY_SECONDS_PER_INGAME_MINUTE = float(os.getenv("DAY_SPM", "4.7666667"))
NIGHT_SECONDS_PER_INGAME_MINUTE = float(os.getenv("NIGHT_SPM", "4.045"))

# Day is 05:30 -> 17:30, Night is 17:30 -> 05:30
SUNRISE_MIN = 5 * 60 + 30
SUNSET_MIN = 17 * 60 + 30

DAY_COLOR = 0xF1C40F    # yellow
NIGHT_COLOR = 0x5865F2  # blue

# ============================================================
# LOOP TIMINGS
# ============================================================
# Time webhook updates scale with day/night by sleeping current SPM
# Status checks: look every 15s, update on change, force every 10 minutes
STATUS_CHECK_EVERY_SECONDS = 15
STATUS_FORCE_UPDATE_SECONDS = 600

# Auto-sync from logs (recommended so time never drifts)
AUTO_SYNC_ENABLED = os.getenv("AUTO_SYNC_ENABLED", "true").lower() == "true"
AUTO_SYNC_EVERY_SECONDS = int(os.getenv("AUTO_SYNC_EVERY_SECONDS", "600"))  # 10 min

STATE_FILE = "state.json"

# ============================================================
# BASIC VALIDATION
# ============================================================
missing = []
for k, v in [
    ("DISCORD_TOKEN", DISCORD_TOKEN),
    ("WEBHOOK_URL", WEBHOOK_URL),
    ("PLAYERS_WEBHOOK_URL", PLAYERS_WEBHOOK_URL),
]:
    if not v:
        missing.append(k)

# NITRADO_TOKEN is only required if autosync is enabled
if AUTO_SYNC_ENABLED and not NITRADO_TOKEN:
    missing.append("NITRADO_TOKEN")

if missing:
    raise RuntimeError(f"Missing env var(s): {', '.join(missing)}")

# ============================================================
# DISCORD SETUP
# ============================================================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ============================================================
# STATE
# ============================================================
def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(data):
    with open(STATE_FILE, "w") as f:
        json.dump(data, f)

state = load_state()  # {"real_epoch":..., "year":..., "day":..., "hour":..., "minute":..., "second":...}

time_webhook_message_id = None
players_webhook_message_id = None

last_day_announce = None  # remember last announced in-game day/year

# ============================================================
# TIME CALCULATION (piecewise day/night speeds)
# ============================================================
def is_day_by_minute(minute_of_day: int) -> bool:
    return SUNRISE_MIN <= minute_of_day < SUNSET_MIN

def seconds_per_minute_for(minute_of_day: int) -> float:
    return DAY_SECONDS_PER_INGAME_MINUTE if is_day_by_minute(minute_of_day) else NIGHT_SECONDS_PER_INGAME_MINUTE

def advance_minutes_piecewise(start_day: int, start_minute_of_day: int, start_second: int, elapsed_real_seconds: float):
    """
    Advances in-game time using different real-seconds-per-in-game-minute for day vs night.
    Also advances seconds smoothly (we track seconds too).
    Returns (day, minute_of_day_int, second_int).
    """
    day = int(start_day)
    total_ingame_seconds = int(start_minute_of_day) * 60 + int(start_second)
    remaining = float(elapsed_real_seconds)

    # Safety cap
    for _ in range(200000):
        if remaining <= 0:
            break

        minute_of_day = (total_ingame_seconds // 60) % 1440
        spm = seconds_per_minute_for(minute_of_day)  # real seconds per in-game minute

        # Convert real seconds -> in-game seconds rate
        # 1 in-game minute = 60 in-game seconds, takes spm real seconds
        # so in-game-seconds per real-second = 60 / spm
        rate = 60.0 / spm if spm > 0 else 0.0

        # Figure next boundary (sunrise/sunset) in in-game seconds
        if is_day_by_minute(minute_of_day):
            boundary_min = SUNSET_MIN
            boundary_day_offset = 0
        else:
            # night: boundary is sunrise; if we're after sunset, sunrise is next day
            if minute_of_day < SUNRISE_MIN:
                boundary_min = SUNRISE_MIN
                boundary_day_offset = 0
            else:
                boundary_min = SUNRISE_MIN
                boundary_day_offset = 1

        current_min = minute_of_day
        current_sec_in_min = total_ingame_seconds % 60
        current_abs = (day - 1) * 86400 + current_min * 60 + current_sec_in_min
        boundary_abs = (day - 1 + boundary_day_offset) * 86400 + boundary_min * 60

        ingame_seconds_to_boundary = boundary_abs - current_abs
        if ingame_seconds_to_boundary < 0:
            ingame_seconds_to_boundary = 0

        real_seconds_to_boundary = ingame_seconds_to_boundary / rate if rate > 0 else float("inf")

        if remaining >= real_seconds_to_boundary and real_seconds_to_boundary > 0:
            # jump exactly to boundary
            remaining -= real_seconds_to_boundary
            total_ingame_seconds += ingame_seconds_to_boundary
        else:
            # advance partially
            add_ingame_seconds = int(remaining * rate)
            total_ingame_seconds += add_ingame_seconds
            remaining = 0

        # normalize day
        while total_ingame_seconds >= 86400:
            total_ingame_seconds -= 86400
            day += 1

    minute_of_day = (total_ingame_seconds // 60) % 1440
    second = total_ingame_seconds % 60
    return day, int(minute_of_day), int(second)

def calculate_time():
    """
    Returns:
      title (str),
      color (int),
      current_spm (float),
      (year, day, hour, minute, second, is_day)
    Year rolls every 365 days.
    """
    if not state:
        return None, None, None, None

    elapsed_real = time.time() - state["real_epoch"]

    start_day = int(state["day"])
    start_year = int(state["year"])
    start_minute_of_day = int(state["hour"]) * 60 + int(state["minute"])
    start_second = int(state.get("second", 0))

    day_num, minute_of_day, second = advance_minutes_piecewise(start_day, start_minute_of_day, start_second, elapsed_real)

    # Year rolling: 365 days per year
    year = start_year
    while day_num > 365:
        day_num -= 365
        year += 1

    hour = minute_of_day // 60
    minute = minute_of_day % 60

    day_now = is_day_by_minute(minute_of_day)
    emoji = "☀️" if day_now else "🌙"
    color = DAY_COLOR if day_now else NIGHT_COLOR

    title = f"{emoji} | Solunaris Time | {hour:02d}:{minute:02d}:{second:02d} | Day {day_num} | Year {year}"
    current_spm = DAY_SECONDS_PER_INGAME_MINUTE if day_now else NIGHT_SECONDS_PER_INGAME_MINUTE

    return title, color, current_spm, (year, day_num, hour, minute, second, day_now)

# ============================================================
# NITRADO LOG AUTO-SYNC
# ============================================================
DAYTIME_RE = re.compile(r"Day\s+(\d+),\s+(\d{1,2}):(\d{2}):(\d{2})")

async def nitrado_download_log_text(session: aiohttp.ClientSession) -> str | None:
    """
    Downloads the log file content via Nitrado API.
    """
    if not NITRADO_TOKEN:
        return None

    headers = {"Authorization": f"Bearer {NITRADO_TOKEN}"}
    # Get a download URL
    url = f"https://api.nitrado.net/services/{NITRADO_SERVICE_ID}/gameservers/file_server/download"
    params = {"file": NITRADO_LOG_FILE_PATH}

    async with session.get(url, headers=headers, params=params, timeout=30) as resp:
        data = await resp.json()
        # Expected: data["data"]["token"]["url"]
        dl = (
            data.get("data", {})
                .get("token", {})
                .get("url")
        )
        if not dl:
            return None

    async with session.get(dl, timeout=30) as resp2:
        return await resp2.text(errors="ignore")

def extract_latest_day_time(log_text: str):
    """
    Finds the last occurrence of "Day X, HH:MM:SS" in the log.
    Returns (day, hour, minute, second) or None
    """
    matches = list(DAYTIME_RE.finditer(log_text))
    if not matches:
        return None
    m = matches[-1]
    day = int(m.group(1))
    hour = int(m.group(2))
    minute = int(m.group(3))
    second = int(m.group(4))
    return day, hour, minute, second

async def autosync_from_logs(session: aiohttp.ClientSession):
    """
    Pull latest Day+Time from logs and reset our epoch to match.
    Keeps current YEAR as-is (we can’t reliably infer year from logs).
    """
    global state
    if not state:
        return False

    txt = await nitrado_download_log_text(session)
    if not txt:
        return False

    latest = extract_latest_day_time(txt)
    if not latest:
        return False

    day, hour, minute, second = latest
    # Keep year from state
    year = int(state["year"])

    # Reset epoch to "now is exactly that in-game time"
    state = {
        "real_epoch": time.time(),
        "year": year,
        "day": day,
        "hour": hour,
        "minute": minute,
        "second": second,
    }
    save_state(state)
    return True

# ============================================================
# SOURCE A2S QUERY (UDP) for status + player count
# ============================================================
def a2s_info(ip: str, port: int, timeout: float = 2.0):
    """
    Minimal Source A2S_INFO query.
    Returns dict or None.
    """
    # A2S_INFO request
    request = b"\xFF\xFF\xFF\xFFTSource Engine Query\x00"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(request, (ip, port))
        data, _ = sock.recvfrom(4096)
    except Exception:
        return None
    finally:
        sock.close()

    # Response should start with 0xFFFFFFFF 'I'
    if len(data) < 6 or data[:4] != b"\xFF\xFF\xFF\xFF" or data[4:5] not in (b"I", b"m"):
        return None

    # Parse: protocol byte then null-terminated strings etc.
    # We'll parse only what we need safely.
    try:
        idx = 5
        _protocol = data[idx]
        idx += 1

        def read_cstr():
            nonlocal idx
            end = data.index(b"\x00", idx)
            s = data[idx:end].decode("utf-8", errors="ignore")
            idx = end + 1
            return s

        name = read_cstr()
        _map = read_cstr()
        _folder = read_cstr()
        _game = read_cstr()

        # short: appid
        idx += 2

        players = data[idx]
        idx += 1
        max_players = data[idx]
        idx += 1

        return {"name": name, "players": int(players), "max_players": int(max_players)}
    except Exception:
        return None

# ============================================================
# WEBHOOK HELPERS (EDIT-ONLY)
# ============================================================
async def webhook_edit_or_create(session: aiohttp.ClientSession, webhook_url: str, message_id_holder: dict, key: str, payload: dict):
    """
    Edits the existing message if we have an ID.
    Otherwise creates once and stores message_id.
    """
    mid = message_id_holder.get(key)

    if mid:
        async with session.patch(f"{webhook_url}/messages/{mid}", json=payload) as r:
            if r.status in (200, 204):
                return True
            # if message deleted, recreate
            if r.status == 404:
                message_id_holder[key] = None

    async with session.post(webhook_url + "?wait=true", json=payload) as r2:
        data = await r2.json()
        message_id_holder[key] = data.get("id")
        return True

# ============================================================
# TIME WEBHOOK LOOP (edits only)
# ============================================================
async def time_webhook_loop():
    global time_webhook_message_id, last_day_announce

    await client.wait_until_ready()

    holder = {"time": time_webhook_message_id}

    async with aiohttp.ClientSession() as session:
        while True:
            if state:
                title, color, current_spm, parts = calculate_time()
                if title:
                    year, day_num, hour, minute, second, is_day = parts

                    embed = {
                        "title": title,
                        "color": color
                    }

                    await webhook_edit_or_create(
                        session,
                        WEBHOOK_URL,
                        holder,
                        "time",
                        {"embeds": [embed]}
                    )
                    time_webhook_message_id = holder["time"]

                    # New day announcement (optional)
                    if NEW_DAY_CHANNEL_ID:
                        key = f"{year}-{day_num}"
                        if last_day_announce != key:
                            # announce only if we have already announced at least once OR if you want immediate announce
                            # We'll announce whenever the day changes.
                            ch = client.get_channel(int(NEW_DAY_CHANNEL_ID))
                            if ch:
                                await ch.send(f"🌅 **New Solunaris day!** Day **{day_num}** | Year **{year}**")
                            last_day_announce = key

                    # Sleep scaled to day/night
                    sleep_for = float(current_spm) if current_spm else DAY_SECONDS_PER_INGAME_MINUTE
                else:
                    sleep_for = DAY_SECONDS_PER_INGAME_MINUTE
            else:
                sleep_for = DAY_SECONDS_PER_INGAME_MINUTE

            await asyncio.sleep(sleep_for)

# ============================================================
# AUTO-SYNC LOOP (Nitrado logs)
# ============================================================
async def autosync_loop():
    await client.wait_until_ready()
    if not AUTO_SYNC_ENABLED:
        return

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                if state:
                    await autosync_from_logs(session)
            except Exception:
                pass
            await asyncio.sleep(AUTO_SYNC_EVERY_SECONDS)

# ============================================================
# SERVER STATUS + PLAYERS
# ============================================================
def format_status_vc_name(online: bool, players: int):
    dot = "🟢" if online else "🔴"
    return f"{dot} Solunaris | {players}/{PLAYER_CAP}"

async def get_server_status():
    info = await asyncio.to_thread(a2s_info, ARK_IP, ARK_QUERY_PORT)
    if not info:
        return False, 0
    players = int(info.get("players", 0))
    return True, players

async def update_status_vc(force: bool = False, last_sent: dict | None = None):
    """
    Only edits VC name if changed, unless force=True.
    """
    if not STATUS_VC_ID:
        return

    online, players = await get_server_status()
    new_name = format_status_vc_name(online, players)

    if last_sent is not None and not force:
        if last_sent.get("name") == new_name:
            return
    last_sent["name"] = new_name

    ch = client.get_channel(int(STATUS_VC_ID))
    if ch:
        try:
            await ch.edit(name=new_name)
        except Exception:
            pass

async def update_players_webhook(force: bool = False, last_sent: dict | None = None):
    """
    Updates the PLAYERS_WEBHOOK_URL embed, edit-only.
    This version keeps ONLY character names (no EOS IDs).
    Because getting platform/GT reliably needs save parsing or a dedicated mod.
    """
    global players_webhook_message_id

    online, players = await get_server_status()
    title = "Online Players" if online else "Server Offline"
    color = 0x2ECC71 if online else 0xE74C3C

    # We can’t reliably get platform names via A2S on ASA; it often doesn’t match.
    # So we show a count + note.
    desc = f"**{players}/{PLAYER_CAP}** online"

    embed = {
        "title": title,
        "description": desc,
        "color": color,
    }

    holder = {"players": players_webhook_message_id}

    async with aiohttp.ClientSession() as session:
        await webhook_edit_or_create(
            session,
            PLAYERS_WEBHOOK_URL,
            holder,
            "players",
            {"embeds": [embed]}
        )
        players_webhook_message_id = holder["players"]

# Main loop: check every 15s, update if changed, force every 10 min
async def status_loop():
    await client.wait_until_ready()

    last_status = {"name": None}
    last_force = 0.0

    while True:
        now = time.time()
        force = (now - last_force) >= STATUS_FORCE_UPDATE_SECONDS
        try:
            await update_status_vc(force=force, last_sent=last_status)
            if force:
                await update_players_webhook(force=True, last_sent={})
                last_force = now
        except Exception:
            pass

        await asyncio.sleep(STATUS_CHECK_EVERY_SECONDS)

# ============================================================
# SLASH COMMANDS
# ============================================================
@tree.command(
    name="day",
    description="Show current Solunaris time",
    guild=discord.Object(id=GUILD_ID),
)
async def day_cmd(interaction: discord.Interaction):
    if not state:
        await interaction.response.send_message("⏳ Time not set yet. Use /settime (admin).", ephemeral=True)
        return
    title, _, _, _ = calculate_time()
    await interaction.response.send_message(title, ephemeral=True)

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
    second="Second (0–59)",
)
async def settime_cmd(interaction: discord.Interaction, year: int, day: int, hour: int, minute: int, second: int = 0):
    # respond fast to avoid “application did not respond”
    await interaction.response.defer(ephemeral=True)

    # role check
    ok = False
    try:
        for r in interaction.user.roles:
            if r.id == ADMIN_ROLE_ID:
                ok = True
                break
    except Exception:
        ok = False

    if not ok:
        await interaction.followup.send("❌ No permission.", ephemeral=True)
        return

    if year < 1 or day < 1 or day > 365 or hour < 0 or hour > 23 or minute < 0 or minute > 59 or second < 0 or second > 59:
        await interaction.followup.send("❌ Invalid values.", ephemeral=True)
        return

    global state
    state = {
        "real_epoch": time.time(),
        "year": year,
        "day": day,
        "hour": hour,
        "minute": minute,
        "second": second,
    }
    save_state(state)

    await interaction.followup.send(
        f"✅ Set to **Day {day}**, **{hour:02d}:{minute:02d}:{second:02d}**, **Year {year}**",
        ephemeral=True,
    )

@tree.command(
    name="status",
    description="Show server status + player count, and refresh the status VC + players channel",
    guild=discord.Object(id=GUILD_ID),
)
async def status_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    online, players = await get_server_status()
    dot = "🟢" if online else "🔴"
    msg = f"{dot} Solunaris is **{'ONLINE' if online else 'OFFLINE'}** — **{players}/{PLAYER_CAP}** players"

    # Trigger refresh of VC + players webhook
    await update_status_vc(force=True, last_sent={"name": None})
    await update_players_webhook(force=True, last_sent={})

    await interaction.followup.send(msg, ephemeral=True)

# ============================================================
# STARTUP
# ============================================================
@client.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print("✅ Commands synced")

    # start loops
    client.loop.create_task(time_webhook_loop())
    client.loop.create_task(status_loop())
    client.loop.create_task(autosync_loop())

client.run(DISCORD_TOKEN)