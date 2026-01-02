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

GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076
PLAYER_CAP = 42

STATUS_VC_ID = int(os.getenv("STATUS_VC_ID", "0"))  # 0 disables

RCON_HOST = os.getenv("RCON_HOST", "31.214.239.2")
RCON_PORT = int(os.getenv("RCON_PORT", "11020"))
RCON_PASSWORD = os.getenv("RCON_PASSWORD", "")

# Status polling
STATUS_POLL_SECONDS = 15
STATUS_FORCE_SECONDS = 10 * 60
RCON_TIMEOUT = 2.0  # keep short so commands never hang

# Smooth day/night minute lengths (real seconds per in-game minute)
DAY_SECONDS_PER_INGAME_MINUTE = 4.7666667
NIGHT_SECONDS_PER_INGAME_MINUTE = 4.045

STATE_FILE = "state.json"

# Day is 05:30 -> 17:30, Night is 17:30 -> 05:30
SUNRISE_MIN = 5 * 60 + 30
SUNSET_MIN  = 17 * 60 + 30

DAY_COLOR = 0xF1C40F    # Yellow
NIGHT_COLOR = 0x5865F2  # Blue

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
# TIME CALCULATION
# =====================
def is_day_by_minute(minute_of_day: int) -> bool:
    return SUNRISE_MIN <= minute_of_day < SUNSET_MIN

def seconds_per_minute_for(minute_of_day: int) -> float:
    return DAY_SECONDS_PER_INGAME_MINUTE if is_day_by_minute(minute_of_day) else NIGHT_SECONDS_PER_INGAME_MINUTE

def advance_minutes_piecewise(start_day: int, start_minute_of_day: int, elapsed_real_seconds: float):
    day = int(start_day)
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
                boundary_total = (day) * 1440 + SUNRISE_MIN

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
    if not state:
        return None

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
    return title, color, current_spm

# =====================
# WEBHOOK LOOP (edits same message)
# =====================
async def update_time_webhook_loop():
    global webhook_message_id
    await client.wait_until_ready()

    async with aiohttp.ClientSession() as session:
        while True:
            if state:
                result = calculate_time()
                if result:
                    title, color, current_spm = result
                    embed = {"color": color, "description": f"**{title}**"}

                    try:
                        if webhook_message_id:
                            await session.patch(
                                f"{WEBHOOK_URL}/messages/{webhook_message_id}",
                                json={"embeds": [embed]},
                            )
                        else:
                            async with session.post(WEBHOOK_URL + "?wait=true", json={"embeds": [embed]}) as resp:
                                data = await resp.json()
                                webhook_message_id = data.get("id")
                    except Exception as e:
                        webhook_message_id = None
                        print(f"[webhook] error, will recreate: {e}")

                    await asyncio.sleep(float(current_spm))
                    continue

            await asyncio.sleep(DAY_SECONDS_PER_INGAME_MINUTE)

# =====================
# SOURCE RCON (pure python, no library)
# =====================
SERVERDATA_AUTH = 3
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_RESPONSE_VALUE = 0

def _pack_rcon_packet(req_id: int, ptype: int, body: str) -> bytes:
    b = body.encode("utf-8") + b"\x00"
    packet = struct.pack("<ii", req_id, ptype) + b + b"\x00"
    return struct.pack("<i", len(packet)) + packet

def _recv_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("socket closed")
        data += chunk
    return data

def _recv_rcon_packet(sock: socket.socket):
    size = struct.unpack("<i", _recv_exact(sock, 4))[0]
    payload = _recv_exact(sock, size)
    req_id, ptype = struct.unpack("<ii", payload[:8])
    body = payload[8:-2].decode("utf-8", "ignore")  # strip two nulls
    return req_id, ptype, body

