import os
import time
import json
import asyncio
import aiohttp
import discord
from discord import app_commands
import socket
import struct

# =====================
# ENV / CONFIG
# =====================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

ARK_HOST = os.getenv("ARK_HOST", "31.214.239.2")
ARK_QUERY_PORT = int(os.getenv("ARK_QUERY_PORT", "5020"))

STATUS_VC_ID = int(os.getenv("STATUS_VC_ID", "0"))  # voice channel to rename for status (0 disables)

GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076
PLAYER_CAP = 42

# Smooth day/night minute lengths (real seconds per in-game minute)
DAY_SECONDS_PER_INGAME_MINUTE = 4.7666667
NIGHT_SECONDS_PER_INGAME_MINUTE = 4.045

STATE_FILE = "state.json"

# Day is 05:30 -> 17:30, Night is 17:30 -> 05:30
SUNRISE_MIN = 5 * 60 + 30   # 05:30
SUNSET_MIN  = 17 * 60 + 30  # 17:30

DAY_COLOR = 0xF1C40F    # Yellow
NIGHT_COLOR = 0x5865F2  # Blue

# Status polling
STATUS_POLL_SECONDS = 15            # check every 15s
STATUS_FORCE_SECONDS = 10 * 60      # force update every 10 minutes
STATUS_SOCKET_TIMEOUT = 1.5         # keep it short so commands don't hang

if not DISCORD_TOKEN or not WEBHOOK_URL:
    raise RuntimeError("Missing DISCORD_TOKEN or WEBHOOK_URL")

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
webhook_message_id = None

# =====================
# TIME CALCULATION (SMOOTH DAY/NIGHT)
# =====================
def is_day_by_minute(minute_of_day: int) -> bool:
    # day: [05:30, 17:30)
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
    minute_of_day = float(start_minute_of_day)  # 0..1439
    remaining = float(elapsed_real_seconds)

    # Prevent runaway loops
    for _ in range(20000):
        if remaining <= 0:
            break

        current_minute_int = int(minute_of_day) % 1440
        spm = seconds_per_minute_for(current_minute_int)

        # Determine next boundary (sunrise or sunset)
        if is_day_by_minute(current_minute_int):
            # next boundary is sunset today
            boundary_total = (day - 1) * 1440 + SUNSET_MIN
        else:
            # night -> next boundary is sunrise (might be next day if after sunset)
            if current_minute_int < SUNRISE_MIN:
                boundary_total = (day - 1) * 1440 + SUNRISE_MIN
            else:
                boundary_total = (day) * 1440 + SUNRISE_MIN  # next day sunrise

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

    elapsed_real = time.time() - state["real_epoch"]

    start_day = int(state["day"])
    start_year = int(state["year"])
    start_minute_of_day = int(state["hour"]) * 60 + int(state["minute"])

    day_num, minute_of_day = advance_minutes_piecewise(start_day, start_minute_of_day, elapsed_real)

    # Year rolling: 365 days per year, calibrated from whatever day you set
    year_num = start_year
    while day_num > 365:
        day_num -= 365
        year_num += 1

    hour = minute_of_day // 60
    minute = minute_of_day % 60

    day_now = is_day_by_minute(minute_of_day)
    emoji = "☀️" if day_now else "🌙"
    color = DAY_COLOR if day_now else NIGHT_COLOR

    title = f"{emoji} | Solunaris Time | {hour:02d}:{minute:02d} | Day {day_num} | Year {year_num}"
    current_spm = DAY_SECONDS_PER_INGAME_MINUTE if day_now else NIGHT_SECONDS_PER_INGAME_MINUTE
    return title, color, current_spm, day_num, year_num, minute_of_day

# =====================
# WEBHOOK: EDIT SAME MESSAGE
# =====================
async def update_time_webhook_loop():
    global webhook_message_id
    await client.wait_until_ready()

    async with aiohttp.ClientSession() as session:
        while True:
            if state:
                result = calculate_time()
                if result:
                    title, color, current_spm, *_ = result

                    # Use embed description to look "bigger" than title; bold it too
                    embed = {
                        "color": color,
                        "description": f"**{title}**"
                    }

                    try:
                        if webhook_message_id:
                            await session.patch(
                                f"{WEBHOOK_URL}/messages/{webhook_message_id}",
                                json={"embeds": [embed]},
                            )
                        else:
                            async with session.post(
                                WEBHOOK_URL + "?wait=true",
                                json={"embeds": [embed]},
                            ) as resp:
                                data = await resp.json()
                                webhook_message_id = data.get("id")
                    except Exception as e:
                        # If message was deleted or ID invalid, recreate it
                        webhook_message_id = None
                        print(f"[webhook] error, will recreate: {e}")

                    await asyncio.sleep(float(current_spm))
                    continue

            await asyncio.sleep(DAY_SECONDS_PER_INGAME_MINUTE)

