import os
import time
import json
import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

# ------------------ ENV ------------------
TOKEN = os.getenv("DISCORD_TOKEN")
VOICE_CHANNEL_ID = int(os.getenv("VOICE_CHANNEL_ID"))
CURRENT_YEAR = int(os.getenv("DEFAULT_YEAR", 1))

# ------------------ CONSTANTS ------------------
DAYS_PER_YEAR = 365

# 1 in-game minute = 4 real seconds
ARK_SECONDS_PER_REAL_SECOND = 15.0
ARK_DAY_SECONDS = 86400

DAY_START = int(5.5 * 3600)     # 05:30
NIGHT_START = int(19.5 * 3600)  # 19:30

CHECK_INTERVAL_SECONDS = 2
RENAME_INTERVAL_SECONDS = 20

STATE_FILE = "solunaris_state.json"

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ------------------ STATE ------------------
REFERENCE_REAL_TIME = None
REF_YEAR = None
REF_DAY_OF_YEAR = None
REF_TOD_SECONDS = None

_last_name = None
_last_rename = 0.0


# ------------------ HELPERS ------------------
def is_daytime(tod):
    return DAY_START <= tod < NIGHT_START


def parse_hhmm(hhmm):
    if ":" not in hhmm:
        raise ValueError("Time must be HH:MM")
    h, m = hhmm.split(":")
    h, m = int(h), int(m)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("Invalid time")
    return h, m


def save_state():
    with open(STATE_FILE, "w") as f:
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


def load_state():
    global REFERENCE_REAL_TIME, REF_YEAR, REF_DAY_OF_YEAR, REF_TOD_SECONDS
    try:
        with open(STATE_FILE, "r") as f:
            d = json.load(f)
        REFERENCE_REAL_TIME = d["real_time"]
        REF_YEAR = d["year"]
        REF_DAY_OF_YEAR = d["day_of_year"]
        REF_TOD_SECONDS = d["tod_seconds"]
        return True
    except Exception:
        return False


def calibrate_state(day_of_year, hh, mm, year):
    global REFERENCE_REAL_TIME, REF_YEAR, REF_DAY_OF_YEAR, REF_TOD_SECONDS
    if not (1 <= day_of_year <= DAYS_PER_YEAR):
        raise ValueError("Day must be 1–365")
    REFERENCE_REAL_TIME = time.time()
    REF_YEAR = year
    REF_DAY_OF_YEAR = day_of_year
    REF_TOD_SECONDS = (hh * 3600) + (mm * 60)
    save_state()


def get_state():
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


async def update_channel(force=False):
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
        if now - _last_rename < RENAME_INTERVAL_SECONDS:
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


@bot.event
async def on_ready():
    loaded = load_state()
    print(f"Logged in as {bot.user} | state_loaded={loaded}")
    if loaded:
        await update_channel(force=True)
    clock_loop.start()


# ------------------ COMMANDS ------------------
@bot.command()
async def calibrate(ctx, day: int, hhmm: str):
    """
    Example: !calibrate 123 05:36
    """
    try:
        hh, mm = parse_hhmm(hhmm)
        calibrate_state(day, hh, mm, CURRENT_YEAR)
        await update_channel(force=True)
        await ctx.send(
            f"✅ Calibrated: Year {CURRENT_YEAR}, Day {day}, {hh:02d}:{mm:02d}"
        )
    except Exception as e:
        await ctx.send(f"❌ Calibration failed: {e}")


@bot.command()
async def solunaris(ctx):
    if REFERENCE_REAL_TIME is None:
        await ctx.send("⚠️ Not calibrated. Use `!calibrate <day> <HH:MM>`")
        return
    year, day, hhmm, emoji = get_state()
    await ctx.send(f"{emoji} Solunaris Year {year} | Day {day} | {hhmm}")


bot.run(TOKEN)