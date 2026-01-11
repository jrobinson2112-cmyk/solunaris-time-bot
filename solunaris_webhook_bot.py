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
    missing = [k for k in [
        "DISCORD_TOKEN", "WEBHOOK_URL", "PLAYERS_WEBHOOK_URL",
        "NITRADO_TOKEN", "NITRADO_SERVICE_ID",
        "RCON_HOST", "RCON_PORT", "RCON_PASSWORD"
    ] if not os.getenv(k)]
    raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

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
_last_vc_edit_ts = 0.0
_last_vc_name = None

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
def is_day(m: int) -> bool:
    return SUNRISE <= m < SUNSET

def spm(m: int) -> float:
    return DAY_SPM if is_day(m) else NIGHT_SPM

def advance_minute(m: int, d: int, y: int):
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

    elapsed = float(time.time() - state["epoch"])
    m = int(state["hour"]) * 60 + int(state["minute"])
    d = int(state["day"])
    y = int(state["year"])

    remaining = elapsed
    while True:
        cur = spm(m)
        if remaining >= cur:
            remaining -= cur
            m, d, y = advance_minute(m, d, y)
        else:
            return m, d, y, remaining, cur

def build_time_embed(m: int, d: int, y: int):
    h, mi = divmod(m, 60)
    emoji = "☀️" if is_day(m) else "🌙"
    color = DAY_COLOR if is_day(m) else NIGHT_COLOR
    return {
        "title": f"{emoji} | Solunaris Time | {h:02d}:{mi:02d} | Day {d} | Year {y}",
        "color": color
    }

def seconds_until_next_round_step(m: int, seconds_into_minute: float, step: int) -> float:
    mod = m % step
    minutes_to_boundary = (step - mod) if mod != 0 else step
    remaining_in_current_minute = max(0.0, spm(m) - seconds_into_minute)
    total = remaining_in_current_minute
    mm = m
    dd = 0
    yy = 0
    for _ in range(minutes_to_boundary - 1):
        mm, dd, yy = advance_minute(mm, dd, yy)
        total += spm(mm)
    return max(0.5, total)

# =====================
# RCON
# =====================
def rcon_packet(req_id: int, ptype: int, body: str) -> bytes:
    data = body.encode("utf-8", errors="ignore") + b"\x00"
    pkt = req_id.to_bytes(4, "little", signed=True) + ptype.to_bytes(4, "little", signed=True) + data + b"\x00"
    return len(pkt).to_bytes(4, "little", signed=True) + pkt

async def rcon_command(cmd: str, timeout: float = 8.0) -> str:
    reader, writer = await asyncio.wait_for(asyncio.open_connection(RCON_HOST, RCON_PORT), timeout=timeout)
    try:
        # auth
        writer.write(rcon_packet(1, 3, RCON_PASSWORD))
        await writer.drain()
        await asyncio.wait_for(reader.read(4096), timeout=timeout)

        # command
        writer.write(rcon_packet(2, 2, cmd))
        await writer.drain()

        data = b""
        end = time.time() + timeout
        while time.time() < end:
            try:
                part = await asyncio.wait_for(reader.read(4096), timeout=0.35)
            except asyncio.TimeoutError:
                break
            if not part:
                break
            data += part

        # parse packets
        out = []
        i = 0
        while i + 4 <= len(data):
            size = int.from_bytes(data[i:i+4], "little", signed=True)
            i += 4
            if size < 10 or i + size > len(data):
                break
            pkt = data[i:i+size]
            i += size
            body = pkt[8:-2]
            txt = body.decode("utf-8", errors="ignore")
            if txt:
                out.append(txt)

        return "".join(out).strip()
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

