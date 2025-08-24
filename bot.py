import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
from discord import app_commands
import importlib

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True
intents.guilds = True

bot = commands.Bot(command_prefix='!', intents=intents)
tree = bot.tree

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    # Load all slash commands from commands folder
    commands_dir = os.path.join(os.path.dirname(__file__), 'commands')
    for filename in os.listdir(commands_dir):
        if filename.endswith('.py'):
            mod_name = f'commands.{filename[:-3]}'
            mod = importlib.import_module(mod_name)
            # Look for any class ending with 'Command' that has an async setup(tree)
            for attr in dir(mod):
                if attr.endswith('Command'):
                    cls = getattr(mod, attr)
                    setup = getattr(cls, 'setup', None)
                    if setup is not None:
                        maybe_coro = setup(tree)
                        if hasattr(maybe_coro, "__await__"):
                            await maybe_coro
    await tree.sync()

if TOKEN is None:
    print("Error: DISCORD_TOKEN environment variable not found.")
    exit(1)
bot.run(TOKEN)
