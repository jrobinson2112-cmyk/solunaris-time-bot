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
# CONFIG (keep your vars)
# =====================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

RCON_HOST = os.getenv("RCON_HOST")  # e.g. 31.214.239.2
RCON_PORT = int(os.getenv("RCON_PORT", "11020"))
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

PLAYER_CAP = 42

# Time calibration
DAY_SPM = 4.7666667
NIGHT_SPM = 4.045

# Day: 05:30–17:30, Night: 17:30–05:30
SUNRISE = 5 * 60 + 30
SUNSET = 17 * 60 + 30

DAY_COLOR = 0xF1C40F
NIGHT_COLOR = 0x5865F2

STATE_FILE = "state.json"

# Put your status VC ID here
STATUS_VC_ID = int(os.getenv("STATUS_VC_ID", "0"))

if not DISCORD_TOKEN or not WEBHOOK_URL:
    raise RuntimeError("Missing DISCORD_TOKEN or WEBHOOK_URL")
if not RCON_HOST or not RCON_PASSWORD:
    raise RuntimeError("Missing RCON_HOST or RCON_PASSWORD")
if STATUS_VC_ID == 0:
    print("⚠️ STATUS_VC_ID not set; status VC renaming will be skipped.")

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
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return None

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f)

state = load_state()
webhook_msg_id = None

# =====================
# TIME LOGIC
# =====================
def is_day(minute_of_day: int) -> bool:
    return SUNRISE <= minute_of_day < SUNSET

def spm(minute_of_day: int) -> float:
    return DAY_SPM if is_day(minute_of_day) else NIGHT_SPM

def advance_time(start_day: int, start_minute: int, elapsed_real_seconds: float):
    # simple piecewise integration minute-by-minute fractionally
    d = int(start_day)
    m = float(start_minute)
    remaining = float(elapsed_real_seconds)

    # loop in small chunks, but efficiently (jump by boundary)
    for _ in range(20000):
        if remaining <= 0:
            break

        minute_int = int(m) % 1440
        current_spm = spm(minute_int)

        # determine next boundary minute
        if is_day(minute_int):
            boundary_total = (d - 1) * 1440 + SUNSET
        else:
            if minute_int < SUNRISE:
                boundary_total = (d - 1) * 1440 + SUNRISE
            else:
                boundary_total = d * 1440 + SUNRISE  # next day sunrise

        current_total = (d - 1) * 1440 + m
        minutes_to_boundary = boundary_total - current_total
        if minutes_to_boundary < 0:
            minutes_to_boundary = 0

        seconds_to_boundary = minutes_to_boundary * current_spm

        if seconds_to_boundary > 0 and remaining >= seconds_to_boundary:
            remaining -= seconds_to_boundary
            m += minutes_to_boundary
        else:
            m += remaining / current_spm if current_spm > 0 else 0
            remaining = 0

        while m >= 1440:
            m -= 1440
            d += 1

    return d, int(m) % 1440

def current_solunaris():
    if not state:
        return None, None, None, None, None

    elapsed = time.time() - state["epoch"]
    start_day = int(state["day"])
    start_year = int(state["year"])
    start_minute = int(state["minute"])

    d, minute_of_day = advance_time(start_day, start_minute, elapsed)

    y = start_year
    while d > 365:
        d -= 365
        y += 1

    hh = minute_of_day // 60
    mm = minute_of_day % 60
    day_now = is_day(minute_of_day)
    emoji = "☀️" if day_now else "🌙"
    color = DAY_COLOR if day_now else NIGHT_COLOR
    cur_spm = spm(minute_of_day)

    title = f"{emoji} | **Solunaris Time** | **{hh:02d}:{mm:02d}** | Day {d} | Year {y}"
    return title, color, cur_spm, d, y

# =====================
# RCON (NO EXTERNAL LIB)
# Source RCON protocol: https://developer.valvesoftware.com/wiki/Source_RCON_Protocol
# =====================
SERVERDATA_AUTH = 3
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_RESPONSE_VALUE = 0

def _pkt(req_id: int, typ: int, body: str) -> bytes:
    b = body.encode("utf-8") + b"\x00"
    return struct.pack("<ii", req_id, typ) + b + b"\x00"

def _read_packet(sock: socket.socket):
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
    req_id, typ = struct.unpack("<ii", data[:8])
    body = data[8:-2].decode("utf-8", errors="ignore")  # strip 2 nulls
    return req_id, typ, body

def rcon_command(host: str, port: int, password: str, command: str, timeout: float = 3.0) -> str:
    req_id = int(time.time()) & 0x7FFFFFFF

    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)

        # AUTH
        auth = _pkt(req_id, SERVERDATA_AUTH, password)
        s.sendall(struct.pack("<i", len(auth)) + auth)

        # read auth response(s)
        authed = False
        for _ in range(3):
            pkt = _read_packet(s)
            if not pkt:
                continue
            rid, typ, body = pkt
            if typ == SERVERDATA_RESPONSE_VALUE or typ == SERVERDATA_AUTH:
                if rid == -1:
                    raise PermissionError("RCON auth failed")
                if rid == req_id:
                    authed = True
                    break
        if not authed:
            raise TimeoutError("RCON auth timeout")

        # COMMAND
        cmd_id = req_id + 1
        payload = _pkt(cmd_id, SERVERDATA_EXECCOMMAND, command)
        s.sendall(struct.pack("<i", len(payload)) + payload)

        # responses can be split; read until we get at least one response
        out = []
        for _ in range(10):
            pkt = _read_packet(s)
            if not pkt:
                break
            rid, typ, body = pkt
            if rid == cmd_id and typ in (SERVERDATA_RESPONSE_VALUE,):
                out.append(body)
                # heuristic: if empty, might be terminator; otherwise continue once
                if body == "":
                    break
        return "".join(out).strip()

