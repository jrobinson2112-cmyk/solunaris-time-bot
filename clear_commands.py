import os
import discord
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN missing")

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)

@client.event
async def on_ready():
    print("Clearing ALL global commands...", flush=True)

    # Remove all global commands:
    tree.clear_commands(guild=None)
    await tree.sync()

    print("DONE. Global commands cleared.", flush=True)
    await client.close()

client.run(TOKEN)