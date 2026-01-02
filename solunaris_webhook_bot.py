import os, time, json, asyncio, aiohttp
import discord
from discord import app_commands
from rcon.source import Client as RconClient

# =====================
# CONFIG
# =====================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = int(os.getenv("RCON_PORT", "11020"))
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

GUILD_ID = 1430388266393276509
ADMIN_ROLE_ID = 1439069787207766076

PLAYER_CAP = 42

DAY_SPM = 4.7666667
NIGHT_SPM = 4.045

SUNRISE = 5 * 60 + 30
SUNSET = 17 * 60 + 30

DAY_COLOR = 0xF1C40F
NIGHT_COLOR = 0x5865F2

STATE_FILE = "state.json"

STATUS_VC_ID = 0  # <-- put your VC ID here

# =====================
# DISCORD
# =====================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# =====================
# STATE
# =====================
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return None

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f)

state = load_state()
webhook_msg_id = None
last_vc_name = None
last_status = None
last_force = 0

# =====================
# TIME LOGIC
# =====================
def is_day(minute): return SUNRISE <= minute < SUNSET

def spm(minute): return DAY_SPM if is_day(minute) else NIGHT_SPM

def advance(day, minute, elapsed):
    m = float(minute)
    d = day
    while elapsed > 0:
        s = spm(int(m) % 1440)
        step = min(elapsed, s)
        m += step / s
        elapsed -= step
        if m >= 1440:
            m -= 1440
            d += 1
    return d, int(m)

def current_time():
    if not state: return None
    elapsed = time.time() - state["epoch"]
    d, m = advance(state["day"], state["minute"], elapsed)
    y = state["year"]
    while d > 365:
        d -= 365
        y += 1
    h = m // 60
    mi = m % 60
    day_now = is_day(m)
    emoji = "☀️" if day_now else "🌙"
    color = DAY_COLOR if day_now else NIGHT_COLOR
    title = f"{emoji} | **Solunaris Time** | **{h:02d}:{mi:02d}** | Day {d} | Year {y}"
    return title, color, spm(m)

# =====================
# RCON STATUS
# =====================
def get_status():
    try:
        with RconClient(RCON_HOST, RCON_PORT, passwd=RCON_PASSWORD, timeout=3) as r:
            players = r.run("ListPlayers")
            count = players.count("\n") if players else 0
            return True, count
    except Exception:
        return False, None

# =====================
# LOOPS
# =====================
async def time_loop():
    global webhook_msg_id
    await client.wait_until_ready()
    async with aiohttp.ClientSession() as session:
        while True:
            if state:
                title, color, wait = current_time()
                embed = {"description": title, "color": color}
                if webhook_msg_id:
                    await session.patch(f"{WEBHOOK_URL}/messages/{webhook_msg_id}", json={"embeds":[embed]})
                else:
                    async with session.post(WEBHOOK_URL+"?wait=true", json={"embeds":[embed]}) as r:
                        webhook_msg_id = (await r.json())["id"]
                await asyncio.sleep(wait)
            else:
                await asyncio.sleep(5)

async def status_loop():
    global last_vc_name, last_status, last_force
    await client.wait_until_ready()
    vc = client.get_channel(STATUS_VC_ID)
    while True:
        online, count = get_status()
        name = f"{'🟢' if online else '🔴'} Solunaris | {count if online else 'Offline'}/{PLAYER_CAP}"
        force = time.time() - last_force > 600
        if name != last_vc_name or force:
            await vc.edit(name=name)
            last_vc_name = name
            last_force = time.time()
        await asyncio.sleep(15)

# =====================
# COMMANDS
# =====================
@tree.command(name="day", guild=discord.Object(id=GUILD_ID))
async def day_cmd(i: discord.Interaction):
    t,_,_ = current_time()
    await i.response.send_message(t or "Not set", ephemeral=True)

@tree.command(name="settime", guild=discord.Object(id=GUILD_ID))
async def settime(i: discord.Interaction, year:int, day:int, hour:int, minute:int):
    if not any(r.id==ADMIN_ROLE_ID for r in i.user.roles):
        await i.response.send_message("❌ No permission", ephemeral=True)
        return
    global state
    state = {"epoch":time.time(),"year":year,"day":day,"minute":hour*60+minute}
    save_state(state)
    await i.response.send_message("✅ Time set", ephemeral=True)

@tree.command(name="status", guild=discord.Object(id=GUILD_ID))
async def status_cmd(i: discord.Interaction):
    online, count = get_status()
    msg = f"{'🟢' if online else '🔴'} Solunaris is {'ONLINE' if online else 'OFFLINE'} — Players: {count}/{PLAYER_CAP}"
    await i.response.send_message(msg, ephemeral=True)

# =====================
# START
# =====================
@client.event
async def on_ready():
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    client.loop.create_task(time_loop())
    client.loop.create_task(status_loop())
    print("✅ Bot ready")

client.run(DISCORD_TOKEN)