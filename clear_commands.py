import discord
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)

@client.event
async def on_ready():
    print("Clearing ALL global commands...")
    tree.clear_commands(guild=None)
    await tree.sync()
    print("DONE. You can now stop this script.")
    await client.close()

client.run(TOKEN)