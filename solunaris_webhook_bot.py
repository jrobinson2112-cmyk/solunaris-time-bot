import os
import json
import time
import asyncio
from typing import Optional, Tuple

import discord
from discord import app_commands


# -----------------------------
# REQUIRED ENV VARS (Railway)
# -----------------------------
# DISCORD_TOKEN=xxxxxxxx
# WEBHOOK_URL=https://discord.com/api/webhooks/....
#
# OPTIONAL ENV VARS
# UPDATE_INTERVAL=4.7
# REAL_SECONDS_PER_INGAME_MINUTE=4.9333
# GUILD_ID=1430388266393276509
# ADMIN_ROLE_ID=1439069787207766076
# DAY_START_HOUR=6
# NIGHT_START_HOUR=18
# STATE_FILE=state.json
#
# Optional defaults if you haven't run /settime yet:
# INITIAL_DAY=103
# INITIAL_HOUR=5
# INITIAL_MINUTE=36
# INITIAL_YEAR=2


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")

GUILD_ID = int(os.getenv("GUILD_ID", "1430388266393276509"))
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "1439069787207766076"))

UPDATE_INTERVAL = float(os.getenv("UPDATE_INTERVAL", "4.7"))
REAL_SECONDS_PER_INGAME_MINUTE = float(os.getenv("REAL_SECONDS_PER_INGAME_MINUTE", "4.9333"))

DAY_START_HOUR = int(os.getenv("DAY_START_HOUR", "6"))
NIGHT_START_HOUR = int(os.getenv("NIGHT_START_HOUR", "18"))

STATE_FILE = os.getenv("STATE_FILE", "state.json")
DAYS_PER_YEAR = 365


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def minutes_from_hm(hour: int, minute: int) -> int:
    if not (0 <= hour <= 23):
        raise ValueError("Hour must be 0-23.")
    if not (0 <= minute <= 59):
        raise ValueError("Minute must be 0-59.")
    return hour * 60 + minute


def fmt_hhmm(minutes_since_midnight: int) -> str:
    hh = minutes_since_midnight // 60
    mm = minutes_since_midnight % 60
    return f"{hh:02d}:{mm:02d}"


def is_daytime(minutes_since_midnight: int) -> bool:
    hour = minutes_since_midnight // 60
    # Day = [DAY_START_HOUR, NIGHT_START_HOUR)
    if DAY_START_HOUR <= NIGHT_START_HOUR:
        return DAY_START_HOUR <= hour < NIGHT_START_HOUR
    return not (NIGHT_START_HOUR <= hour < DAY_START_HOUR)


def compute_current_time(state: dict, now_ts: Optional[float] = None) -> Tuple[int, int, int]:
    """
    Returns:
      current_day (int, Day 1+),
      minutes_since_midnight (0..1439),
      current_year (int)
    """
    if now_ts is None:
        now_ts = time.time()

    base_ts = float(state["base_real_ts"])
    base_day = int(state["base_day"])
    base_minutes = int(state["base_time_minutes"])
    base_year = int(state.get("base_year", 1))

    # Convert base day/time into an absolute minute count
    base_total_minutes = (base_day - 1) * 1440 + base_minutes

    elapsed_real_seconds = max(0.0, now_ts - base_ts)
    elapsed_ingame_minutes = elapsed_real_seconds / REAL_SECONDS_PER_INGAME_MINUTE

    current_total_minutes = base_total_minutes + elapsed_ingame_minutes

    current_day = int(current_total_minutes // 1440) + 1
    minutes_since_midnight = int(current_total_minutes % 1440)

    # Year rolling anchored to the set day/year
    year_offset = (current_day - base_day) // DAYS_PER_YEAR
    current_year = base_year + year_offset

    return current_day, minutes_since_midnight, current_year


def build_display_line(day_num: int, minutes_since_midnight: int, year_num: int) -> str:
    emoji = "☀️" if is_daytime(minutes_since_midnight) else "🌙"
    hhmm = fmt_hhmm(minutes_since_midnight)
    return f"{emoji} | Solunaris Time | {hhmm} | Day {day_num} | Year {year_num}"


def has_admin_role(member: discord.Member) -> bool:
    return any(r.id == ADMIN_ROLE_ID for r in getattr(member, "roles", []))


intents = discord.Intents.default()
intents.message_content = False


class SolunarisBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.state = load_state()
        self.webhook: Optional[discord.Webhook] = None
        self.webhook_message_id: Optional[int] = None
        self.bg_task: Optional[asyncio.Task] = None

    async def setup_hook(self):
        if WEBHOOK_URL:
            self.webhook = discord.Webhook.from_url(WEBHOOK_URL, client=self)

        guild_obj = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild_obj)
        await self.tree.sync(guild=guild_obj)

    async def on_ready(self):
        print(f"Logged in as {self.user} (guild={GUILD_ID})")
        print(f"REAL_SECONDS_PER_INGAME_MINUTE={REAL_SECONDS_PER_INGAME_MINUTE}, UPDATE_INTERVAL={UPDATE_INTERVAL}")

        # Initialize default state if none exists
        if "base_real_ts" not in self.state:
            init_day = int(os.getenv("INITIAL_DAY", "1"))
            init_hour = int(os.getenv("INITIAL_HOUR", "0"))
            init_minute = int(os.getenv("INITIAL_MINUTE", "0"))
            init_year = int(os.getenv("INITIAL_YEAR", "1"))

            init_minutes = minutes_from_hm(init_hour, init_minute)

            self.state.update({
                "base_real_ts": time.time(),
                "base_day": init_day,
                "base_time_minutes": init_minutes,
                "base_year": init_year,
            })
            save_state(self.state)
            print("No saved settime found; using INITIAL_* defaults. Use /settime to set real values.")

        self.webhook_message_id = self.state.get("webhook_message_id")

        if not self.bg_task:
            self.bg_task = asyncio.create_task(self.background_updater())

    async def background_updater(self):
        if not self.webhook:
            print("WEBHOOK_URL missing — cannot update embed.")
            return

        await asyncio.sleep(2)

        while True:
            try:
                day_num, mins, year_num = compute_current_time(self.state)
                line = build_display_line(day_num, mins, year_num)

                embed = discord.Embed(description=line)

                if self.webhook_message_id:
                    try:
                        await self.webhook.edit_message(self.webhook_message_id, embed=embed)
                    except discord.NotFound:
                        msg = await self.webhook.send(embed=embed, wait=True)
                        self.webhook_message_id = msg.id
                        self.state["webhook_message_id"] = msg.id
                        save_state(self.state)
                else:
                    msg = await self.webhook.send(embed=embed, wait=True)
                    self.webhook_message_id = msg.id
                    self.state["webhook_message_id"] = msg.id
                    save_state(self.state)

            except Exception as e:
                print(f"[Updater] Error: {e}")

            await asyncio.sleep(UPDATE_INTERVAL)


