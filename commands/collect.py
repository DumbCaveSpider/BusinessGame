import json
import os
import time
from typing import Dict, Any
import discord
from discord import app_commands

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
USER_FILE = os.path.join(DATA_DIR, 'users.json')


def _load_users() -> Dict[str, Any]:
    if not os.path.exists(USER_FILE):
        return {}
    with open(USER_FILE, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def _save_users(data: Dict[str, Any]):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(USER_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


def _now() -> int:
    return int(time.time())


def _calc_accrued_for_slot(slot: Dict[str, Any]) -> int:
    rate = int(slot.get('income_per_day', 0))
    last = int(slot.get('last_collected_at') or slot.get('created_at') or _now())
    elapsed = max(0, _now() - last)
    days = elapsed / 86400.0
    return int(days * rate)


class CollectCommand:
    @staticmethod
    async def setup(tree: app_commands.CommandTree):
        @tree.command(name="collect", description="Collect all passive income from your businesses")
        @app_commands.allowed_contexts(dms=True, guilds=True, private_channels=True)
        async def collect(interaction: discord.Interaction):
            user_id = str(interaction.user.id)
            data = _load_users()
            user = data.get(user_id)
            if user is None:
                await interaction.response.send_message("> ❌ You have no account yet. Use `/passive` to start.", ephemeral=True)
                return

            total_collected = 0
            for slot in user.get('slots', []):
                if not slot:
                    continue
                accrued = _calc_accrued_for_slot(slot)
                if accrued > 0:
                    total_collected += accrued
                    slot['total_earned'] = int(slot.get('total_earned', 0)) + accrued
                    slot['last_collected_at'] = _now()
            user['balance'] = int(user.get('balance', 0)) + total_collected
            _save_users(data)

            await interaction.response.send_message(f"> 🤑 Collected **${total_collected}**. New balance: **${user['balance']}**", ephemeral=True)
