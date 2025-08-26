import json
import os
import time
from typing import Dict, Any
import discord
from discord import app_commands

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
USER_FILE = os.path.join(DATA_DIR, 'users.json')
MARKET_FILE = os.path.join(DATA_DIR, 'market.json')
PURCHASED_FILE = os.path.join(DATA_DIR, 'purchased_upgrades.json')
STOCK_FILE = os.path.join(DATA_DIR, 'stocks.json')


def _load_users() -> Dict[str, Any]:
    if not os.path.exists(USER_FILE):
        return {}
    with open(USER_FILE, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def _load_market() -> Dict[str, Any]:
    if not os.path.exists(MARKET_FILE):
        return {"upgrades": []}
    with open(MARKET_FILE, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {"upgrades": []}


def _load_purchases() -> Dict[str, Any]:
    if not os.path.exists(PURCHASED_FILE):
        return {}
    with open(PURCHASED_FILE, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def _load_stocks() -> Dict[str, Any]:
    if not os.path.exists(STOCK_FILE):
        return {"current_pct": 50.0}
    with open(STOCK_FILE, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {"current_pct": 50.0}


def _now() -> int:
    return int(time.time())


def _calc_accrued_for_slot(slot: Dict[str, Any]) -> int:
    rate = int(slot.get('income_per_day', 0))
    last = int(slot.get('last_collected_at') or slot.get('created_at') or _now())
    elapsed = max(0, _now() - last)
    days = elapsed / 86400.0
    return int(days * rate)


def _total_boost_pct(slot: Dict[str, Any], owner_id: str, slot_index: int) -> float:
    total = 0.0
    # Prefer purchased upgrades file
    try:
        purchases = _load_purchases()
        urec = purchases.get(str(owner_id), {}) or {}
        ups = urec.get(str(slot_index), []) or []
        for up in ups:
            try:
                total += float(up.get('boost_pct', 0.0))
            except Exception:
                continue
        return float(total)
    except Exception:
        pass
    # Fallback to legacy upgrades stored on the slot
    try:
        ups_legacy = slot.get('upgrades', []) or []
        if ups_legacy:
            mk = _load_market()
            u_map = {str(u.get('id')): u for u in mk.get('upgrades', [])}
            for up in ups_legacy:
                if isinstance(up, dict):
                    total += float(up.get('boost_pct', 0.0))
                else:
                    u = u_map.get(str(up))
                    if u is not None:
                        total += float(u.get('boost_pct', 0.0))
    except Exception:
        pass
    return float(total)


def _effective_income_per_day(slot: Dict[str, Any], owner_id: str, slot_index: int) -> int:
    try:
        base = float(slot.get('income_per_day', 0))
        rating = float(slot.get('rating', 1.0))
        boost_pct = _total_boost_pct(slot, owner_id, slot_index)
        mult = (1.0 + float(boost_pct) / 100.0)
        return max(0, int(round(base * rating * mult)))
    except Exception:
        return int(slot.get('income_per_day', 0) or 0)


def _disp_inc(slot: Dict[str, Any], owner_id: str, slot_index: int, stock_factor: float) -> int:
    inc = _effective_income_per_day(slot, owner_id, slot_index)
    return int(round(inc * (stock_factor if stock_factor else 0.0)))


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

            # Match passive display: compute stock factor once
            stocks = _load_stocks()
            stock_pct = float((stocks or {}).get('current_pct', 50.0))
            stock_factor = (stock_pct / 50.0) if stock_pct != 0 else 0.0

            slots = user.get('slots', [])
            total_businesses = sum(1 for s in slots if s)
            combined_rate = 0
            for idx, s in enumerate(slots):
                if not s:
                    continue
                combined_rate += _disp_inc(s, user_id, idx, stock_factor)
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
