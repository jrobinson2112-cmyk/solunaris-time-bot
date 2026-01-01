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
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

# Day/night minute lengths
DAY_SECONDS_PER_INGAME_MINUTE = 4.7666667
NIGHT_SECONDS_PER_INGAME_MINUTE = 4.045  # as calculated previously

STATE_FILE = "state.json"

# Sunrise/sunset thresholds (in in-game minutes since midnight)
SUNRISE_MIN = 6 * 60   # 06:00
SUNSET_MIN  = 18 * 60  # 18:00

DAY_COLOR = 0xF1C40F    # Yellow
NIGHT_COLOR = 0x5865F2  # Blue

if not DISCORD_TOKEN or not WEBHOOK_URL:
    raise RuntimeError("Missing DISCORD_TOKEN or WEBHOOK_URL")

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
webhook_message_id = None

# =====================
# TIME CALCULATION (SMOOTH DAY/NIGHT SWITCHING)
# =====================
def is_day_by_minute(minute_of_day: int) -> bool:
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

    # Safety: stop any freak infinite loop if something goes wrong
    for _ in range(20000):
        if remaining <= 0:
            break

        current_minute_int = int(minute_of_day) % 1440
        spm = seconds_per_minute_for(current_minute_int)

        # Next boundary in in-game minutes
        if is_day_by_minute(current_minute_int):
            # Day -> next boundary is sunset same day
            boundary_total = (day - 1) * 1440 + SUNSET_MIN
        else:
            # Night -> next boundary is sunrise (might be next day if after sunset)
            if current_minute_int < SUNRISE_MIN:
                boundary_total = (day - 1) * 1440 + SUNRISE_MIN
            else:
                boundary_total = (day) * 1440 + SUNRISE_MIN  # next day sunrise

        current_total = (day - 1) * 1440 + minute_of_day
        minutes_until_boundary = boundary_total - current_total
        if minutes_until_boundary < 0:
            minutes_until_boundary = 0

        seconds_to_boundary = minutes_until_boundary * spm

        if remaining >= seconds_to_boundary and seconds_to_boundary > 0:
            # Jump exactly to boundary
            remaining -= seconds_to_boundary
            minute_of_day += minutes_until_boundary
        else:
            # Advance partially
            add_minutes = remaining / spm if spm > 0 else 0
            minute_of_day += add_minutes
            remaining = 0

        # Normalize day/minute_of_day
        while minute_of_day >= 1440:
            minute_of_day -= 1440
            day += 1

    return day, int(minute_of_day) % 1440


def calculate_time():
    """
    Returns (title, color, current_spm) using smooth piecewise conversion for day/night speed differences.
    Year rolls every 365 days.
    """
    if not state:
        return None, None, None

    elapsed_real = time.time() - state["real_epoch"]

    start_day = int(state["day"])
    start_year = int(state["year"])
    start_minute_of_day = int(state["hour"]) * 60 + int(state["minute"])

    # Advance with day/night piecewise conversion
    day, minute_of_day = advance_minutes_piecewise(start_day, start_minute_of_day, elapsed_real)

    # Year rolling: 365 days per year
    year = start_year
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
    return title, color, current_spm

# =====================
# WEBHOOK UPDATE LOOP (SCALES WITH DAY/NIGHT)
# =====================
async def update_loop():
    """
    Updates the webhook at:
      - every 4.7666667 seconds during in-game day
      - every 4.045 seconds during in-game night
    and switches smoothly at sunrise/sunset.
    """
    global webhook_message_id
    await client.wait_until_ready()

    async with aiohttp.ClientSession() as session:
        while True:
            if state:
                title, color, current_spm = calculate_time()
                embed = {"title": title, "color": color}  # LARGE + BOLD + colored bar

                if webhook_message_id:
                    await session.patch(
                        f"{WEBHOOK_URL}/messages/{webhook_message_id}",
                        json={"embeds": [embed]},
                    )
                else:
                    async with session.post(
                        WEBHOOK_URL + "?wait=true",
                        json={"embeds": [embed]},
                    ) as resp:
                        data = await resp.json()
                        webhook_message_id = data["id"]

                # Sleep scales with the current day/night minute length
                sleep_for = float(current_spm) if current_spm else DAY_SECONDS_PER_INGAME_MINUTE
            else:
                sleep_for = DAY_SECONDS_PER_INGAME_MINUTE

            await asyncio.sleep(sleep_for)

# =====================
# SLASH COMMANDS
# =====================
@tree.command(
    name="day",
    description="Show current Solunaris time",
    guild=discord.Object(id=GUILD_ID),
)
async def day(interaction: discord.Interaction):
    if not state:
        await interaction.response.send_message("⏳ Time not set yet.", ephemeral=True)
        return

    title, _, _ = calculate_time()
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
async def settime(interaction: discord.Interaction, year: int, day: int, hour: int, minute: int):
    if not any(r.id == ADMIN_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return

    if year < 1 or day < 1 or day > 365 or hour < 0 or hour > 23 or minute < 0 or minute > 59:
        await interaction.response.send_message("❌ Invalid values.", ephemeral=True)
        return

    global state
    state = {
        "real_epoch": time.time(),
        "year": year,
        "day": day,
        "hour": hour,
        "minute": minute,
    }
    save_state(state)

    await interaction.response.send_message(
        f"✅ Set to Day {day}, {hour:02d}:{minute:02d}, Year {year}",
        ephemeral=True,
    )

# =====================
# STARTUP
# =====================
@client.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print("✅ Commands synced")
    client.loop.create_task(update_loop())

client.run(DISCORD_TOKEN)