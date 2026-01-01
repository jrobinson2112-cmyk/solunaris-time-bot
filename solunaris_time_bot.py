import os
import time
import discord
from discord.ext import commands, tasks
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
VOICE_CHANNEL_ID = int(os.getenv("VOICE_CHANNEL_ID"))
SOLUNARIS_YEAR = int(os.getenv("DEFAULT_YEAR", 1))

# ---- Your server settings ----
DAY_CYCLE_SPEED = 5.92
DAY_TIME_SPEED = 1.85
NIGHT_TIME_SPEED = 2.18

BASE_ARK_SECONDS_PER_REAL_SECOND = 60.0  # ARK default: 1 real sec = 60 in-game sec

ARK_DAY_SECONDS = 86400

# ARK day/night boundaries (common ARK split)
DAY_START = int(5.5 * 3600)    # 05:30 = 19800
NIGHT_START = int(19.5 * 3600) # 19:30 = 70200

# Speeds (in-game seconds per real second)
DAY_SPEED = BASE_ARK_SECONDS_PER_REAL_SECOND * DAY_CYCLE_SPEED * DAY_TIME_SPEED
NIGHT_SPEED = BASE_ARK_SECONDS_PER_REAL_SECOND * DAY_CYCLE_SPEED * NIGHT_TIME_SPEED

# Reference: 1 Jan 2026 14:27 UTC = Day 102 @ 01:21
REFERENCE_REAL_TIME = datetime(2026, 1, 1, 14, 27, tzinfo=timezone.utc).timestamp()
REFERENCE_TOTAL_ARK_SECONDS = (101 * ARK_DAY_SECONDS) + (1 * 3600) + (21 * 60)  # Day 102 01:21

intents = discord.Intents.default()
intents.message_content = True  # only needed for !setyear / !solunaris
bot = commands.Bot(command_prefix="!", intents=intents)

_last_name = None

def is_daytime(tod: int) -> bool:
    # Day: 05:30–19:30
    return DAY_START <= tod < NIGHT_START

def next_boundary(tod: int) -> int:
    # Returns the next boundary time-of-day in seconds
    if is_daytime(tod):
        return NIGHT_START
    # Night: either up to DAY_START (if before it) or wrap to next day's DAY_START
    return DAY_START

def speed_for(tod: int) -> float:
    return DAY_SPEED if is_daytime(tod) else NIGHT_SPEED

def advance_ark_seconds(reference_total_ark: float, real_elapsed: float) -> float:
    """
    Convert real elapsed seconds -> in-game seconds using piecewise day/night speeds.
    Returns new total ark seconds (since Day 1 00:00 baseline).
    """
    total = reference_total_ark
    remaining_real = real_elapsed

    while remaining_real > 0:
        tod = int(total % ARK_DAY_SECONDS)

        spd = speed_for(tod)  # in-game sec per real sec

        # Determine next boundary in same day; handle wrap for night crossing into next day
        nb = next_boundary(tod)
        if is_daytime(tod):
            in_game_to_boundary = nb - tod
        else:
            # night: if we're before DAY_START, boundary is same day; else wrap
            if tod < DAY_START:
                in_game_to_boundary = DAY_START - tod
            else:
                in_game_to_boundary = (ARK_DAY_SECONDS - tod) + DAY_START

        real_to_boundary = in_game_to_boundary / spd

        if remaining_real >= real_to_boundary:
            total += in_game_to_boundary
            remaining_real -= real_to_boundary
        else:
            total += remaining_real * spd
            remaining_real = 0

    return total

def get_solunaris_state():
    now = time.time()
    real_elapsed = now - REFERENCE_REAL_TIME

    total_ark = advance_ark_seconds(REFERENCE_TOTAL_ARK_SECONDS, real_elapsed)

    ark_day = int(total_ark // ARK_DAY_SECONDS) + 1
    tod = int(total_ark % ARK_DAY_SECONDS)

    hours = tod // 3600
    minutes = (tod % 3600) // 60

    emoji = "☀️" if is_daytime(tod) else "🌙"
    return ark_day, f"{hours:02d}:{minutes:02d}", emoji

async def update_voice_channel_name(force: bool = False):
    global _last_name, SOLUNARIS_YEAR

    channel = bot.get_channel(VOICE_CHANNEL_ID) or await bot.fetch_channel(VOICE_CHANNEL_ID)

    ark_day, ark_time, emoji = get_solunaris_state()
    new_name = f"{emoji} Solunaris Year {SOLUNARIS_YEAR} | Day {ark_day} | {ark_time}"

    if force or new_name != _last_name:
        await channel.edit(name=new_name)
        _last_name = new_name

@tasks.loop(seconds=10)
async def solunaris_loop():
    await update_voice_channel_name()

@bot.command()
async def setyear(ctx, year: int):
    global SOLUNARIS_YEAR
    SOLUNARIS_YEAR = year
    await update_voice_channel_name(force=True)
    await ctx.send(f"✅ Solunaris year set to {SOLUNARIS_YEAR}")

@bot.command()
async def solunaris(ctx):
    ark_day, ark_time, emoji = get_solunaris_state()
    await ctx.send(f"{emoji} Solunaris Year {SOLUNARIS_YEAR} | Day {ark_day} | {ark_time}")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await update_voice_channel_name(force=True)
    solunaris_loop.start()

bot.run(TOKEN)