# solunaris_time_bot.py
#
# Voice channel format:
#   Solunaris | HH:MM | Day X
#
# Slash commands (guild-scoped, instant):
#   /day
#   /calibrate (ADMIN ONLY)
#
# Safe VC rename: max once every 60s

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

# Your server ID (guild-scoped slash commands)
GUILD_ID = 1430388266393276509

# ------------------ CONSTANTS ------------------
DAYS_PER_YEAR = 365
ARK_DAY_SECONDS = 86400

# Measured conversion:
# 20 in-game minutes = 94 real seconds
ARK_SECONDS_PER_REAL_SECOND = 12.7659574468

CHECK_INTERVAL_SECONDS = 2
RENAME_INTERVAL_SECONDS = 60

STATE_FILE = "solunaris_state.json"

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ------------------ STATE ------------------
REFERENCE_REAL_TIME = None
REF_YEAR = None
REF_DAY_OF_YEAR = None
REF_TOD_SECONDS = None

_last_name = None
_last_rename = 0.0


# ------------------ HELPERS ------------------
def parse_hhmm(hhmm: str):
    if ":" not in hhmm:
        raise ValueError("Time must be HH:MM")
    h, m = map(int, hhmm.split(":"))
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
        )


def load_state():
    global REFERENCE_REAL_TIME, REF_YEAR, REF_DAY_OF_YEAR, REF_TOD_SECONDS
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
        REFERENCE_REAL_TIME = d["real_time"]
        REF_YEAR = d["year"]
        REF_DAY_OF_YEAR = d["day_of_year"]
        REF_TOD_SECONDS = d["tod_seconds"]
        return True
    except Exception:
        return False


def calibrate_state(day_of_year: int, hh: int, mm: int, year: int):
    global REFERENCE_REAL_TIME, REF_YEAR, REF_DAY_OF_YEAR, REF_TOD_SECONDS
    if not (1 <= day_of_year <= DAYS_PER_YEAR):
        raise ValueError("Day must be 1–365")
    REFERENCE_REAL_TIME = time.time()
    REF_YEAR = year
    REF_DAY_OF_YEAR = day_of_year
    REF_TOD_SECONDS = hh * 3600 + mm * 60
    save_state()


def get_state():
    real_elapsed = time.time() - REFERENCE_REAL_TIME
    ark_elapsed = real_elapsed * ARK_SECONDS_PER_REAL_SECOND

    total_seconds = REF_TOD_SECONDS + ark_elapsed
    days_passed = int(total_seconds // ARK_DAY_SECONDS)
    tod = int(total_seconds % ARK_DAY_SECONDS)

    day = REF_DAY_OF_YEAR + days_passed
    year = REF_YEAR

    if day > DAYS_PER_YEAR:
        year += (day - 1) // DAYS_PER_YEAR
        day = ((day - 1) % DAYS_PER_YEAR) + 1

    h = tod // 3600
    m = (tod % 3600) // 60

    return year, day, f"{h:02d}:{m:02d}"


async def update_channel(force=False):
    global _last_name, _last_rename

    if REFERENCE_REAL_TIME is None:
        return

    channel = bot.get_channel(VOICE_CHANNEL_ID) or await bot.fetch_channel(
        VOICE_CHANNEL_ID
    )

    _, day, hhmm = get_state()
    name = f"Solunaris | {hhmm} | Day {day}"

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
    await update_channel()


# ------------------ SLASH COMMANDS ------------------
@bot.tree.command(
    name="day",
    description="Show current Solunaris time",
    guild=discord.Object(id=GUILD_ID),
)
async def day_slash(interaction: discord.Interaction):
    if REFERENCE_REAL_TIME is None:
        await interaction.response.send_message(
            "Not calibrated yet. Ask an admin to run /calibrate.",
            ephemeral=True,
        )
        return

    year, day, hhmm = get_state()
    await interaction.response.send_message(
        f"Solunaris | {hhmm} | Day {day} (Year {year})"
    )


@bot.tree.command(
    name="calibrate",
    description="ADMIN ONLY: Calibrate Solunaris time",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.default_permissions(administrator=True)
@app_commands.describe(day="Day (1–365)", time="Time HH:MM")
async def calibrate_slash(interaction: discord.Interaction, day: int, time: str):
    h, m = parse_hhmm(time)
    calibrate_state(day, h, m, CURRENT_YEAR)
    await update_channel(force=True)
    await interaction.response.send_message(
        f"Calibrated → Solunaris | {h:02d}:{m:02d} | Day {day}"
    )


@bot.event
async def on_ready():
    load_state()
    guild = discord.Object(id=GUILD_ID)
    await bot.tree.sync(guild=guild)
    await update_channel(force=True)
    clock_loop.start()
    print(f"Logged in as {bot.user}")


bot.run(TOKEN)