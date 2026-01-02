import os
import time
import json
import asyncio
import aiohttp
import re

import discord
from discord import app_commands
from rcon.source import Client as RconClient

# =========================================================
# ENV VARS (Railway Variables)
# =========================================================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Webhooks (these edit a single message each)
TIME_WEBHOOK_URL = os.getenv("WEBHOOK_URL")                  # time embed webhook
PLAYERS_WEBHOOK_URL = os.getenv("PLAYERS_WEBHOOK_URL")       # online players embed webhook
DAY_ANNOUNCE_WEBHOOK_URL = os.getenv("DAY_ANNOUNCE_WEBHOOK_URL", "")  # optional (posts new message each day)

# Guild / Admin role
GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

# VC IDs (set these in Railway if you want channel renames)
TIME_VC_ID = int(os.getenv("TIME_VC_ID", "0"))       # optional time VC rename
STATUS_VC_ID = int(os.getenv("STATUS_VC_ID", "0"))   # status VC rename (recommended)

# Server status config
SERVER_NAME = "Solunaris"
PLAYER_CAP = 42

# RCON (required for players list + accurate online)
RCON_HOST = os.getenv("RCON_HOST", "31.214.239.2")
RCON_PORT = int(os.getenv("RCON_PORT", "11020"))
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

# =========================================================
# TIME CONFIG
# =========================================================
# Real seconds per in-game minute
DAY_SECONDS_PER_INGAME_MINUTE = 4.7666667
NIGHT_SECONDS_PER_INGAME_MINUTE = 4.045

# Day/night schedule (your latest values)
# Day: 05:30 -> 17:30
# Night: 17:30 -> 05:30
DAY_START_MIN = 5 * 60 + 30
DAY_END_MIN = 17 * 60 + 30

DAY_COLOR = 0xF1C40F    # Yellow
NIGHT_COLOR = 0x5865F2  # Blue

STATE_FILE = "state.json"

# =========================================================
# REQUIRED VAR CHECK (only hard-required)
# =========================================================
missing = []
if not DISCORD_TOKEN:
    missing.append("DISCORD_TOKEN")
if not TIME_WEBHOOK_URL:
    missing.append("WEBHOOK_URL")
if not PLAYERS_WEBHOOK_URL:
    missing.append("PLAYERS_WEBHOOK_URL")
if not RCON_PASSWORD:
    missing.append("RCON_PASSWORD")

if missing:
    raise RuntimeError(f"Missing env var(s): {', '.join(missing)}")

# =========================================================
# DISCORD SETUP
# =========================================================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# =========================================================
# STATE HELPERS
# =========================================================
def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return None

def save_state(data: dict):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass

state = load_state() or {}

time_webhook_message_id = state.get("time_webhook_message_id")
players_webhook_message_id = state.get("players_webhook_message_id")
last_announced_day_key = state.get("last_announced_day_key")

# =========================================================
# TIME CALC (piecewise day/night)
# =========================================================
def is_day(minute_of_day: int) -> bool:
    # Day: [05:30, 17:30)
    return DAY_START_MIN <= minute_of_day < DAY_END_MIN

def spm_for(minute_of_day: int) -> float:
    return DAY_SECONDS_PER_INGAME_MINUTE if is_day(minute_of_day) else NIGHT_SECONDS_PER_INGAME_MINUTE

def next_boundary_total_minutes(day_num: int, minute_of_day: int) -> int:
    """
    Return the absolute in-game minute index of the next boundary (sunrise/sunset).
    day_num is 1-based.
    """
    total_now = (day_num - 1) * 1440 + minute_of_day

    if is_day(minute_of_day):
        # next boundary = day end (17:30) same day
        boundary = (day_num - 1) * 1440 + DAY_END_MIN
        if boundary <= total_now:
            boundary += 1440
        return boundary
    else:
        # next boundary = day start (05:30) next occurrence
        boundary = (day_num - 1) * 1440 + DAY_START_MIN
        if boundary <= total_now:
            boundary += 1440
        return boundary

def advance_minutes_piecewise(start_day: int, start_minute_of_day: int, elapsed_real_seconds: float):
    """
    Advances time using different seconds-per-in-game-minute for day vs night.
    """
    day = int(start_day)
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
            minute_of_day += (remaining / spm) if spm > 0 else 0
            remaining = 0

        while minute_of_day >= 1440:
            minute_of_day -= 1440
            day += 1

    return day, int(minute_of_day) % 1440

