import os
import time
import json
import asyncio
import aiohttp
import discord
from discord import app_commands
import re

# =====================
# ENV
# =====================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PLAYERS_WEBHOOK_URL = os.getenv("PLAYERS_WEBHOOK_URL")
NITRADO_TOKEN = os.getenv("NITRADO_TOKEN")
NITRADO_SERVICE_ID = os.getenv("NITRADO_SERVICE_ID")

RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = int(os.getenv("RCON_PORT", "0"))
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

required = [
    DISCORD_TOKEN,
    WEBHOOK_URL,
    PLAYERS_WEBHOOK_URL,
    NITRADO_TOKEN,
    NITRADO_SERVICE_ID,
    RCON_HOST,
    RCON_PORT,
    RCON_PASSWORD,
]
missing = [k for k in required if not k]
if missing:
    raise RuntimeError("Missing required environment variables")

# =====================
# CONSTANTS
# =====================
GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076
STATUS_VC_ID = 1456615806887657606
ANNOUNCE_CHANNEL_ID = 1430388267446042666
PLAYER_CAP = 42

DAY_SPM = 4.7666667
NIGHT_SPM = 4.045
SUNRISE = 5 * 60 + 30
SUNSET = 17 * 60 + 30

DAY_COLOR = 0xF1C40F
NIGHT_COLOR = 0x5865F2

STATE_FILE = "state.json"

STATUS_POLL_SECONDS = 15
VC_EDIT_MIN_SECONDS = 300
TIME_UPDATE_STEP_MINUTES = 10

GAMELOG_SYNC_SECONDS = 120
SYNC_DRIFT_MINUTES = 2
SYNC_COOLDOWN_SECONDS = 600

# =====================
# DISCORD SETUP
# =====================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

message_ids = {"time": None, "players": None}
last_announced_day = None
_last_vc_edit_ts = 0.0
_last_vc_name = None
_last_sync_ts = 0.0

# =====================
# STATE
# =====================
def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f)

state = load_state()

# =====================
# TIME MODEL
# =====================
def is_day(minute):
    return SUNRISE <= minute < SUNSET

def spm(minute):
    return DAY_SPM if is_day(minute) else NIGHT_SPM

def advance_minute(minute, day, year):
    minute += 1
    if minute >= 1440:
        minute = 0
        day += 1
        if day > 365:
            day = 1
            year += 1
    return minute, day, year

def calculate_time():
    if not state:
        return None

    elapsed = time.time() - state["epoch"]
    minute = state["hour"] * 60 + state["minute"]
    day = state["day"]
    year = state["year"]

    remaining = elapsed
    while True:
        cur_spm = spm(minute)
        if remaining >= cur_spm:
            remaining -= cur_spm
            minute, day, year = advance_minute(minute, day, year)
        else:
            break

    return minute, day, year, remaining

def build_time_embed(minute, day, year):
    h = minute // 60
    m = minute % 60
    emoji = "☀️" if is_day(minute) else "🌙"
    color = DAY_COLOR if is_day(minute) else NIGHT_COLOR
    return {
        "title": f"{emoji} | Solunaris Time | {h:02d}:{m:02d} | Day {day} | Year {year}",
        "color": color
    }

# =====================
# RCON (ROBUST)
# =====================
def _rcon_packet(req_id, ptype, body):
    data = body.encode() + b"\x00"
    pkt = (
        req_id.to_bytes(4, "little", signed=True)
        + ptype.to_bytes(4, "little", signed=True)
        + data
        + b"\x00"
    )
    return len(pkt).to_bytes(4, "little", signed=True) + pkt