def get_server_status():
    """
    ONLINE if RCON reachable.
    Players from ListPlayers (ARK).
    """
    try:
        resp = rcon_command(RCON_HOST, RCON_PORT, RCON_PASSWORD, "ListPlayers", timeout=3.0)
        # ARK often returns lines per player; sometimes header.
        lines = [ln for ln in resp.splitlines() if ln.strip()]
        # crude count: lines that look like a player entry
        # if your output includes a header line, this still works well enough.
        count = 0
        for ln in lines:
            # typical formats contain " - " or ":"; but safest is "contains a name"
            if ln:
                count += 1
        return True, count
    except PermissionError as e:
        return True, None, f"RCON auth failed"
    except Exception as e:
        return False, None, f"{type(e).__name__}"

# =====================
# WEBHOOK TIME LOOP
# =====================
async def time_loop():
    global webhook_msg_id
    await client.wait_until_ready()
    async with aiohttp.ClientSession() as session:
        while True:
            if state:
                title, color, wait, _, _ = current_solunaris()
                embed = {"description": title, "color": color}

                try:
                    if webhook_msg_id:
                        r = await session.patch(
                            f"{WEBHOOK_URL}/messages/{webhook_msg_id}",
                            json={"embeds": [embed]},
                        )
                        # if message deleted => create a new one
                        if r.status == 404:
                            webhook_msg_id = None
                    if not webhook_msg_id:
                        async with session.post(WEBHOOK_URL + "?wait=true", json={"embeds": [embed]}) as resp:
                            data = await resp.json()
                            webhook_msg_id = data["id"]
                except Exception as e:
                    # don’t crash loop
                    pass

                await asyncio.sleep(float(wait))
            else:
                await asyncio.sleep(5)

# =====================
# STATUS VC LOOP
# =====================
async def status_loop():
    await client.wait_until_ready()
    if STATUS_VC_ID == 0:
        return

    vc = client.get_channel(STATUS_VC_ID)
    if vc is None:
        print("⚠️ Could not find status VC by ID.")
        return

    last_name = None
    last_force = 0

    while True:
        online, count, err = get_server_status()

        if online:
            if count is None:
                name = f"🟡 Solunaris | ?/{PLAYER_CAP}"
            else:
                name = f"🟢 Solunaris | {count}/{PLAYER_CAP}"
        else:
            name = f"🔴 Solunaris | Offline"

        force = (time.time() - last_force) > 600  # 10 min
        if name != last_name or force:
            try:
                await vc.edit(name=name)
                last_name = name
                last_force = time.time()
            except discord.HTTPException:
                pass

        await asyncio.sleep(15)

# =====================
# SLASH COMMANDS
# =====================
@tree.command(name="day", description="Show current Solunaris time", guild=discord.Object(id=GUILD_ID))
async def day_cmd(interaction: discord.Interaction):
    title, _, _, _, _ = current_solunaris()
    await interaction.response.send_message(title or "⏳ Time not set yet.", ephemeral=True)

@tree.command(name="settime", description="Set Solunaris time", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(
    year="Year number",
    day="Day of year (1–365)",
    hour="Hour (0–23)",
    minute="Minute (0–59)",
)
async def settime_cmd(interaction: discord.Interaction, year: int, day: int, hour: int, minute: int):
    if not any(r.id == ADMIN_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message("❌ You don’t have permission.", ephemeral=True)
        return

    if year < 1 or not (1 <= day <= 365) or not (0 <= hour <= 23) or not (0 <= minute <= 59):
        await interaction.response.send_message("❌ Invalid values.", ephemeral=True)
        return

    global state
    state = {"epoch": time.time(), "year": year, "day": day, "minute": hour * 60 + minute}
    save_state(state)
    await interaction.response.send_message(f"✅ Set to Day {day} {hour:02d}:{minute:02d} Year {year}", ephemeral=True)

@tree.command(name="status", description="Show server status & players", guild=discord.Object(id=GUILD_ID))
async def status_cmd(interaction: discord.Interaction):
    online, count, err = get_server_status()
    if online:
        if count is None:
            msg = f"🟡 **Solunaris is ONLINE** — Players: ?/{PLAYER_CAP} ({err})"
        else:
            msg = f"🟢 **Solunaris is ONLINE** — Players: {count}/{PLAYER_CAP}"
    else:
        msg = f"🔴 **Solunaris is OFFLINE** — Players: ?/{PLAYER_CAP} ({err})"
    await interaction.response.send_message(msg, ephemeral=True)

# =====================
# STARTUP
# =====================
@client.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    client.loop.create_task(time_loop())
    client.loop.create_task(status_loop())
    print("✅ Ready, commands synced")

client.run(DISCORD_TOKEN)