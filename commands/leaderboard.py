import os
import json
from typing import Any, Dict, List, Tuple

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
    try:
        with open(USER_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _load_market() -> Dict[str, Any]:
    if not os.path.exists(MARKET_FILE):
        return {"upgrades": []}
    try:
        with open(MARKET_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {"upgrades": []}


def _load_purchases() -> Dict[str, Any]:
    if not os.path.exists(PURCHASED_FILE):
        return {}
    try:
        with open(PURCHASED_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _load_stocks() -> Dict[str, Any]:
    if not os.path.exists(STOCK_FILE):
        return {"current_pct": 50.0}
    try:
        with open(STOCK_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {"current_pct": 50.0}


def _clamp_min_rating(r: float) -> float:
    try:
        r = float(r)
    except Exception:
        r = 0.0
    return r if r >= 0.1 else 0.1


def _total_boost_pct(slot: Dict[str, Any], owner_id: str | None = None, slot_index: int | None = None) -> float:
    total = 0.0
    try:
        if owner_id is not None and slot_index is not None:
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
    # Stock factor matches passive: pct/50.0, 0 if pct == 0
    return int(round(inc * (stock_factor if stock_factor else 0.0)))


def _summarize_user(user_id: str, user: Dict[str, Any], stock_factor: float) -> Tuple[int, float, int]:
    total_income = 0
    total_rating = 0.0
    count = 0
    for idx, slot in enumerate(user.get('slots', []) or []):
        if not slot:
            continue
        inc = _disp_inc(slot, user_id, idx, stock_factor)
        rating = _clamp_min_rating(slot.get('rating', 1.0) or 1.0)
        total_income += inc
        total_rating += rating
        count += 1
    return total_income, total_rating, count


def _flatten_businesses(data: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    rows: List[Tuple[str, Dict[str, Any]]] = []
    for uid, user in (data or {}).items():
        for slot in user.get('slots', []) or []:
            if not slot:
                continue
            rows.append((uid, slot))
    return rows


def _add_chunked_field(embed: discord.Embed, title: str, lines: List[str]) -> None:
    """Add one or more fields so that each field value stays within 1024 chars."""
    if not lines:
        embed.add_field(name=title, value="(none)", inline=False)
        return
    chunk = []
    current_len = 0
    field_index = 0
    for line in lines:
        line_str = line.rstrip()
        # +1 for newline if chunk not empty
        extra = len(line_str) + (1 if chunk else 0)
        if current_len + extra > 1024:
            embed.add_field(name=title if field_index == 0 else f"{title} (cont.)", value="\n".join(chunk), inline=False)
            field_index += 1
            chunk = [line_str]
            current_len = len(line_str)
        else:
            if chunk:
                current_len += 1  # newline
            chunk.append(line_str)
            current_len += len(line_str)
    if chunk:
        embed.add_field(name=title if field_index == 0 else f"{title} (cont.)", value="\n".join(chunk), inline=False)


class LeaderboardCommand:
    @staticmethod
    async def setup(tree: app_commands.CommandTree):
        @tree.command(name="leaderboard", description="Show leaderboards for wealth or businesses")
        @app_commands.describe(
            category="Which leaderboard to view",
        )
        @app_commands.choices(
            category=[
                app_commands.Choice(name="Richest users (by income/day)", value="richest"),
                app_commands.Choice(name="Most valuable businesses", value="business"),
            ]
        )
        @app_commands.allowed_contexts(dms=True, guilds=True, private_channels=True)
        async def leaderboard(
            interaction: discord.Interaction,
            category: app_commands.Choice[str],
        ):
            data = _load_users()
            # Load global stock once and compute factor to match passive display
            stock = _load_stocks()
            stock_pct = float((stock or {}).get('current_pct', 50.0))
            stock_factor = (stock_pct / 50.0) if stock_pct != 0 else 0.0
            cat = category.value
            top_n = 20

            if cat == "richest":
                # Rank users by total income/day; show total rating across their businesses
                rows: List[Tuple[str, int, float, int]] = []
                for uid, user in (data or {}).items():
                    total_income, total_rating, count = _summarize_user(uid, user, stock_factor)
                    rows.append((uid, total_income, total_rating, count))
                rows.sort(key=lambda x: (x[1], x[2]), reverse=True)
                top = rows[:top_n]

                title = "Leaderboard — Richest users"
                desc = f"Top {len(top)} by stock-adjusted daily income."
                embed = discord.Embed(title=title, description=desc, color=discord.Color.blurple())
                if not top:
                    embed.add_field(name="No data", value="No users found.")
                else:
                    lines = []
                    for i, (uid, income, total_rating, count) in enumerate(top, start=1):
                        mention = f"<@{uid}>"
                        lines.append(
                            f"**{i}.** {mention} — 💵 Income **<:greensl:1409394243025502258>{income}/day** • ⭐ **{total_rating:.1f}** across **{count}** business(es)"
                        )
                    _add_chunked_field(embed, "Users", lines)
                await interaction.response.send_message(embed=embed)

            else:  # business
                # Rank businesses by base*rating (approximate value); show owner
                scored: List[Tuple[str, str, int, float]] = []
                for uid, user in (data or {}).items():
                    for idx, slot in enumerate((user.get('slots', []) or [])):
                        if not slot:
                            continue
                        name = slot.get('name', 'Business')
                        rating = _clamp_min_rating(slot.get('rating', 1.0) or 1.0)
                        disp_income = _disp_inc(slot, uid, idx, stock_factor)
                        scored.append((uid, name, disp_income, rating))
                scored.sort(key=lambda x: (x[2], x[3]), reverse=True)
                top = scored[:top_n]

                title = "Leaderboard — Most valuable businesses"
                desc = f"Top {len(top)} businesses by stock-adjusted income and rating."
                embed = discord.Embed(title=title, description=desc, color=discord.Color.gold())
                if not top:
                    embed.add_field(name="No data", value="No businesses found.")
                else:
                    lines = []
                    for i, (uid, name, disp_income, rating) in enumerate(top, start=1):
                        mention = f"<@{uid}>"
                        short_name = name if len(str(name)) <= 64 else (str(name)[:61] + "...")
                        lines.append(
                            f"**{i}.** {short_name} — {mention} • 💵 Income **<:greensl:1409394243025502258>{disp_income}/day** • ⭐ **{rating:.1f}**"
                        )
                    _add_chunked_field(embed, "Businesses", lines)
                await interaction.response.send_message(embed=embed)
