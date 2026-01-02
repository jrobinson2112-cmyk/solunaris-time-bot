import os
import time
import json
import asyncio
import socket
import aiohttp
import discord
from discord import app_commands

# =====================
# CONFIG / ENV
# =====================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# Optional env vars
STATUS_VOICE_CHANNEL_ID = os.getenv("STATUS_VOICE_CHANNEL_ID")     # voice channel ID to rename for server status
DAY_ANNOUNCE_CHANNEL_ID = os.getenv("DAY_ANNOUNCE_CHANNEL_ID")     # text channel ID for "new day" message (optional)

GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

# Ark server query (A2S / Source query)
ARK_HOST = "31.214.239.2"
ARK_QUERY_PORT = 5020
PLAYER_CAP = 42

# Day/night minute lengths (your measured values)
DAY_SECONDS_PER_INGAME_MINUTE = 4.7666667
NIGHT_SECONDS_PER_INGAME_MINUTE = 4.045

# Day/night boundaries (YOU SAID day = 05:30–17:30)
SUNRISE_MIN = 5 * 60 + 30   # 05:30
SUNSET_MIN  = 17 * 60 + 30  # 17:30

DAY_COLOR = 0xF1C40F    # Yellow
NIGHT_COLOR = 0x5865F2  # Blue

STATE_FILE = "state.json"

if not DISCORD_TOKEN or not WEBHOOK_URL:
    raise RuntimeError("Missing DISCORD_TOKEN or WEBHOOK_URL")

# =====================
# DISCORD SETUP
# =====================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# =====================
# STATE LOAD/SAVE
# =====================
def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return None

def save_state(data):
    with open(STATE_FILE, "w") as f:
        json.dump(data, f)

state = load_state() or None
webhook_message_id = None
if isinstance(state, dict):
    webhook_message_id = state.get("webhook_message_id")

# =====================
# TIME CALC (DAY/NIGHT PIECEWISE)
# =====================
def is_day_by_minute(minute_of_day: int) -> bool:
    return SUNRISE_MIN <= minute_of_day < SUNSET_MIN

def seconds_per_minute_for(minute_of_day: int) -> float:
    return DAY_SECONDS_PER_INGAME_MINUTE if is_day_by_minute(minute_of_day) else NIGHT_SECONDS_PER_INGAME_MINUTE

def advance_minutes_piecewise(start_day: int, start_minute_of_day: int, elapsed_real_seconds: float):
    """
    Advances in-game time using different real-seconds-per-in-game-minute for day vs night.
    Smooth at sunrise/sunset by integrating across segments.
    Returns (day, minute_of_day_int).
    """
    day = int(start_day)
    minute_of_day = float(start_minute_of_day)
    remaining = float(elapsed_real_seconds)

    for _ in range(20000):
        if remaining <= 0:
            break

        current_min_int = int(minute_of_day) % 1440
        spm = seconds_per_minute_for(current_min_int)

        # Determine next boundary total minute index (relative to day count)
        if is_day_by_minute(current_min_int):
            # next boundary: sunset same day
            boundary_total = (day - 1) * 1440 + SUNSET_MIN
        else:
            # night -> next boundary is sunrise (might be next day)
            if current_min_int < SUNRISE_MIN:
                boundary_total = (day - 1) * 1440 + SUNRISE_MIN
            else:
                boundary_total = (day) * 1440 + SUNRISE_MIN  # next day's sunrise

        current_total = (day - 1) * 1440 + minute_of_day
        minutes_until_boundary = boundary_total - current_total
        if minutes_until_boundary < 0:
            minutes_until_boundary = 0

        seconds_to_boundary = minutes_until_boundary * spm

        if seconds_to_boundary > 0 and remaining >= seconds_to_boundary:
            remaining -= seconds_to_boundary
            minute_of_day += minutes_until_boundary
        else:
            add_minutes = remaining / spm if spm > 0 else 0
            minute_of_day += add_minutes
            remaining = 0

        while minute_of_day >= 1440:
            minute_of_day -= 1440
            day += 1

    return day, int(minute_of_day) % 1440

