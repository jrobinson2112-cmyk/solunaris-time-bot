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
TIME_WEBHOOK_URL = os.getenv("WEBHOOK_URL")          # Solunaris Time webhook
PLAYER_WEBHOOK_URL = os.getenv("PLAYER_WEBHOOK_URL") # #online players webhook

RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = int(os.getenv("RCON_PORT", "11020"))
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

STATUS_VC_ID_RAW = os.getenv("STATUS_VC_ID")
PLAYER_CAP = int(os.getenv("PLAYER_CAP", "42"))

GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

STATE_FILE = "state.json"

# Day/night minute lengths (real seconds per in-game minute)
DAY_SECONDS_PER_INGAME_MINUTE = 4.7666667
NIGHT_SECONDS_PER_INGAME_MINUTE = 4.045

# Day is from 05:30 to 17:30, night is from 17:30 to 05:30
SUNRISE_MIN = 5 * 60 + 30
SUNSET_MIN  = 17 * 60 + 30

DAY_COLOR = 0xF1C40F    # yellow
NIGHT_COLOR = 0x5865F2  # blue

# Polling
STATUS_POLL_SECONDS = 15
FORCE_UPDATE_SECONDS = 10 * 60

# Safety timeouts
RCON_TIMEOUT = 2.5
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN")

if not TIME_WEBHOOK_URL:
    raise RuntimeError("Missing WEBHOOK_URL (time webhook)")

if not PLAYER_WEBHOOK_URL:
    raise RuntimeError("Missing PLAYER_WEBHOOK_URL (online players webhook)")

if not RCON_HOST or not RCON_PASSWORD:
    raise RuntimeError("Missing RCON_HOST or RCON_PASSWORD")

if not STATUS_VC_ID_RAW:
    raise RuntimeError("Missing STATUS_VC_ID")
STATUS_VC_ID = int(STATUS_VC_ID_RAW)

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
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return None

def save_state(data):
    with open(STATE_FILE, "w") as f:
        json.dump(data, f)

state = load_state()
time_webhook_message_id = None
player_webhook_message_id = None

if isinstance(state, dict):
    time_webhook_message_id = state.get("time_webhook_message_id")
    player_webhook_message_id = state.get("player_webhook_message_id")

# =====================
# TIME CALC (SMOOTH DAY/NIGHT)
# =====================
def is_day_by_minute(minute_of_day: int) -> bool:
    return SUNRISE_MIN <= minute_of_day < SUNSET_MIN

def seconds_per_minute_for(minute_of_day: int) -> float:
    return DAY_SECONDS_PER_INGAME_MINUTE if is_day_by_minute(minute_of_day) else NIGHT_SECONDS_PER_INGAME_MINUTE

def advance_minutes_piecewise(start_day: int, start_minute_of_day: int, elapsed_real_seconds: float):
    """
    Advances in-game time with different real seconds per in-game minute for day vs night.
    Returns (day_num, minute_of_day_int).
    """
    day = int(start_day)
    minute_of_day = float(start_minute_of_day)
    remaining = float(elapsed_real_seconds)

    for _ in range(20000):
        if remaining <= 0:
            break

        current_min_int = int(minute_of_day) % 1440
        spm = seconds_per_minute_for(current_min_int)

        # Determine next boundary
        if is_day_by_minute(current_min_int):
            boundary_total = (day - 1) * 1440 + SUNSET_MIN
        else:
            if current_min_int < SUNRISE_MIN:
                boundary_total = (day - 1) * 1440 + SUNRISE_MIN
            else:
                boundary_total = day * 1440 + SUNRISE_MIN  # next day sunrise

        current_total = (day - 1) * 1440 + minute_of_day
        minutes_until_boundary = boundary_total - current_total
        if minutes_until_boundary < 0:
            minutes_until_boundary = 0

        seconds_to_boundary = minutes_until_boundary * spm

        if seconds_to_boundary > 0 and remaining >= seconds_to_boundary:
            remaining -= seconds_to_boundary
            minute_of_day += minutes_until_boundary
        else:
            minute_of_day += (remaining / spm) if spm > 0 else 0
            remaining = 0

        while minute_of_day >= 1440:
            minute_of_day -= 1440
            day += 1

    return day, int(minute_of_day) % 1440

