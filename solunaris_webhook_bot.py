import os
import time
import json
import asyncio
import aiohttp
import socket
import discord
from discord import app_commands

# =====================
# ENV / CONFIG
# =====================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

GUILD_ID = int(os.getenv("GUILD_ID", "1430388266393276509"))
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "1439069787207766076"))

# Optional channels
DAY_ANNOUNCE_CHANNEL_ID = os.getenv("DAY_ANNOUNCE_CHANNEL_ID")  # optional (text channel)
STATUS_VOICE_CHANNEL_ID = os.getenv("STATUS_VOICE_CHANNEL_ID")  # required for status VC

# A2S (Steam query) - your server
A2S_HOST = os.getenv("A2S_HOST", "31.214.239.2")
A2S_PORT = int(os.getenv("A2S_PORT", "5021"))  # Nitrado usually: game port + 1

# Player cap (forced)
PLAYER_CAP = int(os.getenv("PLAYER_CAP", "42"))

# =====================
# TIME / DAY-NIGHT CONFIG
# =====================
# Updated from your longer measurements:
DAY_SECONDS_PER_INGAME_MINUTE = 4.7405
NIGHT_SECONDS_PER_INGAME_MINUTE = 3.98

# Day: 05:30 -> 17:30, Night: 17:30 -> 05:30
SUNRISE_MIN = 5 * 60 + 30   # 05:30
SUNSET_MIN  = 17 * 60 + 30  # 17:30

DAY_COLOR = 0xF1C40F    # Yellow
NIGHT_COLOR = 0x5865F2  # Blue

STATE_FILE = "state.json"

# =====================
# STATUS VC UPDATE POLICY (your new rules)
# =====================
# Check for changes every 15s (A2S query only; no Discord edit unless needed)
STATUS_POLL_SECONDS = float(os.getenv("STATUS_POLL_SECONDS", "15"))

# If changed, update immediately (edit VC name)
# Even if not changed, force an update every 10 minutes
STATUS_FORCE_UPDATE_SECONDS = float(os.getenv("STATUS_FORCE_UPDATE_SECONDS", "600"))

# Safety minimum between VC edits to reduce 429 risk (edits are the expensive part)
STATUS_MIN_SECONDS_BETWEEN_EDITS = float(os.getenv("STATUS_MIN_SECONDS_BETWEEN_EDITS", "120"))

# =====================
# BASIC VALIDATION
# =====================
if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")
if not WEBHOOK_URL:
    raise RuntimeError("Missing WEBHOOK_URL")

# =====================
# DISCORD SETUP
# =====================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# =====================
# STATE STORAGE
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
# TIME CALCULATION
# =====================
def is_day_by_minute(minute_of_day: int) -> bool:
    return SUNRISE_MIN <= minute_of_day < SUNSET_MIN

def seconds_per_minute_for(minute_of_day: int) -> float:
    return DAY_SECONDS_PER_INGAME_MINUTE if is_day_by_minute(minute_of_day) else NIGHT_SECONDS_PER_INGAME_MINUTE

