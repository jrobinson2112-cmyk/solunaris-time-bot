# solunaris_time_bot.py
#
# ✅ Voice channel name format:
#    Solunaris | HH:MM | Day X
#
# ✅ Updates VC name safely every 60 seconds (avoid 429 rate limits)
# ✅ Time conversion (measured): 20 in-game minutes = 94 real seconds
#
# ✅ Slash commands (GUILD ONLY = instant):
#    /day (everyone)
#    /calibrate (Discord Admin role only)
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
from discord.ext import tasks
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

# ------------------ CONFIG ------------------
GUILD_ID = 1430388266393276509
GUILD_OBJ = discord.Object(id=GUILD_ID)

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set")

VOICE_CHANNEL_ID_RAW = os.getenv("VOICE_CHANNEL_ID")
if not VOICE_CHANNEL_ID_RAW:
    raise RuntimeError("VOICE_CHANNEL_ID is not set")
VOICE_CHANNEL_ID = int(VOICE_CHANNEL_ID_RAW)

DEFAULT_YEAR = int(os.getenv("DEFAULT_YEAR", "1"))

STATE_FILE = "solunaris_state.json"

# 1 in-game day = 24h = 86400 in-game seconds
ARK_DAY_SECONDS = 86400
DAYS_PER_YEAR = 365

# ✅ Measured conversion: 20 in-game minutes (1200s) = 94 real seconds
ARK_SECONDS_PER_REAL_SECOND = 1200 / 94  # 12.7659574468

# Safe rename cadence to avoid 429
RENAME_INTERVAL_SECONDS = 60

# Role allowed to run /calibrate
CALIBRATE_ROLE_NAME = "Discord Admin"

# ------------------ DISCORD CLIENT ------------------
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ------------------ STATE ------------------
REFERENCE_REAL_TIME = None   # unix time when calibrated
REF_YEAR = None              # year at calibration moment
REF_DAY_OF_YEAR = None       # day (1..365) at calibration moment
REF_TOD_SECONDS = None       # seconds into day at calibration moment

_last_channel_name = None
_last_rename_time = 0.0


# ------------------ HELPERS ------------------
def parse_hhmm(value: str) -> tuple[int, int]:
    value = value.strip()
    if ":" not in value:
        raise ValueError("Time must be HH:MM (example 14:28)")
    h_str, m_str = value.split(":", 1)
    h, m = int(h_str), int(m_str)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("Invalid time (HH 0–23, MM 0–59)")
    return h, m


def has_role(interaction: discord.Interaction, role_name: str) -> bool:
    """True if the invoking user has a role with the exact name."""
    if interaction.guild is None or interaction.user is None:
        return False

    member = interaction.guild.get_member(interaction.user.id)
    if member is None:
        return False

    return any(r.name == role_name for r in member.roles)


def is_calibrated() -> bool:
    return (
        REFERENCE_REAL_TIME is not None
        and REF_YEAR is not None
        and REF_DAY_OF_YEAR is not None
        and REF_TOD_SECONDS is not None
    )


def save_state() -> None:
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


def calibrate(day_of_year: int, hh: int, mm: int, year: int) -> None:
    global REFERENCE_REAL_TIME, REF_YEAR, REF_DAY_OF_YEAR, REF_TOD_SECONDS
    if not (1 <= day_of_year <= DAYS_PER_YEAR):
        raise ValueError("Day must be 1–365")

    REFERENCE_REAL_TIME = time.time()
    REF_YEAR = year
    REF_DAY_OF_YEAR = day_of_year
    REF_TOD_SECONDS = hh * 3600 + mm * 60
    save_state()


def current_solunaris() -> tuple[int, int, str]:
    """
    Returns (year, day_of_year, hhmm) from stored calibration + real elapsed time.
    Year increments after day 365.
    """
    now = time.time()
    real_elapsed = now - REFERENCE_REAL_TIME
    ark_elapsed = real_elapsed * ARK_SECONDS_PER_REAL_SECOND

    total = REF_TOD_SECONDS + ark_elapsed
    days_passed = int(total // ARK_DAY_SECONDS)
    tod = int(total % ARK_DAY_SECONDS)

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


async def rename_voice_channel(force: bool = False) -> None:
    global _last_channel_name, _last_rename_time

    if not is_calibrated():
        return

    ch = client.get_channel(VOICE_CHANNEL_ID)
    if ch is None:
        ch = await client.fetch_channel(VOICE_CHANNEL_ID)

    _, day, hhmm = current_solunaris()
    new_name = f"Solunaris | {hhmm} | Day {day}"

    now = time.time()
    if not force:
        if new_name == _last_channel_name:
            return
        if (now - _last_rename_time) < RENAME_INTERVAL_SECONDS:
            return

    await ch.edit(name=new_name)
    _last_channel_name = new_name
    _last_rename_time = now
    print(f"✅ Updated VC → {new_name}", flush=True)


# ------------------ LOOP ------------------
@tasks.loop(seconds=5)
async def tick():
    # check frequently; rename is throttled to 60s inside rename_voice_channel()
    try:
        await rename_voice_channel(force=False)
    except Exception as e:
        print(f"Tick error: {e}", flush=True)


# ------------------ SLASH COMMANDS (GUILD ONLY) ------------------
@tree.command(name="day", description="Show current Solunaris time", guild=GUILD_OBJ)
async def day_cmd(interaction: discord.Interaction):
    if not is_calibrated():
        await interaction.response.send_message(
            "⚠️ Not calibrated yet. Use `/calibrate` (Discord Admin role only).",
            ephemeral=True,
        )
        return

    year, day, hhmm = current_solunaris()
    await interaction.response.send_message(
        f"Solunaris | {hhmm} | Day {day} (Year {year})",
        ephemeral=False,
    )


@tree.command(
    name="calibrate",
    description="Discord Admin only: Calibrate Solunaris time",
    guild=GUILD_OBJ,
)
@app_commands.describe(day="Day (1–365)", time="Time HH:MM")
async def calibrate_cmd(interaction: discord.Interaction, day: int, time: str):
    # 🔐 Role check (NO Administrator permission required)
    if not has_role(interaction, CALIBRATE_ROLE_NAME):
        await interaction.response.send_message(
            f"❌ You must have the **{CALIBRATE_ROLE_NAME}** role to use this command.",
            ephemeral=True,
        )
        return

    try:
        hh, mm = parse_hhmm(time)
        calibrate(day_of_year=day, hh=hh, mm=mm, year=DEFAULT_YEAR)
        await rename_voice_channel(force=True)
        await interaction.response.send_message(
            f"✅ Calibrated → Solunaris | {hh:02d}:{mm:02d} | Day {day}",
            ephemeral=True,
        )
    except Exception as e:
        await interaction.response.send_message(
            f"❌ Calibration failed: {e}",
            ephemeral=True,
        )


# ------------------ READY EVENT (ONE SYNC ONLY) ------------------
@client.event
async def on_ready():
    loaded = load_state()
    print(f"Logged in as {client.user} | state_loaded={loaded}", flush=True)

    # ✅ ONE AND ONLY sync call — guild only
    await tree.sync(guild=GUILD_OBJ)
    print(
        f"✅ Guild commands synced: {[c.name for c in tree.get_commands(guild=GUILD_OBJ)]}",
        flush=True,
    )

    if loaded:
        await rename_voice_channel(force=True)

    if not tick.is_running():
        tick.start()


client.run(TOKEN)