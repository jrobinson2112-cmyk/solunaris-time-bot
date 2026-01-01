import discord
from discord import app_commands
from discord.ext import tasks
import time
import os

# =====================
# CONFIG (Railway Variables)
# =====================
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))

# The TEXT channel that will be renamed
TEXT_CHANNEL_ID = int(os.getenv("TEXT_CHANNEL_ID"))

# Only users with THIS role can run /calibrate
CALIBRATE_ROLE_ID = 1439069787207766076

# Update interval
UPDATE_INTERVAL = 30  # seconds

# Measured conversion:
# 20 in-game minutes = 94 real seconds => 1 in-game minute = 4.7 real seconds
REAL_SECONDS_PER_INGAME_MINUTE = 4.7

# Day/Night split
DAY_START_HOUR = 6     # 06:00
NIGHT_START_HOUR = 18  # 18:00

# =====================
# STATE (in-memory; resets on restart)
# =====================
calibration_real_time = None   # real unix timestamp
calibration_day = None         # in-game day at calibration
calibration_minute = None      # in-game minute-of-day (0..1439)

# =====================
# BOT SETUP
# =====================
intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# =====================
# HELPERS
# =====================
def get_day_night_emoji(hour: int) -> str:
    return "☀️" if DAY_START_HOUR <= hour < NIGHT_START_HOUR else "🌙"


def calculate_ingame_time():
    if calibration_real_time is None:
        return None

    elapsed_real = time.time() - calibration_real_time
    ingame_minutes_passed = elapsed_real / REAL_SECONDS_PER_INGAME_MINUTE

    total_minutes = calibration_minute + ingame_minutes_passed
    days_passed = int(total_minutes // 1440)
    minute_of_day = int(total_minutes % 1440)

    day = calibration_day + days_passed
    hour = minute_of_day // 60
    minute = minute_of_day % 60
    return day, hour, minute


async def has_calibrate_role(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False

    roles = getattr(interaction.user, "roles", None)
    if roles and any(r.id == CALIBRATE_ROLE_ID for r in roles):
        return True

    member = await interaction.guild.fetch_member(interaction.user.id)
    return any(r.id == CALIBRATE_ROLE_ID for r in member.roles)


def make_channel_name(day: int, hour: int, minute: int) -> str:
    emoji = get_day_night_emoji(hour)

    # Discord sometimes strips leading emoji in some clients.
    # The zero-width space keeps it stable.
    # Also using the same "dot" style as Discord often uses: "・"
    return f"\u200B{emoji} Solunaris Time | {hour:02d}:{minute:02d} | Day {day}"


async def update_text_channel_name():
    data = calculate_ingame_time()
    if data is None:
        return

    day, hour, minute = data
    new_name = make_channel_name(day, hour, minute)

    channel = bot.get_channel(TEXT_CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(TEXT_CHANNEL_ID)

    # If name already matches, skip edit (saves rate limit)
    if getattr(channel, "name", None) == new_name:
        return

    await channel.edit(name=new_name)

# =====================
# TASK LOOP
# =====================
@tasks.loop(seconds=UPDATE_INTERVAL)
async def channel_updater():
    try:
        await update_text_channel_name()
    except discord.HTTPException as e:
        # If Discord rate-limits briefly, just skip this cycle
        print(f"Rename HTTPException: {e}", flush=True)
    except Exception as e:
        print(f"Updater error: {e}", flush=True)

# =====================
# SLASH COMMANDS
# =====================
@tree.command(
    name="day",
    description="Show current Solunaris in-game time",
    guild=discord.Object(id=GUILD_ID),
)
async def day(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    data = calculate_ingame_time()
    if data is None:
        await interaction.followup.send(
            "❌ Time has not been calibrated yet. Use `/calibrate`.",
            ephemeral=True,
        )
        return

    day_num, hour, minute = data
    emoji = get_day_night_emoji(hour)

    await interaction.followup.send(
        f"{emoji} **Solunaris Time**\nDay **{day_num}** — **{hour:02d}:{minute:02d}**",
        ephemeral=True,
    )


@tree.command(
    name="calibrate",
    description="Calibrate Solunaris time (restricted role)",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(day="Current in-game day", hour="Hour (0–23)", minute="Minute (0–59)")
async def calibrate(interaction: discord.Interaction, day: int, hour: int, minute: int):
    await interaction.response.defer(ephemeral=True)

    if not await has_calibrate_role(interaction):
        await interaction.followup.send(
            "❌ You must have the required admin role to use `/calibrate`.",
            ephemeral=True,
        )
        return

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        await interaction.followup.send(
            "❌ Invalid time. Hour must be 0–23 and minute must be 0–59.",
            ephemeral=True,
        )
        return

    global calibration_real_time, calibration_day, calibration_minute
    calibration_real_time = time.time()
    calibration_day = day
    calibration_minute = (hour * 60) + minute

    # OPTIONAL: rename immediately once (safe for text channels)
    try:
        await update_text_channel_name()
    except Exception as e:
        await interaction.followup.send(
            f"✅ Calibrated, but channel rename failed: {e}",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        f"✅ Calibrated. Channel will update every {UPDATE_INTERVAL}s.",
        ephemeral=True,
    )

# =====================
# EVENTS
# =====================
@bot.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))

    if not channel_updater.is_running():
        channel_updater.start()

    print(f"✅ Logged in as {bot.user}", flush=True)

# =====================
# RUN
# =====================
bot.run(TOKEN)