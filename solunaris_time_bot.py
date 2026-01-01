import discord
from discord import app_commands
from discord.ext import tasks
import time
import os

# =====================
# CONFIG
# =====================
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
VOICE_CHANNEL_ID = int(os.getenv("VOICE_CHANNEL_ID"))

CALIBRATE_ROLE_ID = 1439069787207766076  # <-- REQUIRED ROLE
UPDATE_INTERVAL = 120  # seconds (safe for rate limits)

# Measured conversion:
# 20 in-game minutes = 94 real seconds => 1 in-game minute = 4.7 real seconds
REAL_SECONDS_PER_INGAME_MINUTE = 4.7

# Day/Night split (ARK-like)
DAY_START_HOUR = 6    # 06:00
NIGHT_START_HOUR = 18 # 18:00

# =====================
# STATE
# =====================
calibration_real_time = None
calibration_day = None
calibration_minute = None  # minute-of-day at calibration time

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
    """
    Returns (day, hour, minute) based on calibration and elapsed real time.
    """
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
    """
    Reliable role check (works even if member cache is empty).
    """
    if interaction.guild is None:
        return False

    roles = getattr(interaction.user, "roles", None)
    if roles and any(r.id == CALIBRATE_ROLE_ID for r in roles):
        return True

    member = await interaction.guild.fetch_member(interaction.user.id)
    return any(r.id == CALIBRATE_ROLE_ID for r in member.roles)


async def update_voice_channel():
    data = calculate_ingame_time()
    if data is None:
        return

    day, hour, minute = data
    emoji = get_day_night_emoji(hour)

    # ✅ Voice Channel format:
    # ☀️ Solunaris | 14:28 | Day 103
    name = f"{emoji} Solunaris | {hour:02d}:{minute:02d} | Day {day}"

    channel = bot.get_channel(VOICE_CHANNEL_ID)
    if channel is None:
        channel = await bot.fetch_channel(VOICE_CHANNEL_ID)

    try:
        await channel.edit(name=name)
    except discord.HTTPException:
        # If Discord blocks a rename momentarily, we just skip this cycle.
        pass

# =====================
# TASK LOOP
# =====================
@tasks.loop(seconds=UPDATE_INTERVAL)
async def voice_channel_updater():
    await update_voice_channel()

# =====================
# SLASH COMMANDS
# =====================
@tree.command(name="day", description="Show current Solunaris in-game time", guild=discord.Object(id=GUILD_ID))
async def day(interaction: discord.Interaction):
    data = calculate_ingame_time()
    if data is None:
        await interaction.response.send_message(
            "❌ Time has not been calibrated yet. Use `/calibrate`.",
            ephemeral=True
        )
        return

    day_num, hour, minute = data
    emoji = get_day_night_emoji(hour)

    await interaction.response.send_message(
        f"{emoji} **Solunaris Time**\nDay **{day_num}** — **{hour:02d}:{minute:02d}**",
        ephemeral=True
    )


@tree.command(name="calibrate", description="Calibrate Solunaris time", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(day="Current in-game day", hour="Hour (0–23)", minute="Minute (0–59)")
async def calibrate(interaction: discord.Interaction, day: int, hour: int, minute: int):
    if not await has_calibrate_role(interaction):
        await interaction.response.send_message(
            "❌ You must have the required admin role to use this command.",
            ephemeral=True
        )
        return

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        await interaction.response.send_message(
            "❌ Invalid time. Hour must be 0–23 and minute must be 0–59.",
            ephemeral=True
        )
        return

    global calibration_real_time, calibration_day, calibration_minute

    calibration_real_time = time.time()
    calibration_day = day
    calibration_minute = (hour * 60) + minute

    await update_voice_channel()

    emoji = get_day_night_emoji(hour)
    await interaction.response.send_message(
        f"✅ Calibrated to {emoji} **Solunaris | {hour:02d}:{minute:02d} | Day {day}**",
        ephemeral=True
    )

# =====================
# EVENTS
# =====================
@bot.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))

    if not voice_channel_updater.is_running():
        voice_channel_updater.start()

    print(f"✅ Logged in as {bot.user}")

# =====================
# RUN
# =====================
bot.run(TOKEN)