def calculate_solunaris_time():
    """
    Returns dict with title, color, sleep_for, day, year, hour, minute, is_day
    Year rolls every 365 days.
    """
    if not state:
        return None

    elapsed_real = time.time() - float(state["real_epoch"])
    start_day = int(state["day"])
    start_year = int(state["year"])
    start_minute_of_day = int(state["hour"]) * 60 + int(state["minute"])

    day_num, minute_of_day = advance_minutes_piecewise(start_day, start_minute_of_day, elapsed_real)

    year = start_year
    while day_num > 365:
        day_num -= 365
        year += 1

    hour = minute_of_day // 60
    minute = minute_of_day % 60

    day_now = is_day_by_minute(minute_of_day)
    emoji = "☀️" if day_now else "🌙"
    color = DAY_COLOR if day_now else NIGHT_COLOR
    sleep_for = DAY_SECONDS_PER_INGAME_MINUTE if day_now else NIGHT_SECONDS_PER_INGAME_MINUTE

    title = f"{emoji} | Solunaris Time | {hour:02d}:{minute:02d} | Day {day_num} | Year {year}"
    return {
        "title": title,
        "color": color,
        "sleep_for": float(sleep_for),
        "day": day_num,
        "year": year,
        "hour": hour,
        "minute": minute,
        "is_day": day_now
    }

# =====================
# WEBHOOK HELPERS (EDIT SAME MESSAGE)
# =====================
async def webhook_edit_or_create(session: aiohttp.ClientSession, webhook_url: str, message_id: str | None, payload: dict):
    """
    Tries to PATCH existing webhook message. If missing/deleted, POSTs a new one and returns new message_id.
    """
    # Try edit
    if message_id:
        try:
            async with session.patch(
                f"{webhook_url}/messages/{message_id}",
                json=payload,
                timeout=HTTP_TIMEOUT
            ) as resp:
                if resp.status in (200, 204):
                    return message_id
                if resp.status == 404:
                    message_id = None
        except Exception:
            message_id = None

    # Create new
    async with session.post(
        webhook_url + "?wait=true",
        json=payload,
        timeout=HTTP_TIMEOUT
    ) as resp:
        data = await resp.json()
        return data.get("id")

def persist_message_ids():
    global state
    if not isinstance(state, dict):
        return
    state["time_webhook_message_id"] = time_webhook_message_id
    state["player_webhook_message_id"] = player_webhook_message_id
    save_state(state)

# =====================
# RCON (SOURCE PROTOCOL) — NO EXTERNAL LIB
# =====================
SERVERDATA_AUTH = 3
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_RESPONSE_VALUE = 0

def _pack_rcon(req_id: int, ptype: int, body: str) -> bytes:
    body_bytes = body.encode("utf-8") + b"\x00"
    packet = struct.pack("<ii", req_id, ptype) + body_bytes + b"\x00"
    return struct.pack("<i", len(packet)) + packet

def _recv_exact(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("socket closed")
        data += chunk
    return data

def _recv_rcon(sock: socket.socket):
    size = struct.unpack("<i", _recv_exact(sock, 4))[0]
    payload = _recv_exact(sock, size)
    req_id, ptype = struct.unpack("<ii", payload[:8])
    body = payload[8:-2].decode("utf-8", errors="ignore")  # strip 2 nulls
    return req_id, ptype, body

def rcon_exec(command: str, timeout: float = RCON_TIMEOUT) -> str:
    """
    Execute RCON command and return response string (best-effort).
    """
    req_id_auth = 1001
    req_id_cmd = 1002

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)

    try:
        sock.connect((RCON_HOST, RCON_PORT))

        # AUTH
        sock.sendall(_pack_rcon(req_id_auth, SERVERDATA_AUTH, RCON_PASSWORD))

        authed = False
        # read a few packets to find auth response
        for _ in range(6):
            rid, ptype, _ = _recv_rcon(sock)
            if ptype == SERVERDATA_AUTH_RESPONSE and rid == req_id_auth:
                authed = True
                break
            if rid == -1:
                raise PermissionError("RCON auth failed")
        if not authed:
            raise TimeoutError("RCON auth timeout")

        # EXEC
        sock.sendall(_pack_rcon(req_id_cmd, SERVERDATA_EXECCOMMAND, command))

        # Read responses (can be multiple). Stop after short idle.
        out = ""
        end = time.time() + timeout
        while time.time() < end:
            try:
                rid, ptype, body = _recv_rcon(sock)
                if rid != req_id_cmd:
                    continue
                if ptype == SERVERDATA_RESPONSE_VALUE:
                    out += body
                    # Heuristic: if we got something and it ends in newline, likely complete
                    if out.endswith("\n"):
                        break
            except socket.timeout:
                break

        return out.strip()

    finally:
        try:
            sock.close()
        except Exception:
            pass

