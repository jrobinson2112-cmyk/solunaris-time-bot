import os
import time
import json
import asyncio
import aiohttp
import discord
from discord import app_commands

# =====================
# ENV
# =====================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PLAYERS_WEBHOOK_URL = os.getenv("PLAYERS_WEBHOOK_URL")
NITRADO_TOKEN = os.getenv("NITRADO_TOKEN")
NITRADO_SERVICE_ID = os.getenv("NITRADO_SERVICE_ID")

if not all([DISCORD_TOKEN, WEBHOOK_URL, PLAYERS_WEBHOOK_URL, NITRADO_TOKEN, NITRADO_SERVICE_ID]):
    raise RuntimeError("Missing required environment variables")

# =====================
# CONSTANTS
# =====================
GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076
STATUS_VC_ID = 1456615806887657606
ANNOUNCE_CHANNEL_ID = 1430388267446042666
PLAYER_CAP = 42

DAY_SPM = 4.7666667
NIGHT_SPM = 4.045
SUNRISE = 5 * 60 + 30
SUNSET = 17 * 60 + 30

DAY_COLOR = 0xF1C40F
NIGHT_COLOR = 0x5865F2

STATE_FILE = "state.json"

# =====================
# DISCORD SETUP
# =====================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# =====================
# SHARED STATE
# =====================
message_ids = {
    "time": None,
    "players": None,
}

last_announced_day = None

# =====================
# STATE FILE
# =====================
def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f)

state = load_state()

# =====================
# TIME LOGIC
# =====================
def is_day(minute):
    return SUNRISE <= minute < SUNSET

def spm(minute):
    return DAY_SPM if is_day(minute) else NIGHT_SPM

def calculate_time():
    if not state:
        return None

    elapsed = time.time() - state["epoch"]
    minute_of_day = state["hour"] * 60 + state["minute"]
    day = state["day"]
    year = state["year"]

    remaining = elapsed
    while remaining > 0:
        s = spm(minute_of_day)
        remaining -= s
        minute_of_day += 1
        if minute_of_day >= 1440:
            minute_of_day = 0
            day += 1
            if day > 365:
                day = 1
                year += 1

    hour = minute_of_day // 60
    minute = minute_of_day % 60
    emoji = "☀️" if is_day(minute_of_day) else "🌙"
    color = DAY_COLOR if is_day(minute_of_day) else NIGHT_COLOR

    title = f"{emoji} | Solunaris Time | {hour:02d}:{minute:02d} | Day {day} | Year {year}"
    return title, color, year, day

# =====================
# NITRADO STATUS
# =====================
async def get_server_status():
    headers = {"Authorization": f"Bearer {NITRADO_TOKEN}"}
    url = f"https://api.nitrado.net/services/{NITRADO_SERVICE_ID}/gameservers"

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as r:
            data = await r.json()

    gs = data["data"]["gameserver"]
    online = gs["status"] in ("started", "running", "online")
    players = int(gs.get("query", {}).get("player_current", 0))
    return online, players

# =====================
# WEBHOOK HELPER
# =====================
async def upsert_webhook(session, url, key, embed):
    mid = message_ids[key]
    if mid:
        await session.patch(f"{url}/messages/{mid}", json={"embeds": [embed]})
    else:
        async with session.post(url + "?wait=true", json={"embeds": [embed]}) as r:
            message_ids[key] = (await r.json())["id"]

# =====================
# LOOPS
# =====================
async def time_loop():
    global last_announced_day
    await client.wait_until_ready()

    async with aiohttp.ClientSession() as session:
        while True:
            t = calculate_time()
            if t:
                title, color, year, day = t
                embed = {"title": title, "color": color}
                await upsert_webhook(session, WEBHOOK_URL, "time", embed)

                absolute_day = year * 365 + day
                if last_announced_day is None:
                    last_announced_day = absolute_day
                elif absolute_day > last_announced_day:
                    ch = client.get_channel(ANNOUNCE_CHANNEL_ID)
                    if ch:
                        await ch.send(f"📅 **New Solunaris Day** — Day **{day}**, Year **{year}**")
                    last_announced_day = absolute_day

            await asyncio.sleep(DAY_SPM)

async def status_loop():
    await client.wait_until_ready()
    async with aiohttp.ClientSession() as session:
        while True:
            online, players = await get_server_status()
            emoji = "🟢" if online else "🔴"

            vc = client.get_channel(STATUS_VC_ID)
            if vc:
                await vc.edit(name=f"{emoji} Solunaris | {players}/{PLAYER_CAP}")

            embed = {
                "title": "Online Players",
                "description": f"**{players}/{PLAYER_CAP}** online",
                "color": 0x2ECC71 if online else 0xE74C3C,
            }
            await upsert_webhook(session, PLAYERS_WEBHOOK_URL, "players", embed)

            await asyncio.sleep(15)

# =====================
# COMMANDS
# =====================
@tree.command(name="settime", guild=discord.Object(id=GUILD_ID))
async def settime(i: discord.Interaction, year: int, day: int, hour: int, minute: int):
    if not any(r.id == ADMIN_ROLE_ID for r in i.user.roles):
        await i.response.send_message("❌ No permission", ephemeral=True)
        return

    global state
    state = {
        "epoch": time.time(),
        "year": year,
        "day": day,
        "hour": hour,
        "minute": minute,
    }
    save_state(state)
    await i.response.send_message("✅ Time set", ephemeral=True)

@tree.command(name="status", guild=discord.Object(id=GUILD_ID))
async def status(i: discord.Interaction):
    online, players = await get_server_status()
    emoji = "🟢" if online else "🔴"
    await i.response.send_message(
        f"{emoji} **Solunaris** — {players}/{PLAYER_CAP} players",
        ephemeral=True,
    )

# =====================
# START
# =====================
@client.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    client.loop.create_task(time_loop())
    client.loop.create_task(status_loop())
    print("✅ Solunaris bot online")

client.run(DISCORD_TOKEN)