# =====================
# ARK QUERY (UDP A2S_INFO) — in thread
# =====================
def _a2s_info_query(host: str, port: int, timeout: float):
    """
    Source Engine A2S_INFO query.
    Returns dict: {"online": bool, "players": int|None, "max_players": int|None, "error": str|None}
    """
    addr = (host, port)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)

    # A2S_INFO request
    packet = b"\xFF\xFF\xFF\xFFTSource Engine Query\x00"
    try:
        s.sendto(packet, addr)
        data, _ = s.recvfrom(4096)

        # Basic sanity
        if len(data) < 6 or data[:4] != b"\xFF\xFF\xFF\xFF":
            return {"online": True, "players": None, "max_players": None, "error": "bad response"}

        # Handle split packets? (rare) — if so, just treat as online unknown.
        header = data[4]
        if header == 0x6C:  # 'l' split
            return {"online": True, "players": None, "max_players": None, "error": "split response"}

        if header != 0x49:  # 'I' A2S_INFO
            return {"online": True, "players": None, "max_players": None, "error": f"unexpected header {header}"}

        # Parse: https://developer.valvesoftware.com/wiki/Server_queries#A2S_INFO
        # We only need players + max players; both are 1 byte near the end, but need to walk strings safely.
        idx = 5  # after 0x49
        idx += 1  # protocol byte

        def read_cstring(buf, start):
            end = buf.find(b"\x00", start)
            if end == -1:
                return None, start
            return buf[start:end].decode("utf-8", "ignore"), end + 1

        # name, map, folder, game
        for _ in range(4):
            _, idx = read_cstring(data, idx)
        if idx + 2 > len(data):
            return {"online": True, "players": None, "max_players": None, "error": "short data"}

        idx += 2  # app id (short)

        if idx + 3 > len(data):
            return {"online": True, "players": None, "max_players": None, "error": "short data"}

        players = data[idx]
        max_players = data[idx + 1]
        # bots = data[idx+2] (unused)

        return {"online": True, "players": int(players), "max_players": int(max_players), "error": None}
    except socket.timeout:
        return {"online": False, "players": None, "max_players": None, "error": "TimeoutError"}
    except Exception as e:
        return {"online": False, "players": None, "max_players": None, "error": str(e)}
    finally:
        try:
            s.close()
        except Exception:
            pass

async def get_server_status():
    # run blocking UDP query in a thread so slash commands don't timeout
    return await asyncio.to_thread(_a2s_info_query, ARK_HOST, ARK_QUERY_PORT, STATUS_SOCKET_TIMEOUT)

def format_status_text(st: dict):
    online = st.get("online", False)
    players = st.get("players", None)

    dot = "🟢" if online else "🔴"
    if players is None:
        return f"{dot} Solunaris | Players: ?/{PLAYER_CAP}"
    return f"{dot} Solunaris | Players: {players}/{PLAYER_CAP}"

# =====================
# STATUS VC UPDATER
# =====================
async def status_vc_loop():
    await client.wait_until_ready()
    if not STATUS_VC_ID:
        print("ℹ️ STATUS_VC_ID not set; status VC updates disabled.")
        return

    last_name = None
    last_force = 0.0

    while True:
        try:
            st = await get_server_status()
            new_name = format_status_text(st)

            force = (time.time() - last_force) >= STATUS_FORCE_SECONDS
            changed = (new_name != last_name)

            if changed or force:
                channel = client.get_channel(STATUS_VC_ID)
                if channel is None:
                    # fetch if not cached
                    channel = await client.fetch_channel(STATUS_VC_ID)

                # Only attempt if it supports edit
                await channel.edit(name=new_name)
                last_name = new_name
                last_force = time.time()

        except Exception as e:
            print(f"[status_vc] error: {e}")

        await asyncio.sleep(STATUS_POLL_SECONDS)

# =====================
# SLASH COMMANDS
# =====================
@tree.command(
    name="day",
    description="Show current Solunaris time",
    guild=discord.Object(id=GUILD_ID),
)
async def day_cmd(interaction: discord.Interaction):
    # respond fast
    await interaction.response.defer(ephemeral=True)

    if not state:
        await interaction.followup.send("⏳ Time not set yet.", ephemeral=True)
        return

    result = calculate_time()
    title = result[0] if result else "⏳ Time not set yet."
    await interaction.followup.send(title, ephemeral=True)


@tree.command(
    name="settime",
    description="Set Solunaris time",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(
    year="Year number",
    day="Day of year (1–365)",
    hour="Hour (0–23)",
    minute="Minute (0–59)",
)
async def settime_cmd(interaction: discord.Interaction, year: int, day: int, hour: int, minute: int):
    await interaction.response.defer(ephemeral=True)

    # Role-gated (not admin perm)
    if not any(getattr(r, "id", None) == ADMIN_ROLE_ID for r in getattr(interaction.user, "roles", [])):
        await interaction.followup.send("❌ You don't have the required role to use /settime.", ephemeral=True)
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
    save_state(state)

    await interaction.followup.send(
        f"✅ Set to Day {day}, {hour:02d}:{minute:02d}, Year {year}",
        ephemeral=True,
    )


@tree.command(
    name="status",
    description="Show Solunaris server status + players",
    guild=discord.Object(id=GUILD_ID),
)
async def status_cmd(interaction: discord.Interaction):
    # IMPORTANT: acknowledge instantly so it never times out
    await interaction.response.defer(ephemeral=True)

    st = await get_server_status()
    msg = format_status_text(st)

    # add error hint if query failed
    if not st.get("online", False) and st.get("error"):
        msg += f"\n(query failed: {st['error']})"

    await interaction.followup.send(msg, ephemeral=True)

# =====================
# STARTUP
# =====================
@client.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print("✅ Commands synced")

    client.loop.create_task(update_time_webhook_loop())
    client.loop.create_task(status_vc_loop())

client.run(DISCORD_TOKEN)