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
RCON_PORT = os.getenv("RCON_PORT")
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
if not all(required):
    raise RuntimeError("Missing required environment variables")

RCON_PORT = int(RCON_PORT)

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

# ===== GAMELOG SYNC =====
GAMELOG_SYNC_SECONDS = 120
SYNC_DRIFT_MINUTES = 2
SYNC_COOLDOWN_SECONDS = 600

# =====================
# DISCORD
# =====================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# =====================
# STATE
# =====================
message_ids = {"time": None, "players": None}
last_announced_day = None
_last_sync_ts = 0.0

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
# TIME LOGIC
# =====================
def is_day(m): return SUNRISE <= m < SUNSET
def spm(m): return DAY_SPM if is_day(m) else NIGHT_SPM

def advance_minute(m, d, y):
    m += 1
    if m >= 1440:
        m = 0
        d += 1
        if d > 365:
            d = 1
            y += 1
    return m, d, y

def calculate_time_details():
    if not state:
        return None

    elapsed = time.time() - state["epoch"]
    m = state["hour"] * 60 + state["minute"]
    d = state["day"]
    y = state["year"]

    remaining = elapsed
    while True:
        cur_spm = spm(m)
        if remaining >= cur_spm:
            remaining -= cur_spm
            m, d, y = advance_minute(m, d, y)
        else:
            return m, d, y, remaining, cur_spm

def build_time_embed(m, d, y):
    h, mi = divmod(m, 60)
    emoji = "☀️" if is_day(m) else "🌙"
    color = DAY_COLOR if is_day(m) else NIGHT_COLOR
    return {
        "title": f"{emoji} | Solunaris Time | {h:02d}:{mi:02d} | Day {d} | Year {y}",
        "color": color,
    }

# =====================
# RCON
# =====================
def rcon_packet(req_id, ptype, body):
    data = body.encode() + b"\x00"
    pkt = req_id.to_bytes(4, "little") + ptype.to_bytes(4, "little") + data + b"\x00"
    return len(pkt).to_bytes(4, "little") + pkt

async def rcon_command(cmd, timeout=6):
    r, w = await asyncio.open_connection(RCON_HOST, RCON_PORT)
    try:
        w.write(rcon_packet(1, 3, RCON_PASSWORD))
        await w.drain()
        await r.read(4096)

        w.write(rcon_packet(2, 2, cmd))
        await w.drain()

        data = b""
        end = time.time() + timeout
        while time.time() < end:
            try:
                part = await asyncio.wait_for(r.read(4096), timeout=0.3)
            except asyncio.TimeoutError:
                break
            if not part:
                break
            data += part

        out = []
        i = 0
        while i + 4 <= len(data):
            size = int.from_bytes(data[i:i+4], "little")
            i += 4
            pkt = data[i:i+size]
            i += size
            txt = pkt[8:-2].decode(errors="ignore")
            if txt:
                out.append(txt)

        return "".join(out)
    finally:
        w.close()
        await w.wait_closed()

# =====================
# GAMELOG PARSING
# =====================
_DAYTIME_RE = re.compile(
    r"(?:^|[\s\[])"
    r"Day\s*(\d+)\s*,\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})"
    r"(?:\s*:)?",
    re.IGNORECASE
)

def parse_latest_gamelog_time(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for ln in reversed(lines):
        m = _DAYTIME_RE.search(ln)
        if m:
            return int(m[1]), int(m[2]), int(m[3]), int(m[4])
    print("⚠️ GetGameLog parse failed. Last 20 lines:")
    for l in lines[-20:]:
        print(l)
    return None

def apply_gamelog_sync(d, h, m):
    global state
    if not state:
        return False, "No state"

    cur = calculate_time_details()
    if not cur:
        return False, "No calc"

    cur_m, cur_d, cur_y, _, _ = cur
    target_m = h * 60 + m

    diff = (d - cur_d) * 1440 + (target_m - cur_m)
    while diff > 720: diff -= 1440
    while diff < -720: diff += 1440

    if abs(diff) < SYNC_DRIFT_MINUTES:
        return False, f"Drift {diff} min < threshold"

    state["epoch"] -= diff * spm(cur_m)
    state["day"] = d
    state["hour"] = h
    state["minute"] = m
    save_state(state)

    return True, f"Synced (drift {diff} min)"

# =====================
# LOOPS
# =====================
async def gamelog_sync_loop():
    global _last_sync_ts
    await client.wait_until_ready()

    while True:
        try:
            if not state:
                await asyncio.sleep(GAMELOG_SYNC_SECONDS)
                continue

            if time.time() - _last_sync_ts < SYNC_COOLDOWN_SECONDS:
                await asyncio.sleep(GAMELOG_SYNC_SECONDS)
                continue

            log = await rcon_command("GetGameLog")
            parsed = parse_latest_gamelog_time(log)
            if parsed:
                d, h, m, s = parsed
                changed, msg = apply_gamelog_sync(d, h, m)
                print("GameLog sync:", msg)
                if changed:
                    _last_sync_ts = time.time()
        except Exception as e:
            print("GameLog sync error:", e)

        await asyncio.sleep(GAMELOG_SYNC_SECONDS)

# =====================
# SLASH COMMAND
# =====================
@tree.command(name="sync", guild=discord.Object(id=GUILD_ID))
async def sync_cmd(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    log = await rcon_command("GetGameLog")
    parsed = parse_latest_gamelog_time(log)
    if not parsed:
        await i.followup.send("❌ No Day/Time found in GetGameLog", ephemeral=True)
        return
    d, h, m, s = parsed
    changed, msg = apply_gamelog_sync(d, h, m)
    await i.followup.send(f"✅ {msg}", ephemeral=True)

# =====================
# START
# =====================
@client.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    client.loop.create_task(gamelog_sync_loop())
    print("✅ Solunaris Time Bot Online")

client.run(DISCORD_TOKEN)