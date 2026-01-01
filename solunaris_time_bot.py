# solunaris_time_bot.py
#
# ✅ Voice channel name format:
#    Solunaris | HH:MM | Day X
#
# ✅ Updates the VC name safely every 60s (avoids Discord 429 rate limits)
# ✅ Time conversion (measured day & night): 20 in-game minutes = 94 real seconds
# ✅ Auto year rolling: 1 year = 365 days (Year increases after Day 365)
#
# ✅ Slash commands (guild-scoped = instant):
#    /day                 (everyone)
#    /calibrate           (ADMIN ONLY)
#
# ✅ Fixes "Application command not found":
#    - ONLY syncs guild commands (no global command deletion in code)
#    - clears + syncs guild command tree on startup
#    - prints which commands are registered
#
# REQUIRED Railway Variables:
#   DISCORD_TOKEN
#   VOICE_CHANNEL_ID
#
# OPTIONAL Railway Variables:
#   DEFAULT_YEAR (default 1)
#
# NOTE:
#   If you previously created global slash commands, Discord may show duplicates
#   for up to ~1 hour. Guild commands will work immediately.

import os
import time
import json
import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

# ------------------ CONFIG ------------------
GUILD_ID = 1430388266393276509
GUILD_OBJ = discord.Object(id=GUILD_ID)

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set in Railway Variables")

_voice_id = os.getenv("VOICE_CHANNEL_ID")
if not _voice_id:
    raise RuntimeError("VOICE_CHANNEL_ID is not set in Railway Variables")
VOICE_CHANNEL_ID = int(_voice_id)

DEFAULT_YEAR = int(os.getenv("DEFAULT_YEAR", 1))

DAYS_PER_YEAR = 365
ARK_DAY_SECONDS = 86400

# ✅ Measured: 20 in-game minutes = 94 real seconds
ARK_SECONDS_PER_REAL_SECOND = (20 * 60) / 94  # 12.7659574468

# Internal loop checks frequently, but we only rename VC at most once per 60s.
CHECK_INTERVAL_SECONDS = 2
RENAME_INTERVAL_SECONDS = 60

STATE_FILE = "solunaris_state.json"

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# ------------------ STATE ------------------
REFERENCE_REAL_TIME = None   # real unix timestamp when calibrated
REF_YEAR = None              # year at calibration moment
REF_DAY_OF_YEAR = None       # day (1..365) at calibration moment
REF_TOD_SECONDS = None       # time-of-day (seconds) at calibration moment

_last_name = None
_last_rename = 0.0


# ------------------ HELPERS ------------------
def parse_hhmm(hhmm: str):
    hhmm = hhmm.strip()
    if ":" not in hhmm:
        raise ValueError("Time must be HH:MM (example 14:28)")
    h, m = hhmm.split(":", 1)
    h, m = int(h), int(m)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("Invalid time. HH 0-23, MM 0-59.")
    return h, m


def is_calibrated() -> bool:
    return (
        REFERENCE_REAL_TIME is not None
        and REF_YEAR is not None
        and REF_DAY_OF_YEAR is not None
        and REF_TOD_SECONDS is not None
    )


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
    global REFERENCE_REAL_TIME, REF_YEAR, REF_DAY_OF_YEAR, REF_TOD_SECONDS
    if not (1 <= day_of_year <= DAYS_PER_YEAR):
        raise ValueError("Day must be 1–365")

    REFERENCE_REAL_TIME = time.time()
    REF_YEAR = year
    REF_DAY_OF_YEAR = day_of_year
    REF_TOD_SECONDS = (hh * 3600) + (mm * 60)
    save_state()


def get_state():
    """Return (year, day_of_year, hhmm) based on calibration + elapsed real time."""
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
    return year, day_of_year, hhmm


async def update_channel(force: bool = False):
    """Rename the voice channel (rate-limit safe)."""
    global _last_name, _last_rename

    if not is_calibrated():
        return

    channel = bot.get_channel(VOICE_CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(VOICE_CHANNEL_ID)

    _, day, hhmm = get_state()
    name = f"Solunaris | {hhmm} | Day {day}"

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


# ------------------ SLASH COMMANDS (GUILD ONLY) ------------------
@bot.tree.command(name="day", description="Show current Solunaris time", guild=GUILD_OBJ)
async def day_slash(interaction: discord.Interaction):
    if not is_calibrated():
        await interaction.response.send_message(
            "⚠️ Not calibrated yet. Ask an admin to run `/calibrate`.",
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
    guild=GUILD_OBJ,
)
@app_commands.default_permissions(administrator=True)
@app_commands.describe(day="Day (1–365)", time="Time HH:MM")
async def calibrate_slash(interaction: discord.Interaction, day: int, time: str):
    try:
        hh, mm = parse_hhmm(time)
        calibrate_state(day, hh, mm, DEFAULT_YEAR)
        await update_channel(force=True)
        await interaction.response.send_message(
            f"✅ Calibrated → Solunaris | {hh:02d}:{mm:02d} | Day {day}"
        )
    except Exception as e:
        await interaction.response.send_message(
            f"❌ Calibration failed: {e}",
            ephemeral=True,
        )


# ------------------ STARTUP ------------------
@bot.event
async def on_ready():
    loaded = load_state()
    print(f"Logged in as {bot.user} | state_loaded={loaded}")

    try:
        # ✅ Clear and sync ONLY guild commands (fast + reliable)
        bot.tree.clear_commands(guild=GUILD_OBJ)
        await bot.tree.sync(guild=GUILD_OBJ)

        cmds = [c.name for c in bot.tree.get_commands(guild=GUILD_OBJ)]
        print(f"✅ Guild commands synced: {cmds}")
    except Exception as e:
        print(f"❌ Slash command sync failed: {e}")

    if loaded:
        await update_channel(force=True)

    if not clock_loop.is_running():
        clock_loop.start()


bot.run(TOKEN)