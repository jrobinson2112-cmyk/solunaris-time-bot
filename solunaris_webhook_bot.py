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

GUILD_ID = int(os.getenv("GUILD_ID", "1430388266393276509"))
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "1439069787207766076"))

DAY_ANNOUNCE_CHANNEL_ID = os.getenv("DAY_ANNOUNCE_CHANNEL_ID")  # optional (text channel)
STATUS_VOICE_CHANNEL_ID = os.getenv("STATUS_VOICE_CHANNEL_ID")  # required for status VC

# RCON
RCON_HOST = os.getenv("RCON_HOST", "31.214.239.2")
RCON_PORT = int(os.getenv("RCON_PORT", "11020"))
RCON_PASSWORD = os.getenv("RCON_PASSWORD", "").strip()

# A2S fallback (often blocked for ASA; kept as last resort)
A2S_HOST = os.getenv("A2S_HOST", "31.214.239.2")
A2S_PORT = int(os.getenv("A2S_PORT", "5021"))

PLAYER_CAP = int(os.getenv("PLAYER_CAP", "42"))

# =====================
# TIME CONFIG
# =====================
DAY_SECONDS_PER_INGAME_MINUTE = 4.7405
NIGHT_SECONDS_PER_INGAME_MINUTE = 3.98

SUNRISE_MIN = 5 * 60 + 30
SUNSET_MIN  = 17 * 60 + 30

DAY_COLOR = 0xF1C40F
NIGHT_COLOR = 0x5865F2

STATE_FILE = "state.json"

# =====================
# STATUS POLICY
# =====================
STATUS_POLL_SECONDS = float(os.getenv("STATUS_POLL_SECONDS", "15"))
STATUS_FORCE_UPDATE_SECONDS = float(os.getenv("STATUS_FORCE_UPDATE_SECONDS", "600"))
STATUS_MIN_SECONDS_BETWEEN_EDITS = float(os.getenv("STATUS_MIN_SECONDS_BETWEEN_EDITS", "120"))

# =====================
# VALIDATION
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
# TIME MATH
# =====================
def is_day_by_minute(minute_of_day: int) -> bool:
    return SUNRISE_MIN <= minute_of_day < SUNSET_MIN

def seconds_per_minute_for(minute_of_day: int) -> float:
    return DAY_SECONDS_PER_INGAME_MINUTE if is_day_by_minute(minute_of_day) else NIGHT_SECONDS_PER_INGAME_MINUTE

def advance_minutes_piecewise(start_day: int, start_minute_of_day: int, elapsed_real_seconds: float):
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
# RCON (robust status)
# =====================
SERVERDATA_AUTH = 3
SERVERDATA_EXECCOMMAND = 2

def _rcon_packet(packet_id: int, ptype: int, body: str) -> bytes:
    body_bytes = body.encode("utf-8") + b"\x00"
    payload = struct.pack("<ii", packet_id, ptype) + body_bytes + b"\x00"
    return struct.pack("<i", len(payload)) + payload

def _rcon_read(sock: socket.socket):
    raw_len = sock.recv(4)
    if len(raw_len) < 4:
        return None
    (length,) = struct.unpack("<i", raw_len)
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            break
        data += chunk
    if len(data) < 8:
        return None
    packet_id, ptype = struct.unpack("<ii", data[:8])
    body = data[8:-2].decode("utf-8", errors="replace")
    return packet_id, ptype, body

async def rcon_status(host: str, port: int, password: str, timeout: float = 3.0):
    """
    Returns a dict:
      {
        "reachable": bool,   # TCP connect worked
        "authed": bool,      # RCON auth ok
        "players": int|None  # None if unknown
      }
    """
    loop = asyncio.get_running_loop()

    def _do():
        result = {"reachable": False, "authed": False, "players": None}
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            s.connect((host, port))
            result["reachable"] = True

            # AUTH
            if not password:
                s.close()
                return result

            s.sendall(_rcon_packet(1, SERVERDATA_AUTH, password))

            # Servers sometimes send an empty response first; read a couple
            resp = _rcon_read(s)
            if resp and resp[0] == -1:
                s.close()
                return result  # auth failed

            # If not auth reply, read one more
            if resp and resp[1] != SERVERDATA_AUTH:
                resp2 = _rcon_read(s)
                if resp2 and resp2[0] == -1:
                    s.close()
                    return result
                resp = resp2

            if not resp:
                s.close()
                return result

            if resp[0] == -1:
                s.close()
                return result

            result["authed"] = True

            # EXEC ListPlayers
            s.sendall(_rcon_packet(2, SERVERDATA_EXECCOMMAND, "ListPlayers"))

            bodies = []
            s.settimeout(0.6)
            for _ in range(6):
                r = _rcon_read(s)
                if not r:
                    break
                bodies.append(r[2])

            s.close()

            text = "\n".join(bodies).strip()
            if not text:
                result["players"] = 0
                return result

            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

            # Very simple heuristic: count non-header lines
            filtered = []
            for ln in lines:
                low = ln.lower()
                if low.startswith("players") or low.startswith("there are") or low.startswith("id") or low.startswith("name"):
                    continue
                filtered.append(ln)

            count = len(filtered) if filtered else len(lines)
            result["players"] = max(0, min(count, PLAYER_CAP))
            return result

        except Exception:
            return result

    return await loop.run_in_executor(None, _do)

