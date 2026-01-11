import os
import time
import json
import asyncio
import aiohttp
import discord
from discord import app_commands
import re
from typing import Optional, Tuple

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
    missing = []
    for k in [
        "DISCORD_TOKEN",
        "WEBHOOK_URL",
        "PLAYERS_WEBHOOK_URL",
        "NITRADO_TOKEN",
        "NITRADO_SERVICE_ID",
        "RCON_HOST",
        "RCON_PORT",
        "RCON_PASSWORD",
    ]:
        if not os.getenv(k):
            missing.append(k)
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

# Your current model (leave as-is)
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
      minute_of_day (0..1439),
      day,
      year,
      seconds_into_current_minute (real seconds),
      current_minute_spm (real seconds per in-game minute)
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
# RCON (robust)
# =====================
SERVERDATA_AUTH = 3
SERVERDATA_AUTH_RESPONSE = 2
SERVERDATA_EXECCOMMAND = 2
SERVERDATA_RESPONSE_VALUE = 0

def _rcon_packet(req_id: int, ptype: int, body: bytes) -> bytes:
    # body must already be bytes (we control encoding upstream)
    payload = (
        req_id.to_bytes(4, "little", signed=True) +
        ptype.to_bytes(4, "little", signed=True) +
        body + b"\x00" + b"\x00"
    )
    size = len(payload)
    return size.to_bytes(4, "little", signed=True) + payload

def _decode_rcon_text(b: bytes) -> str:
    # Try to preserve special characters better than utf-8 ignore
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return b.decode(enc)
        except Exception:
            continue
    return b.decode("utf-8", errors="replace")

async def _rcon_read_packet(reader: asyncio.StreamReader, timeout: float) -> Optional[Tuple[int, int, bytes]]:
    # Returns (req_id, ptype, body_bytes) or None
    try:
        size_b = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
    except Exception:
        return None

    size = int.from_bytes(size_b, "little", signed=True)
    if size < 10 or size > 10_000_000:
        return None

    try:
        pkt = await asyncio.wait_for(reader.readexactly(size), timeout=timeout)
    except Exception:
        return None

    req_id = int.from_bytes(pkt[0:4], "little", signed=True)
    ptype = int.from_bytes(pkt[4:8], "little", signed=True)
    body = pkt[8:-2]  # strip 2 nulls
    return req_id, ptype, body

async def rcon_command(command: str, timeout: float = 10.0) -> str:
    """
    Reliable Source RCON:
    - Auth
    - Exec command
    - Send an empty exec as terminator
    - Read packets until we see terminator response or timeout
    """
    reader, writer = await asyncio.wait_for(asyncio.open_connection(RCON_HOST, RCON_PORT), timeout=timeout)
    try:
        # AUTH
        writer.write(_rcon_packet(1, SERVERDATA_AUTH, RCON_PASSWORD.encode("utf-8")))
        await writer.drain()

        auth_ok = False
        auth_deadline = time.time() + timeout
        while time.time() < auth_deadline:
            pkt = await _rcon_read_packet(reader, timeout=timeout)
            if not pkt:
                break
            req_id, ptype, body = pkt
            if ptype == SERVERDATA_AUTH_RESPONSE:
                if req_id == -1:
                    raise RuntimeError("RCON auth failed")
                auth_ok = True
                break
        if not auth_ok:
            raise RuntimeError("RCON auth: no response")

        # EXEC
        writer.write(_rcon_packet(2, SERVERDATA_EXECCOMMAND, command.encode("utf-8")))
        await writer.drain()

        # TERMINATOR (forces server to flush multi-packet responses)
        writer.write(_rcon_packet(3, SERVERDATA_EXECCOMMAND, b""))
        await writer.drain()

        chunks: list[bytes] = []
        deadline = time.time() + timeout

        while time.time() < deadline:
            pkt = await _rcon_read_packet(reader, timeout=0.6)
            if not pkt:
                break
            req_id, ptype, body = pkt
            if ptype != SERVERDATA_RESPONSE_VALUE:
                continue

            # terminator response is usually req_id == 3 and empty body
            if req_id == 3 and (body is None or len(body) == 0):
                break

            if body:
                chunks.append(body)

        if not chunks:
            return ""

        return _decode_rcon_text(b"".join(chunks)).strip()

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
        out = await rcon_command("ListPlayers", timeout=10.0)
        names = parse_listplayers(out)
    except Exception as e:
        rcon_ok = False
        rcon_err = str(e)

    online = online_nitrado or rcon_ok
    count = len(names) if names else nitrado_count
    emoji = "🟢" if online else "🔴"

    if names:
        lines = [f"{idx+1:02d}) {n}" for idx, n in enumerate(names[:50])]
        player_list_text = "\n".join(lines)
        desc = f"**{count}/{PLAYER_CAP}** online\n\n{player_list_text}"
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
# Supports BOTH:
#   Day 233, 17:45:33:
#   Day 233, 17:45:33 -
_DAYTIME_RE = re.compile(r"Day\s+(\d+),\s*(\d{1,2}):(\d{2}):(\d{2})\s*[:\-]")

def parse_latest_daytime_from_gamelog(text: str) -> Optional[Tuple[int, int, int, int]]:
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        m = _DAYTIME_RE.search(ln)
        if not m:
            continue
        day = int(m.group(1))
        hour = int(m.group(2))
        minute = int(m.group(3))
        second = int(m.group(4))
        return day, hour, minute, second
    return None

