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
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # time webhook
PLAYERS_WEBHOOK_URL = os.getenv("PLAYERS_WEBHOOK_URL")  # players webhook
NITRADO_TOKEN = os.getenv("NITRADO_TOKEN")
NITRADO_SERVICE_ID = os.getenv("NITRADO_SERVICE_ID")

RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = os.getenv("RCON_PORT")
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

required_keys = [
    "DISCORD_TOKEN",
    "WEBHOOK_URL",
    "PLAYERS_WEBHOOK_URL",
    "NITRADO_TOKEN",
    "NITRADO_SERVICE_ID",
    "RCON_HOST",
    "RCON_PORT",
    "RCON_PASSWORD",
]
missing = [k for k in required_keys if not os.getenv(k)]
if missing:
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

# Poll intervals
STATUS_POLL_SECONDS = 15

# VC rename rate-limit (prevents Discord 429s)
VC_EDIT_MIN_SECONDS = 300  # 5 minutes
_last_vc_edit_ts = 0.0
_last_vc_name = None

# Time webhook: only update on round 10 minutes (00,10,20,30,40,50)
TIME_UPDATE_STEP_MINUTES = 10

# =====================
# GAMELOG SYNC (RCON)
# =====================
GAMELOG_SYNC_SECONDS = 120          # how often to check GetGameLog
SYNC_DRIFT_MINUTES = 2              # only correct if drift >= this many in-game minutes
SYNC_COOLDOWN_SECONDS = 600         # don't resync more than once per 10 minutes

# IMPORTANT: ASA requires GetGameLog PAGE INDEX (e.g. "GetGameLog 1")
GAMELOG_PAGE = 1                    # page 1 = newest
GAMELOG_PAGES_TO_SCAN = 3           # scan newest..older pages for a Day/time line

# =====================
# DISCORD SETUP
# =====================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# =====================
# SHARED STATE
# =====================
message_ids = {
    "time": None,
    "players": None,
}
last_announced_day = None

# =====================
# STATE FILE
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
# TIME LOGIC
# =====================
def is_day(minute_of_day: int) -> bool:
    return SUNRISE <= minute_of_day < SUNSET

def spm(minute_of_day: int) -> float:
    return DAY_SPM if is_day(minute_of_day) else NIGHT_SPM

def _advance_one_minute(minute_of_day: int, day: int, year: int):
    minute_of_day += 1
    if minute_of_day >= 1440:
        minute_of_day = 0
        day += 1
        if day > 365:
            day = 1
            year += 1
    return minute_of_day, day, year

def calculate_time_details():
    """
    Returns:
      - minute_of_day (0..1439)
      - day, year
      - seconds_into_current_minute (real seconds since current in-game minute started)
      - current_minute_spm (real seconds per in-game minute for current minute)
    """
    if not state:
        return None

    elapsed = float(time.time() - state["epoch"])
    minute_of_day = int(state["hour"]) * 60 + int(state["minute"])
    day = int(state["day"])
    year = int(state["year"])

    remaining = elapsed

    while True:
        cur_spm = spm(minute_of_day)
        if remaining >= cur_spm:
            remaining -= cur_spm
            minute_of_day, day, year = _advance_one_minute(minute_of_day, day, year)
            continue

        seconds_into_current_minute = remaining
        return minute_of_day, day, year, seconds_into_current_minute, cur_spm

def build_time_embed(minute_of_day: int, day: int, year: int):
    hour = minute_of_day // 60
    minute = minute_of_day % 60
    emoji = "☀️" if is_day(minute_of_day) else "🌙"
    color = DAY_COLOR if is_day(minute_of_day) else NIGHT_COLOR
    title = f"{emoji} | Solunaris Time | {hour:02d}:{minute:02d} | Day {day} | Year {year}"
    return {"title": title, "color": color}

