import os
import json
from typing import Any, Dict, List, Tuple

import discord
from discord import app_commands


DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
USER_FILE = os.path.join(DATA_DIR, 'users.json')


def _load_users() -> Dict[str, Any]:
    if not os.path.exists(USER_FILE):
        return {}
    try:
        with open(USER_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _clamp_min_rating(r: float) -> float:
    try:
        r = float(r)
    except Exception:
        r = 0.0
    return r if r >= 0.1 else 0.1


def _summarize_user(user_id: str, user: Dict[str, Any]) -> Tuple[int, float, int]:
    total_income = 0
    total_rating = 0.0
    count = 0
    for slot in user.get('slots', []) or []:
        if not slot:
            continue
        inc = int(slot.get('income_per_day', 0) or 0)
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
            cat = category.value
            top_n = 20

            if cat == "richest":
                # Rank users by total income/day; show total rating across their businesses
                rows: List[Tuple[str, int, float, int]] = []
                for uid, user in (data or {}).items():
                    total_income, total_rating, count = _summarize_user(uid, user)
                    rows.append((uid, total_income, total_rating, count))
                rows.sort(key=lambda x: (x[1], x[2]), reverse=True)
                top = rows[:top_n]

                title = "Leaderboard — Richest users"
                desc = f"Top {len(top)} by total daily income."
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
                biz = _flatten_businesses(data)
                scored: List[Tuple[str, str, int, float, float]] = []
                for uid, slot in biz:
                    name = slot.get('name', 'Business')
                    base = int(slot.get('base_income_per_day', slot.get('income_per_day', 0)) or 0)
                    rating = _clamp_min_rating(slot.get('rating', 1.0) or 1.0)
                    income = int(slot.get('income_per_day', 0) or 0)
                    value = float(base if base > 0 else income) * float(rating)
                    scored.append((uid, name, income, rating, value))
                scored.sort(key=lambda x: x[4], reverse=True)
                top = scored[:top_n]

                title = "Leaderboard — Most valuable businesses"
                desc = f"Top {len(top)} businesses by value and rating."
                embed = discord.Embed(title=title, description=desc, color=discord.Color.gold())
                if not top:
                    embed.add_field(name="No data", value="No businesses found.")
                else:
                    lines = []
                    for i, (uid, name, income, rating, value) in enumerate(top, start=1):
                        mention = f"<@{uid}>"
                        short_name = name if len(str(name)) <= 64 else (str(name)[:61] + "...")
                        lines.append(
                            f"**{i}.** {short_name} — {mention} • 💵 Income **<:greensl:1409394243025502258>{income}/day** • ⭐ **{rating:.1f}** • 🏷️ Value **<:greensl:1409394243025502258>{int(value)}**"
                        )
                    _add_chunked_field(embed, "Businesses", lines)
                await interaction.response.send_message(embed=embed)