def minute_of_day_from_hm(hour: int, minute: int) -> int:
    return hour * 60 + minute

def clamp_minutes(diff: int) -> int:
    while diff > 720:
        diff -= 1440
    while diff < -720:
        diff += 1440
    return diff

def real_seconds_for_minute_delta(start_minute: int, delta_minutes: int) -> float:
    """
    Convert an in-game minute delta into real seconds according to your day/night SPM model.
    We step minute-by-minute so crossing sunrise/sunset stays accurate.
    """
    if delta_minutes == 0:
        return 0.0
    sign = 1 if delta_minutes > 0 else -1
    steps = abs(delta_minutes)
    total = 0.0
    m = start_minute
    d = 0
    y = 0
    for _ in range(steps):
        total += spm(m)
        # move 1 minute in the direction of delta
        if sign > 0:
            m, d, y = _advance_one_minute(m, d, y)
        else:
            m -= 1
            if m < 0:
                m = 1439
    return total * sign

def apply_gamelog_sync(parsed_day: int, parsed_hour: int, parsed_minute: int, parsed_second: int):
    """
    Adjust state['epoch'] so that NOW aligns with the parsed in-game time.
    Uses seconds to tighten alignment.
    """
    global state
    if not state:
        return False, "No state set"

    details = calculate_time_details()
    if not details:
        return False, "No calculated time details"

    cur_mod, cur_day, cur_year, seconds_into_minute, cur_spm = details
    target_mod = minute_of_day_from_hm(parsed_hour, parsed_minute)

    # day diff within same year (you’re not using year rollover from logs)
    day_diff = parsed_day - cur_day
    if day_diff > 180:
        day_diff -= 365
    elif day_diff < -180:
        day_diff += 365

    minute_diff = (day_diff * 1440) + (target_mod - cur_mod)
    minute_diff = clamp_minutes(minute_diff)

    # If minute drift small, still allow seconds-level correction, but only if >= threshold minutes
    if abs(minute_diff) < SYNC_DRIFT_MINUTES:
        return False, f"Drift {minute_diff} min < threshold"

    # Convert minute drift to real seconds properly across sunrise/sunset
    shift_seconds = real_seconds_for_minute_delta(cur_mod, minute_diff)

    # Now add a seconds alignment within the minute.
    # Convert parsed in-game seconds (0..59) into real seconds into minute based on current minute spm.
    desired_seconds_into_minute = (parsed_second / 60.0) * spm(target_mod)

    # After shifting epoch for minute_diff, we want current seconds_into_minute to match desired.
    # We adjust epoch further by the delta between our current seconds_into_minute and desired.
    seconds_delta = seconds_into_minute - desired_seconds_into_minute
    # Positive seconds_delta means we are "ahead" inside the minute -> move epoch forward a bit
    fine_adjust = seconds_delta

    state["epoch"] = float(state["epoch"]) - shift_seconds + fine_adjust

    # Store the new anchor time (keeping your existing structure)
    state["day"] = int(parsed_day)
    state["hour"] = int(parsed_hour)
    state["minute"] = int(parsed_minute)
    save_state(state)

    return True, f"Synced using GetGameLog (minute drift {minute_diff}m)"

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

async def try_sync_once() -> Tuple[bool, str]:
    global _last_sync_ts

    if not state:
        return False, "No state set (use /settime first)."

    now = time.time()
    if (now - _last_sync_ts) < SYNC_COOLDOWN_SECONDS:
        remaining = int(SYNC_COOLDOWN_SECONDS - (now - _last_sync_ts))
        return False, f"Sync cooldown active ({remaining}s remaining)."

    log_text = await rcon_command("GetGameLog", timeout=15.0)
    if not log_text:
        return False, "GetGameLog returned empty output."

    parsed = parse_latest_daytime_from_gamelog(log_text)
    if not parsed:
        return False, "No Day/Time found in GetGameLog."

    d, h, m, s = parsed
    changed, msg = apply_gamelog_sync(d, h, m, s)
    if changed:
        _last_sync_ts = time.time()
    return changed, msg

async def gamelog_sync_loop():
    await client.wait_until_ready()

    while True:
        try:
            changed, msg = await try_sync_once()
            # Don’t spam logs; only print if it actually syncs or if there's a real error.
            if changed:
                print("GameLog sync:", msg)
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
async def sync_cmd(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    try:
        changed, msg = await try_sync_once()
        if changed:
            await i.followup.send(f"✅ {msg}", ephemeral=True)
        else:
            await i.followup.send(f"ℹ️ {msg}", ephemeral=True)
    except Exception as e:
        await i.followup.send(f"❌ Sync error: {e}", ephemeral=True)

@tree.command(name="debuggamelog", guild=discord.Object(id=GUILD_ID))
async def debuggamelog(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    try:
        text = await rcon_command("GetGameLog", timeout=15.0)
        if not text:
            await i.followup.send("❌ GetGameLog returned empty output.", ephemeral=True)
            return
        lines = [ln for ln in text.splitlines() if ln.strip()]
        snippet = "\n".join(lines[-25:]) if lines else text[:1500]
        if len(snippet) > 1800:
            snippet = snippet[-1800:]
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