def calculate_time():
    """
    Returns (title, color, current_spm, day_num, year_num, minute_of_day)
    Year rolls every 365 days.
    """
    if not state:
        return None

    elapsed_real = time.time() - float(state["real_epoch"])

    start_day = int(state["day"])
    start_year = int(state["year"])
    start_minute_of_day = int(state["hour"]) * 60 + int(state["minute"])

    day_num, minute_of_day = advance_minutes_piecewise(start_day, start_minute_of_day, elapsed_real)

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
    current_spm = DAY_SECONDS_PER_INGAME_MINUTE if day_now else NIGHT_SECONDS_PER_INGAME_MINUTE

    title = f"{emoji} | Solunaris Time | {hour:02d}:{minute:02d} | Day {day_num} | Year {year}"
    return title, color, current_spm, day_num, year, minute_of_day

# =====================
# WEBHOOK HELPERS (EDIT SAME MESSAGE)
# =====================
async def webhook_upsert_embed(session: aiohttp.ClientSession, embed: dict):
    """
    Creates a webhook message once, then edits it forever.
    If it was deleted/invalid, recreate automatically.
    """
    global webhook_message_id, state

    # If we have an id, try patch first
    if webhook_message_id:
        try:
            async with session.patch(
                f"{WEBHOOK_URL}/messages/{webhook_message_id}",
                json={"embeds": [embed]},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status in (200, 204):
                    return
                if resp.status == 404:
                    webhook_message_id = None
        except Exception:
            webhook_message_id = None

    # Create new message
    async with session.post(
        WEBHOOK_URL + "?wait=true",
        json={"embeds": [embed]},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        data = await resp.json()
        webhook_message_id = data.get("id")

        if isinstance(state, dict):
            state["webhook_message_id"] = webhook_message_id
            save_state(state)

# =====================
# ARK SERVER STATUS (A2S)
# =====================
def _read_cstring(buf: bytes, idx: int):
    end = buf.index(b"\x00", idx)
    return buf[idx:end].decode("utf-8", errors="replace"), end + 1

def _a2s_info_blocking(host: str, port: int, timeout: float = 2.5):
    """
    Minimal A2S_INFO query. Returns dict:
    {"online": bool, "players": int|None, "max_players": int|None, "name": str|None}
    """
    addr = (host, port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)

    try:
        req = b"\xFF\xFF\xFF\xFFTSource Engine Query\x00"
        sock.sendto(req, addr)
        data, _ = sock.recvfrom(4096)

        # Challenge
        if len(data) >= 5 and data[4] == 0x41:
            challenge = data[5:9]
            sock.sendto(req + challenge, addr)
            data, _ = sock.recvfrom(4096)

        # A2S_INFO response type
        if len(data) < 6 or data[4] != 0x49:
            return {"online": False, "players": None, "max_players": None, "name": None}

        i = 5  # protocol
        i += 1

        name, i = _read_cstring(data, i)
        _map, i = _read_cstring(data, i)
        _folder, i = _read_cstring(data, i)
        _game, i = _read_cstring(data, i)

        if i + 2 > len(data):
            return {"online": True, "players": None, "max_players": None, "name": name}
        i += 2  # app id

        if i + 3 > len(data):
            return {"online": True, "players": None, "max_players": None, "name": name}

        players = int(data[i])
        max_players = int(data[i + 1])

        return {"online": True, "players": players, "max_players": max_players, "name": name}

    except Exception:
        return {"online": False, "players": None, "max_players": None, "name": None}
    finally:
        try:
            sock.close()
        except Exception:
            pass

async def get_server_status():
    return await asyncio.to_thread(_a2s_info_blocking, ARK_HOST, ARK_QUERY_PORT, 2.5)

# =====================
# LOOPS
# =====================
last_day_key = None

async def time_update_loop():
    """
    Updates webhook at current in-game minute length (day/night),
    and optionally posts a message when a new day starts.
    """
    global last_day_key
    await client.wait_until_ready()

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                if state:
                    out = calculate_time()
                    if out:
                        title, color, current_spm, day_num, year_num, _minute_of_day = out

                        embed = {
                            "title": title,   # title appears larger/bolder in embeds
                            "color": int(color),
                        }

                        await webhook_upsert_embed(session, embed)

                        # Day announcement (optional)
                        if DAY_ANNOUNCE_CHANNEL_ID:
                            key = f"{year_num}:{day_num}"
                            if last_day_key is None:
                                last_day_key = key
                            elif key != last_day_key:
                                ch = client.get_channel(int(DAY_ANNOUNCE_CHANNEL_ID))
                                if ch:
                                    await ch.send(f"📅 **New Solunaris Day!** Day {day_num} — Year {year_num}")
                                last_day_key = key

                        sleep_for = float(current_spm) if current_spm else DAY_SECONDS_PER_INGAME_MINUTE
                    else:
                        sleep_for = DAY_SECONDS_PER_INGAME_MINUTE
                else:
                    sleep_for = DAY_SECONDS_PER_INGAME_MINUTE

            except Exception as e:
                # Don't crash loop
                sleep_for = 5.0

            await asyncio.sleep(sleep_for)

async def status_update_loop():
    """
    Checks every 15s, updates VC only if changed,
    but forces refresh every 10 minutes regardless.
    """
    await client.wait_until_ready()

    if not STATUS_VOICE_CHANNEL_ID:
        print("ℹ️ STATUS_VOICE_CHANNEL_ID not set; skipping status VC updates.")
        return

    vc_id = int(STATUS_VOICE_CHANNEL_ID)
    last_name = None
    last_force = 0.0

    while True:
        try:
            info = await get_server_status()
            online = bool(info.get("online"))
            players = info.get("players")
            max_players = info.get("max_players")

            cap = int(max_players) if isinstance(max_players, int) and max_players > 0 else PLAYER_CAP

            if online:
                emoji = "🟢"
                ptxt = f"{players}/{cap}" if isinstance(players, int) else f"?/{cap}"
            else:
                emoji = "🔴"
                ptxt = f"?/{cap}"

            new_name = f"{emoji} Solunaris | {ptxt}"

            now = time.time()
            must_force = (now - last_force) >= 600  # 10 minutes
            changed = (new_name != last_name)

            if changed or must_force:
                ch = client.get_channel(vc_id)
                if ch:
                    try:
                        await ch.edit(name=new_name)
                        last_name = new_name
                        last_force = now
                    except discord.Forbidden:
                        print("❌ Missing permission to rename the status voice channel.")
                    except Exception as e:
                        print(f"⚠️ Failed to edit VC name: {e}")

        except Exception as e:
            print(f"⚠️ Status loop error: {e}")

        await asyncio.sleep(15)

# =====================
# SLASH COMMANDS
# =====================
@tree.command(
    name="day",
    description="Show current Solunaris time",
    guild=discord.Object(id=GUILD_ID),
)
async def day_cmd(interaction: discord.Interaction):
    if not state:
        await interaction.response.send_message("⏳ Time not set yet. Use /settime.", ephemeral=True)
        return

    out = calculate_time()
    if not out:
        await interaction.response.send_message("⚠️ Time not available.", ephemeral=True)
        return

    title, _, _, _, _, _ = out
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
)
async def settime_cmd(interaction: discord.Interaction, year: int, day: int, hour: int, minute: int):
    # Role check
    roles = getattr(interaction.user, "roles", [])
    if not any(getattr(r, "id", None) == ADMIN_ROLE_ID for r in roles):
        await interaction.response.send_message("❌ No permission (missing required role).", ephemeral=True)
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
        "webhook_message_id": webhook_message_id,
    }
    save_state(state)

    await interaction.response.send_message(
        f"✅ Set to Day {day}, {hour:02d}:{minute:02d}, Year {year}",
        ephemeral=True,
    )

