import os
import time
import json
import asyncio
import aiohttp
import discord
from discord import app_commands

# =====================
# CONFIG (ENV VARS)
# =====================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")                 # Time embed webhook (single message edited)
PLAYERS_WEBHOOK_URL = os.getenv("PLAYERS_WEBHOOK_URL") # Players list webhook (single message edited)
STATUS_VC_ID = os.getenv("STATUS_VC_ID")               # Voice channel ID for status (🟢/🔴 Solunaris | x/42)
DAY_ANNOUNCE_CHANNEL_ID = os.getenv("DAY_ANNOUNCE_CHANNEL_ID")  # Optional: channel to post "New day" messages

# Nitrado API (Option 1)
NITRADO_TOKEN = os.getenv("NITRADO_TOKEN")            # Long-life token
NITRADO_SERVICE_ID = os.getenv("NITRADO_SERVICE_ID")  # e.g. 17997739

# Your fixed IDs
GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

# Server info
PLAYER_CAP = 42

# Day/night minute lengths (real seconds per in-game minute)
DAY_SECONDS_PER_INGAME_MINUTE = 4.7666667
NIGHT_SECONDS_PER_INGAME_MINUTE = 4.045

# Day / night boundaries (in-game minutes since midnight)
# You said: day 05:30 -> 17:30, night 17:30 -> 05:30
SUNRISE_MIN = 5 * 60 + 30   # 05:30
SUNSET_MIN  = 17 * 60 + 30  # 17:30

# Embed colors
DAY_COLOR = 0xF1C40F    # Yellow
NIGHT_COLOR = 0x5865F2  # Blue

STATE_FILE = "state.json"

# Status poll behaviour
STATUS_CHECK_EVERY = 15          # look for changes every 15s
STATUS_FORCE_UPDATE_EVERY = 600  # update every 10 mins regardless of change

# =====================
# VALIDATION
# =====================
missing = []
for k, v in {
    "DISCORD_TOKEN": DISCORD_TOKEN,
    "WEBHOOK_URL": WEBHOOK_URL,
    "PLAYERS_WEBHOOK_URL": PLAYERS_WEBHOOK_URL,
    "NITRADO_TOKEN": NITRADO_TOKEN,
    "NITRADO_SERVICE_ID": NITRADO_SERVICE_ID,
    "STATUS_VC_ID": STATUS_VC_ID,
}.items():
    if not v:
        missing.append(k)

if missing:
    raise RuntimeError(f"Missing env var(s): {', '.join(missing)}")

STATUS_VC_ID = int(STATUS_VC_ID)
DAY_ANNOUNCE_CHANNEL_ID = int(DAY_ANNOUNCE_CHANNEL_ID) if DAY_ANNOUNCE_CHANNEL_ID else None

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
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(data):
    with open(STATE_FILE, "w") as f:
        json.dump(data, f)

state = load_state()
time_webhook_message_id = None
players_webhook_message_id = None

# Keep track of last day/year announced to avoid repeats
last_announced_day_year = None

# =====================
# TIME CALCULATION
# =====================
def is_day_by_minute(minute_of_day: int) -> bool:
    # day is from SUNRISE_MIN (inclusive) to SUNSET_MIN (exclusive)
    return SUNRISE_MIN <= minute_of_day < SUNSET_MIN

def seconds_per_minute_for(minute_of_day: int) -> float:
    return DAY_SECONDS_PER_INGAME_MINUTE if is_day_by_minute(minute_of_day) else NIGHT_SECONDS_PER_INGAME_MINUTE