bot = SolunarisBot()


# -----------------------------
# /day
# -----------------------------
@bot.tree.command(name="day", description="Show the current Solunaris in-game time/day/year.")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def day_cmd(interaction: discord.Interaction):
    try:
        day_num, mins, year_num = compute_current_time(bot.state)
        line = build_display_line(day_num, mins, year_num)
        await interaction.response.send_message(line, ephemeral=True)
    except Exception as e:
        if interaction.response.is_done():
            await interaction.followup.send(f"Error: {e}", ephemeral=True)
        else:
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)


# -----------------------------
# /settime (role-gated)
# -----------------------------
@bot.tree.command(name="settime", description="(Admin role) Set the current in-game Day/Hour/Minute/Year.")
@app_commands.guilds(discord.Object(id=GUILD_ID))
async def settime_cmd(
    interaction: discord.Interaction,
    day: int,
    hour: int,
    minute: int,
    year: int
):
    await interaction.response.defer(ephemeral=True)

    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.followup.send("This command must be used inside the server.", ephemeral=True)
        return

    if not has_admin_role(interaction.user):
        await interaction.followup.send("❌ You must have the required admin role to use /settime.", ephemeral=True)
        return

    try:
        if day < 1:
            raise ValueError("Day must be 1 or higher.")
        if year < 1:
            raise ValueError("Year must be 1 or higher.")

        minutes = minutes_from_hm(hour, minute)

        bot.state["base_real_ts"] = time.time()
        bot.state["base_day"] = int(day)
        bot.state["base_time_minutes"] = int(minutes)
        bot.state["base_year"] = int(year)
        save_state(bot.state)

        # Force immediate webhook update
        if bot.webhook:
            day_num, mins, year_num = compute_current_time(bot.state)
            line = build_display_line(day_num, mins, year_num)
            embed = discord.Embed(description=line)

            if bot.webhook_message_id:
                try:
                    await bot.webhook.edit_message(bot.webhook_message_id, embed=embed)
                except discord.NotFound:
                    msg = await bot.webhook.send(embed=embed, wait=True)
                    bot.webhook_message_id = msg.id
                    bot.state["webhook_message_id"] = msg.id
                    save_state(bot.state)
            else:
                msg = await bot.webhook.send(embed=embed, wait=True)
                bot.webhook_message_id = msg.id
                bot.state["webhook_message_id"] = msg.id
                save_state(bot.state)

        await interaction.followup.send(
            f"✅ Set to {fmt_hhmm(minutes)} on Day {day} (Year {year}).",
            ephemeral=True
        )

    except Exception as e:
        await interaction.followup.send(f"Error: {e}", ephemeral=True)


# -----------------------------
# Run
# -----------------------------
if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing.")
if not WEBHOOK_URL:
    raise RuntimeError("WEBHOOK_URL is missing.")

bot.run(DISCORD_TOKEN)