def advance_minutes_piecewise(start_day: int, start_minute_of_day: int, elapsed_real_seconds: float):
    """
    Smoothly integrates time across day/night with different seconds-per-in-game-minute.
    Returns (day, minute_of_day_int).
    """
    day = start_day
    minute_of_day = float(start_minute_of_day)
    remaining = float(elapsed_real_seconds)

    for _ in range(20000):
        if remaining <= 0:
            break

        current_minute_int = int(minute_of_day) % 1440
        spm = seconds_per_minute_for(current_minute_int)

        if is_day_by_minute(current_minute_int):
            boundary_total = (day - 1) * 1440 + SUNSET_MIN
        else:
            if current_minute_int < SUNRISE_MIN:
                boundary_total = (day - 1) * 1440 + SUNRISE_MIN
            else:
                boundary_total = day * 1440 + SUNRISE_MIN

        current_total = (day - 1) * 1440 + minute_of_day
        minutes_until_boundary = boundary_total - current_total
        if minutes_until_boundary < 0:
            minutes_until_boundary = 0

        seconds_to_boundary = minutes_until_boundary * spm

        if remaining >= seconds_to_boundary and seconds_to_boundary > 0:
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
    Returns:
      (title, color, current_spm, day_num, year_num)
    Year rolls every 365 days.
    """
    if not state:
        return None, None, None, None, None

    elapsed_real = time.time() - state["real_epoch"]

    start_day = int(state["day"])
    start_year = int(state["year"])
    start_minute_of_day = int(state["hour"]) * 60 + int(state["minute"])

    day_num, minute_of_day = advance_minutes_piecewise(start_day, start_minute_of_day, elapsed_real)

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
    return title, color, float(current_spm), int(day_num), int(year_num)

# =====================
# A2S QUERY (challenge-aware)
# =====================
A2S_INFO_REQUEST = b"\xFF\xFF\xFF\xFFTSource Engine Query\x00"

async def a2s_query_info(host: str, port: int, timeout: float = 2.0):
    """
    Returns (online: bool, players: int, max_players: int)
    Handles A2S challenge response (0x41).
    """
    loop = asyncio.get_running_loop()

    def _udp_exchange(payload: bytes) -> bytes:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        try:
            s.sendto(payload, (host, port))
            data, _ = s.recvfrom(4096)
            return data
        finally:
            s.close()

    def _read_cstring(data: bytes, idx: int):
        end = data.find(b"\x00", idx)
        if end == -1:
            return "", idx
        return data[idx:end].decode("utf-8", errors="replace"), end + 1

    try:
        data = await loop.run_in_executor(None, _udp_exchange, A2S_INFO_REQUEST)

        # Challenge?
        if len(data) >= 9 and data[:4] == b"\xFF\xFF\xFF\xFF" and data[4] == 0x41:
            challenge = data[5:9]
            data = await loop.run_in_executor(None, _udp_exchange, A2S_INFO_REQUEST + challenge)

        # Expect INFO response
        if len(data) < 6 or data[:4] != b"\xFF\xFF\xFF\xFF" or data[4] != 0x49:
            return False, 0, 0

        idx = 5
        idx += 1  # protocol

        # name, map, folder, game
        _, idx = _read_cstring(data, idx)
        _, idx = _read_cstring(data, idx)
        _, idx = _read_cstring(data, idx)
        _, idx = _read_cstring(data, idx)

        # app id
        if idx + 2 > len(data):
            return False, 0, 0
        idx += 2

        # players, max players, bots
        if idx + 3 > len(data):
            return False, 0, 0
        players = data[idx]
        max_players = data[idx + 1]

        return True, int(players), int(max_players)

    except Exception:
        return False, 0, 0

# =====================
# LOOPS
# =====================
async def update_time_loop():
    """Edits one webhook message; optionally posts a message at each new day."""
    global webhook_message_id, state
    await client.wait_until_ready()

    async with aiohttp.ClientSession() as session:
        while True:
            if not state:
                await asyncio.sleep(5)
                continue

            title, color, current_spm, day_num, year_num = calculate_time()
            embed = {"title": title, "color": color}

            # Day rollover announcement (optional)
            if DAY_ANNOUNCE_CHANNEL_ID:
                current_key = f"{year_num}-{day_num}"
                last_key = state.get("last_announced_day_key")

                if last_key is None:
                    state["last_announced_day_key"] = current_key
                    save_state(state)
                elif last_key != current_key:
                    try:
                        ch = client.get_channel(int(DAY_ANNOUNCE_CHANNEL_ID))
                        if ch is None:
                            ch = await client.fetch_channel(int(DAY_ANNOUNCE_CHANNEL_ID))
                        await ch.send(f"🌅 **A new day begins in Solunaris!** Day **{day_num}** | Year **{year_num}**")
                    except Exception as e:
                        print(f"[announce] {e}", flush=True)

                    state["last_announced_day_key"] = current_key
                    save_state(state)

            # Webhook edit / create once
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
                print(f"[webhook] {e}", flush=True)
                webhook_message_id = None

            # Keep your fast day/night ticking for the TIME embed
            await asyncio.sleep(float(current_spm) if current_spm else 5)

async def update_status_vc_loop():
    """
    Checks for status changes every 15s, but only edits the VC name if:
      - status/name changed, OR
      - 10 minutes have passed since last edit (forced refresh)
    Also enforces a minimum delay between edits to reduce 429 risk.
    """
    await client.wait_until_ready()

    if not STATUS_VOICE_CHANNEL_ID:
        print("⚠️ STATUS_VOICE_CHANNEL_ID not set; skipping status VC loop.")
        return

    channel_id = int(STATUS_VOICE_CHANNEL_ID)

    last_target_name = None
    last_edit_ts = 0.0

    while True:
        try:
            online, players, _max_players = await a2s_query_info(A2S_HOST, A2S_PORT, timeout=2.0)

            if online:
                target_name = f"🟢 Solunaris | {players}/{PLAYER_CAP}"
            else:
                target_name = f"🔴 Solunaris | 0/{PLAYER_CAP}"

            now = time.time()
            changed = (target_name != last_target_name)
            force_due = (now - last_edit_ts) >= STATUS_FORCE_UPDATE_SECONDS
            can_edit = (now - last_edit_ts) >= STATUS_MIN_SECONDS_BETWEEN_EDITS

            # Only edit if something changed OR we are due for forced refresh,
            # and we haven't edited too recently.
            if can_edit and (changed or force_due):
                ch = client.get_channel(channel_id)
                if ch is None:
                    ch = await client.fetch_channel(channel_id)

                # Avoid pointless API calls: if Discord already has the right name and we're not forcing,
                # skip edit. If we're forcing, we still do it.
                if force_due or getattr(ch, "name", None) != target_name:
                    await ch.edit(name=target_name, reason="Solunaris server status update")

                last_target_name = target_name
                last_edit_ts = now

        except discord.Forbidden:
            print("❌ Missing permission: Manage Channels (for the status VC).", flush=True)
        except Exception as e:
            print(f"[status_vc] {e}", flush=True)

        await asyncio.sleep(STATUS_POLL_SECONDS)

# =====================
# SLASH COMMANDS
# =====================
@tree.command(name="day", description="Show current Solunaris time", guild=discord.Object(id=GUILD_ID))
async def day_cmd(interaction: discord.Interaction):
    if not state:
        await interaction.response.send_message("⏳ Time not set yet.", ephemeral=True)
        return
    title, _, _, _, _ = calculate_time()
    await interaction.response.send_message(title, ephemeral=True)

@tree.command(name="status", description="Show Solunaris server status and players", guild=discord.Object(id=GUILD_ID))
async def status_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    online, players, _max_players = await a2s_query_info(A2S_HOST, A2S_PORT, timeout=2.0)
    if online:
        msg = f"🟢 **Solunaris is ONLINE** — Players: **{players}/{PLAYER_CAP}**"
    else:
        msg = f"🔴 **Solunaris is OFFLINE** — Players: **0/{PLAYER_CAP}**"

    await interaction.followup.send(msg, ephemeral=True)

@tree.command(name="settime", description="Set Solunaris time (admin role only)", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(year="Year (>=1)", day="Day (1–365)", hour="Hour (0–23)", minute="Minute (0–59)")
async def settime_cmd(interaction: discord.Interaction, year: int, day: int, hour: int, minute: int):
    # Role-gated
    if not getattr(interaction.user, "roles", None) or not any(r.id == ADMIN_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message("❌ You must have the required admin role to use /settime.", ephemeral=True)
        return

    if year < 1 or day < 1 or day > 365 or hour < 0 or hour > 23 or minute < 0 or minute > 59:
        await interaction.response.send_message("❌ Invalid values.", ephemeral=True)
        return

    global state
    state = {
        "real_epoch": time.time(),
        "year": year,
        "day": day,
        "hour": hour,
        "minute": minute,
        "last_announced_day_key": f"{year}-{day}",  # prevents immediate "new day" post
    }
    save_state(state)

    await interaction.response.send_message(
        f"✅ Set to **Day {day}**, **{hour:02d}:{minute:02d}**, **Year {year}**.",
        ephemeral=True,
    )

# =====================
# STARTUP
# =====================
@client.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print("✅ Slash commands synced to guild")
    print(f"✅ Logged in as {client.user}")

    client.loop.create_task(update_time_loop())
    client.loop.create_task(update_status_vc_loop())

client.run(DISCORD_TOKEN)