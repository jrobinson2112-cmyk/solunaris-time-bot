import os
import time
import json
import asyncio
import discord
from discord import app_commands
import a2s

# =========================
# CONFIG
# =========================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

ARK_ADDRESS = ("31.214.239.2", 5020)
PLAYER_CAP = 42

STATE_FILE = "state.json"

DAY_SECONDS_PER_INGAME_MINUTE = 4.7666667
NIGHT_SECONDS_PER_INGAME_MINUTE = 4.045

SUNRISE_MIN = 330   # 05:30
SUNSET_MIN  = 1050  # 17:30

DAY_COLOR = 0xF1C40F
NIGHT_COLOR = 0x5865F2

# =========================
# DISCORD SETUP
# =========================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# =========================
# STATE
# =========================
def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(data):
    with open(STATE_FILE, "w") as f:
        json.dump(data, f)

state = load_state()

# =========================
# TIME HELPERS
# =========================
def is_day(minute_of_day: int) -> bool:
    return SUNRISE_MIN <= minute_of_day < SUNSET_MIN

def calculate_time():
    if not state:
        return None

    elapsed = time.time() - state["real_epoch"]
    minute_len = DAY_SECONDS_PER_INGAME_MINUTE

    minutes_passed = int(elapsed / minute_len)

    total_minutes = (
        (state["day"] - 1) * 1440 +
        state["hour"] * 60 +
        state["minute"] +
        minutes_passed
    )

    day = (total_minutes // 1440) + 1
    minute_of_day = total_minutes % 1440

    year = state["year"]
    while day > 365:
        day -= 365
        year += 1

    hour = minute_of_day // 60
    minute = minute_of_day % 60

    emoji = "☀️" if is_day(minute_of_day) else "🌙"
    color = DAY_COLOR if is_day(minute_of_day) else NIGHT_COLOR

    return {
        "text": f"{emoji} | Solunaris Time | {hour:02d}:{minute:02d} | Day {day} | Year {year}",
        "color": color
    }

# =========================
# SERVER STATUS (A2S ONLY)
# =========================
def query_server():
    try:
        info = a2s.info(ARK_ADDRESS, timeout=2.0)
        players = a2s.players(ARK_ADDRESS, timeout=2.0)
        return True, len(players)
    except Exception:
        return False, None

# =========================
# COMMANDS
# =========================
@tree.command(name="status", description="Show server status", guild=discord.Object(id=GUILD_ID))
async def status_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    online, players = query_server()

    if online:
        msg = f"🟢 **Solunaris is ONLINE** — Players: {players}/{PLAYER_CAP}"
    else:
        msg = f"🔴 **Solunaris is OFFLINE**"

    await interaction.followup.send(msg, ephemeral=True)

@tree.command(name="day", description="Show current Solunaris time", guild=discord.Object(id=GUILD_ID))
async def day_cmd(interaction: discord.Interaction):
    if not state:
        await interaction.response.send_message("⏳ Time not set.", ephemeral=True)
        return

    data = calculate_time()
    await interaction.response.send_message(data["text"], ephemeral=True)

@tree.command(name="settime", description="Set Solunaris time", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(year="Year", day="Day (1-365)", hour="Hour", minute="Minute")
async def settime_cmd(interaction: discord.Interaction, year: int, day: int, hour: int, minute: int):
    if not any(r.id == ADMIN_ROLE_ID for r in interaction.user.roles):
        await interaction.response.send_message("❌ No permission.", ephemeral=True)
        return

    if not (1 <= day <= 365 and 0 <= hour <= 23 and 0 <= minute <= 59):
        await interaction.response.send_message("❌ Invalid values.", ephemeral=True)
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

    await interaction.response.send_message("✅ Time updated.", ephemeral=True)

# =========================
# STARTUP
# =========================
@client.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print("✅ Bot online and commands synced")

client.run(DISCORD_TOKEN)