def seconds_until_next_round_step(minute_of_day: int, day: int, year: int, seconds_into_minute: float, step: int):
    """
    Real seconds until next in-game minute boundary where (minute % step == 0).
    If currently exactly on a boundary, waits step minutes ahead (not immediately).
    """
    m = minute_of_day
    mod = m % step
    minutes_to_boundary = (step - mod) if mod != 0 else step

    cur_spm = spm(m)
    remaining_in_current_minute = max(0.0, cur_spm - seconds_into_minute)
    total = remaining_in_current_minute

    m2 = m
    d2, y2 = day, year
    for _ in range(minutes_to_boundary - 1):
        m2, d2, y2 = _advance_one_minute(m2, d2, y2)
        total += spm(m2)

    return max(0.5, total)

# =====================
# NITRADO STATUS (COUNT)
# =====================
async def get_server_status(session: aiohttp.ClientSession):
    headers = {"Authorization": f"Bearer {NITRADO_TOKEN}"}
    url = f"https://api.nitrado.net/services/{NITRADO_SERVICE_ID}/gameservers"

    async with session.get(url, headers=headers) as r:
        data = await r.json()

    gs = data["data"]["gameserver"]
    status = str(gs.get("status", "")).lower()
    online = status in ("started", "running", "online")
    players = int(gs.get("query", {}).get("player_current", 0) or 0)
    return online, players

# =====================
# RCON (Minimal Source RCON)
# =====================
def _rcon_make_packet(req_id: int, ptype: int, body: str) -> bytes:
    data = body.encode("utf-8") + b"\x00"
    packet = (
        req_id.to_bytes(4, "little", signed=True)
        + ptype.to_bytes(4, "little", signed=True)
        + data
        + b"\x00"
    )
    size = len(packet)
    return size.to_bytes(4, "little", signed=True) + packet

async def rcon_command(command: str, timeout: float = 6.0) -> str:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(RCON_HOST, RCON_PORT), timeout=timeout
    )
    try:
        # auth
        writer.write(_rcon_make_packet(1, 3, RCON_PASSWORD))
        await writer.drain()

        raw = await asyncio.wait_for(reader.read(4096), timeout=timeout)
        if len(raw) < 12:
            raise RuntimeError("RCON auth failed (short response)")

        # command
        writer.write(_rcon_make_packet(2, 2, command))
        await writer.drain()

        chunks = []
        end_time = time.time() + timeout
        while time.time() < end_time:
            try:
                part = await asyncio.wait_for(reader.read(4096), timeout=0.4)
            except asyncio.TimeoutError:
                break
            if not part:
                break
            chunks.append(part)

        if not chunks:
            return ""

        data = b"".join(chunks)
        out = []
        i = 0
        while i + 4 <= len(data):
            size = int.from_bytes(data[i:i+4], "little", signed=True)
            i += 4
            if i + size > len(data) or size < 10:
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

def parse_listplayers(output: str):
    players = []
    if not output:
        return players

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue

        if ". " in line:
            line = line.split(". ", 1)[1]

        if "," in line:
            name = line.split(",", 1)[0].strip()
        else:
            name = line.strip()

        if name and name.lower() not in ("executing", "listplayers", "done"):
            players.append(name)

    return players

# =====================
# WEBHOOK HELPER
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

async def update_players_embed(session: aiohttp.ClientSession):
    online_nitrado, nitrado_count = await get_server_status(session)

    names = []
    rcon_ok = True
    rcon_err = None
    try:
        out = await rcon_command("ListPlayers", timeout=6.0)
        names = parse_listplayers(out)
    except Exception as e:
        rcon_ok = False
        rcon_err = str(e)

    online = online_nitrado or rcon_ok
    count = len(names) if names else nitrado_count
    emoji = "🟢" if online else "🔴"

    if names:
        lines = [f"{idx+1:02d}) {n}" for idx, n in enumerate(names[:50])]
        desc = f"**{count}/{PLAYER_CAP}** online\n\n" + "\n".join(lines)
    else:
        if not rcon_ok:
            desc = f"**{count}/{PLAYER_CAP}** online\n\n*(Could not fetch player names via RCON: {rcon_err})*"
        else:
            desc = f"**{count}/{PLAYER_CAP}** online\n\n*(No player list returned.)*"

    embed = {
        "title": "Online Players",
        "description": desc,
        "color": 0x2ECC71 if online else 0xE74C3C,
        "footer": {"text": f"Last update: {time.strftime('%H:%M:%S')}"}
    }

    await upsert_webhook(session, PLAYERS_WEBHOOK_URL, "players", embed)
    return emoji, count, online

