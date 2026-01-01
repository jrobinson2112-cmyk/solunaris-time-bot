import os
import time
import json
import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks

# =====================
# CONFIG (Railway Variables)
# =====================
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing")

GUILD_ID = int(os.getenv("GUILD_ID", "0"))
if not GUILD_ID:
    raise RuntimeError("GUILD_ID is missing")

WEBHOOK_URL = os.getenv("WEBHOOK_URL")
if not WEBHOOK_URL:
    raise RuntimeError("WEBHOOK_URL is missing")

CALIBRATE_ROLE_ID = 1439069787207766076

UPDATE_INTERVAL = float(os.getenv("UPDATE_INTERVAL", "4.7"))


# Measured conversion:
# 20 in-game minutes = 94 real seconds => 1 in-game minute = 4.7 real seconds
REAL_SECONDS_PER_INGAME_MINUTE = 94 / 20  # 4.7

# Day/Night split
DAY_START_HOUR = 6
NIGHT_START_HOUR = 18

STATE_FILE = "solunaris_state.json"

# =====================
# DISCORD SETUP
# =====================
intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# We'll create this on startup
http_session: aiohttp.ClientSession | None = None
webhook: discord.Webhook | None = None

# =====================
# STATE (persisted)
# =====================
state = {
    # calibration
    "calibration_real_time": None,   # unix timestamp float
    "calibration_day": None,         # int
    "calibration_minute": None,      # int (0..1439)
    # webhook message id to edit
    "webhook_message_id": None,      # int
}

# =====================
# HELPERS
# =====================
def load_state():
    global state
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state.update(json.load(f))
    except Exception:
        pass


def save_state():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def is_calibrated() -> bool:
    return (
        state["calibration_real_time"] is not None
        and state["calibration_day"] is not None
        and state["calibration_minute"] is not None
    )


def get_day_night_emoji(hour: int) -> str:
    return "☀️" if DAY_START_HOUR <= hour < NIGHT_START_HOUR else "🌙"


def calculate_ingame_time():
    """
    Returns (day, hour, minute) based on calibration and elapsed real time.
    """
    if not is_calibrated():
        return None

    elapsed_real = time.time() - float(state["calibration_real_time"])
    ingame_minutes_passed = elapsed_real / REAL_SECONDS_PER_INGAME_MINUTE

    total_minutes = float(state["calibration_minute"]) + ingame_minutes_passed

    days_passed = int(total_minutes // 1440)
    minute_of_day = int(total_minutes % 1440)

    day = int(state["calibration_day"]) + days_passed
    hour = minute_of_day // 60
    minute = minute_of_day % 60
    return day, hour, minute


def format_line(day: int, hour: int, minute: int) -> str:
    emoji = get_day_night_emoji(hour)
    hhmm = f"{hour:02d}:{minute:02d}"
    # Leading zero-width space prevents occasional stripping when emoji is first
    return f"\u200B{emoji} Solunaris Time | {hhmm} | Day {day}"


async def user_has_calibrate_role(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False

    roles = getattr(interaction.user, "roles", None)
    if roles and any(r.id == CALIBRATE_ROLE_ID for r in roles):
        return True

    member = await interaction.guild.fetch_member(interaction.user.id)
    return any(r.id == CALIBRATE_ROLE_ID for r in member.roles)


async def ensure_webhook_message_exists():
    """
    Ensures we have a message_id to edit. If missing, sends a new message and stores its ID.
    """
    assert webhook is not None

    if state.get("webhook_message_id"):
        return

    # Post an initial message
    msg = await webhook.send(
        "🕒 Solunaris Time | not calibrated yet",
        wait=True
    )
    state["webhook_message_id"] = msg.id
    save_state()


async def edit_webhook_message(content: str):
    """
    Edit the stored webhook message.
    """
    assert webhook is not None

    msg_id = state.get("webhook_message_id")
    if not msg_id:
        await ensure_webhook_message_exists()
        msg_id = state.get("webhook_message_id")

    await webhook.edit_message(int(msg_id), content=content)


# =====================
# LOOP
# =====================
@tasks.loop(seconds=UPDATE_INTERVAL)
async def updater():
    try:
        if not is_calibrated():
            await edit_webhook_message("🕒 Solunaris Time | not calibrated yet")
            return

        data = calculate_ingame_time()
        if not data:
            return

        day, hour, minute = data
        line = format_line(day, hour, minute)
        await edit_webhook_message(line)

    except discord.HTTPException as e:
        print(f"Webhook HTTPException: {e}", flush=True)
    except Exception as e:
        print(f"Updater error: {e}", flush=True)


# =====================
# SLASH COMMANDS
# =====================
@tree.command(
    name="day",
    description="Show current Solunaris in-game time",
    guild=discord.Object(id=GUILD_ID),
)
async def day_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not is_calibrated():
        await interaction.followup.send("❌ Not calibrated yet. Use `/calibrate`.", ephemeral=True)
        return

    day, hour, minute = calculate_ingame_time()
    await interaction.followup.send(format_line(day, hour, minute), ephemeral=True)


@tree.command(
    name="calibrate",
    description="Calibrate Solunaris time (restricted role)",
    guild=discord.Object(id=GUILD_ID),
)
@app_commands.describe(day="Current in-game day", hour="Hour (0–23)", minute="Minute (0–59)")
async def calibrate_cmd(interaction: discord.Interaction, day: int, hour: int, minute: int):
    await interaction.response.defer(ephemeral=True)

    if not await user_has_calibrate_role(interaction):
        await interaction.followup.send("❌ You don’t have the required role to use `/calibrate`.", ephemeral=True)
        return

    if not (1 <= day and 0 <= hour <= 23 and 0 <= minute <= 59):
        await interaction.followup.send("❌ Invalid input. Day >= 1, hour 0–23, minute 0–59.", ephemeral=True)
        return

    # Save calibration
    state["calibration_real_time"] = time.time()
    state["calibration_day"] = int(day)
    state["calibration_minute"] = int(hour) * 60 + int(minute)
    save_state()

    # Update webhook immediately once (safe)
    line = format_line(day, hour, minute)
    try:
        await ensure_webhook_message_exists()
        await edit_webhook_message(line)
    except Exception as e:
        await interaction.followup.send(f"✅ Calibrated, but webhook update failed: {e}", ephemeral=True)
        return

    await interaction.followup.send(f"✅ Calibrated. Webhook updates every {UPDATE_INTERVAL}s.", ephemeral=True)


# =====================
# EVENTS
# =====================
@client.event
async def on_ready():
    global http_session, webhook

    load_state()

    print(f"✅ Logged in as {client.user}", flush=True)

    # Sync slash commands (guild only)
    await tree.sync(guild=discord.Object(id=GUILD_ID))
    print("✅ Slash commands synced", flush=True)

    # Setup webhook client
    http_session = aiohttp.ClientSession()
    webhook = discord.Webhook.from_url(WEBHOOK_URL, session=http_session)

    await ensure_webhook_message_exists()

    if not updater.is_running():
        updater.start()


@client.event
async def on_disconnect():
    # Not strictly necessary, but tidy.
    if http_session and not http_session.closed:
        try:
            await http_session.close()
        except Exception:
            pass


client.run(TOKEN)