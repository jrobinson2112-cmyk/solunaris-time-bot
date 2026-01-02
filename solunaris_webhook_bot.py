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
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # time webhook
PLAYERS_WEBHOOK_URL = os.getenv("PLAYERS_WEBHOOK_URL")  # players webhook
NITRADO_TOKEN = os.getenv("NITRADO_TOKEN")
NITRADO_SERVICE_ID = os.getenv("NITRADO_SERVICE_ID")

RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT = os.getenv("RCON_PORT")
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

required = [DISCORD_TOKEN, WEBHOOK_URL, PLAYERS_WEBHOOK_URL, NITRADO_TOKEN, NITRADO_SERVICE_ID, RCON_HOST, RCON_PORT, RCON_PASSWORD]
if not all(required):
    missing = []
    for k in ["DISCORD_TOKEN", "WEBHOOK_URL", "PLAYERS_WEBHOOK_URL", "NITRADO_TOKEN", "NITRADO_SERVICE_ID", "RCON_HOST", "RCON_PORT", "RCON_PASSWORD"]:
        if not os.getenv(k):
            missing.append(k)
    raise RuntimeError("Missing required environment variables: " + ", ".join(missing))

RCON_PORT = int(RCON_PORT)

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
# NITRADO STATUS (COUNT)
# =====================
async def get_server_status(session: aiohttp.ClientSession):
    headers = {"Authorization": f"Bearer {NITRADO_TOKEN}"}
    url = f"https://api.nitrado.net/services/{NITRADO_SERVICE_ID}/gameservers"

    async with session.get(url, headers=headers) as r:
        data = await r.json()

    gs = data["data"]["gameserver"]
    # nitrado sometimes returns different status strings, keep it flexible
    status = str(gs.get("status", "")).lower()
    online = status in ("started", "running", "online")

    players = int(gs.get("query", {}).get("player_current", 0) or 0)
    return online, players

# =====================
# RCON (Source RCON)
# =====================
def _rcon_make_packet(req_id: int, ptype: int, body: str) -> bytes:
    data = body.encode("utf-8") + b"\x00"
    packet = req_id.to_bytes(4, "little", signed=True) + ptype.to_bytes(4, "little", signed=True) + data + b"\x00"
    size = len(packet)
    return size.to_bytes(4, "little", signed=True) + packet

async def rcon_command(command: str, timeout: float = 5.0) -> str:
    """
    Minimal Source RCON client.
    ptype: 3 = auth, 2 = exec command
    """
    reader, writer = await asyncio.wait_for(asyncio.open_connection(RCON_HOST, RCON_PORT), timeout=timeout)

    try:
        # auth
        writer.write(_rcon_make_packet(1, 3, RCON_PASSWORD))
        await writer.drain()

        # auth response (can be split across packets)
        raw = await asyncio.wait_for(reader.read(4096), timeout=timeout)
        if len(raw) < 12:
            raise RuntimeError("RCON auth failed (short response)")

        # send command
        writer.write(_rcon_make_packet(2, 2, command))
        await writer.drain()

        # read response (may come in multiple packets)
        chunks = []
        end_time = time.time() + timeout
        while time.time() < end_time:
            try:
                part = await asyncio.wait_for(reader.read(4096), timeout=0.3)
            except asyncio.TimeoutError:
                break
            if not part:
                break
            chunks.append(part)

        if not chunks:
            return ""

        data = b"".join(chunks)

        # Parse packets: [size][id][type][body]\x00\x00 ...
        # We'll just extract printable bodies.
        out = []
        i = 0
        while i + 4 <= len(data):
            size = int.from_bytes(data[i:i+4], "little", signed=True)
            i += 4
            if i + size > len(data) or size < 10:
                break
            pkt = data[i:i+size]
            i += size

            body = pkt[8:-2]  # skip id+type, strip trailing \x00\x00
            try:
                txt = body.decode("utf-8", errors="ignore")
            except Exception:
                txt = ""
            if txt:
                out.append(txt)

        return "".join(out).strip()
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

def parse_listplayers(output: str):
    """
    Expected lines like:
    0. Name, 0002xxxxxxxx...
    """
    players = []
    if not output:
        return players

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        # handle both "0. Name, id" and "0. Name , id"
        if ". " in line:
            line = line.split(". ", 1)[1]
        # split by comma, take left as name
        if "," in line:
            name = line.split(",", 1)[0].strip()
        else:
            name = line.strip()

        # filter junk
        if name and name.lower() not in ("executing", "listplayers", "done"):
            players.append(name)

    return players

# =====================
# WEBHOOK HELPER
# =====================
async def upsert_webhook(session, url, key, embed):
    mid = message_ids.get(key)
    if mid:
        async with session.patch(f"{url}/messages/{mid}", json={"embeds": [embed]}) as r:
            # if message got deleted, recreate
            if r.status == 404:
                message_ids[key] = None
                return await upsert_webhook(session, url, key, embed)
        return

    async with session.post(url + "?wait=true", json={"embeds": [embed]}) as r:
        data = await r.json()
        message_ids[key] = data["id"]

async def update_players_embed(session: aiohttp.ClientSession):
    """
    Updates the players webhook embed using RCON ListPlayers (names)
    and Nitrado player count (fallback).
    """
    online_nitrado, nitrado_count = await get_server_status(session)

    # RCON list (names)
    names = []
    rcon_ok = True
    rcon_err = None
    try:
        out = await rcon_command("ListPlayers", timeout=6.0)
        names = parse_listplayers(out)
    except Exception as e:
        rcon_ok = False
        rcon_err = str(e)

    # decide "online" best-effort
    online = online_nitrado or rcon_ok

    count = len(names) if names else nitrado_count
    emoji = "🟢" if online else "🔴"

    # build list text
    if names:
        lines = [f"{idx+1:02d}) {n}" for idx, n in enumerate(names[:50])]
        player_list_text = "\n".join(lines)
        desc = f"**{count}/{PLAYER_CAP}** online\n\n{player_list_text}"
    else:
        if not rcon_ok:
            desc = f"**{count}/{PLAYER_CAP}** online\n\n*(Could not fetch player names via RCON: {rcon_err})*"
        else:
            desc = f"**{count}/{PLAYER_CAP}** online\n\n*(No player list returned.)*"

    embed = {
        "title": "Online Players",
        "description": desc,
        "color": 0x2ECC71 if online else 0xE74C3C,
        "footer": {"text": f"Last update: {time.strftime('%H:%M:%S')}"}
    }

    await upsert_webhook(session, PLAYERS_WEBHOOK_URL, "players", embed)
    return emoji, count, online

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
            emoji, count, online = await update_players_embed(session)

            vc = client.get_channel(STATUS_VC_ID)
            if vc:
                await vc.edit(name=f"{emoji} Solunaris | {count}/{PLAYER_CAP}")

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
    await i.response.defer(ephemeral=True)

    async with aiohttp.ClientSession() as session:
        emoji, count, online = await update_players_embed(session)

    await i.followup.send(
        f"{emoji} **Solunaris** — {count}/{PLAYER_CAP} players",
        ephemeral=True,
    )

# =====================
# START
# =====================
@client.event
async def on_ready():
    # restore saved webhook message ids if you want:
    # (optional) you can persist message_ids to a file later
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    client.loop.create_task(time_loop())
    client.loop.create_task(status_loop())
    print("✅ Solunaris bot online")

client.run(DISCORD_TOKEN)