# =====================
# PLAYER PARSING / PLATFORM
# =====================
def detect_platform(token: str) -> str:
    t = (token or "").strip().lower()
    if "xbox" in t or "xboxlive" in t:
        return "🎮 Xbox"
    if "psn" in t or "playstation" in t:
        return "🎮 PlayStation"
    # steam ids are long numeric; EOS ids vary
    if t.isdigit() and len(t) >= 15:
        return "🖥️ PC (Steam)"
    if "eos" in t:
        return "🖥️ PC (EOS)"
    return "❓ Unknown"

def parse_listplayers(output: str):
    """
    Returns list of dicts: {character, id_token, platform}
    Handles common formats like:
      1. Name, 7656119...
      2. Name, XboxLive:xxxx
      3. Name, PSN:xxxx
    """
    if not output:
        return []

    lower = output.lower()
    if "no players connected" in lower:
        return []

    players = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "." not in line:
            # sometimes output can be different; skip non-entries
            continue

        # split "1. rest"
        try:
            _, rest = line.split(".", 1)
            rest = rest.strip()
        except ValueError:
            continue

        # split "Name, token"
        if "," in rest:
            name, token = rest.split(",", 1)
            character = name.strip()
            token = token.strip()
        else:
            character = rest.strip()
            token = ""

        players.append({
            "character": character,
            "id_token": token,
            "platform": detect_platform(token)
        })

    return players

# =====================
# STATUS + PLAYER LIST LOOPS (CHANGE-ONLY + FORCE)
# =====================
_last_vc_name = None
_last_players_hash = None
_last_force_ts = 0.0

def build_vc_name(online: bool, count: int | None):
    if not online:
        return f"🔴 Solunaris | Players ?/{PLAYER_CAP}"
    if count is None:
        return f"🟡 Solunaris | Players ?/{PLAYER_CAP}"
    return f"🟢 Solunaris | Players {count}/{PLAYER_CAP}"

def build_player_message(players: list[dict], online: bool):
    if not online:
        return f"## 🔴 Solunaris Server Offline\n\n_No player list available._"

    if not players:
        return f"## 🧑‍🚀 Online Players (0/{PLAYER_CAP})\n\n_No players online_"

    lines = []
    for p in players:
        # “Character name” = p['character']
        # “Xbox/ps/pc name” = whatever ARK gives us (token) — we show it raw in code font
        lines.append(f"**{p['character']}** — {p['platform']} `{p['id_token']}`")

    return f"## 🧑‍🚀 Online Players ({len(players)}/{PLAYER_CAP})\n\n" + "\n".join(lines)

async def status_and_players_loop():
    global _last_vc_name, _last_players_hash, _last_force_ts
    global player_webhook_message_id

    await client.wait_until_ready()
    vc = client.get_channel(STATUS_VC_ID)

    async with aiohttp.ClientSession() as session:
        while True:
            now = time.time()
            force = (now - _last_force_ts) >= FORCE_UPDATE_SECONDS

            online = False
            players = []
            count = None

            try:
                out = await asyncio.to_thread(rcon_exec, "ListPlayers", RCON_TIMEOUT)
                players = parse_listplayers(out)
                online = True
                count = len(players)
            except Exception:
                # If RCON fails, we treat as offline/unknown
                online = False
                players = []
                count = None

            # VC update (only on change or force)
            new_vc_name = build_vc_name(online, count)
            if vc and (force or new_vc_name != _last_vc_name):
                try:
                    await vc.edit(name=new_vc_name, reason="Solunaris status update")
                    _last_vc_name = new_vc_name
                except Exception:
                    pass

            # Player list webhook (only on change or force)
            content = build_player_message(players, online)
            content_hash = hash(content)

            if force or content_hash != _last_players_hash:
                try:
                    player_webhook_message_id = await webhook_edit_or_create(
                        session,
                        PLAYER_WEBHOOK_URL,
                        player_webhook_message_id,
                        {"content": content}
                    )
                    _last_players_hash = content_hash
                    _last_force_ts = now

                    # persist ids
                    if isinstance(state, dict):
                        persist_message_ids()
                except Exception:
                    pass

            await asyncio.sleep(STATUS_POLL_SECONDS)