def calculate_time():
    """
    Returns:
      (title, color, current_spm, day_num, year_num, hour, minute, day_now)
    """
    if "real_epoch" not in state:
        return None

    elapsed_real = time.time() - float(state["real_epoch"])

    start_year = int(state.get("year", 1))
    start_day = int(state.get("day", 1))
    start_hour = int(state.get("hour", 0))
    start_minute = int(state.get("minute", 0))

    start_minute_of_day = start_hour * 60 + start_minute

    day_num, minute_of_day = advance_minutes_piecewise(start_day, start_minute_of_day, elapsed_real)

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

# =========================================================
# RCON: PLAYERS LIST (character names only)
# =========================================================
EOS_RE = re.compile(r"\b\d{15,}\b")

def _clean_player_line(line: str) -> str:
    """
    Converts RCON output lines to just a readable name.
    Removes EOS-style long numeric IDs if present.
    """
    s = line.strip()
    if not s:
        return ""

    # remove long numeric ids
    s = EOS_RE.sub("", s).strip()

    # remove common prefixes like "1. " or "01) "
    s = re.sub(r"^\s*\d+\s*[\.\)]\s*", "", s).strip()

    # collapse whitespace
    s = re.sub(r"\s+", " ", s).strip()
    return s

def rcon_list_players_sync(host: str, port: int, password: str, timeout: int = 5) -> list[str]:
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

async def get_status_and_players():
    """
    Returns (online, count_or_None, players_list, error_string)
    """
    try:
        players = await asyncio.to_thread(
            rcon_list_players_sync, RCON_HOST, RCON_PORT, RCON_PASSWORD
        )
        return True, len(players), players, ""
    except Exception as e:
        return False, None, [], f"{type(e).__name__}: {e}"

# =========================================================
# WEBHOOK HELPER (edit-or-send one message)
# =========================================================
async def webhook_edit_or_send(session: aiohttp.ClientSession, webhook_url: str, message_id: str | None, payload: dict):
    """
    Edit an existing webhook message by ID.
    If it's missing (deleted/unknown), create a new message and return its ID.
    """
    if message_id:
        try:
            async with session.patch(f"{webhook_url}/messages/{message_id}", json=payload) as resp:
                if resp.status == 200:
                    return message_id
                if resp.status == 404:
                    message_id = None
        except Exception:
            # fall back to sending
            message_id = None

    # create message
    try:
        async with session.post(webhook_url + "?wait=true", json=payload) as resp:
            data = await resp.json()
            return data.get("id")
    except Exception:
        return message_id

# =========================================================
# CHANNEL RENAME HELPER
# =========================================================
async def rename_channel(channel_id: int, new_name: str):
    if not channel_id:
        return
    try:
        ch = client.get_channel(channel_id)
        if ch and getattr(ch, "name", "") != new_name:
            await ch.edit(name=new_name, reason="Solunaris bot update")
    except Exception:
        pass

