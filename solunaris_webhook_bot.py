"""
Solunaris Time + Server Status bot (copy/paste)

What it does:
- Keeps ONE Solunaris Time embed updated via a webhook (edits, never spams new messages)
- /day shows the current Solunaris time
- /settime (role-gated) calibrates Solunaris time (year/day/hour/minute)
- Posts a message in an announcement channel at the start of each NEW in-game day
- Creates/updates ONE “online players” embed via a webhook (edits, never spams new messages)
- Creates/updates a STATUS VOICE CHANNEL name: 🟢 Solunaris | x/42  (or 🔴 Solunaris | 0/42)
- /status shows status + players and also forces an update of the online-players webhook

IMPORTANT for Railway/Python:
- Use Python 3.12 (NOT 3.13) to avoid: ModuleNotFoundError: audioop
  In Railway, set a runtime or add a nixpacks config; easiest is to set:
  "PYTHON_VERSION=3.12"

ENV VARS you must set:
- DISCORD_TOKEN                 (your bot token)
- WEBHOOK_URL                   (Solunaris Time webhook URL)
- PLAYERS_WEBHOOK_URL           (online players webhook URL)
- NITRADO_TOKEN                 (Nitrado API token - long-lived)
- NITRADO_SERVICE_ID            (e.g. 17997739)

Optional ENV VARS:
- STATUS_SOURCE                 "nitrado" (default) or "a2s" (not recommended)

Hardcoded IDs (you provided):
- GUILD_ID                      1430388266393276509
- ADMIN_ROLE_ID                 1439069787207766076
- STATUS_VC_ID                  1456615806887657606
- ANNOUNCE_CHANNEL_ID           1430388267446042666

NOTE:
- If you want the “Solunaris Time” webhook message to be a specific message you already posted,
  you can set TIME_WEBHOOK_MESSAGE_ID in env and it will edit that message.
  Same for PLAYERS_WEBHOOK_MESSAGE_ID.
"""

import os
import time
import json
import asyncio
import aiohttp
import discord
from discord import app_commands

# =====================
# CONFIG
# =====================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # Solunaris time webhook
PLAYERS_WEBHOOK_URL = os.getenv("PLAYERS_WEBHOOK_URL")  # Online players webhook

NITRADO_TOKEN = os.getenv("NITRADO_TOKEN")
NITRADO_SERVICE_ID = os.getenv("NITRADO_SERVICE_ID")  # e.g. "17997739"

STATUS_SOURCE = os.getenv("STATUS_SOURCE", "nitrado").lower()  # "nitrado" recommended

GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

STATUS_VC_ID = 1456615806887657606
ANNOUNCE_CHANNEL_ID = 1430388267446042666

PLAYER_CAP = 42

# Day/night minute lengths (your measured values)
DAY_SECONDS_PER_INGAME_MINUTE = 4.7666667
NIGHT_SECONDS_PER_INGAME_MINUTE = 4.045

# Day is 05:30 -> 17:30, Night is 17:30 -> 05:30
SUNRISE_MIN = 5 * 60 + 30   # 05:30
SUNSET_MIN = 17 * 60 + 30   # 17:30

DAY_COLOR = 0xF1C40F    # Yellow
NIGHT_COLOR = 0x5865F2  # Blue

STATE_FILE = "state.json"

TIME_WEBHOOK_MESSAGE_ID = os.getenv("TIME_WEBHOOK_MESSAGE_ID")  # optional
PLAYERS_WEBHOOK_MESSAGE_ID = os.getenv("PLAYERS_WEBHOOK_MESSAGE_ID")  # optional

missing = []
for k, v in [
    ("DISCORD_TOKEN", DISCORD_TOKEN),
    ("WEBHOOK_URL", WEBHOOK_URL),
    ("PLAYERS_WEBHOOK_URL", PLAYERS_WEBHOOK_URL),
    ("NITRADO_TOKEN", NITRADO_TOKEN),
    ("NITRADO_SERVICE_ID", NITRADO_SERVICE_ID),
]:
    if not v:
        missing.append(k)

if missing:
    raise RuntimeError(f"Missing env var(s): {', '.join(missing)}")

# =====================
# DISCORD SETUP
# =====================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# =====================
# STATE (time calibration)
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

time_webhook_message_id = str(TIME_WEBHOOK_MESSAGE_ID) if TIME_WEBHOOK_MESSAGE_ID else None
players_webhook_message_id = str(PLAYERS_WEBHOOK_MESSAGE_ID) if PLAYERS_WEBHOOK_MESSAGE_ID else None