# =====================
# STATUS SOURCE
# =====================
async def get_server_status():
    """
    Prefer RCON:
      - reachable+authed => ONLINE with players
      - reachable but not authed => ONLINE but players unknown
      - not reachable => OFFLINE
    """
    rs = await rcon_status(RCON_HOST, RCON_PORT, RCON_PASSWORD)

    if rs["reachable"]:
        if rs["authed"] and rs["players"] is not None:
            return "online", rs["players"]
        # reachable but auth failed or no password => online, unknown players
        return "unknown", None

    return "offline", None

# =====================
# LOOPS
# =====================
async def update_time_loop():
    global webhook_message_id, state
    await client.wait_until_ready()

    async with aiohttp.ClientSession() as session:
        while True:
            if not state:
                await asyncio.sleep(5)
                continue

            title, color, current_spm, day_num, year_num = calculate_time()
            embed = {"title": title, "color": color}

            if DAY_ANNOUNCE_CHANNEL_ID:
                current_key = f"{year_num}-{day_num}"
                last_key = state.get("last_announced_day_key")
                if last_key is None:
                    state["last_announced_day_key"] = current_key
                    save_state(state)
                elif last_key != current_key:
                    try:
                        ch = client.get_channel(int(DAY_ANNOUNCE_CHANNEL_ID)) or await client.fetch_channel(int(DAY_ANNOUNCE_CHANNEL_ID))
                        await ch.send(f"🌅 **A new day begins in Solunaris!** Day **{day_num}** | Year **{year_num}**")
                    except Exception as e:
                        print(f"[announce] {e}", flush=True)
                    state["last_announced_day_key"] = current_key
                    save_state(state)

            try:
                if webhook_message_id:
                    await session.patch(f"{WEBHOOK_URL}/messages/{webhook_message_id}", json={"embeds": [embed]})
                else:
                    async with session.post(WEBHOOK_URL + "?wait=true", json={"embeds": [embed]}) as resp:
                        data = await resp.json()
                        webhook_message_id = data.get("id")
            except Exception as e:
                print(f"[webhook] {e}", flush=True)
                webhook_message_id = None

            await asyncio.sleep(float(current_spm) if current_spm else 5)

async def update_status_vc_loop():
    await client.wait_until_ready()

    if not STATUS_VOICE_CHANNEL_ID:
        print("⚠️ STATUS_VOICE_CHANNEL_ID not set; skipping status VC loop.")
        return
    channel_id = int(STATUS_VOICE_CHANNEL_ID)

    last_target_name = None
    last_edit_ts = 0.0

    while True:
        try:
            status, players = await get_server_status()

            if status == "online":
                target_name = f"🟢 Solunaris | {players}/{PLAYER_CAP}"
            elif status == "unknown":
                # Server reachable but can't read players (bad password / not enabled)
                target_name = f"🟡 Solunaris | ?/{PLAYER_CAP}"
            else:
                target_name = f"🔴 Solunaris | 0/{PLAYER_CAP}"

            now = time.time()
            changed = (target_name != last_target_name)
            force_due = (now - last_edit_ts) >= STATUS_FORCE_UPDATE_SECONDS
            can_edit = (now - last_edit_ts) >= STATUS_MIN_SECONDS_BETWEEN_EDITS

            if can_edit and (changed or force_due):
                ch = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
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

    status, players = await get_server_status()
    if status == "online":
        msg = f"🟢 **Solunaris is ONLINE** — Players: **{players}/{PLAYER_CAP}**"
    elif status == "unknown":
        msg = f"🟡 **Solunaris is ONLINE** — Players: **?/{PLAYER_CAP}** (RCON auth not working)"
    else:
        msg = f"🔴 **Solunaris is OFFLINE** — Players: **0/{PLAYER_CAP}**"

    await interaction.followup.send(msg, ephemeral=True)

@tree.command(name="settime", description="Set Solunaris time (admin role only)", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(year="Year (>=1)", day="Day (1–365)", hour="Hour (0–23)", minute="Minute (0–59)")
async def settime_cmd(interaction: discord.Interaction, year: int, day: int, hour: int, minute: int):
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
        "last_announced_day_key": f"{year}-{day}",
    }
    save_state(state)
    await interaction.response.send_message(f"✅ Set to **Day {day}**, **{hour:02d}:{minute:02d}**, **Year {year}**.", ephemeral=True)

# =====================
# STARTUP
# =====================
@client.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print("✅ Slash commands synced to guild")
    print(f"✅ Logged in as {client.user}")
    if not RCON_PASSWORD:
        print("⚠️ RCON_PASSWORD not set. Status will show 🟡 (online unknown) if reachable, or 🔴 if unreachable.", flush=True)

    client.loop.create_task(update_time_loop())
    client.loop.create_task(update_status_vc_loop())

client.run(DISCORD_TOKEN)