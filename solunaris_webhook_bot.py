import os
import time
import json
import asyncio
import aiohttp
import re

import discord
from discord import app_commands
from rcon.source import Client as RconClient

# =====================
# REQUIRED ENV VARS
# =====================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # main Solunaris Time webhook (edits one message)
PLAYERS_WEBHOOK_URL = os.getenv("PLAYERS_WEBHOOK_URL")  # online-players channel webhook (edits one message)

# Nitrado token is NOT required for option B (RCON list),
# but keep the env var check relaxed so you don't crash if missing.
NITRADO_TOKEN = os.getenv("NITRADO_TOKEN", "")

# =====================
# DISCORD CONFIG
# =====================
GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

# VC channel IDs (set in Railway)
TIME_VC_ID = int(os.getenv("TIME_VC_ID", "0"))          # optional: rename a VC to show time
STATUS_VC_ID = int(os.getenv("STATUS_VC_ID", "0"))      # required for server status VC rename

# Optional: announce new day in another channel via webhook
DAY_ANNOUNCE_WEBHOOK_URL = os.getenv("DAY_ANNOUNCE_WEBHOOK_URL", "")  # optional

PLAYER_CAP = 42

# =====================
# SERVER (for status + players)
# =====================
SERVER_NAME = "Solunaris"

# Query port you gave
QUERY_HOST = "31.214.239.2"
QUERY_PORT = 5020  # used only for reachability check (UDP query can be blocked; we do RCON anyway)

# RCON settings (must be set)
RCON_HOST = os.getenv("RCON_HOST", "31.214.239.2")
RCON_PORT = int(os.getenv("RCON_PORT", "11020"))
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

# =====================
# TIME CONFIG
# =====================
DAY_SECONDS_PER_INGAME_MINUTE = 4.7666667
NIGHT_SECONDS_PER_INGAME_MINUTE = 4.045  # your measured night minute

# Day = 05:30 to 17:30, Night = 17:30 to 05:30
DAY_START_MIN = 5 * 60 + 30     # 05:30
DAY_END_MIN = 17 * 60 + 30      # 17:30

DAY_COLOR = 0xF1C40F    # Yellow
NIGHT_COLOR = 0x5865F2  # Blue

STATE_FILE = "state.json"

# =====================
# VALIDATION
# =====================
missing = []
if not DISCORD_TOKEN:
    missing.append("DISCORD_TOKEN")
if not WEBHOOK_URL:
    missing.append("WEBHOOK_URL")
if not PLAYERS_WEBHOOK_URL:
    missing.append("PLAYERS_WEBHOOK_URL")
if not RCON_PASSWORD:
    missing.append("RCON_PASSWORD")

if missing:
    raise RuntimeError(f"Missing env var(s): {', '.join(missing)}")

# =====================
# DISCORD SETUP
# =====================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# =====================
# STATE HELPERS
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

# store the message IDs we edit (persisted in state file too)
webhook_message_id = None
players_webhook_message_id = None
last_announced_day_key = None

if state:
    webhook_message_id = state.get("webhook_message_id")
    players_webhook_message_id = state.get("players_webhook_message_id")
    last_announced_day_key = state.get("last_announced_day_key")

# =====================
# TIME CALCULATION (piecewise day/night)
# =====================
def is_day(minute_of_day: int) -> bool:
    # Day: [05:30, 17:30)
    if DAY_START_MIN <= DAY_END_MIN:
        return DAY_START_MIN <= minute_of_day < DAY_END_MIN
    # (not used here, but safe for wrap case)
    return minute_of_day >= DAY_START_MIN or minute_of_day < DAY_END_MIN

def spm_for(minute_of_day: int) -> float:
    return DAY_SECONDS_PER_INGAME_MINUTE if is_day(minute_of_day) else NIGHT_SECONDS_PER_INGAME_MINUTE

