import os
import json
import time
import random
from typing import Dict, Any, List

import discord
from discord import app_commands

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
STOCK_FILE = os.path.join(DATA_DIR, 'stocks.json')
USER_FILE = os.path.join(DATA_DIR, 'users.json')


def _now() -> int:
    return int(time.time())


def _ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)


def _load_stocks() -> Dict[str, Any]:
    # Ensure persistence: create and save a default file if missing or invalid
    if not os.path.exists(STOCK_FILE):
        data = {"current_pct": 50.0, "last_tick": _now(), "history": [{"t": _now(), "pct": 50.0}]}
        _save_stocks(data)
        return data
    with open(STOCK_FILE, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            data = {"current_pct": 50.0, "last_tick": _now(), "history": [{"t": _now(), "pct": 50.0}]}
            _save_stocks(data)
            return data


def _save_stocks(data: Dict[str, Any]):
    _ensure_dirs()
    with open(STOCK_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


def _load_users() -> Dict[str, Any]:
    if not os.path.exists(USER_FILE):
        return {}
    with open(USER_FILE, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def _save_users(data: Dict[str, Any]):
    _ensure_dirs()
    with open(USER_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


def _tick_if_needed() -> Dict[str, Any]:
    data = _load_stocks()
    now = _now()
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
    curr = float(data.get('current_pct', 50.0))
    hist: List[Dict[str, Any]] = list(data.get('history', []))
    for i in range(int(steps)):
        change = random.uniform(-10.0, 10.0)
        curr = max(0.0, min(100.0, curr + change))
        last += 3600
        hist.append({"t": last, "pct": round(curr, 1)})
    # Keep last 48 entries
    if len(hist) > 48:
        hist = hist[-48:]
    data['current_pct'] = round(curr, 1)
    data['last_tick'] = last
    data['history'] = hist
    _save_stocks(data)
    return data


def _apply_stock_to_all_users(stock_pct: float) -> None:
    data = _load_users()
    changed = False
    # Factor baseline: 50% => 1.0x
    factor = (stock_pct / 50.0) if stock_pct != 0 else 0.0
    for uid, user in list(data.items()):
        slots = user.get('slots', []) or []
        for idx, slot in enumerate(slots):
            if slot is None:
                continue
            base = int(slot.get('base_income_per_day', slot.get('scores', {}).get('total', slot.get('income_per_day', 0))))
            # Ensure we have a baseline stored
            slot['base_income_per_day'] = base
            new_income = max(0, int(round(base * factor)))
            if int(slot.get('income_per_day', 0)) != new_income:
                slot['income_per_day'] = new_income
                changed = True
            # Also refresh current base income to track stocks so future rates are based on latest value
            # Avoid permanently zeroing the base when stock is 0%
            if factor > 0.0 and int(slot.get('base_income_per_day', 0)) != new_income:
                slot['base_income_per_day'] = new_income
                changed = True
    if changed:
        _save_users(data)


def _render_stocks_embed(data: Dict[str, Any]) -> discord.Embed:
    curr = float(data.get('current_pct', 50.0))
    last = int(data.get('last_tick', _now()))
    now = _now()
    until_next = max(0, (last + 3600) - now)
    mins = until_next // 60
    secs = until_next % 60
    color = discord.Color.green() if curr >= 50 else discord.Color.red()
    embed = discord.Embed(title="📈 Global Stock Market", color=color)
    embed.add_field(name="💸 Current", value=f"{curr:.1f}%", inline=True)
    embed.add_field(name="⌚ Next tick", value=f"in {mins}m {secs}s", inline=True)
    # History (last 12)
    hist = list(data.get('history', []))[-12:]
    if hist:
        lines = []
        for item in hist:
            t = time.strftime('%m-%d %H:%M', time.localtime(int(item.get('t', now))))
            lines.append(f"{t} — {float(item.get('pct', 0.0)):.1f}%")
        embed.add_field(name="📈 Recent", value="\n".join(lines), inline=False)
    embed.set_footer(text="Updates every hour by ±10%")
    return embed


class StocksView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = _tick_if_needed()
        # Apply to all businesses so current rate matches display logic
        try:
            _apply_stock_to_all_users(float(data.get('current_pct', 50.0)))
        except Exception:
            pass
        embed = _render_stocks_embed(data)
        await interaction.response.edit_message(embed=embed, view=StocksView())


class StocksCommand:
    @staticmethod
    async def setup(tree: app_commands.CommandTree):
        @tree.command(name="stocks", description="View the global stock market and history")
        @app_commands.allowed_contexts(dms=True, guilds=True, private_channels=True)
        async def stocks(interaction: discord.Interaction):
            data = _tick_if_needed()
            # Apply to all users on open as well
            try:
                _apply_stock_to_all_users(float(data.get('current_pct', 50.0)))
            except Exception:
                pass
            embed = _render_stocks_embed(data)
            await interaction.response.send_message(embed=embed, view=StocksView())
