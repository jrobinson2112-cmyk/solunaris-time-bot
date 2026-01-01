# solunaris_time_bot.py
#
# Renames a DISCORD VOICE CHANNEL to show Solunaris Year/Day + in-game time.
# Updates every 10 seconds IRL (but only renames when the displayed text changes).

import os
import time
import discord
from discord.ext import commands, tasks
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
VOICE_CHANNEL_ID = int(os.getenv("VOICE_CHANNEL_ID"))  # <-- set this in Railway Variables
SOLUNARIS_YEAR = int(os.getenv("DEFAULT_YEAR", 1))     # <-- set this in Railway Variables

# ---- Solunaris server timing (your Nitrado settings) ----
ARK_SECONDS_PER_REAL_SECOND = 355.263
ARK_DAY_SECONDS = 86400

# Reference: 1 Jan 2026 14:27 UTC = Day 102 @ 01:21
REFERENCE_REAL_TIME = datetime(2026, 1, 1, 14, 27, tzinfo=timezone.utc).timestamp()
REFERENCE_ARK_DAY_NUMBER = 102
REFERENCE_ARK_TIME_SECONDS = (1 * 3600) + (21 * 60)  # 01:21

intents = discord.Intents.default()
# Message content is only required if you want !setyear / !solunaris commands:
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

_last_name: str | None = None

def get_solunaris_day_time_and_emoji():
    now = time.time()
    real_elapsed = now - REFERENCE_REAL_TIME
    ark_seconds_elapsed = real_elapsed * ARK_SECONDS_PER_REAL_SECOND

    total_ark_seconds = REFERENCE_ARK_TIME_SECONDS + ark_seconds_elapsed

    days_elapsed = int(total_ark_seconds // ARK_DAY_SECONDS)
    ark_day = REFERENCE_ARK_DAY_NUMBER + days_elapsed

    time_of_day_seconds = int(total_ark_seconds % ARK_DAY_SECONDS)
    hours = time_of_day_seconds // 3600
    minutes = (time_of_day_seconds % 3600) // 60

    # Day/Night emoji (simple split; adjust if you want exact ASA sunrise/sunset rules)
    emoji = "☀️" if 5 <= hours < 19 else "🌙"

    return ark_day, f"{hours:02d}:{minutes:02d}", emoji

async def update_voice_channel_name(force: bool = False):
    global _last_name, SOLUNARIS_YEAR

    channel = bot.get_channel(VOICE_CHANNEL_ID)
    if channel is None:
        # Fallback in case cache isn't ready yet
        channel = await bot.fetch_channel(VOICE_CHANNEL_ID)

    ark_day, ark_time, emoji = get_solunaris_day_time_and_emoji()
    new_name = f"{emoji} Solunaris Year {SOLUNARIS_YEAR} | Day {ark_day} | {ark_time}"

    # Only call Discord API if the name actually needs changing
    if force or new_name != _last_name:
        await channel.edit(name=new_name)
        _last_name = new_name

@tasks.loop(seconds=10)
async def solunaris_loop():
    await update_voice_channel_name()

@bot.command()
async def setyear(ctx, year: int):
    """Set the Solunaris year manually: !setyear 2"""
    global SOLUNARIS_YEAR
    SOLUNARIS_YEAR = year
    await update_voice_channel_name(force=True)
    await ctx.send(f"✅ Solunaris year set to {SOLUNARIS_YEAR}")

@bot.command()
async def solunaris(ctx):
    """Show current Solunaris time: !solunaris"""
    ark_day, ark_time, emoji = get_solunaris_day_time_and_emoji()
    await ctx.send(f"{emoji} Solunaris Year {SOLUNARIS_YEAR} | Day {ark_day} | {ark_time}")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    await update_voice_channel_name(force=True)
    solunaris_loop.start()

bot.run(TOKEN)