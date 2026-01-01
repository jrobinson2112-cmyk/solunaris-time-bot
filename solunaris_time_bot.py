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

# ------------------ YEAR ROLLING ------------------
DAYS_PER_YEAR = int(os.getenv("DAYS_PER_YEAR", 100))
YEAR_START_DAY = int(os.getenv("YEAR_START_DAY", 1))
YEAR_START_YEAR = int(os.getenv("YEAR_START_YEAR", 1))

# ------------------ TIME CONFIG ------------------
# 1 in-game minute = 4 real seconds → 60 / 4 = 15 in-game seconds per real second
ARK_SECONDS_PER_REAL_SECOND = float(os.getenv("ARK_SECONDS_PER_REAL_SECOND", 15.0))
ARK_DAY_SECONDS = 86400

# Day/Night split (emoji only)
DAY_START = int(5.5 * 3600)     # 05:30
NIGHT_START = int(19.5 * 3600)  # 19:30

CAL_FILE = "calibration.json"

# ------------------ DISCORD ------------------
intents = discord.Intents.default()
intents.message_content = True  # needed for commands
bot = commands.Bot(command_prefix="!", intents=intents)

_last_displayed_minute = None

# Calibration state (loaded/saved)
REFERENCE_REAL_TIME = None
REFERENCE_TOTAL_ARK_SECONDS = None

def is_daytime(tod):
    return DAY_START <= tod < NIGHT_START

def parse_hhmm(s: str):
    s = s.strip()
    if ":" not in s:
        raise ValueError("Time must be HH:MM")
    hh, mm = s.split(":", 1)
    hh = int(hh)
    mm = int(mm)
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("HH must be 0-23 and MM 0-59")
    return hh, mm

def load_calibration():
    global REFERENCE_REAL_TIME, REFERENCE_TOTAL_ARK_SECONDS
    try:
        with open(CAL_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        REFERENCE_REAL_TIME = float(data["reference_real_time"])
        REFERENCE_TOTAL_ARK_SECONDS = float(data["reference_total_ark_seconds"])
        return True
    except Exception:
        return False

def save_calibration():
    with open(CAL_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "reference_real_time": REFERENCE_REAL_TIME,
                "reference_total_ark_seconds": REFERENCE_TOTAL_ARK_SECONDS,
            },
            f,
            indent=2,
        )

def set_calibration(day: int, hh: int, mm: int):
    """
    Sets calibration so that 'now' equals Day <day> at HH:MM.
    """
    global REFERENCE_REAL_TIME, REFERENCE_TOTAL_ARK_SECONDS
    REFERENCE_REAL_TIME = time.time()
    REFERENCE_TOTAL_ARK_SECONDS = ((day - 1) * ARK_DAY_SECONDS) + (hh * 3600) + (mm * 60)
    save_calibration()

def get_day_and_time():
    # Must be calibrated before use
    now = time.time()
    elapsed = now - REFERENCE_REAL_TIME
    total_ark = REFERENCE_TOTAL_ARK_SECONDS + (elapsed * ARK_SECONDS_PER_REAL_SECOND)

    day = int(total_ark // ARK_DAY_SECONDS) + 1
    tod = int(total_ark % ARK_DAY_SECONDS)

    hours = tod // 3600
    minutes = (tod % 3600) // 60
    hhmm = f"{hours:02d}:{minutes:02d}"

    emoji = "☀️" if is_daytime(tod) else "🌙"
    return day, hhmm, emoji

def get_year(day):
    if day < YEAR_START_DAY:
        return YEAR_START_YEAR
    return YEAR_START_YEAR + ((day - YEAR_START_DAY) // DAYS_PER_YEAR)

async def rename_channel(force=False):
    global _last_displayed_minute

    channel = bot.get_channel(VOICE_CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(VOICE_CHANNEL_ID)

    day, hhmm, emoji = get_day_and_time()

    # Avoid Discord rate limits: rename only when HH:MM changes
    if not force and hhmm == _last_displayed_minute:
        return

    year = get_year(day)
    name = f"{emoji} Solunaris Year {year} | Day {day} | {hhmm}"

    await channel.edit(name=name)
    _last_displayed_minute = hhmm
    print(f"Updated channel → {name}")

@tasks.loop(seconds=2)
async def clock_loop():
    try:
        await rename_channel()
    except discord.HTTPException as e:
        # If Discord rate-limits, it will recover on its own.
        print(f"Discord HTTP error (likely rate limit): {e}")
    except Exception as e:
        print(f"Unexpected error: {e}")

@bot.event
async def on_ready():
    # Try load calibration; if not found, bot will still run but commands will tell you to calibrate.
    ok = load_calibration()
    print(f"Logged in as {bot.user} | calibration_loaded={ok}")
    if ok:
        await rename_channel(force=True)
    clock_loop.start()

# ------------------ COMMANDS ------------------

@bot.command()
async def solunaris(ctx):
    if REFERENCE_REAL_TIME is None:
        await ctx.send("⚠️ Not calibrated yet. Run: `!calibrate <day> <HH:MM>` (example: `!calibrate 103 05:36`)")
        return
    day, hhmm, emoji = get_day_and_time()
    year = get_year(day)
    await ctx.send(f"{emoji} Solunaris Year {year} | Day {day} | {hhmm}")

@bot.command()
async def calibrate(ctx, day: int, hhmm: str):
    """
    One-command calibration.
    Example: !calibrate 103 05:36
    """
    try:
        hh, mm = parse_hhmm(hhmm)
        set_calibration(day, hh, mm)
        await rename_channel(force=True)
        await ctx.send(f"✅ Calibrated: Day {day} @ {hh:02d}:{mm:02d} (saved).")
    except Exception as e:
        await ctx.send(f"❌ Calibration failed: {e}")

@bot.command()
async def setyearlen(ctx, days: int):
    global DAYS_PER_YEAR
    DAYS_PER_YEAR = days
    if REFERENCE_REAL_TIME is not None:
        await rename_channel(force=True)
    await ctx.send(f"✅ Year length set to {days} days. (Set Railway var DAYS_PER_YEAR to persist.)")

@bot.command()
async def setyearstart(ctx, year: int, day: int):
    global YEAR_START_YEAR, YEAR_START_DAY
    YEAR_START_YEAR = year
    YEAR_START_DAY = day
    if REFERENCE_REAL_TIME is not None:
        await rename_channel(force=True)
    await ctx.send(f"✅ Year baseline set: Year {year} at Day {day}. (Set Railway vars to persist.)")

bot.run(TOKEN)