def _rcon_listplayers(host: str, port: int, password: str, timeout: float):
    """
    Returns dict: {"online": bool, "players": int|None, "error": str|None}
    Uses Source RCON ListPlayers command.
    """
    if not password:
        return {"online": False, "players": None, "error": "RCON_PASSWORD not set"}

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)

    try:
        sock.connect((host, port))

        req_id = 1234
        # AUTH
        sock.sendall(_pack_rcon_packet(req_id, SERVERDATA_AUTH, password))
        # server sends two packets, read until auth response
        authed = False
        for _ in range(3):
            rid, ptype, body = _recv_rcon_packet(sock)
            if ptype == SERVERDATA_AUTH_RESPONSE and rid == req_id:
                authed = True
                break
        if not authed:
            return {"online": False, "players": None, "error": "RCON auth failed"}

        # EXEC ListPlayers
        req_id = 5678
        sock.sendall(_pack_rcon_packet(req_id, SERVERDATA_EXECCOMMAND, "ListPlayers"))

        # Responses may come in multiple packets; read a few quickly
        out = ""
        end_time = time.time() + timeout
        while time.time() < end_time:
            try:
                rid, ptype, body = _recv_rcon_packet(sock)
                if rid != req_id:
                    continue
                out += body
                # heuristic: if response ends with newline, we likely got it all
                if out.endswith("\n") or len(out) > 5000:
    break
            except socket.timeout:
                break

        # Parse player count (ARK ListPlayers lists one player per line usually)
        # Count lines that contain an ID pattern: "PlayerName, <something>" varies by host.
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        # Filter out obvious headers
        player_lines = [ln for ln in lines if not ln.lower().startswith("players")]

        players = len(player_lines) if player_lines else 0
        return {"online": True, "players": players, "error": None}

    except socket.timeout:
        return {"online": False, "players": None, "error": "TimeoutError"}
    except Exception as e:
        return {"online": False, "players": None, "error": str(e)}
    finally:
        try:
            sock.close()
        except Exception:
            pass

async def get_server_status():
    return await asyncio.to_thread(_rcon_listplayers, RCON_HOST, RCON_PORT, RCON_PASSWORD, RCON_TIMEOUT)

def format_status_text(st: dict):
    online = st.get("online", False)
    players = st.get("players", None)
    dot = "🟢" if online else "🔴"
    if players is None:
        return f"{dot} Solunaris | Players: ?/{PLAYER_CAP}"
    return f"{dot} Solunaris | Players: {players}/{PLAYER_CAP}"

# =====================
# STATUS VC LOOP
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
                ch = client.get_channel(STATUS_VC_ID) or await client.fetch_channel(STATUS_VC_ID)
                await ch.edit(name=new_name)
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
    await interaction.response.defer(ephemeral=True)

    if not state:
        await interaction.followup.send("⏳ Time not set yet.", ephemeral=True)
        return

    result = calculate_time()
    await interaction.followup.send(result[0] if result else "⏳ Time not set yet.", ephemeral=True)


@tree.command(
    name="settime",
    description="Set Solunaris time",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(year="Year", day="Day (1-365)", hour="Hour (0-23)", minute="Minute (0-59)")
async def settime_cmd(interaction: discord.Interaction, year: int, day: int, hour: int, minute: int):
    await interaction.response.defer(ephemeral=True)

    if not any(getattr(r, "id", None) == ADMIN_ROLE_ID for r in getattr(interaction.user, "roles", [])):
        await interaction.followup.send("❌ You don't have the required role to use /settime.", ephemeral=True)
        return

    if year < 1 or not (1 <= day <= 365) or not (0 <= hour <= 23) or not (0 <= minute <= 59):
        await interaction.followup.send("❌ Invalid values.", ephemeral=True)
        return

    global state
    state = {"real_epoch": time.time(), "year": year, "day": day, "hour": hour, "minute": minute}
    save_state(state)

    await interaction.followup.send(f"✅ Set to Day {day}, {hour:02d}:{minute:02d}, Year {year}", ephemeral=True)


@tree.command(
    name="status",
    description="Show Solunaris server status + players",
    guild=discord.Object(id=GUILD_ID),
)
async def status_cmd(interaction: discord.Interaction):
    # Always ack instantly so Discord never times out
    await interaction.response.defer(ephemeral=True)

    st = await get_server_status()
    msg = format_status_text(st)

    if not st.get("online", False) and st.get("error"):
        msg += f"\n(rcon failed: {st['error']})"

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