async def rcon_command(command, timeout=8):
    reader, writer = await asyncio.open_connection(RCON_HOST, RCON_PORT)

    def parse(buf):
        out, i = [], 0
        while i + 4 <= len(buf):
            size = int.from_bytes(buf[i:i+4], "little", signed=True)
            if i + 4 + size > len(buf) or size < 10:
                break
            pkt = buf[i+4:i+4+size]
            body = pkt[8:-2]
            out.append(body.decode("utf-8", errors="replace"))
            i += 4 + size
        return out, buf[i:]

    try:
        writer.write(_rcon_packet(1, 3, RCON_PASSWORD))
        await writer.drain()
        await reader.read(4096)

        writer.write(_rcon_packet(2, 2, command))
        await writer.drain()

        buf = b""
        texts = []
        end = time.time() + timeout
        while time.time() < end:
            try:
                chunk = await asyncio.wait_for(reader.read(8192), timeout=1.0)
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            buf += chunk
            parsed, buf = parse(buf)
            texts.extend(parsed)

        return "".join(texts).strip()
    finally:
        writer.close()
        await writer.wait_closed()

# =====================
# GAMELOG SYNC
# =====================
DAY_RE = re.compile(r"Day\s+(\d+),\s+(\d{1,2}):(\d{2}):(\d{2})")

def extract_latest_daytime(text):
    for line in reversed(text.splitlines()):
        m = DAY_RE.search(line)
        if m:
            return tuple(map(int, m.groups()))
    return None

def apply_sync(parsed):
    global state
    if not state:
        return "No state"

    minute, day, year, _ = calculate_time()
    p_day, p_h, p_m, _ = parsed
    target_minute = p_h * 60 + p_m

    diff = (p_day - day) * 1440 + (target_minute - minute)
    while diff > 720: diff -= 1440
    while diff < -720: diff += 1440

    if abs(diff) < SYNC_DRIFT_MINUTES:
        return f"Drift {diff} min < threshold"

    state["epoch"] -= diff * spm(minute)
    state["day"] = p_day
    state["hour"] = p_h
    state["minute"] = p_m
    save_state(state)
    return f"Synced ({diff} min)"

# =====================
# LOOPS
# =====================
async def time_loop():
    await client.wait_until_ready()
    async with aiohttp.ClientSession() as session:
        while True:
            res = calculate_time()
            if not res:
                await asyncio.sleep(5)
                continue

            minute, day, year, sec = res
            if minute % TIME_UPDATE_STEP_MINUTES == 0:
                embed = build_time_embed(minute, day, year)
                await session.post(WEBHOOK_URL + "?wait=true", json={"embeds":[embed]})

            await asyncio.sleep(30)

async def gamelog_sync_loop():
    global _last_sync_ts
    await client.wait_until_ready()

    while True:
        try:
            if time.time() - _last_sync_ts < SYNC_COOLDOWN_SECONDS:
                await asyncio.sleep(GAMELOG_SYNC_SECONDS)
                continue

            text = await rcon_command("GetGameLog")
            parsed = extract_latest_daytime(text)
            if parsed:
                msg = apply_sync(parsed)
                print("GameLog sync:", msg)
                _last_sync_ts = time.time()
        except Exception as e:
            print("GameLog sync error:", e)

        await asyncio.sleep(GAMELOG_SYNC_SECONDS)

# =====================
# COMMANDS
# =====================
@tree.command(name="sync", guild=discord.Object(id=GUILD_ID))
async def sync_cmd(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    text = await rcon_command("GetGameLog")
    parsed = extract_latest_daytime(text)
    if not parsed:
        await i.followup.send("❌ No Day/Time found in GetGameLog", ephemeral=True)
        return
    msg = apply_sync(parsed)
    await i.followup.send(f"✅ {msg}", ephemeral=True)

@tree.command(name="debuggamelog", guild=discord.Object(id=GUILD_ID))
async def debug_log(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    text = await rcon_command("GetGameLog")
    await i.followup.send(f"```text\n{text[:1800]}\n```", ephemeral=True)

# =====================
# START
# =====================
@client.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    client.loop.create_task(time_loop())
    client.loop.create_task(gamelog_sync_loop())
    print("✅ Tradewinds Time Bot online")

client.run(DISCORD_TOKEN)