@tree.command(
    name="status",
    description="Show Solunaris server status & players",
    guild=discord.Object(id=GUILD_ID),
)
async def status_cmd(interaction: discord.Interaction):
    # Must respond quickly to avoid "application did not respond"
    await interaction.response.defer(ephemeral=True)

    info = await get_server_status()
    online = bool(info.get("online"))
    players = info.get("players")
    max_players = info.get("max_players")

    cap = int(max_players) if isinstance(max_players, int) and max_players > 0 else PLAYER_CAP

    if online:
        emoji = "🟢"
        ptxt = f"{players}/{cap}" if isinstance(players, int) else f"?/{cap}"
        msg = f"{emoji} **Solunaris is ONLINE** — Players: **{ptxt}**"
    else:
        emoji = "🔴"
        msg = f"{emoji} **Solunaris is OFFLINE** — Players: **?/{cap}** (query failed)"

    await interaction.followup.send(msg, ephemeral=True)

# =====================
# STARTUP
# =====================
@client.event
async def on_ready():
    try:
        await tree.sync(guild=discord.Object(id=GUILD_ID))
        print("✅ Commands synced")
    except Exception as e:
        print(f"⚠️ Sync failed: {e}")

    client.loop.create_task(time_update_loop())
    client.loop.create_task(status_update_loop())
    print("✅ Loops started")

client.run(DISCORD_TOKEN)