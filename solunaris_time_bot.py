# solunaris_time_bot.py
#
# ✅ Updates a VOICE CHANNEL name (Discord-safe: max once per 60s)
# ✅ Uses your measured time conversion (day & night): 20 in-game minutes = 94 real seconds
# ✅ Year/day rolling: Day is 1..365 (day-of-year). After Day 365 -> Day 1 and Year +1
# ✅ SLASH COMMANDS with autocomplete:
#    /day
#    /calibrate day:123 time:14:28
#
# REQUIRED Railway Variables:
#   DISCORD_TOKEN
#   VOICE_CHANNEL_ID
#
# OPTIONAL Railway Variables:
#   DEFAULT_YEAR (default 1)

import os
import time
import json
import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

# ------------------ ENV ------------------
TOKEN = os.getenv("DISCORD_TOKEN")
VOICE_CHANNEL_ID = int(os.getenv("VOICE_CHANNEL_ID"))
CURRENT_YEAR = int(os.getenv("DEFAULT_YEAR", 1))

# ------------------ CONSTANTS ------------------
DAYS_PER_YEAR = 365
ARK_DAY_SECONDS = 86400

# ✅ Measured (day AND night): 20 in-game minutes = 94 real seconds
# => in-game seconds per real second = (20*60) / 94 = 12.7659574468
ARK_SECONDS_PER_REAL_SECOND = 12.7659574468

# Emoji split (display only)
DAY_START = int(5.5 * 3600)     # 05:30
NIGHT_START = int(19.5 * 3600)  # 19:30

# Loop/check vs rename throttling
CHECK_INTERVAL_SECONDS = 2
RENAME_INTERVAL_SECONDS = 60  # ✅ properly safe to avoid 429s

STATE_FILE = "solunaris_state.json"

# ------------------ DISCORD BOT ------------------
# Slash commands do NOT require message_content intent
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ------------------ STATE ------------------
# Saved calibration baseline:
# "now" at calibration moment == (REF_YEAR, REF_DAY_OF_YEAR, REF_TOD_SECONDS)
REFERENCE_REAL_TIME = None
REF_YEAR = None
REF_DAY_OF_YEAR = None   # rolling day-of-year (1..365) that you enter in /calibrate
REF_TOD_SECONDS = None   # seconds since midnight at calibration moment

_last_name = None
_last_rename = 0.0


# ------------------ HELPERS ------------------
def is_daytime(tod: int) -> bool:
    return DAY_START <= tod < NIGHT_START


def parse_hhmm(hhmm: str):
    hhmm = hhmm.strip()
    if ":" not in hhmm:
        raise ValueError("Time must be HH:MM (example 14:28)")
    h, m = hhmm.split(":", 1)
    h, m = int(h), int(m)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("Invalid time. HH 0-23, MM 0-59.")
    return h, m


def save_state():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "real_time": REFERENCE_REAL_TIME,
                "year": REF_YEAR,
                "day_of_year": REF_DAY_OF_YEAR,
                "tod_seconds": REF_TOD_SECONDS,
            },
            f,
            indent=2,
        )


def load_state() -> bool:
    global REFERENCE_REAL_TIME, REF_YEAR, REF_DAY_OF_YEAR, REF_TOD_SECONDS
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        REFERENCE_REAL_TIME = float(d["real_time"])
        REF_YEAR = int(d["year"])
        REF_DAY_OF_YEAR = int(d["day_of_year"])
        REF_TOD_SECONDS = int(d["tod_seconds"])
        return True
    except Exception:
        return False


def calibrate_state(day_of_year: int, hh: int, mm: int, year: int):
    """
    Sets "now" to Year=<year>, Day=<day_of_year> (rolling 1..365), time HH:MM.
    """
    global REFERENCE_REAL_TIME, REF_YEAR, REF_DAY_OF_YEAR, REF_TOD_SECONDS
    if not (1 <= day_of_year <= DAYS_PER_YEAR):
        raise ValueError("Day must be 1–365")
    REFERENCE_REAL_TIME = time.time()
    REF_YEAR = year
    REF_DAY_OF_YEAR = day_of_year
    REF_TOD_SECONDS = (hh * 3600) + (mm * 60)
    save_state()


