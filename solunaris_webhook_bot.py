import os
import time
import json
import asyncio
import aiohttp
import socket
import struct
import discord
from discord import app_commands

# =====================
# ENV / CONFIG
# =====================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# Your guild + admin role
GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

# ARK server query (Steam A2S)
SERVER_IP = os.getenv("SERVER_IP", "31.214.239.2")
QUERY_PORT = int(os.getenv("QUERY_PORT", "5020"))
PLAYER_CAP = int(os.getenv("PLAYER_CAP", "42"))

# VC that shows server status (must be a VOICE channel id)
STATUS_VOICE_CHANNEL_ID = os.getenv("STATUS_VOICE_CHANNEL_ID")  # required for status VC updates

# Optional: announce each new in-game day in a text channel
DAY_ANNOUNCE_CHANNEL_ID = os.getenv("DAY_ANNOUNCE_CHANNEL_ID")  # optional

STATE_FILE = "state.json"

# Day/night timing
DAY_SECONDS_PER_INGAME_MINUTE = 4.7666667
NIGHT_SECONDS_PER_INGAME_MINUTE = 4.045

# Day is 05:30 -> 17:30, night is 17:30 -> 05:30
SUNRISE_MIN = 5 * 60 + 30   # 05:30
SUNSET_MIN  = 17 * 60 + 30  # 17:30

DAY_COLOR = 0xF1C40F    # yellow
NIGHT_COLOR = 0x5865F2  # blue

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
last_announced_day = None  # (year, day)

# =====================
# IN-GAME TIME CALC (PIECEWISE DAY/NIGHT)
# =====================
def is_day_by_minute(minute_of_day: int) -> bool:
    return SUNRISE_MIN <= minute_of_day < SUNSET_MIN

def seconds_per_minute_for(minute_of_day: int) -> float:
    return DAY_SECONDS_PER_INGAME_MINUTE if is_day_by_minute(minute_of_day) else NIGHT_SECONDS_PER_INGAME_MINUTE

def advance_minutes_piecewise(start_day: int, start_minute_of_day: int, elapsed_real_seconds: float):
    """
    Advance in-game time while switching between day/night real-seconds-per-in-game-minute.
    Returns (day_of_year, minute_of_day_int).
    """
    day = int(start_day)
    minute_of_day = float(start_minute_of_day)  # 0..1439
    remaining = float(elapsed_real_seconds)

    for _ in range(20000):
        if remaining <= 0:
            break

        current_min_int = int(minute_of_day) % 1440
        spm = seconds_per_minute_for(current_min_int)

        # Determine next boundary (sunrise/sunset)
        if is_day_by_minute(current_min_int):
            boundary_total = (day - 1) * 1440 + SUNSET_MIN
        else:
            # Night -> sunrise next (same day if before sunrise, else next day)
            if current_min_int < SUNRISE_MIN:
                boundary_total = (day - 1) * 1440 + SUNRISE_MIN
            else:
                boundary_total = day * 1440 + SUNRISE_MIN

        current_total = (day - 1) * 1440 + minute_of_day
        minutes_until_boundary = max(0.0, boundary_total - current_total)
        seconds_to_boundary = minutes_until_boundary * spm

        if seconds_to_boundary > 0 and remaining >= seconds_to_boundary:
            remaining -= seconds_to_boundary
            minute_of_day += minutes_until_boundary
        else:
            # Partial within current segment
            minute_of_day += (remaining / spm) if spm > 0 else 0
            remaining = 0

        # roll day
        while minute_of_day >= 1440:
            minute_of_day -= 1440
            day += 1

    return day, int(minute_of_day) % 1440

def calculate_time():
    """
    Returns (title, color, current_spm, year, day, hour, minute, is_day).
    Year rolls every 365 days.
    """
    if not state:
        return None

    elapsed_real = time.time() - float(state["real_epoch"])

    start_day = int(state["day"])
    start_year = int(state["year"])
    start_minute_of_day = int(state["hour"]) * 60 + int(state["minute"])

    day, minute_of_day = advance_minutes_piecewise(start_day, start_minute_of_day, elapsed_real)

    year = start_year
    while day > 365:
        day -= 365
        year += 1

    hour = minute_of_day // 60
    minute = minute_of_day % 60

    day_now = is_day_by_minute(minute_of_day)
    emoji = "☀️" if day_now else "🌙"
    color = DAY_COLOR if day_now else NIGHT_COLOR

    title = f"{emoji} | Solunaris Time | {hour:02d}:{minute:02d} | Day {day} | Year {year}"
    current_spm = DAY_SECONDS_PER_INGAME_MINUTE if day_now else NIGHT_SECONDS_PER_INGAME_MINUTE
    return title, color, current_spm, year, day, hour, minute, day_now