# =====================
# WEBHOOK (time + players)
# =====================
async def upsert_webhook(session: aiohttp.ClientSession, url: str, key: str, embed: dict):
    mid = message_ids.get(key)
    if mid:
        async with session.patch(f"{url}/messages/{mid}", json={"embeds": [embed]}) as r:
            if r.status == 404:
                message_ids[key] = None
                return await upsert_webhook(session, url, key, embed)
        return

    async with session.post(url + "?wait=true", json={"embeds": [embed]}) as r:
        data = await r.json()
        message_ids[key] = data["id"]

# =====================
# GAMELOG PARSING (FIXED)
# =====================
_RICHCOLOR_RE = re.compile(r"<\s*RichColor\b[^>]*>", re.IGNORECASE)
_XMLTAG_RE = re.compile(r"</?[^>]+>")  # any xml-ish tag
# tolerant: Day 216, 16:53:34 (colon optional), allows dot separators too
_DAYTIME_RE = re.compile(
    r"Day\s*(\d+)\s*[, ]\s*(\d{1,2})[:.](\d{2})[:.](\d{2})",
    re.IGNORECASE
)

def clean_gamelog_line(line: str) -> str:
    if not line:
        return ""
    s = line.strip()
    s = _RICHCOLOR_RE.sub("", s)
    s = _XMLTAG_RE.sub("", s)
    # normalize weird spacing
    s = s.replace("\u200b", "").replace("\ufeff", "")
    return s.strip()

def parse_latest_gamelog_time(text: str):
    """
    Returns (day, hour, minute, second) from the most recent line containing Day/time.
    Accepts any line in GetGameLog (no tribe filter).
    """
    if not text:
        return None

    raw_lines = [ln for ln in text.splitlines() if ln.strip()]
    # scan from bottom
    for raw in reversed(raw_lines):
        ln = clean_gamelog_line(raw)
        m = _DAYTIME_RE.search(ln)
        if m:
            day = int(m.group(1))
            hour = int(m.group(2))
            minute = int(m.group(3))
            second = int(m.group(4))
            return day, hour, minute, second

    # debug: print last few lines to Railway logs
    print("ℹ️ No parsable Day/time found in GetGameLog. Last 15 cleaned lines:")
    for l in [clean_gamelog_line(x) for x in raw_lines[-15:]]:
        print(l)
    return None

def apply_gamelog_sync(parsed_day: int, parsed_hour: int, parsed_minute: int):
    global state
    if not state:
        return False, "No state set (/settime first)"

    cur = calculate_time_details()
    if not cur:
        return False, "No calculated time"

    cur_m, cur_d, cur_y, _, _ = cur
    target_m = parsed_hour * 60 + parsed_minute

    diff = (parsed_day - cur_d) * 1440 + (target_m - cur_m)
    while diff > 720:
        diff -= 1440
    while diff < -720:
        diff += 1440

    if abs(diff) < SYNC_DRIFT_MINUTES:
        return False, f"Drift {diff} min < threshold"

    # shift epoch so our simulated time matches parsed time "now"
    state["epoch"] = float(state["epoch"]) - (diff * spm(cur_m))
    state["day"] = int(parsed_day)
    state["hour"] = int(parsed_hour)
    state["minute"] = int(parsed_minute)
    save_state(state)

    return True, f"Synced using GetGameLog (drift {diff} min)"

# =====================
# LOOPS
# =====================
async def time_loop():
    global last_announced_day
    await client.wait_until_ready()

    async with aiohttp.ClientSession() as session:
        while True:
            details = calculate_time_details()
            if not details:
                await asyncio.sleep(5)
                continue

            m, d, y, seconds_into_minute, _ = details

            # only update on round 10 minutes
            if (m % TIME_UPDATE_STEP_MINUTES) == 0:
                embed = build_time_embed(m, d, y)
                await upsert_webhook(session, WEBHOOK_URL, "time", embed)

                absolute_day = y * 365 + d
                if last_announced_day is None:
                    last_announced_day = absolute_day
                elif absolute_day > last_announced_day:
                    ch = client.get_channel(ANNOUNCE_CHANNEL_ID)
                    if ch:
                        await ch.send(f"📅 **New Solunaris Day** — Day **{d}**, Year **{y}**")
                    last_announced_day = absolute_day

            sleep_for = seconds_until_next_round_step(m, seconds_into_minute, TIME_UPDATE_STEP_MINUTES)
            await asyncio.sleep(sleep_for)