last_announced_day_global = None  # remembers last announced absolute day index (year*365 + day)

# =====================
# SOLUNARIS TIME CALCULATION (smooth day/night switching)
# =====================
def is_day_by_minute(minute_of_day: int) -> bool:
    # Day spans sunrise..sunset, and sunset..sunrise is night
    return SUNRISE_MIN <= minute_of_day < SUNSET_MIN

def seconds_per_minute_for(minute_of_day: int) -> float:
    return DAY_SECONDS_PER_INGAME_MINUTE if is_day_by_minute(minute_of_day) else NIGHT_SECONDS_PER_INGAME_MINUTE

def advance_minutes_piecewise(start_day: int, start_minute_of_day: int, elapsed_real_seconds: float):
    """
    Advances in-game time using different real-seconds-per-in-game-minute for day vs night.
    Smooth at sunrise/sunset by integrating across segments.
    Returns (day_of_year_int, minute_of_day_int, years_added_int)
    where day_of_year_int is 1..365 and years_added_int is how many years rolled forward.
    """
    # Work in "absolute days since calibration year/day 1" space
    # We'll handle year rolling later, but we still need day counting.
    day = int(start_day)
    minute_of_day = float(start_minute_of_day)
    remaining = float(elapsed_real_seconds)

    # Safety loop
    for _ in range(20000):
        if remaining <= 0:
            break

        current_min_int = int(minute_of_day) % 1440
        spm = seconds_per_minute_for(current_min_int)

        # Determine next boundary (sunrise or sunset)
        if is_day_by_minute(current_min_int):
            # day -> next boundary is sunset today
            boundary_total_minutes = (day - 1) * 1440 + SUNSET_MIN
        else:
            # night -> next boundary is sunrise (maybe next day)
            if current_min_int < SUNRISE_MIN:
                boundary_total_minutes = (day - 1) * 1440 + SUNRISE_MIN
            else:
                boundary_total_minutes = day * 1440 + SUNRISE_MIN  # next day sunrise

        current_total_minutes = (day - 1) * 1440 + minute_of_day
        minutes_until_boundary = boundary_total_minutes - current_total_minutes
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

        # Normalize day/minute
        while minute_of_day >= 1440:
            minute_of_day -= 1440
            day += 1

    # Now apply year rolling (365 days per year)
    years_added = 0
    while day > 365:
        day -= 365
        years_added += 1

    return int(day), int(minute_of_day) % 1440, years_added

def calculate_time():
    """
    Returns:
      (title_str, color_int, current_spm_float, year_int, day_int, hour_int, minute_int, is_day_bool)
    """
    if not state:
        return None

    elapsed_real = time.time() - float(state["real_epoch"])

    start_year = int(state["year"])
    start_day = int(state["day"])  # 1..365
    start_minute_of_day = int(state["hour"]) * 60 + int(state["minute"])

    day, minute_of_day, years_added = advance_minutes_piecewise(start_day, start_minute_of_day, elapsed_real)
    year = start_year + years_added

    hour = minute_of_day // 60
    minute = minute_of_day % 60

    day_now = is_day_by_minute(minute_of_day)
    emoji = "☀️" if day_now else "🌙"
    color = DAY_COLOR if day_now else NIGHT_COLOR
    current_spm = DAY_SECONDS_PER_INGAME_MINUTE if day_now else NIGHT_SECONDS_PER_INGAME_MINUTE

    title = f"{emoji} | Solunaris Time | {hour:02d}:{minute:02d} | Day {day} | Year {year}"
    return title, color, float(current_spm), year, day, hour, minute, day_now

# =====================
# NITRADO STATUS + PLAYERS (Option 1: Nitrado API)
# =====================
async def nitrado_get_gameserver_info(session: aiohttp.ClientSession):
    headers = {"Authorization": f"Bearer {NITRADO_TOKEN}"}
    url = f"https://api.nitrado.net/services/{NITRADO_SERVICE_ID}/gameservers"
    async with session.get(url, headers=headers, timeout=20) as resp:
        data = await resp.json()
    return data.get("data", {}).get("gameserver")

