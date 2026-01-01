import discord
from discord.ext import commands, tasks
import os
import time
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
TEXT_CHANNEL_ID = int(os.getenv("TEXT_CHANNEL_ID"))
ARK_YEAR = int(os.getenv("DEFAULT_YEAR", 1))

ARK_SECONDS_PER_REAL_SECOND = 355.263
ARK_DAY_SECONDS = 86400

REFERENCE_REAL_TIME = datetime(2026, 1, 1, 14, 27, tzinfo=timezone.utc).timestamp()
REFERENCE_ARK_DAY_NUMBER = 102
REFERENCE_ARK_TIME_SECONDS = (1 * 3600) + (21 * 60)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

def get_solunaris_day_and_time():
    now = time.time()
    real_elapsed = now - REFERENCE_REAL_TIME
    ark_seconds_elapsed = real_elapsed * ARK_SECONDS_PER_REAL_SECOND

    total_ark_seconds = REFERENCE_ARK_TIME_SECONDS + ark_seconds_elapsed
    days_elapsed = int(total_ark_seconds // ARK_DAY_SECONDS)
    ark_day = REFERENCE_ARK_DAY_NUMBER + days_elapsed

    time_of_day_seconds = int(total_ark_seconds % ARK_DAY_SECONDS)
    hours = time_of_day_seconds // 3600
    minutes = (time_of_day_seconds % 3600) // 60

    emoji = "☀️" if 5 <= hours < 19 else "🌙"
    return ark_day, f"{hours:02d}:{minutes:02d}", emoji

async def update_channel():
    channel = bot.get_channel(TEXT_CHANNEL_ID)
    ark_day, ark_time, emoji = get_solunaris_day_and_time()
    new_name = f"{emoji} Solunaris Year {ARK_YEAR} | Day {ark_day} | {ark_time}"
    if channel.name != new_name:
        await channel.edit(name=new_name)

@tasks.loop(seconds=60)
async def solunaris_loop():
    await update_channel()

@bot.command()
async def setyear(ctx, year: int):
    global ARK_YEAR
    ARK_YEAR = year
    await update_channel()
    await ctx.send(f"✅ Solunaris year set to {ARK_YEAR}")

@bot.command()
async def solunaris(ctx):
    ark_day, ark_time, emoji = get_solunaris_day_and_time()
    await ctx.send(f"{emoji} Solunaris Year {ARK_YEAR} | Day {ark_day} | {ark_time}")

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    solunaris_loop.start()

bot.run(TOKEN)
