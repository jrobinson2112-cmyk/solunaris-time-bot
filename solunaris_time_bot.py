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
UPDATE_INTERVAL = 60  # seconds

# ARK time conversion (measured)
REAL_SECONDS_PER_INGAME_MINUTE = 4.7

# =====================
# STATE
# =====================
calibration_real_time = None
calibration_day = None
calibration_minute = None

# =====================
# BOT SETUP
# =====================
intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# =====================
# HELPERS
# =====================
def calculate_ingame_time():
    if calibration_real_time is None:
        return None

    elapsed = time.time() - calibration_real_time
    ingame_minutes_passed = elapsed / REAL_SECONDS_PER_INGAME_MINUTE

    total_minutes = calibration_minute + ingame_minutes_passed
    day = calibration_day + int(total_minutes // 1440)
    minute_of_day = int(total_minutes % 1440)

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


async def update_voice_channel():
    data = calculate_ingame_time()
    if data is None:
        return

    day, hour, minute = data
    name = f"Solunaris | {hour:02d}:{minute:02d} | Day {day}"

    channel = bot.get_channel(VOICE_CHANNEL_ID)
    if channel:
        try:
            await channel.edit(name=name)
        except discord.HTTPException:
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
            "❌ Time has not been calibrated yet.", ephemeral=True
        )
        return

    day, hour, minute = data
    await interaction.response.send_message(
        f"🕒 **Solunaris Time**\nDay **{day}** — **{hour:02d}:{minute:02d}**",
        ephemeral=True
    )


@tree.command(name="calibrate", description="Calibrate Solunaris time", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(day="Current in-game day", hour="Hour (0–23)", minute="Minute (0–59)")
async def calibrate(
    interaction: discord.Interaction,
    day: int,
    hour: int,
    minute: int
):
    if not await has_calibrate_role(interaction):
        await interaction.response.send_message(
            "❌ You must have the required admin role to use this command.",
            ephemeral=True
        )
        return

    global calibration_real_time, calibration_day, calibration_minute

    calibration_real_time = time.time()
    calibration_day = day
    calibration_minute = hour * 60 + minute

    await update_voice_channel()

    await interaction.response.send_message(
        f"✅ Calibrated to **Day {day} — {hour:02d}:{minute:02d}**",
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