# =========================================================
# MAIN LOOP
# - Time embed updates every in-game minute length
# - Status/players check every 15s; update only on change; force update every 10m
# =========================================================
async def update_loop():
    global time_webhook_message_id, players_webhook_message_id, last_announced_day_key

    await client.wait_until_ready()

    last_force_status = 0.0
    last_status_snapshot = None  # (online, count)

    async with aiohttp.ClientSession() as session:
        while True:
            # ---------------------------
            # TIME UPDATE
            # ---------------------------
            sleep_for = 15.0  # default
            t = calculate_time()
            if t:
                title, color, current_spm, day_num, year, hour, minute, day_now = t
                sleep_for = float(current_spm)

                time_embed = {"title": title, "color": color}

                time_webhook_message_id = await webhook_edit_or_send(
                    session,
                    TIME_WEBHOOK_URL,
                    time_webhook_message_id,
                    {"embeds": [time_embed]},
                )

                # Persist IDs
                state["time_webhook_message_id"] = time_webhook_message_id
                save_state(state)

                # Optional VC rename for time
                if TIME_VC_ID:
                    await rename_channel(TIME_VC_ID, title)

                # Optional new day announcement
                day_key = f"{year}-{day_num}"
                if DAY_ANNOUNCE_WEBHOOK_URL and last_announced_day_key != day_key:
                    # Don't announce the very first time we ever run
                    if last_announced_day_key is not None:
                        ann_embed = {
                            "title": f"🌅 New Day Started — Day {day_num} | Year {year}",
                            "color": DAY_COLOR,
                        }
                        # post a new message (no edit)
                        await webhook_edit_or_send(session, DAY_ANNOUNCE_WEBHOOK_URL, None, {"embeds": [ann_embed]})

                    last_announced_day_key = day_key
                    state["last_announced_day_key"] = last_announced_day_key
                    save_state(state)

            # ---------------------------
            # STATUS/PLAYERS UPDATE
            # ---------------------------
            now = time.time()
            force = (now - last_force_status >= 600) or (last_status_snapshot is None)

            online, count, players, err = await get_status_and_players()
            snapshot = (online, count if count is not None else -1)
            changed = (snapshot != last_status_snapshot)
            should_update = force or changed

            if should_update:
                # Status VC rename
                if STATUS_VC_ID:
                    if online:
                        vc_name = f"🟢 {SERVER_NAME} | {count}/{PLAYER_CAP}"
                    else:
                        vc_name = f"🔴 {SERVER_NAME} | ?/{PLAYER_CAP}"
                    await rename_channel(STATUS_VC_ID, vc_name)

                # Players embed (character name only)
                if online:
                    header = f"{SERVER_NAME}\nPlayers: {count}/{PLAYER_CAP}"
                else:
                    header = f"{SERVER_NAME}\nPlayers: ?/{PLAYER_CAP}\n({err})"

                if online:
                    if players:
                        shown = players[:40]
                        lines = [f"{i+1:02d}) {p}" for i, p in enumerate(shown)]
                        body = "\n".join(lines)
                        if len(players) > len(shown):
                            body += f"\n… +{len(players) - len(shown)} more"
                    else:
                        body = "(No players online.)"
                else:
                    body = "(Player list unavailable.)"

                players_embed = {
                    "title": "Online Players",
                    "description": f"**{header}**\n\n{body}",
                    "color": 0x2ECC71 if online else 0xE74C3C,
                }

                players_webhook_message_id = await webhook_edit_or_send(
                    session,
                    PLAYERS_WEBHOOK_URL,
                    players_webhook_message_id,
                    {"embeds": [players_embed]},
                )

                state["players_webhook_message_id"] = players_webhook_message_id
                save_state(state)

                last_status_snapshot = snapshot
                last_force_status = now

            # ---------------------------
            # Sleep: keep status polls responsive
            # ---------------------------
            await asyncio.sleep(min(sleep_for, 15.0))

# =========================================================
# SLASH COMMANDS
# =========================================================
@tree.command(
    name="day",
    description="Show current Solunaris time",
    guild=discord.Object(id=GUILD_ID),
)
async def day_cmd(interaction: discord.Interaction):
    if "real_epoch" not in state:
        await interaction.response.send_message("⏳ Time not set yet. Use /settime.", ephemeral=True)
        return

    t = calculate_time()
    if not t:
        await interaction.response.send_message("⏳ Time not available.", ephemeral=True)
        return

    await interaction.response.send_message(t[0], ephemeral=True)

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
    # Role gate
    roles = getattr(interaction.user, "roles", [])
    if not any(getattr(r, "id", 0) == ADMIN_ROLE_ID for r in roles):
        await interaction.response.send_message("❌ You don't have permission to use this.", ephemeral=True)
        return

    if year < 1 or day < 1 or day > 365 or hour < 0 or hour > 23 or minute < 0 or minute > 59:
        await interaction.response.send_message("❌ Invalid values.", ephemeral=True)
        return

    # Save calibration
    state["real_epoch"] = time.time()
    state["year"] = year
    state["day"] = day
    state["hour"] = hour
    state["minute"] = minute

    # keep ids if already created
    state["time_webhook_message_id"] = state.get("time_webhook_message_id", time_webhook_message_id)
    state["players_webhook_message_id"] = state.get("players_webhook_message_id", players_webhook_message_id)
    state["last_announced_day_key"] = state.get("last_announced_day_key", last_announced_day_key)

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
    # Prevent "application did not respond"
    await interaction.response.defer(ephemeral=True, thinking=True)

    online, count, players, err = await get_status_and_players()
    if online:
        msg = f"🟢 **{SERVER_NAME} is ONLINE** — Players: **{count}/{PLAYER_CAP}**"
    else:
        msg = f"🔴 **{SERVER_NAME} is OFFLINE** — Players: **?/{PLAYER_CAP}**\n({err})"

    await interaction.followup.send(msg, ephemeral=True)

# =========================================================
# STARTUP
# =========================================================
@client.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print("✅ Commands synced: /day /settime /status")
    client.loop.create_task(update_loop())

client.run(DISCORD_TOKEN)