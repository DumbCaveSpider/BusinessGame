import os
import json
import asyncio
import time
import random
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


# ---- Autonomous hourly stock ticker ----

def _stocks_now() -> int:
    return int(time.time())


def _ensure_data_dir() -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        pass


def _load_stocks() -> dict:
    _ensure_data_dir()
    default = {"current_pct": 50.0, "last_tick": _stocks_now(), "history": [{"t": _stocks_now(), "pct": 50.0}]}
    return _load_json(STOCKS_FILE, default)


def _save_stocks(data: dict) -> None:
    _ensure_data_dir()
    try:
        with open(STOCKS_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _tick_stocks_if_needed() -> dict:
    data = _load_stocks()
    now = _stocks_now()
    last = int(data.get('last_tick', 0) or 0)
    if last <= 0:
        data['last_tick'] = now
        if not data.get('history'):
            data['history'] = [{"t": now, "pct": float(data.get('current_pct', 50.0))}]
        _save_stocks(data)
        return data
    elapsed = now - last
    if elapsed < 3600:
        return data
    steps = elapsed // 3600
    try:
        curr = float(data.get('current_pct', 50.0))
    except Exception:
        curr = 50.0
    hist = list(data.get('history', []))
    for _ in range(int(steps)):
        change = random.uniform(-10.0, 10.0)
        curr = max(0.0, min(100.0, curr + change))
        last += 3600
        hist.append({"t": last, "pct": round(curr, 1)})
    if len(hist) > 48:
        hist = hist[-48:]
    data['current_pct'] = round(curr, 1)
    data['last_tick'] = last
    data['history'] = hist
    _save_stocks(data)
    return data


async def _stocks_task():
    await bot.wait_until_ready()
    # Initial tick (if needed), then loop hourly checks every minute
    try:
        _tick_stocks_if_needed()
    except Exception:
        pass
    while not bot.is_closed():
        try:
            _tick_stocks_if_needed()
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
    # Start autonomous stock ticker
    try:
        bot.loop.create_task(_stocks_task())
    except Exception:
        pass

if TOKEN is None:
    print("Error: DISCORD_TOKEN environment variable not found.")
    exit(1)
bot.run(TOKEN)