def advance_minutes_piecewise(start_day: int, start_minute_of_day: int, elapsed_real_seconds: float):
    """
    Advances in-game time using different real-seconds-per-in-game-minute for day vs night.
    Smooth at sunrise/sunset by integrating across segments.
    Returns (day, minute_of_day_int).
    """
    day = start_day
    minute_of_day = float(start_minute_of_day)
    remaining = float(elapsed_real_seconds)

    for _ in range(20000):
        if remaining <= 0:
            break

        current_minute_int = int(minute_of_day) % 1440
        spm = seconds_per_minute_for(current_minute_int)

        # Next boundary in in-game minutes (total minutes since Day 1 00:00)
        if is_day_by_minute(current_minute_int):
            boundary_total = (day - 1) * 1440 + SUNSET_MIN  # sunset same day
        else:
            # night -> next sunrise (might be same day if before sunrise, else next day)
            if current_minute_int < SUNRISE_MIN:
                boundary_total = (day - 1) * 1440 + SUNRISE_MIN
            else:
                boundary_total = (day) * 1440 + SUNRISE_MIN

        current_total = (day - 1) * 1440 + minute_of_day
        minutes_until_boundary = boundary_total - current_total
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

        while minute_of_day >= 1440:
            minute_of_day -= 1440
            day += 1

    return day, int(minute_of_day) % 1440

def calculate_time():
    """
    Returns (title, color, current_spm, day, year, minute_of_day).
    Year rolls every 365 days.
    """
    if not state:
        return None, None, None, None, None, None

    elapsed_real = time.time() - state["real_epoch"]

    start_day = int(state["day"])
    start_year = int(state["year"])
    start_minute_of_day = int(state["hour"]) * 60 + int(state["minute"])

    day, minute_of_day = advance_minutes_piecewise(start_day, start_minute_of_day, elapsed_real)

    year = start_year
    # roll years forward for every 365 days
    while day > 365:
        day -= 365
        year += 1

    hour = minute_of_day // 60
    minute = minute_of_day % 60

    day_now = is_day_by_minute(minute_of_day)
    emoji = "☀️" if day_now else "🌙"
    color = DAY_COLOR if day_now else NIGHT_COLOR

    title = f"{emoji} | Solunaris Time | {hour:02d}:{minute:02d} | Day {day} | Year {year}"
    current_spm = DAY_SECONDS_PER_INGAME_MINUTE if day_now else NIGHT_SECONDS_PER_INGAME_MINUTE
    return title, color, float(current_spm), day, year, minute_of_day

# =====================
# NITRADO API HELPERS
# =====================
NITRADO_BASE = "https://api.nitrado.net"