async def nitrado_status_players():
    """
    Returns (online: bool, players: int)
    """
    async with aiohttp.ClientSession() as session:
        gs = await nitrado_get_gameserver_info(session)
        if not gs:
            return False, 0

        status = (gs.get("status") or "").lower()
        online = status in ("started", "running", "online")

        q = gs.get("query", {}) or {}
        players = q.get("player_current")
        if players is None:
            players = q.get("players")
        if players is None:
            players = 0

        try:
            players = int(players)
        except Exception:
            players = 0

        return online, players

async def get_server_status():
    """
    Returns (online: bool, players: int)
    """
    if STATUS_SOURCE == "nitrado":
        return await nitrado_status_players()

    # If you ever re-enable A2S, implement it here.
    return await nitrado_status_players()

# =====================
# ONLINE PLAYERS LIST (from Nitrado query if available)
# - We will try to pull a player list from Nitrado's query section if present.
# - If your Nitrado API doesn't include names, it will show just "Player 1..N".
# =====================
def format_players_embed(players_count: int, names: list[str]):
    if not names:
        desc = f"Players Online: **{players_count}/{PLAYER_CAP}**\n\n(No player names available via Nitrado API)"
    else:
        lines = "\n".join(f"• {n}" for n in names[:50])
        if len(names) > 50:
            lines += f"\n… and {len(names) - 50} more"
        desc = f"Players Online: **{players_count}/{PLAYER_CAP}**\n\n{lines}"

    embed = {
        "title": "Online Players",
        "description": desc,
        "color": 0x2ECC71 if players_count > 0 else 0x95A5A6,
    }
    return embed

async def nitrado_player_names():
    """
    Best-effort: some Nitrado responses include player_list under query; many do not.
    Returns list[str]
    """
    async with aiohttp.ClientSession() as session:
        gs = await nitrado_get_gameserver_info(session)
        if not gs:
            return []
        q = gs.get("query", {}) or {}

        # Possible shapes:
        # - q.get("player_list") -> list of dicts or strings
        # - q.get("players") sometimes is list (rare)
        pl = q.get("player_list")
        names = []

        if isinstance(pl, list):
            for p in pl:
                if isinstance(p, str):
                    names.append(p)
                elif isinstance(p, dict):
                    # try common keys
                    for key in ("name", "player_name", "character", "character_name"):
                        if p.get(key):
                            names.append(str(p.get(key)))
                            break

        return names

# =====================
# WEBHOOK HELPERS (edit-only, never spam)
# =====================
async def webhook_upsert_embed(session: aiohttp.ClientSession, webhook_url: str, message_id_holder: dict, key: str, embed: dict):
    """
    Edits existing message if we have an id, otherwise creates once and stores id.
    message_id_holder is a dict with key->id string
    """
    mid = message_id_holder.get(key)
    if mid:
        await session.patch(f"{webhook_url}/messages/{mid}", json={"embeds": [embed]})
        return mid

    # Create once
    async with session.post(webhook_url + "?wait=true", json={"embeds": [embed]}) as resp:
        data = await resp.json()
        mid_new = str(data["id"])
        message_id_holder[key] = mid_new
        return mid_new