def get_state():
    """
    Returns (year, day_of_year, hhmm, emoji)
    Day rolls 1..365 and year increments after day 365.
    """
    now = time.time()
    real_elapsed = now - REFERENCE_REAL_TIME
    ark_elapsed = real_elapsed * ARK_SECONDS_PER_REAL_SECOND

    total_seconds = REF_TOD_SECONDS + ark_elapsed
    days_passed = int(total_seconds // ARK_DAY_SECONDS)
    tod = int(total_seconds % ARK_DAY_SECONDS)

    day_of_year = REF_DAY_OF_YEAR + days_passed
    year = REF_YEAR

    if day_of_year > DAYS_PER_YEAR:
        years_forward = (day_of_year - 1) // DAYS_PER_YEAR
        year += years_forward
        day_of_year = ((day_of_year - 1) % DAYS_PER_YEAR) + 1

    h = tod // 3600
    m = (tod % 3600) // 60
    hhmm = f"{h:02d}:{m:02d}"

    emoji = "☀️" if is_daytime(tod) else "🌙"
    return year, day_of_year, hhmm, emoji


async def update_channel(force: bool = False):
    """
    Renames the voice channel, but throttles edits to avoid 429 rate limits.
    """
    global _last_name, _last_rename

    if REFERENCE_REAL_TIME is None:
        return

    channel = bot.get_channel(VOICE_CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(VOICE_CHANNEL_ID)

    year, day, hhmm, emoji = get_state()
    name = f"{emoji} Solunaris Year {year} | Day {day} | {hhmm}"

    now = time.time()
    if not force:
        if name == _last_name:
            return
        if (now - _last_rename) < RENAME_INTERVAL_SECONDS:
            return

    await channel.edit(name=name)
    _last_name = name
    _last_rename = now
    print(f"Updated → {name}")


@tasks.loop(seconds=CHECK_INTERVAL_SECONDS)
async def clock_loop():
    try:
        await update_channel()
    except Exception as e:
        print(f"Loop error: {e}")


# ------------------ SLASH COMMANDS ------------------
@bot.tree.command(name="day", description="Show the current Solunaris Year/Day/Time")
async def day_slash(interaction: discord.Interaction):
    if REFERENCE_REAL_TIME is None:
        await interaction.response.send_message(
            "⚠️ Not calibrated yet. Use `/calibrate` first.",
            ephemeral=True,
        )
        return

    year, day, hhmm, emoji = get_state()
    await interaction.response.send_message(
        f"{emoji} Solunaris Year {year} | Day {day} | {hhmm}"
    )


@bot.tree.command(name="calibrate", description="Calibrate Solunaris day/time (Day is 1–365 rolling)")
@app_commands.describe(
    day="Day-of-year (1–365) used for year rolling",
    time="In-game time (HH:MM)",
)
async def calibrate_slash(interaction: discord.Interaction, day: int, time: str):
    global CURRENT_YEAR
    try:
        hh, mm = parse_hhmm(time)
        calibrate_state(day, hh, mm, CURRENT_YEAR)
        await update_channel(force=True)
        await interaction.response.send_message(
            f"✅ Calibrated: Year {CURRENT_YEAR}, Day {day}, {hh:02d}:{mm:02d}"
        )
    except Exception as e:
        await interaction.response.send_message(
            f"❌ Calibration failed: {e}",
            ephemeral=True,
        )


@bot.event
async def on_ready():
    loaded = load_state()
    print(f"Logged in as {bot.user} | state_loaded={loaded}")

    # Sync slash commands
    try:
        await bot.tree.sync()
        print("Slash commands synced")
    except Exception as e:
        print(f"Slash command sync failed: {e}")

    if loaded:
        await update_channel(force=True)

    if not clock_loop.is_running():
        clock_loop.start()


bot.run(TOKEN)