# =====================
# GAMELOG SYNC HELPERS
# =====================
_DAYTIME_RE = re.compile(r"Day\s+(\d+),\s+(\d{1,2}):(\d{2}):(\d{2})\s*:")

def parse_latest_daytime_from_gamelog(text: str):
    """
    Returns (day, hour, minute, second) from most recent matching line, or None.
    """
    if not text:
        return None

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        m = _DAYTIME_RE.search(ln)
        if not m:
            continue
        return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))

    return None

async def fetch_latest_daytime_via_rcon() -> tuple[int, int, int, int] | None:
    """
    ASA needs page indices: GetGameLog 1 (newest), 2, 3...
    We scan the newest few pages and pick the latest parsable Day/time.
    """
    best = None
    for page in range(1, max(1, int(GAMELOG_PAGES_TO_SCAN)) + 1):
        text = await rcon_command(f"GetGameLog {page}", timeout=8.0)
        if not text:
            continue

        # ASA placeholder string (means you forgot the page, or server didn't return)
        if "Server received, But no response" in text:
            continue

        parsed = parse_latest_daytime_from_gamelog(text)
        if parsed:
            # Because we scan newest->older, the first hit is usually the best.
            best = parsed
            break

    return best

def minute_of_day_from_hm(hour: int, minute: int) -> int:
    return hour * 60 + minute

def clamp_minute_diff(diff: int) -> int:
    while diff > 720:
        diff -= 1440
    while diff < -720:
        diff += 1440
    return diff

def apply_gamelog_sync(parsed_day: int, parsed_hour: int, parsed_minute: int):
    """
    Adjust state['epoch'] so calculate_time_details() aligns with parsed in-game time NOW.
    """
    global state
    if not state:
        return False, "No state set"

    details = calculate_time_details()
    if not details:
        return False, "No calculated time"

    cur_minute_of_day, cur_day, cur_year, seconds_into_minute, cur_spm = details
    target_minute_of_day = minute_of_day_from_hm(parsed_hour, parsed_minute)

    # handle day wrap within a year
    day_diff = parsed_day - cur_day
    if day_diff > 180:
        day_diff -= 365
    elif day_diff < -180:
        day_diff += 365

    minute_diff = (day_diff * 1440) + (target_minute_of_day - cur_minute_of_day)
    minute_diff = clamp_minute_diff(minute_diff)

    if abs(minute_diff) < SYNC_DRIFT_MINUTES:
        return False, f"Drift {minute_diff} min < threshold"

    # shift epoch by the real seconds that correspond to that many in-game minutes
    real_seconds_shift = minute_diff * spm(cur_minute_of_day)
    state["epoch"] = float(state["epoch"]) - real_seconds_shift

    # keep displayed anchor consistent
    state["day"] = int(parsed_day)
    state["hour"] = int(parsed_hour)
    state["minute"] = int(parsed_minute)
    save_state(state)

    return True, f"Synced using GetGameLog (drift {minute_diff} min)"

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

            minute_of_day, day, year, seconds_into_minute, cur_spm = details

            if (minute_of_day % TIME_UPDATE_STEP_MINUTES) == 0:
                embed = build_time_embed(minute_of_day, day, year)
                await upsert_webhook(session, WEBHOOK_URL, "time", embed)

                absolute_day = year * 365 + day
                if last_announced_day is None:
                    last_announced_day = absolute_day
                elif absolute_day > last_announced_day:
                    ch = client.get_channel(ANNOUNCE_CHANNEL_ID)
                    if ch:
                        await ch.send(f"📅 **New Solunaris Day** — Day **{day}**, Year **{year}**")
                    last_announced_day = absolute_day

            sleep_for = seconds_until_next_round_step(
                minute_of_day, day, year, seconds_into_minute, TIME_UPDATE_STEP_MINUTES
            )
            await asyncio.sleep(sleep_for)

