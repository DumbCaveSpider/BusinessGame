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


def _now() -> int:
    return int(time.time())


def _calc_accrued_for_slot(slot: Dict[str, Any]) -> int:
    rate = int(slot.get('income_per_day', 0))
    last = int(slot.get('last_collected_at') or slot.get('created_at') or _now())
    elapsed = max(0, _now() - last)
    days = elapsed / 86400.0
    return int(days * rate)


class IncomeCommand:
    @staticmethod
    async def setup(tree: app_commands.CommandTree):
        @tree.command(name="income", description="Show your balance, combined daily rate, ready amount, and total businesses")
        @app_commands.allowed_contexts(dms=True, guilds=True, private_channels=True)
        async def income(interaction: discord.Interaction):
            user_id = str(interaction.user.id)
            data = _load_users()
            user = data.get(user_id)
            if user is None:
                await interaction.response.send_message("You have no account yet. Use /passive to start.", ephemeral=True)
                return

            slots = user.get('slots', [])
            total_businesses = sum(1 for s in slots if s)
            combined_rate = sum(int(s.get('income_per_day', 0)) for s in slots if s)
            ready_total = 0
            total_rating = 0.0
            for s in slots:
                if not s:
                    continue
                ready_total += _calc_accrued_for_slot(s)
                # Sum ratings with minimum 0.1 clamp (no maximum)
                try:
                    r = float(s.get('rating', 1.0) or 1.0)
                except Exception:
                    r = 1.0
                if r < 0.1:
                    r = 0.1
                total_rating += r

            balance = int(user.get('balance', 0))

            embed = discord.Embed(title="Income Overview", color=discord.Color.green())
            embed.add_field(name="🪙 Balance", value=f"<:greensl:1409394243025502258>{balance}", inline=True)
            embed.add_field(name="📈 Combined Rate", value=f"<:greensl:1409394243025502258>{combined_rate}/day", inline=True)
            embed.add_field(name="💵 Ready to Collect", value=f"<:greensl:1409394243025502258>{ready_total}", inline=True)
            embed.add_field(name="🏢 Businesses", value=str(total_businesses), inline=True)
            embed.add_field(name="⭐ Total Rating", value=f"{total_rating:.1f}", inline=True)
            await interaction.response.send_message(embed=embed, ephemeral=True)