def next_boundary_total_minutes(day_num: int, minute_of_day: int) -> int:
    """
    Return absolute in-game minute index of next day/night boundary.
    day_num is 1-based.
    """
    total_now = (day_num - 1) * 1440 + minute_of_day

    if is_day(minute_of_day):
        # next boundary is day end (17:30) same day
        boundary = (day_num - 1) * 1440 + DAY_END_MIN
        if boundary <= total_now:
            boundary += 1440
        return boundary
    else:
        # next boundary is day start (05:30); may be next day if already past
        boundary = (day_num - 1) * 1440 + DAY_START_MIN
        if boundary <= total_now:
            boundary += 1440
        return boundary

def advance_minutes_piecewise(start_day: int, start_minute_of_day: int, elapsed_real_seconds: float):
    day = start_day
    minute_of_day = float(start_minute_of_day)
    remaining = float(elapsed_real_seconds)

    for _ in range(50000):
        if remaining <= 0:
            break

        minute_int = int(minute_of_day) % 1440
        spm = spm_for(minute_int)

        boundary_total = next_boundary_total_minutes(day, minute_int)
        current_total = (day - 1) * 1440 + minute_of_day
        minutes_to_boundary = boundary_total - current_total
        if minutes_to_boundary < 0:
            minutes_to_boundary = 0

        seconds_to_boundary = minutes_to_boundary * spm

        if seconds_to_boundary > 0 and remaining >= seconds_to_boundary:
            remaining -= seconds_to_boundary
            minute_of_day += minutes_to_boundary
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
      (display_title, color, current_spm, day_num, year_num, hour, minute, is_day_bool)
    """
    if not state:
        return None

    elapsed_real = time.time() - state["real_epoch"]
    start_day = int(state["day"])
    start_year = int(state["year"])
    start_minute = int(state["hour"]) * 60 + int(state["minute"])

    day_num, minute_of_day = advance_minutes_piecewise(start_day, start_minute, elapsed_real)

    # roll years every 365 days
    year = start_year
    while day_num > 365:
        day_num -= 365
        year += 1

    hour = minute_of_day // 60
    minute = minute_of_day % 60

    day_now = is_day(minute_of_day)
    emoji = "☀️" if day_now else "🌙"
    color = DAY_COLOR if day_now else NIGHT_COLOR
    current_spm = DAY_SECONDS_PER_INGAME_MINUTE if day_now else NIGHT_SECONDS_PER_INGAME_MINUTE

    title = f"{emoji} | Solunaris Time | {hour:02d}:{minute:02d} | Day {day_num} | Year {year}"
    return title, color, current_spm, day_num, year, hour, minute, day_now

# =====================
# RCON PLAYER LIST + STATUS
# =====================
EOS_RE = re.compile(r"\b\d{15,}\b")  # strips long numeric ids if present

def _clean_player_line(line: str) -> str:
    s = line.strip()
    s = EOS_RE.sub("", s).strip()
    s = s.replace("|", " ").replace(":", " ").strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"^\s*\d+\s*[\.\)]\s*", "", s).strip()
    return s

def rcon_list_players_sync(host: str, port: int, password: str, timeout: int = 4) -> list[str]:
    with RconClient(host, port, passwd=password, timeout=timeout) as rcon:
        raw = rcon.run("ListPlayers") or ""
        if not raw.strip():
            raw = rcon.run("listplayers") or ""

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    players = []
    for ln in lines:
        low = ln.lower()
        if "players" in low and "connected" in low:
            continue
        if low.startswith("there are") or low.startswith("no players"):
            continue
        cleaned = _clean_player_line(ln)
        if cleaned:
            players.append(cleaned)
    return players

async def get_players_via_rcon():
    try:
        players = await asyncio.to_thread(
            rcon_list_players_sync,
            RCON_HOST, RCON_PORT, RCON_PASSWORD
        )
        return True, players, ""
    except Exception as e:
        return False, [], f"{type(e).__name__}: {e}"

async def get_status_and_players():
    """
    Returns: (online_bool, players_count_int_or_None, players_list_or_empty, error_str)
    """
    ok, players, err = await get_players_via_rcon()
    if not ok:
        return False, None, [], err

    return True, len(players), players, ""

# =====================
# WEBHOOK HELPERS
# =====================
async def webhook_edit_or_send(session: aiohttp.ClientSession, webhook_url: str, message_id: str | None, payload: dict):
    """
    Edit existing webhook message, or create one and return its id.
    """
    if message_id:
        # edit
        async with session.patch(f"{webhook_url}/messages/{message_id}", json=payload) as resp:
            if resp.status == 404:
                # message deleted; create new
                message_id = None
            else:
                # even if non-200, don't crash loop
                return message_id

    # create
    async with session.post(webhook_url + "?wait=true", json=payload) as resp:
        data = await resp.json()
        return data.get("id")

# =====================
# VC RENAMING HELPERS
# =====================
async def rename_channel(channel_id: int, new_name: str):
    if not channel_id:
        return
    try:
        ch = client.get_channel(channel_id)
        if ch and ch.name != new_name:
            await ch.edit(name=new_name, reason="Solunaris bot update")
    except Exception:
        # ignore rename errors (missing perms etc.)
        pass

# =====================
# MAIN UPDATE LOOP
# =====================
async def update_loop():
    global webhook_message_id, players_webhook_message_id, last_announced_day_key

    await client.wait_until_ready()
    async with aiohttp.ClientSession() as session:
        last_force_status = 0.0
        last_status_snapshot = None  # (online, count)

        while True:
            # ----- TIME EMBED UPDATE -----
            if state:
                t = calculate_time()
                if t:
                    title, color, current_spm, day_num, year, hour, minute, day_now = t

                    embed = {
                        "title": title,
                        "color": color,
                    }

                    webhook_message_id = await webhook_edit_or_send(
                        session,
                        WEBHOOK_URL,
                        webhook_message_id,
                        {"embeds": [embed]},
                    )

                    # persist ids
                    state["webhook_message_id"] = webhook_message_id
                    save_state(state)

                    # optional: rename time VC too (emoji at start)
                    if TIME_VC_ID:
                        await rename_channel(TIME_VC_ID, title)

                    # day-change announcement
                    day_key = f"{year}-{day_num}"
                    if DAY_ANNOUNCE_WEBHOOK_URL and last_announced_day_key != day_key:
                        # only announce when we cross into a new day
                        if last_announced_day_key is not None:
                            ann_embed = {
                                "title": f"🌅 New Day Started — Day {day_num} | Year {year}",
                                "color": DAY_COLOR,
                            }
                            await webhook_edit_or_send(
                                session,
                                DAY_ANNOUNCE_WEBHOOK_URL,
                                None,  # always post a new announcement message
                                {"embeds": [ann_embed]},
                            )
                        last_announced_day_key = day_key
                        state["last_announced_day_key"] = last_announced_day_key
                        save_state(state)

                    # sleep scales to in-game minute length
                    sleep_for = float(current_spm)
                else:
                    sleep_for = DAY_SECONDS_PER_INGAME_MINUTE
            else:
                sleep_for = DAY_SECONDS_PER_INGAME_MINUTE

            # ----- STATUS + PLAYERS CHECK (poll every 15s, update on change, force every 10 mins) -----
            now = time.time()
            if now - last_force_status >= 600 or last_status_snapshot is None:
                force = True
            else:
                force = False

            # every loop also checks, but we only update if needed
            online, count, players, err = await get_status_and_players()

            snapshot = (online, count if count is not None else -1)

            changed = (snapshot != last_status_snapshot)
            should_update = force or changed

            if should_update:
                # status vc rename
                if STATUS_VC_ID:
                    if online:
                        vc_name = f"🟢 {SERVER_NAME} | {count}/{PLAYER_CAP}"
                    else:
                        vc_name = f"🔴 {SERVER_NAME} | ?/{PLAYER_CAP}"
                    await rename_channel(STATUS_VC_ID, vc_name)

                # players webhook (edits one message)
                if online:
                    header = f"{SERVER_NAME}\nPlayers: {count}/{PLAYER_CAP}"
                else:
                    header = f"{SERVER_NAME}\nPlayers: ?/{PLAYER_CAP}\n({err})"

                if online and players:
                    shown = players[:40]
                    lines = [f"{i+1:02d}) {p}" for i, p in enumerate(shown)]
                    desc = "\n".join(lines)
                    if len(players) > len(shown):
                        desc += f"\n… +{len(players) - len(shown)} more"
                else:
                    desc = "(No players online.)" if online else "(Player list unavailable.)"

                players_embed = {
                    "title": "Online Players",
                    "description": f"**{header}**\n\n{desc}",
                    "color": 0x2ECC71 if online else 0xE74C3C,
                }

                players_webhook_message_id = await webhook_edit_or_send(
                    session,
                    PLAYERS_WEBHOOK_URL,
                    players_webhook_message_id,
                    {"embeds": [players_embed]},
                )

                # persist ids
                if state is None:
                    # time isn't set yet, but we still want to persist webhook ids
                    tmp = load_state() or {}
                    tmp["players_webhook_message_id"] = players_webhook_message_id
                    save_state(tmp)
                else:
                    state["players_webhook_message_id"] = players_webhook_message_id
                    save_state(state)

                last_status_snapshot = snapshot
                last_force_status = now

            # poll status every 15 seconds, but time loop sleeps by current_spm
            # so we sleep the smaller of them to keep both responsive
            await asyncio.sleep(min(sleep_for, 15.0))

# =====================
# SLASH COMMANDS
# =====================
@tree.command(
    name="day",
    description="Show current Solunaris time",
    guild=discord.Object(id=GUILD_ID),
)
async def day_cmd(interaction: discord.Interaction):
    if not state:
        await interaction.response.send_message("⏳ Time not set yet. Use /settime.", ephemeral=True)
        return
    t = calculate_time()
    if not t:
        await interaction.response.send_message("⏳ Time not available.", ephemeral=True)
        return
    title = t[0]
    await interaction.response.send_message(title, ephemeral=True)

@tree.command(
    name="settime",
    description="Set Solunaris time (admin role required)",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(
    year="Year number",
    day="Day of year (1–365)",
    hour="Hour (0–23)",
    minute="Minute (0–59)",
)
async def settime_cmd(interaction: discord.Interaction, year: int, day: int, hour: int, minute: int):
    # role gate
    if not getattr(interaction.user, "roles", None) or not any(r.id == ADMIN_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message("❌ You don't have permission to use this.", ephemeral=True)
        return

    if year < 1 or day < 1 or day > 365 or hour < 0 or hour > 23 or minute < 0 or minute > 59:
        await interaction.response.send_message("❌ Invalid values.", ephemeral=True)
        return

    global state, last_announced_day_key
    state = {
        "real_epoch": time.time(),
        "year": year,
        "day": day,
        "hour": hour,
        "minute": minute,
        "webhook_message_id": webhook_message_id,
        "players_webhook_message_id": players_webhook_message_id,
        "last_announced_day_key": last_announced_day_key,
    }
    save_state(state)

    await interaction.response.send_message(
        f"✅ Set to Day {day}, {hour:02d}:{minute:02d}, Year {year}",
        ephemeral=True,
    )

@tree.command(
    name="status",
    description="Show server status and players online",
    guild=discord.Object(id=GUILD_ID),
)
async def status_cmd(interaction: discord.Interaction):
    # ALWAYS respond quickly to avoid "application did not respond"
    await interaction.response.defer(ephemeral=True, thinking=True)

    online, count, players, err = await get_status_and_players()
    if online:
        msg = f"🟢 **{SERVER_NAME} is ONLINE** — Players: **{count}/{PLAYER_CAP}**"
    else:
        msg = f"🔴 **{SERVER_NAME} is OFFLINE** — Players: **?/{PLAYER_CAP}**\n({err})"

    await interaction.followup.send(msg, ephemeral=True)

# =====================
# STARTUP
# =====================
@client.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print("✅ Commands synced: /day /settime /status")
    client.loop.create_task(update_loop())

client.run(DISCORD_TOKEN)