async def status_loop():
    global _last_vc_edit_ts, _last_vc_name
    await client.wait_until_ready()

    async with aiohttp.ClientSession() as session:
        while True:
            emoji, count, online = await update_players_embed(session)

            vc = client.get_channel(STATUS_VC_ID)
            if vc:
                new_name = f"{emoji} Solunaris | {count}/{PLAYER_CAP}"
                now = time.time()

                if new_name != _last_vc_name and (now - _last_vc_edit_ts) >= VC_EDIT_MIN_SECONDS:
                    try:
                        await vc.edit(name=new_name)
                        _last_vc_name = new_name
                        _last_vc_edit_ts = now
                    except discord.HTTPException:
                        pass

            await asyncio.sleep(STATUS_POLL_SECONDS)

_last_sync_ts = 0.0

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

            parsed = await fetch_latest_daytime_via_rcon()
            if not parsed:
                print("GameLog sync: no Day/time line found (scan pages 1..{})".format(GAMELOG_PAGES_TO_SCAN))
                await asyncio.sleep(GAMELOG_SYNC_SECONDS)
                continue

            d, h, m, s = parsed
            changed, msg = apply_gamelog_sync(d, h, m)
            print(f"GameLog sync: {msg}")

            if changed:
                _last_sync_ts = time.time()

        except Exception as e:
            print(f"GameLog sync error: {e}")

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
    state = {
        "epoch": time.time(),
        "year": int(year),
        "day": int(day),
        "hour": int(hour),
        "minute": int(minute),
    }
    save_state(state)
    await i.response.send_message("✅ Time set", ephemeral=True)

@tree.command(name="status", guild=discord.Object(id=GUILD_ID))
async def status(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        emoji, count, online = await update_players_embed(session)
    await i.followup.send(f"{emoji} **Solunaris** — {count}/{PLAYER_CAP} players", ephemeral=True)

@tree.command(name="sync", guild=discord.Object(id=GUILD_ID))
async def sync_time(i: discord.Interaction):
    """
    Manually force a GetGameLog sync now.
    """
    await i.response.defer(ephemeral=True)

    if not state:
        await i.followup.send("❌ Time not set yet. Use `/settime` first.", ephemeral=True)
        return

    try:
        parsed = await fetch_latest_daytime_via_rcon()
        if not parsed:
            await i.followup.send("❌ No Day/Time found in GetGameLog.", ephemeral=True)
            return

        d, h, m, s = parsed
        changed, msg = apply_gamelog_sync(d, h, m)
        if changed:
            global _last_sync_ts
            _last_sync_ts = time.time()
            await i.followup.send(f"✅ {msg} — now {h:02d}:{m:02d} Day {d}", ephemeral=True)
        else:
            await i.followup.send(f"ℹ️ {msg} — latest {h:02d}:{m:02d} Day {d}", ephemeral=True)

    except Exception as e:
        await i.followup.send(f"❌ Sync error: {e}", ephemeral=True)

@tree.command(name="debuggamelog", guild=discord.Object(id=GUILD_ID))
async def debuggamelog(i: discord.Interaction):
    """
    Shows first ~1200 chars of GetGameLog 1 so you can confirm output.
    """
    await i.response.defer(ephemeral=True)
    try:
        text = await rcon_command("GetGameLog 1", timeout=8.0)  # <-- FIX: page index required on ASA
        if not text:
            await i.followup.send("❌ Empty GetGameLog 1 response.", ephemeral=True)
            return
        snippet = text[:1200]
        await i.followup.send(f"```text\n{snippet}\n```", ephemeral=True)
    except Exception as e:
        await i.followup.send(f"❌ Debug error: {e}", ephemeral=True)

# =====================
# START
# =====================
@client.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    client.loop.create_task(time_loop())
    client.loop.create_task(status_loop())
    client.loop.create_task(gamelog_sync_loop())
    print("✅ Solunaris bot online")

client.run(DISCORD_TOKEN)