# =====================
# WEBHOOK (EDIT SAME MESSAGE)
# =====================
async def upsert_webhook_embed(session: aiohttp.ClientSession, embed: dict):
    """
    Edit the same webhook message each time; recreate if deleted/invalid.
    """
    global webhook_message_id

    # Try edit existing message
    if webhook_message_id:
        try:
            async with session.patch(
                f"{WEBHOOK_URL}/messages/{webhook_message_id}",
                json={"embeds": [embed]},
            ) as resp:
                if resp.status == 404:
                    webhook_message_id = None
                elif resp.status >= 400:
                    text = await resp.text()
                    print(f"[webhook] PATCH failed {resp.status}: {text}")
                    # keep id; might be rate limit etc.
        except Exception as e:
            print(f"[webhook] PATCH exception: {type(e).__name__}: {e}")

    # Create new message if needed
    if not webhook_message_id:
        try:
            async with session.post(
                WEBHOOK_URL + "?wait=true",
                json={"embeds": [embed]},
            ) as resp:
                data = await resp.json()
                if "id" in data:
                    webhook_message_id = data["id"]
                else:
                    print(f"[webhook] POST no id: {data}")
        except Exception as e:
            print(f"[webhook] POST exception: {type(e).__name__}: {e}")

# =====================
# A2S (STEAM QUERY) - ONLINE + PLAYERS
# =====================
A2S_HEADER = b"\xFF\xFF\xFF\xFF"

def _a2s_info_packet() -> bytes:
    # A2S_INFO: "TSource Engine Query\0"
    return A2S_HEADER + b"\x54" + b"Source Engine Query\x00"

def _parse_a2s_info(data: bytes):
    """
    Returns dict or raises.
    Format (simplified):
      - header(4) + type(1=0x49) + protocol + name + map + folder + game + id(2) + players + max + bots + ...
    """
    if not data.startswith(A2S_HEADER):
        raise ValueError("Bad A2S header")

    payload = data[4:]
    if len(payload) < 2 or payload[0] != 0x49:
        raise ValueError("Not A2S_INFO response")

    # helper to read null-terminated strings
    idx = 2  # payload[0]=0x49, payload[1]=protocol
    def read_cstr(i):
        end = payload.find(b"\x00", i)
        if end == -1:
            raise ValueError("Bad string")
        return payload[i:end].decode("utf-8", "replace"), end + 1

    name, idx = read_cstr(idx)
    _map, idx = read_cstr(idx)
    _folder, idx = read_cstr(idx)
    _game, idx = read_cstr(idx)

    if idx + 2 > len(payload):
        raise ValueError("Truncated (id)")
    idx += 2  # app id

    if idx + 3 > len(payload):
        raise ValueError("Truncated (players/max/bots)")
    players = payload[idx]
    max_players = payload[idx + 1]
    bots = payload[idx + 2]
    # done
    return {
        "name": name,
        "players": int(players),
        "max_players": int(max_players),
        "bots": int(bots),
    }

async def query_server_a2s(ip: str, port: int, timeout: float = 2.5):
    """
    UDP query to Steam A2S_INFO. Returns dict {online, players, max, note}
    """
    loop = asyncio.get_running_loop()
    packet = _a2s_info_packet()
    addr = (ip, port)

    def _do_query():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(timeout)
        try:
            sock.sendto(packet, addr)
            data, _ = sock.recvfrom(2048)
            info = _parse_a2s_info(data)
            return info
        finally:
            sock.close()

    try:
        info = await loop.run_in_executor(None, _do_query)
        return {
            "online": True,
            "players": info.get("players", 0),
            "max": info.get("max_players", PLAYER_CAP) or PLAYER_CAP,
            "note": "",
        }
    except Exception as e:
        return {
            "online": False,
            "players": None,
            "max": PLAYER_CAP,
            "note": f"(query failed: {type(e).__name__})",
        }

# =====================
# STATUS VC UPDATE LOOP
# =====================
_last_status_name = None
_last_status_forced = 0.0

def format_status_vc_name(online: bool, players: int | None, max_players: int):
    dot = "🟢" if online else "🔴"
    p = "?" if players is None else str(players)
    return f"{dot} Solunaris | {p}/{max_players}"

async def status_loop():
    """
    Checks every 15s.
    Only renames VC if changed, BUT forces an update every 10 minutes regardless.
    """
    global _last_status_name, _last_status_forced

    if not STATUS_VOICE_CHANNEL_ID:
        print("ℹ️ STATUS_VOICE_CHANNEL_ID not set; skipping status VC loop.")
        return

    await client.wait_until_ready()
    chan_id = int(STATUS_VOICE_CHANNEL_ID)

    while True:
        status = await query_server_a2s(SERVER_IP, QUERY_PORT, timeout=2.5)
        new_name = format_status_vc_name(status["online"], status["players"], status["max"])

        now = time.time()
        force = (now - _last_status_forced) >= (10 * 60)

        if force or (new_name != _last_status_name):
            try:
                ch = client.get_channel(chan_id) or await client.fetch_channel(chan_id)
                await ch.edit(name=new_name, reason="Solunaris server status update")
                _last_status_name = new_name
                _last_status_forced = now
            except Exception as e:
                print(f"[status_vc] rename failed: {type(e).__name__}: {e}")

        await asyncio.sleep(15)