# =====================
# LOOPS
# =====================
async def solunaris_time_loop():
    """
    Updates the Solunaris Time webhook message.
    Sleep scales with current day/night minute length.
    Also posts a message in ANNOUNCE_CHANNEL_ID at the start of each new day.
    """
    global time_webhook_message_id, last_announced_day_global
    await client.wait_until_ready()

    message_ids = {"time": time_webhook_message_id}

    async with aiohttp.ClientSession() as session:
        while True:
            if state:
                calc = calculate_time()
                if calc:
                    title, color, current_spm, year, day, hour, minute, _ = calc

                    embed = {"title": title, "color": color}
                    await webhook_upsert_embed(session, WEBHOOK_URL, message_ids, "time", embed)
                    time_webhook_message_id = message_ids["time"]

                    # New day announcement (absolute day index so year/day works)
                    absolute_day = (year - 1) * 365 + day
                    if last_announced_day_global is None:
                        last_announced_day_global = absolute_day

                    if absolute_day > last_announced_day_global:
                        # announce each missed day if bot was down for a bit
                        channel = client.get_channel(ANNOUNCE_CHANNEL_ID)
                        if channel:
                            for d in range(last_announced_day_global + 1, absolute_day + 1):
                                # Convert back to year/day for message
                                ann_year = ((d - 1) // 365) + 1
                                ann_day = ((d - 1) % 365) + 1
                                await channel.send(f"📅 **A new day has begun on Solunaris!** Day **{ann_day}**, Year **{ann_year}**.")
                        last_announced_day_global = absolute_day

                    sleep_for = float(current_spm)
                else:
                    sleep_for = DAY_SECONDS_PER_INGAME_MINUTE
            else:
                sleep_for = DAY_SECONDS_PER_INGAME_MINUTE

            await asyncio.sleep(sleep_for)

async def status_loop():
    """
    Checks status every 15s and updates VC only if changed.
    Also forces a refresh every 10 minutes regardless.
    Updates the players webhook message as well when it runs.
    """
    await client.wait_until_ready()

    last_status_name = None
    last_force = 0

    message_ids = {"players": players_webhook_message_id}

    while True:
        try:
            online, players = await get_server_status()
            emoji = "🟢" if online else "🔴"
            vc_name = f"{emoji} Solunaris | {players}/{PLAYER_CAP}"

            now = time.time()
            force = (now - last_force) >= 600  # 10 minutes

            # Update VC only if changed or forced
            if force or vc_name != last_status_name:
                guild = client.get_guild(GUILD_ID)
                if guild:
                    vc = guild.get_channel(STATUS_VC_ID)
                    if vc and isinstance(vc, discord.VoiceChannel):
                        await vc.edit(name=vc_name, reason="Solunaris status update")
                last_status_name = vc_name
                if force:
                    last_force = now

            # Update online players webhook (edit-only)
            # Best-effort names from Nitrado
            names = await nitrado_player_names()
            if not names and players > 0:
                names = [f"Player {i+1}" for i in range(players)]

            embed = format_players_embed(players, names)

            async with aiohttp.ClientSession() as session:
                await webhook_upsert_embed(session, PLAYERS_WEBHOOK_URL, message_ids, "players", embed)
                # keep persisted id in memory
                global players_webhook_message_id
                players_webhook_message_id = message_ids["players"]

        except Exception as e:
            # Don't crash the service; just log and keep looping
            print(f"[status_loop] Error: {e}")

        await asyncio.sleep(15)

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

    calc = calculate_time()
    if not calc:
        await interaction.response.send_message("⏳ Time not available yet.", ephemeral=True)
        return

    title, _, _, _, _, _, _, _ = calc
    await interaction.response.send_message(title, ephemeral=True)

@tree.command(
    name="settime",
    description="Set Solunaris time (role restricted)",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(
    year="Year number (>=1)",
    day="Day of year (1–365)",
    hour="Hour (0–23)",
    minute="Minute (0–59)",
)
async def settime_cmd(interaction: discord.Interaction, year: int, day: int, hour: int, minute: int):
    # Role check
    if not any(getattr(r, "id", None) == ADMIN_ROLE_ID for r in getattr(interaction.user, "roles", [])):
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return

    if year < 1 or day < 1 or day > 365 or hour < 0 or hour > 23 or minute < 0 or minute > 59:
        await interaction.response.send_message("❌ Invalid values.", ephemeral=True)
        return

    global state
    state = {
        "real_epoch": time.time(),
        "year": int(year),
        "day": int(day),
        "hour": int(hour),
        "minute": int(minute),
    }
    save_state(state)

    await interaction.response.send_message(
        f"✅ Set to **Year {year}**, **Day {day}**, **{hour:02d}:{minute:02d}**",
        ephemeral=True,
    )

@tree.command(
    name="status",
    description="Show server status + players and bump the players update",
    guild=discord.Object(id=GUILD_ID),
)
async def status_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    online, players = await get_server_status()
    emoji = "🟢" if online else "🔴"
    text = f"{emoji} **Solunaris** is **{'ONLINE' if online else 'OFFLINE'}** — **{players}/{PLAYER_CAP}** players"

    # Force a players webhook refresh immediately
    names = await nitrado_player_names()
    if not names and players > 0:
        names = [f"Player {i+1}" for i in range(players)]
    embed = format_players_embed(players, names)

    message_ids = {"players": players_webhook_message_id}
    async with aiohttp.ClientSession() as session:
        await webhook_upsert_embed(session, PLAYERS_WEBHOOK_URL, message_ids, "players", embed)
        global players_webhook_message_id
        players_webhook_message_id = message_ids["players"]

    await interaction.followup.send(text, ephemeral=True)

# =====================
# STARTUP
# =====================
@client.event
async def on_ready():
    # Sync guild commands (fast)
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print("✅ Commands synced")

    # Start loops
    client.loop.create_task(solunaris_time_loop())
    client.loop.create_task(status_loop())

client.run(DISCORD_TOKEN)