async def gamelog_sync_loop():
    global _last_sync_ts
    await client.wait_until_ready()

    while True:
        try:
            if not state:
                await asyncio.sleep(GAMELOG_SYNC_SECONDS)
                continue

            now = time.time()
            if (now - _last_sync_ts) < SYNC_COOLDOWN_SECONDS:
                await asyncio.sleep(GAMELOG_SYNC_SECONDS)
                continue

            text = await rcon_command("GetGameLog", timeout=10.0)
            parsed = parse_latest_gamelog_time(text)
            if not parsed:
                await asyncio.sleep(GAMELOG_SYNC_SECONDS)
                continue

            dd, hh, mm, ss = parsed
            changed, msg = apply_gamelog_sync(dd, hh, mm)
            print("GameLog sync:", msg)

            if changed:
                _last_sync_ts = time.time()

        except Exception as e:
            print("GameLog sync error:", e)

        await asyncio.sleep(GAMELOG_SYNC_SECONDS)

# =====================
# COMMANDS
# =====================
@tree.command(name="settime", guild=discord.Object(id=GUILD_ID))
async def settime(i: discord.Interaction, year: int, day: int, hour: int, minute: int):
    if not any(r.id == ADMIN_ROLE_ID for r in i.user.roles):
        await i.response.send_message("❌ No permission", ephemeral=True)
        return

    if year < 1 or day < 1 or day > 365 or hour < 0 or hour > 23 or minute < 0 or minute > 59:
        await i.response.send_message("❌ Invalid values.", ephemeral=True)
        return

    global state
    state = {"epoch": time.time(), "year": int(year), "day": int(day), "hour": int(hour), "minute": int(minute)}
    save_state(state)
    await i.response.send_message("✅ Time set", ephemeral=True)

@tree.command(name="sync", guild=discord.Object(id=GUILD_ID))
async def sync_cmd(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    try:
        text = await rcon_command("GetGameLog", timeout=10.0)
        parsed = parse_latest_gamelog_time(text)
        if not parsed:
            await i.followup.send("❌ No Day/Time found in GetGameLog (check /debuggamelog).", ephemeral=True)
            return
        dd, hh, mm, ss = parsed
        changed, msg = apply_gamelog_sync(dd, hh, mm)
        await i.followup.send(f"✅ {msg}", ephemeral=True)
    except Exception as e:
        await i.followup.send(f"❌ Sync failed: {e}", ephemeral=True)

@tree.command(name="debuggamelog", guild=discord.Object(id=GUILD_ID))
async def debuggamelog_cmd(i: discord.Interaction):
    """
    Shows the last ~12 cleaned lines from GetGameLog so we can confirm formatting.
    """
    await i.response.defer(ephemeral=True)
    try:
        text = await rcon_command("GetGameLog", timeout=10.0)
        lines = [ln for ln in text.splitlines() if ln.strip()]
        tail = [clean_gamelog_line(x) for x in lines[-12:]]
        payload = "\n".join(tail).strip()
        if not payload:
            await i.followup.send("No output from GetGameLog.", ephemeral=True)
            return

        # keep it under Discord limits
        if len(payload) > 1800:
            payload = payload[-1800:]

        await i.followup.send(f"```text\n{payload}\n```", ephemeral=True)
    except Exception as e:
        await i.followup.send(f"Debug failed: {e}", ephemeral=True)

# =====================
# START
# =====================
@client.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    client.loop.create_task(time_loop())
    client.loop.create_task(gamelog_sync_loop())
    print("✅ Solunaris bot online")

client.run(DISCORD_TOKEN)