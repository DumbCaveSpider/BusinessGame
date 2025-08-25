import os
import json
import asyncio
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

# Data paths for presence calculation
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
USER_FILE = os.path.join(DATA_DIR, 'users.json')
STOCKS_FILE = os.path.join(DATA_DIR, 'stocks.json')


def _load_json(path: str, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def _calc_total_income_and_stock() -> tuple[int, float]:
    users = _load_json(USER_FILE, {})
    total_income = 0
    for user in (users or {}).values():
        for slot in user.get('slots', []) or []:
            if not slot:
                continue
            try:
                total_income += int(slot.get('income_per_day', 0) or 0)
            except Exception:
                continue
    stocks = _load_json(STOCKS_FILE, {"current_pct": 50.0})
    try:
        pct = float(stocks.get('current_pct', 50.0) or 50.0)
    except Exception:
        pct = 50.0
    return total_income, pct


async def _update_presence_once():
    total_income, pct = _calc_total_income_and_stock()
    text = f"GL${total_income}/day • Global Stock {pct:.1f}%"
    try:
        await bot.change_presence(activity=discord.CustomActivity(name=text))
    except Exception:
        # Fallback: some bots cannot set CustomActivity; use a standard Activity
        await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=text))


async def _presence_task():
    await bot.wait_until_ready()
    # Initial set
    try:
        await _update_presence_once()
    except Exception:
        pass
    # Refresh periodically
    while not bot.is_closed():
        try:
            await _update_presence_once()
        except Exception:
            pass
        await asyncio.sleep(60)

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
    # Start presence updater
    try:
        bot.loop.create_task(_presence_task())
    except Exception:
        pass

if TOKEN is None:
    print("Error: DISCORD_TOKEN environment variable not found.")
    exit(1)
bot.run(TOKEN)