# =====================
# TIME WEBHOOK LOOP + NEW DAY ANNOUNCE
# =====================
async def time_loop():
    """
    Edits the same webhook message each time.
    Sleep scales with current in-game minute length (day vs night).
    Also posts a message at the start of each new in-game day (optional channel).
    """
    global last_announced_day

    await client.wait_until_ready()

    async with aiohttp.ClientSession() as session:
        while True:
            if state:
                calc = calculate_time()
                if calc:
                    title, color, current_spm, year, day, hour, minute, day_now = calc

                    # Embed: "bigger/bolder" look by using description with markdown bold
                    embed = {
                        "title": "",
                        "description": f"**{title}**",
                        "color": color,
                    }

                    await upsert_webhook_embed(session, embed)

                    # Announce new day (optional)
                    if DAY_ANNOUNCE_CHANNEL_ID:
                        key = (year, day)
                        if last_announced_day != key and (hour == 0 and minute == 0):
                            try:
                                ch = client.get_channel(int(DAY_ANNOUNCE_CHANNEL_ID)) or await client.fetch_channel(int(DAY_ANNOUNCE_CHANNEL_ID))
                                await ch.send(f"📅 **A new day has begun!** Day **{day}**, Year **{year}**.")
                                last_announced_day = key
                            except Exception as e:
                                print(f"[day_announce] failed: {type(e).__name__}: {e}")

                    sleep_for = float(current_spm) if current_spm else DAY_SECONDS_PER_INGAME_MINUTE
                else:
                    sleep_for = DAY_SECONDS_PER_INGAME_MINUTE
            else:
                # no state yet
                sleep_for = DAY_SECONDS_PER_INGAME_MINUTE

            await asyncio.sleep(sleep_for)

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
        await interaction.followup.send("⏳ Time not set yet. Use /settime.", ephemeral=True)
        return

    calc = calculate_time()
    if not calc:
        await interaction.followup.send("⚠️ Could not calculate time (state missing).", ephemeral=True)
        return

    title, _, _, year, day, hour, minute, _, = calc
    await interaction.followup.send(f"**{title}**", ephemeral=True)

@tree.command(
    name="settime",
    description="Set Solunaris time (admin role only)",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(
    year="Year number (>=1)",
    day="Day of year (1–365)",
    hour="Hour (0–23)",
    minute="Minute (0–59)",
)
async def settime_cmd(interaction: discord.Interaction, year: int, day: int, hour: int, minute: int):
    # respond fast
    await interaction.response.defer(ephemeral=True)

    # Role gate
    try:
        has_role = any(getattr(r, "id", None) == ADMIN_ROLE_ID for r in getattr(interaction.user, "roles", []))
    except Exception:
        has_role = False

    if not has_role:
        await interaction.followup.send("❌ You must have the required admin role to use /settime.", ephemeral=True)
        return

    if year < 1 or day < 1 or day > 365 or hour < 0 or hour > 23 or minute < 0 or minute > 59:
        await interaction.followup.send("❌ Invalid values. Day 1–365, hour 0–23, minute 0–59.", ephemeral=True)
        return

    global state
    state = {
        "real_epoch": time.time(),
        "year": year,
        "day": day,
        "hour": hour,
        "minute": minute,
    }
    save_state(state)

    await interaction.followup.send(
        f"✅ Time set to **Day {day}**, **{hour:02d}:{minute:02d}**, **Year {year}**.",
        ephemeral=True,
    )

@tree.command(
    name="status",
    description="Show Solunaris server status and players online",
    guild=discord.Object(id=GUILD_ID),
)
async def status_cmd(interaction: discord.Interaction):
    # ✅ MUST defer immediately (prevents 'application did not respond' / 404)
    await interaction.response.defer(ephemeral=True)

    status = await query_server_a2s(SERVER_IP, QUERY_PORT, timeout=2.5)
    online = status["online"]
    players = status["players"]
    maxp = status["max"]

    if online:
        msg = f"🟢 **Solunaris is ONLINE** — Players: **{players}/{maxp}**"
    else:
        msg = f"🔴 **Solunaris is OFFLINE** — Players: **?/{maxp}**"

    note = status.get("note")
    if note:
        msg += f"\n{note}"

    await interaction.followup.send(msg, ephemeral=True)

# =====================
# STARTUP
# =====================
@client.event
async def on_ready():
    try:
        await tree.sync(guild=discord.Object(id=GUILD_ID))
        print("✅ Guild commands synced: /day /settime /status")
    except Exception as e:
        print(f"⚠️ Command sync failed: {type(e).__name__}: {e}")

    print(f"✅ Logged in as {client.user} (guild={GUILD_ID})")
    client.loop.create_task(time_loop())
    client.loop.create_task(status_loop())

client.run(DISCORD_TOKEN)