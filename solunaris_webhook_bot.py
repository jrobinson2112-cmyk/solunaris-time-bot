import os
import time
import json
import asyncio
import aiohttp
import discord
from discord import app_commands

# =====================
# CONFIG (ENV VARS)
# =====================
TOKEN = os.getenv("DISCORD_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

SECONDS_PER_INGAME_MINUTE = 4.9333
UPDATE_INTERVAL = 4.9333  # seconds (SAFE)

STATE_FILE = "state.json"

# Day/Night split
DAY_START = 6 * 60     # 06:00
NIGHT_START = 18 * 60 # 18:00

# =====================
# DISCORD SETUP
# =====================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# =====================
# STATE
# =====================
def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

state = load_state()

# =====================
# TIME CALCULATION
# =====================
def get_ingame_time():
    if not state:
        return None

    elapsed_real = time.time() - state["real_epoch"]
    elapsed_minutes = int(elapsed_real / SECONDS_PER_INGAME_MINUTE)

    start_minutes = state["hour"] * 60 + state["minute"]
    total_minutes = start_minutes + elapsed_minutes

    days_passed, minute_of_day = divmod(total_minutes, 1440)
    hour, minute = divmod(minute_of_day, 60)

    day = state["day"] + days_passed
    year = state["year"]

    while day > 365:
        day -= 365
        year += 1

    is_day = DAY_START <= (hour * 60 + minute) < NIGHT_START
    emoji = "☀️" if is_day else "🌙"

    title = f"{emoji} | Solunaris Time | {hour:02d}:{minute:02d} | Day {day} | Year {year}"
    return title

# =====================
# WEBHOOK UPDATE LOOP
# =====================
async def update_loop():
    await client.wait_until_ready()
    async with aiohttp.ClientSession() as session:
        while True:
            if state:
                title = get_ingame_time()
                embed = {
                    "title": title,
                    "color": 0xF1C40F,
                }
                await session.post(
                    WEBHOOK_URL,
                    json={"embeds": [embed]},
                )
            await asyncio.sleep(UPDATE_INTERVAL)

# =====================
# SLASH COMMANDS
# =====================
@tree.command(name="day", description="Show current Solunaris time", guild=discord.Object(id=GUILD_ID))
async def day(interaction: discord.Interaction):
    if not state:
        await interaction.response.send_message("⏳ Time has not been set yet.", ephemeral=True)
        return

    await interaction.response.send_message(get_ingame_time(), ephemeral=True)

@tree.command(name="settime", description="Set Solunaris time", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(
    year="Starting year",
    day="Day of year (1-365)",
    hour="Hour (0-23)",
    minute="Minute (0-59)",
)
async def settime(
    interaction: discord.Interaction,
    year: int,
    day: int,
    hour: int,
    minute: int,
):
    if not any(r.id == ADMIN_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message(
            "❌ You do not have permission to use this command.",
            ephemeral=True,
        )
        return

    global state
    state = {
        "real_epoch": time.time(),
        "year": year,
        "day": day,
        "hour": hour,
        "minute": minute,
    }
    save_state(state)

    await interaction.response.send_message(
        f"✅ Time set to **Day {day}, {hour:02d}:{minute:02d}, Year {year}**",
        ephemeral=True,
    )

# =====================
# STARTUP
# =====================
@client.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print("✅ Commands synced")
    client.loop.create_task(update_loop())

client.run(TOKEN)