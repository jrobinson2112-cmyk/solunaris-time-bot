import os
import asyncio
import time
import aiohttp
import discord
from discord import app_commands

# =====================
# CONFIG
# =====================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
NITRADO_API_TOKEN = os.getenv("NITRADO_API_TOKEN")
NITRADO_SERVICE_ID = os.getenv("NITRADO_SERVICE_ID")
STATUS_VC_ID = int(os.getenv("STATUS_VC_ID"))
PLAYER_CAP = int(os.getenv("PLAYER_CAP", "42"))

GUILD_ID = 1430388266393276509

NITRADO_API_URL = f"https://api.nitrado.net/services/{NITRADO_SERVICE_ID}/gameservers"

CHECK_INTERVAL = 15          # check for changes
FORCE_UPDATE_INTERVAL = 600  # force VC rename every 10 mins

# =====================
# DISCORD SETUP
# =====================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# =====================
# STATE
# =====================
last_status = None
last_players = None
last_vc_update = 0

# =====================
# NITRADO API
# =====================
async def fetch_server_status():
    headers = {
        "Authorization": f"Bearer {NITRADO_API_TOKEN}",
        "Accept": "application/json",
    }

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        async with session.get(NITRADO_API_URL, headers=headers) as resp:
            data = await resp.json()

    gs = data["data"]["gameserver"]
    status = gs["status"]
    players = gs.get("players", {}).get("online", 0)

    online = status == "started"
    return online, players

# =====================
# VC UPDATE
# =====================
async def update_status_vc(force=False):
    global last_status, last_players, last_vc_update

    try:
        online, players = await fetch_server_status()
    except Exception:
        online, players = False, 0

    now = time.time()

    changed = (online != last_status) or (players != last_players)
    force_due = (now - last_vc_update) >= FORCE_UPDATE_INTERVAL

    if not changed and not force and not force_due:
        return

    emoji = "🟢" if online else "🔴"
    name = f"{emoji} Solunaris | {players}/{PLAYER_CAP}"

    channel = client.get_channel(STATUS_VC_ID)
    if channel:
        try:
            await channel.edit(name=name)
            last_vc_update = now
            last_status = online
            last_players = players
        except Exception:
            pass

# =====================
# BACKGROUND LOOP
# =====================
async def status_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        await update_status_vc()
        await asyncio.sleep(CHECK_INTERVAL)

# =====================
# SLASH COMMAND
# =====================
@tree.command(
    name="status",
    description="Show Solunaris server status",
    guild=discord.Object(id=GUILD_ID),
)
async def status_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        online, players = await fetch_server_status()
        emoji = "🟢" if online else "🔴"
        state = "ONLINE" if online else "OFFLINE"
        msg = f"{emoji} **Solunaris is {state}** — Players: **{players}/{PLAYER_CAP}**"
    except Exception:
        msg = "🔴 **Solunaris status unavailable**"

    await interaction.followup.send(msg, ephemeral=True)

# =====================
# READY
# =====================
@client.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print("✅ Commands synced")
    client.loop.create_task(status_loop())

# =====================
# START
# =====================
client.run(DISCORD_TOKEN)