async def nitrado_get(session: aiohttp.ClientSession, path: str):
    headers = {"Authorization": f"Bearer {NITRADO_TOKEN}"}
    async with session.get(NITRADO_BASE + path, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        data = await resp.json(content_type=None)
        return resp.status, data

async def fetch_server_status_and_players(session: aiohttp.ClientSession):
    """
    Returns:
      online: bool
      players_online: int|None
      players_list: list[str] (character names only)
      server_name: str|None
      version: str|None
      error: str|None
    """
    # This endpoint returns game server info incl players if available for the game.
    # If your Nitrado plan/game doesn't expose player list via API, players_list may be empty.
    status, data = await nitrado_get(session, f"/services/{NITRADO_SERVICE_ID}/gameservers")

    if status != 200:
        return False, None, [], None, None, f"nitrado http {status}"

    try:
        gs = data["data"]["gameserver"]
        # name/version may be nested depending on Nitrado response
        server_name = gs.get("hostname") or gs.get("name")
        version = gs.get("version")

        # "status" can be "started"/"stopped" etc
        raw_status = (gs.get("status") or "").lower()
        online = raw_status in ("started", "running", "online")

        players_online = None
        # Nitrado commonly has "slots" and "players" depending on game
        # Try a few likely keys:
        for key_path in [
            ("query", "player_current"),
            ("query", "player_current"),
            ("game_specific", "players"),
            ("players",),
            ("player_current",),
        ]:
            cur = gs
            ok = True
            for k in key_path:
                if isinstance(cur, dict) and k in cur:
                    cur = cur[k]
                else:
                    ok = False
                    break
            if ok and isinstance(cur, int):
                players_online = cur
                break

        # Player list: if Nitrado provides it, keep only character name
        players_list = []
        possible_lists = [
            ("query", "players"),            # sometimes list of players
            ("game_specific", "players_list"),
            ("players_list",),
        ]
        for key_path in possible_lists:
            cur = gs
            ok = True
            for k in key_path:
                if isinstance(cur, dict) and k in cur:
                    cur = cur[k]
                else:
                    ok = False
                    break
            if ok and isinstance(cur, list):
                # attempt to extract a "name" field; if list is strings, use directly
                for p in cur:
                    if isinstance(p, str):
                        players_list.append(p.strip())
                    elif isinstance(p, dict):
                        # Try common fields that represent character name
                        for nk in ("character", "character_name", "name", "playername"):
                            if nk in p and isinstance(p[nk], str) and p[nk].strip():
                                players_list.append(p[nk].strip())
                                break
                break

        # final cleanup: remove empties & dedupe
        players_list = [x for x in players_list if x]
        seen = set()
        players_list = [x for x in players_list if not (x in seen or seen.add(x))]

        return online, players_online, players_list, server_name, version, None
    except Exception as e:
        return False, None, [], None, None, f"parse error: {e}"

# =====================
# WEBHOOK UPDATES
# =====================
async def upsert_webhook_embed(session: aiohttp.ClientSession, webhook_url: str, message_id_ref: dict, key: str, embed: dict):
    """
    Creates once, then edits the same message forever.
    message_id_ref is a dict holding ids.
    """
    mid = message_id_ref.get(key)
    if mid:
        async with session.patch(f"{webhook_url}/messages/{mid}", json={"embeds": [embed]}) as resp:
            if resp.status == 404:
                # message deleted -> recreate
                message_id_ref[key] = None
            elif resp.status >= 400:
                # keep going, but don't crash
                return
    if not message_id_ref.get(key):
        async with session.post(webhook_url + "?wait=true", json={"embeds": [embed]}) as resp:
            data = await resp.json(content_type=None)
            if isinstance(data, dict) and "id" in data:
                message_id_ref[key] = data["id"]

async def time_update_loop():
    """
    Updates the time webhook at:
      - every 4.7666667 seconds during in-game day
      - every 4.045 seconds during in-game night
    """
    global time_webhook_message_id, last_announced_day_year
    await client.wait_until_ready()

    msg_ids = {"time": None}
    # try load from memory if you want persistence; for now it's runtime only
    msg_ids["time"] = time_webhook_message_id

    async with aiohttp.ClientSession() as session:
        while True:
            if state:
                title, color, current_spm, day, year, minute_of_day = calculate_time()

                # Large/bold feel: use embed title + a blank description line
                embed = {
                    "title": title,
                    "description": "",
                    "color": color,
                }

                await upsert_webhook_embed(session, WEBHOOK_URL, msg_ids, "time", embed)
                time_webhook_message_id = msg_ids["time"]

                # Day change announcement (optional)
                if DAY_ANNOUNCE_CHANNEL_ID and day and year:
                    dy = (year, day)
                    if last_announced_day_year is None:
                        last_announced_day_year = dy
                    elif dy != last_announced_day_year:
                        ch = client.get_channel(DAY_ANNOUNCE_CHANNEL_ID)
                        if ch:
                            try:
                                await ch.send(f"📅 **A new day has begun!** Day {day} | Year {year}")
                            except:
                                pass
                        last_announced_day_year = dy

                sleep_for = float(current_spm) if current_spm else DAY_SECONDS_PER_INGAME_MINUTE
            else:
                sleep_for = DAY_SECONDS_PER_INGAME_MINUTE

            await asyncio.sleep(sleep_for)

async def players_and_status_loop():
    """
    - Poll every 15s for changes (Nitrado API)
    - Update immediately if changed
    - Force update every 10 minutes regardless
    - Update status VC name: 🟢 Solunaris | x/42  OR 🔴 Solunaris | 0/42
    - Update players webhook: character names only
    """
    await client.wait_until_ready()

    msg_ids = {"players": None}
    global players_webhook_message_id
    msg_ids["players"] = players_webhook_message_id

    last_payload = None
    last_force = 0.0

    async with aiohttp.ClientSession() as session:
        while True:
            now = time.time()
            changed = False
            force = (now - last_force) >= STATUS_FORCE_UPDATE_EVERY

            online, players_online, players_list, server_name, version, err = await fetch_server_status_and_players(session)

            # fallbacks
            if players_online is None:
                players_online = len(players_list) if players_list else 0

            # Build payload snapshot for change detection
            payload = {
                "online": online,
                "players_online": players_online,
                "players_list": players_list[:],  # copy
                "server_name": server_name,
                "version": version,
                "err": err,
            }

            if last_payload != payload:
                changed = True

            if changed or force:
                last_force = now
                last_payload = payload

                # --- Update VC ---
                try:
                    guild = client.get_guild(GUILD_ID)
                    if guild:
                        vc = guild.get_channel(STATUS_VC_ID)
                        if vc:
                            dot = "🟢" if online else "🔴"
                            name = f"{dot} Solunaris | {players_online}/{PLAYER_CAP}"
                            if vc.name != name:
                                await vc.edit(name=name)
                except:
                    pass

                # --- Update Players Webhook (character names only) ---
                # Remove EOS/platform: we ONLY print character names.
                if players_list:
                    lines = []
                    for i, nm in enumerate(players_list, start=1):
                        lines.append(f"{i:02d}) {nm}")
                    body = "\n".join(lines)
                    header = f"**{server_name or 'Solunaris'}**\n**{players_online}/{PLAYER_CAP}** players online.\n"
                    desc = header + "\n" + body
                else:
                    if err:
                        desc = f"**Solunaris**\nPlayers: **?/{PLAYER_CAP}**\n\n(query failed: {err})"
                    else:
                        desc = f"**Solunaris**\nPlayers: **{players_online}/{PLAYER_CAP}**\n\n(No player list available via API.)"

                embed = {
                    "title": "Online Players",
                    "description": desc[:4096],
                    "color": 0x2ECC71 if online else 0xE74C3C,
                }

                await upsert_webhook_embed(session, PLAYERS_WEBHOOK_URL, msg_ids, "players", embed)
                players_webhook_message_id = msg_ids["players"]

            await asyncio.sleep(STATUS_CHECK_EVERY)

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
        await interaction.response.send_message("⏳ Time not set yet.", ephemeral=True)
        return

    title, _, _, _, _, _ = calculate_time()
    await interaction.response.send_message(title, ephemeral=True)

@tree.command(
    name="settime",
    description="Set Solunaris time",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(
    year="Year number",
    day="Day of year (1–365)",
    hour="Hour (0–23)",
    minute="Minute (0–59)",
)
async def settime_cmd(interaction: discord.Interaction, year: int, day: int, hour: int, minute: int):
    if not hasattr(interaction.user, "roles") or not any(r.id == ADMIN_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message("❌ You must have the required admin role to use /settime.", ephemeral=True)
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
        f"✅ Set to Day {day}, {hour:02d}:{minute:02d}, Year {year}",
        ephemeral=True,
    )

@tree.command(
    name="status",
    description="Show Solunaris server status & players online",
    guild=discord.Object(id=GUILD_ID),
)
async def status_cmd(interaction: discord.Interaction):
    # Make sure we respond quickly to avoid "application did not respond"
    await interaction.response.defer(ephemeral=True)

    async with aiohttp.ClientSession() as session:
        online, players_online, players_list, server_name, version, err = await fetch_server_status_and_players(session)

    if players_online is None:
        players_online = len(players_list) if players_list else 0

    dot = "🟢" if online else "🔴"
    name = server_name or "Solunaris"
    msg = f"{dot} **{name}** — Players: **{players_online}/{PLAYER_CAP}**"
    if err:
        msg += f"\n(query failed: {err})"

    await interaction.followup.send(msg, ephemeral=True)

# =====================
# STARTUP
# =====================
@client.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print("✅ Commands synced")

    # start background loops
    client.loop.create_task(time_update_loop())
    client.loop.create_task(players_and_status_loop())

client.run(DISCORD_TOKEN)