# =====================
# TIME WEBHOOK LOOP
# =====================
async def time_webhook_loop():
    global time_webhook_message_id

    await client.wait_until_ready()
    async with aiohttp.ClientSession() as session:
        while True:
            sol = calculate_solunaris_time()
            if not sol:
                await asyncio.sleep(5)
                continue

            embed = {
                "title": sol["title"],
                "color": sol["color"]
            }

            try:
                time_webhook_message_id = await webhook_edit_or_create(
                    session,
                    TIME_WEBHOOK_URL,
                    time_webhook_message_id,
                    {"embeds": [embed]}
                )
                if isinstance(state, dict):
                    persist_message_ids()
            except Exception:
                # If webhook fails, just wait and try again
                pass

            await asyncio.sleep(sol["sleep_for"])

# =====================
# SLASH COMMANDS
# =====================
@tree.command(
    name="day",
    description="Show current Solunaris time",
    guild=discord.Object(id=GUILD_ID),
)
async def day_cmd(interaction: discord.Interaction):
    sol = calculate_solunaris_time()
    if not sol:
        await interaction.response.send_message("⏳ Time not set yet. Use /settime.", ephemeral=True)
        return
    await interaction.response.send_message(sol["title"], ephemeral=True)

@tree.command(
    name="settime",
    description="Set Solunaris time (role restricted)",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(
    year="Year number",
    day="Day of year (1–365)",
    hour="Hour (0–23)",
    minute="Minute (0–59)",
)
async def settime_cmd(interaction: discord.Interaction, year: int, day: int, hour: int, minute: int):
    # Role restriction
    roles = getattr(interaction.user, "roles", [])
    if not any(getattr(r, "id", None) == ADMIN_ROLE_ID for r in roles):
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return

    if year < 1 or not (1 <= day <= 365) or not (0 <= hour <= 23) or not (0 <= minute <= 59):
        await interaction.response.send_message("❌ Invalid values.", ephemeral=True)
        return

    global state
    state = {
        "real_epoch": time.time(),
        "year": int(year),
        "day": int(day),
        "hour": int(hour),
        "minute": int(minute),
        "time_webhook_message_id": time_webhook_message_id,
        "player_webhook_message_id": player_webhook_message_id,
    }
    save_state(state)

    await interaction.response.send_message(
        f"✅ Set to Day {day}, {hour:02d}:{minute:02d}, Year {year}",
        ephemeral=True
    )

@tree.command(
    name="status",
    description="Show Solunaris server status and players",
    guild=discord.Object(id=GUILD_ID),
)
async def status_cmd(interaction: discord.Interaction):
    # Always defer fast so it never times out
    await interaction.response.defer(ephemeral=True)

    try:
        out = await asyncio.to_thread(rcon_exec, "ListPlayers", RCON_TIMEOUT)
        players = parse_listplayers(out)
        msg = f"🟢 **Solunaris is ONLINE** — Players: **{len(players)}/{PLAYER_CAP}**"
        if players:
            sample = "\n".join([f"- {p['character']} ({p['platform']})" for p in players[:10]])
            msg += "\n\n**Online now:**\n" + sample
            if len(players) > 10:
                msg += f"\n… and {len(players)-10} more."
    except Exception:
        msg = f"🔴 **Solunaris is OFFLINE (RCON unreachable)**"

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
        print(f"⚠️ Command sync failed: {e}")

    client.loop.create_task(time_webhook_loop())
    client.loop.create_task(status_and_players_loop())
    print("✅ Loops started")

client.run(DISCORD_TOKEN)