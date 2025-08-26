import json
import os
import time
import asyncio
from typing import Dict, Any, List, Tuple
import discord
from discord import app_commands

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
USER_FILE = os.path.join(DATA_DIR, 'users.json')
MARKET_FILE = os.path.join(DATA_DIR, 'market.json')
PURCHASED_FILE = os.path.join(DATA_DIR, 'purchased_upgrades.json')
STOCK_FILE = os.path.join(DATA_DIR, 'stocks.json')
SELL_MULTIPLIER = 0.5  # assumed resale value = income_per_day * SELL_MULTIPLIER
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

# Try official Google Gemini client
try:
    from google import genai  # type: ignore
except Exception:  # pragma: no cover
    genai = None  # type: ignore


# Simple JSON persistence

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


def _save_purchases(data: Dict[str, Any]):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(PURCHASED_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


def _load_stocks() -> Dict[str, Any]:
    if not os.path.exists(STOCK_FILE):
        return {"current_pct": 50.0}
    with open(STOCK_FILE, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {"current_pct": 50.0}


def _ensure_user(user_id: str) -> Dict[str, Any]:
    data = _load_users()
    user = data.get(user_id)
    if user is None:
        user = {
            'balance': 100,
            'slots': [None],  # start with one slot
            'purchased_slots': 0,
        }
        data[user_id] = user
        _save_users(data)
    return user


def _next_slot_cost(user: Dict[str, Any]) -> int:
    return 1000 * (2 ** user.get('purchased_slots', 0))


def _now() -> int:
    return int(time.time())


def _calc_accrued_for_slot(slot: Dict[str, Any], owner_id: str | None = None, slot_index: int | None = None) -> int:
    rate = _effective_income_per_day(slot, owner_id, slot_index)
    last = int(slot.get('last_collected_at') or slot.get('created_at') or _now())
    elapsed = max(0, _now() - last)
    days = elapsed / 86400.0
    accrued = int(days * rate)
    pending = int(slot.get('pending_collect', 0))
    return accrued + pending

def _total_boost_pct(slot: Dict[str, Any], owner_id: str | None = None, slot_index: int | None = None) -> float:
    """Sum boost_pct from purchased upgrades (preferred) or legacy slot['upgrades'].
    Returns total percent (e.g., 12.5 for +12.5%).
    """
    total = 0.0
    # Prefer purchases file if context is available
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


def _effective_income_per_day(slot: Dict[str, Any], owner_id: str | None = None, slot_index: int | None = None) -> int:
    """Return the effective income_per_day scaled by rating and upgrades boost.
    inc_effective = income_per_day * rating * (1 + total_boost_pct/100).
    """
    try:
        base = float(slot.get('income_per_day', 0))
        rating = float(slot.get('rating', 1.0))
        boost_pct = _total_boost_pct(slot, owner_id, slot_index)
        mult = (1.0 + float(boost_pct) / 100.0)
        return max(0, int(round(base * rating * mult)))
    except Exception:
        return int(slot.get('income_per_day', 0))


def _sell_value(slot: Dict[str, Any], owner_id: str | None = None, slot_index: int | None = None) -> int:
    # Sell value scales with rating and upgrades: higher effective income => higher value
    effective = _effective_income_per_day(slot, owner_id, slot_index)
    return int(effective * SELL_MULTIPLIER + _calc_accrued_for_slot(slot, owner_id, slot_index))


# --------------- Gemini Scoring Integration ---------------

def _extract_json(text: str) -> Dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        snippet = text[start:end + 1]
        try:
            return json.loads(snippet)
        except Exception:
            return {}
    return {}


_GENAI_CLIENT = None

def _get_genai_client():
    global _GENAI_CLIENT
    if _GENAI_CLIENT is not None:
        return _GENAI_CLIENT
    if not GEMINI_API_KEY or genai is None:
        return None
    try:
        _GENAI_CLIENT = genai.Client(api_key=GEMINI_API_KEY)
        print(f"[Gemini] Initialized google-genai client with key ...{_mask_key(GEMINI_API_KEY)}")
    except Exception as e:
        print(f"[Gemini] Failed to init google-genai client: {type(e).__name__}: {e}")
        _GENAI_CLIENT = None
    return _GENAI_CLIENT


def _mask_key(key: str | None) -> str:
    if not key:
        return "<missing>"
    k = key.strip()
    if len(k) <= 8:
        return "***" + k[-4:]
    return k[:4] + "***" + k[-4:]


async def _gemini_generate(prompt: str) -> str:
    if not GEMINI_API_KEY:
        print("[Gemini] Missing GEMINI_API_KEY; skipping request.")
        return ''
    if genai is None:
        print("[Gemini] google-genai package not installed; skipping request.")
        return ''

    client = _get_genai_client()
    if client is None:
        return ''

    # Use a supported fast model; adjust if you prefer pro
    model_name = "gemini-2.5-flash"

    def _call_sync() -> str:
        try:
            resp = client.models.generate_content(model=model_name, contents=prompt)
            text = getattr(resp, 'text', None)
            if text:
                print(f"[Gemini] {model_name} OK; text first 200: {text[:200]}")
                return text
            # If SDK returns full object, stringify for parser
            j = None
            try:
                j = resp.to_dict()  # type: ignore[attr-defined]
            except Exception:
                try:
                    j = resp.__dict__
                except Exception:
                    j = None
            if j is not None:
                s = json.dumps(j)
                print(f"[Gemini] {model_name} no .text; dumping json first 200: {s[:200]}")
                return s
            return ''
        except Exception as e:
            print(f"[Gemini] generate_content error: {type(e).__name__}: {e}")
            return ''

    # Offload sync client to a thread to avoid blocking the event loop
    return await asyncio.to_thread(_call_sync)


async def _score_business_with_gemini(name: str, desc: str) -> Tuple[int, int, int]:
    prompt = (
        "You are scoring a business idea on three criteria. "
        "Return ONLY a compact JSON object with integer fields 'difficulty', 'earning', and 'realistic', each 0-10.\n\n"
        f"Business Name: {name}\n"
        f"Description: {desc}\n\n"
        "Rules:\n"
        "- difficulty: Higher means harder to set up.\n"
        "- earning: Higher means it can make more money.\n"
        "- realistic: Higher means it's more realistic.\n"
        "- If the description is too short or vague (fewer than 2 sentences or under 12 words), decrease all three scores to reflect low detail.\n"
        "Output example: {\"difficulty\": 4, \"earning\": 7, \"realistic\": 6}"
    )
    text = await _gemini_generate(prompt)
    if not text:
        print("[Gemini] Empty response; using fallback scores.")
        return (0, 0, 0)

    parsed: Dict[str, Any] | None = None
    model_text: str | None = None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and 'candidates' in parsed:
            try:
                cand = parsed.get('candidates', [{}])[0]
                parts = cand.get('content', {}).get('parts', [])
                if parts and isinstance(parts[0], dict) and 'text' in parts[0]:
                    model_text = parts[0]['text']
                    print(f"[Gemini] Extracted model text (first 200): {(model_text or '')[:200]}")
            except Exception as e:
                print(f"[Gemini] Nested parse error: {type(e).__name__}: {e}")
        else:
            model_text = text
    except Exception:
        model_text = text

    if not model_text:
        print("[Gemini] No model text present; using fallback scores.")
        return (1, 1, 1)

    data = _extract_json(model_text)
    print(f"[Gemini] Parsed JSON: {data}")
    try:
        d = int(max(0, min(10, int(data.get('difficulty', 5)))))
        e = int(max(0, min(10, int(data.get('earning', 10)))))
        r = int(max(0, min(10, int(data.get('realistic', 5)))))
        # Post-adjustment: penalize insufficient descriptions locally as a safeguard
        # Consider too short if < 2 sentences or < 12 words
        try:
            simple = desc or ""
            sent_parts = simple.replace('!', '.').replace('?', '.').split('.')
            sentences = sum(1 for p in sent_parts if p.strip())
            words = len([w for w in simple.split() if w.strip()])
            if sentences < 2 or words < 12:
                penalty = 3
                d = max(0, d - penalty)
                e = max(0, e - penalty)
                r = max(0, r - penalty)
        except Exception:
            pass
        print(f"[Gemini] Final scores -> difficulty: {d}, earning: {e}, realistic: {r}")
        return (d, e, r)
    except Exception as e:
        print(f"[Gemini] Score parse error: {type(e).__name__}: {e}; using fallback scores.")
        return (1, 1, 1)


def _render_passive_embed(
    user: Dict[str, Any],
    notice: str | None = None,
    owner_id: str | None = None,
    owner_name: str | None = None,
    owner_avatar: str | None = None,
) -> discord.Embed:
    title = "Passive Businesses"
    embed = discord.Embed(title=title, color=discord.Color.blurple())
    if owner_avatar:
        embed.set_author(name=owner_name or "", icon_url=owner_avatar)

    # Put notice in the description (not in footer)
    if notice:
        embed.description = notice

    # Show current global stock as its own field
    stock_data = _load_stocks()
    global_stock_pct = float(stock_data.get('current_pct', 50.0))
    embed.add_field(name="📉 Global Stock", value=f"{global_stock_pct:.1f}%", inline=False)

    # Show each slot as its own field
    if not user.get('slots'):
        embed.add_field(name="Slots", value="You have no slots yet.", inline=False)
    else:
        purchases = _load_purchases() if owner_id else {}
        user_purchases: Dict[str, Any] = purchases.get(str(owner_id), {}) if owner_id else {}
        for idx, slot in enumerate(user['slots']):
            field_name = f"Slot {idx + 1}"
            if slot is None:
                field_value = "Empty"
            else:
                name = slot.get('name', 'Business')
                inc = _effective_income_per_day(slot, owner_id, idx)
                # Use global stock (already loaded)
                stock_pct = global_stock_pct
                stock_factor = stock_pct / 50.0 if stock_pct != 0 else 0.0
                disp_inc = int(round(inc * stock_factor))
                base = int(slot.get('base_income_per_day', int(slot.get('income_per_day', 0))))
                wins = int(slot.get('wins', 0))
                losses = int(slot.get('losses', 0))
                rating = float(slot.get('rating', 1.0))
                ready = _calc_accrued_for_slot(slot, owner_id, idx)
                # Compute total boost from purchased upgrades file, fallback to legacy slot['upgrades']
                slot_key = str(idx)
                ups_from_file: List[Dict[str, Any]] = user_purchases.get(slot_key, []) if isinstance(user_purchases, dict) else []
                ups_legacy = slot.get('upgrades', []) or []
                total_boost = 0.0
                up_count = 0
                if ups_from_file:
                    for up in ups_from_file:
                        try:
                            total_boost += float(up.get('boost_pct', 0.0))
                        except Exception:
                            continue
                    up_count = len(ups_from_file)
                elif ups_legacy:
                    mk = _load_market()
                    u_map = {str(u.get('id')): u for u in mk.get('upgrades', [])}
                    for up in ups_legacy:
                        if isinstance(up, dict):
                            total_boost += float(up.get('boost_pct', 0.0))
                        else:
                            u = u_map.get(str(up))
                            if u is not None:
                                total_boost += float(u.get('boost_pct', 0.0))
                    up_count = len(ups_legacy)
                sold = int(slot.get('products_sold', 0))
                field_value = (
                    f"{name} — <:greensl:1409394243025502258>{disp_inc}/day (Base <:greensl:1409394243025502258>{base}) • ⭐ {rating:.1f}\n"
                    f"W/L: {wins}/{losses} • Ready: <:greensl:1409394243025502258>{ready} • Sold: {sold}"
                )
                # Add a compact upgrades summary line if any
                if up_count:
                    field_value += f"\nUpgrades: {up_count} • Boost: +{total_boost:.1f}%"
            embed.add_field(name=field_name, value=field_value, inline=False)

    # Keep costs and balance in the footer
    cost = _next_slot_cost(user)
    footer = f"Buy new slot cost: GL${cost} • Balance: GL${user.get('balance', 0)}"
    embed.set_footer(text=footer)
    return embed


def _render_business_embed(
    slot: Dict[str, Any],
    slot_index: int,
    user: Dict[str, Any],
    owner_id: str | None = None,
    owner_name: str | None = None,
    owner_avatar: str | None = None,
) -> discord.Embed:
    name = slot.get('name', f'Business {slot_index + 1}')
    inc = _effective_income_per_day(slot, owner_id, slot_index)
    base = int(slot.get('base_income_per_day', int(slot.get('income_per_day', 0))))
    rating = float(slot.get('rating', 1.0))
    total_earned = int(slot.get('total_earned', 0))
    ready = _calc_accrued_for_slot(slot, owner_id, slot_index)
    value = _sell_value(slot, owner_id, slot_index)
    title = f"{name}"
    embed = discord.Embed(title=title, color=discord.Color.gold())
    if owner_avatar:
        embed.set_author(name=owner_name or "", icon_url=owner_avatar)
    stock = _load_stocks()
    stock_pct = float(stock.get('current_pct', 50.0))
    stock_factor = stock_pct / 50.0 if stock_pct != 0 else 0.0
    disp_inc = int(round(inc * stock_factor))
    embed.add_field(name="📈 Rate", value=f"<:greensl:1409394243025502258>{disp_inc}/day (Base <:greensl:1409394243025502258>{base})", inline=True)
    embed.add_field(name="📉 Stock", value=f"{stock_pct:.1f}% from <:greensl:1409394243025502258>{int(slot.get('income_per_day', 0))}/day", inline=True)
    embed.add_field(name="⭐ Rating", value=f"{rating:.1f}", inline=True)
    embed.add_field(name="💵 Ready to collect", value=f"<:greensl:1409394243025502258>{ready}", inline=True)
    embed.add_field(name="💰 Total earned", value=f"<:greensl:1409394243025502258>{total_earned}", inline=True)
    embed.add_field(name="🛒 Products sold", value=str(int(slot.get('products_sold', 0))), inline=True)
    wins = int(slot.get('wins', 0))
    losses = int(slot.get('losses', 0))
    embed.add_field(name="🏆 Record", value=f"{wins} wins / {losses} losses", inline=True)
    # Upgrades applied details and total boost (prefer purchases file, fallback to legacy)
    lines: List[str] = []
    total_boost = 0.0
    purchases = _load_purchases() if owner_id else {}
    ups_from_file: List[Dict[str, Any]] = []
    if owner_id:
        ups_from_file = (purchases.get(str(owner_id), {}) or {}).get(str(slot_index), []) or []
    if ups_from_file:
        for up in ups_from_file:
            b = float(up.get('boost_pct', 0.0))
            total_boost += b
            lines.append(f"• {up.get('name', 'Upgrade')} (+{b:.1f}%)")
    else:
        ups_legacy: List[Any] = slot.get('upgrades', []) or []
        if ups_legacy:
            market = _load_market()
            up_map = {str(u.get('id')): u for u in market.get('upgrades', [])}
            for up in ups_legacy:
                if isinstance(up, dict):
                    b = float(up.get('boost_pct', 0.0))
                    total_boost += b
                    lines.append(f"• {up.get('name', 'Upgrade')} (+{b:.1f}%)")
                else:
                    u = up_map.get(str(up))
                    if u is None:
                        lines.append("• Upgrade (+0.0%)")
                        continue
                    b = float(u.get('boost_pct', 0.0))
                    total_boost += b
                    lines.append(f"• {u.get('name', 'Upgrade')} (+{b:.1f}%)")
    if lines:
        embed.add_field(name="🧩 Upgrades", value="\n".join(lines)[:1024], inline=False)
        embed.add_field(name="📊 Total boost", value=f"+{total_boost:.1f}%", inline=True)
    if slot.get('desc'):
        embed.add_field(name="About", value=slot['desc'], inline=False)
    embed.set_footer(text=f"Slot {slot_index + 1} • Sell value: GL${value} • Balance: GL${user.get('balance', 0)}")
    return embed


class CreateBusinessModal(discord.ui.Modal, title="Create Business"):
    _fallback_message: discord.Message | None
    def __init__(self, user_id: int, slot_index: int, origin_message: discord.Message | None = None, origin_channel_id: int | None = None, origin_message_id: int | None = None):
        super().__init__()
        self.user_id = str(user_id)
        self.slot_index = slot_index
        # Message containing the original embed/view to edit in place
        self.origin_message = origin_message
        # IDs as a reliable fallback to refetch the message if needed
        self.origin_channel_id = origin_channel_id
        self.origin_message_id = origin_message_id
        # If we fail to edit the origin message, we'll post a follow-up and keep editing that instead
        self._fallback_message = None  # will hold a discord.Message

        self.name = discord.ui.TextInput(
            label="Business name",
            placeholder="e.g., Daily Brew Coffee",
            max_length=80
        )
        self.desc = discord.ui.TextInput(
            label="What does it do?",
            style=discord.TextStyle.paragraph,
            placeholder="Describe the business idea briefly",
            max_length=500
        )
        self.add_item(self.name)
        self.add_item(self.desc)

    async def _resolve_origin_message(self, interaction: discord.Interaction) -> discord.Message | None:
        """Resolve and cache the original message to edit, using cached object or fetch by IDs."""
        if self.origin_message is not None:
            return self.origin_message
        try:
            if self.origin_channel_id and self.origin_message_id and interaction.client:
                # Try cache first
                ch = interaction.client.get_channel(self.origin_channel_id)  # type: ignore[attr-defined]
                # If not cached, try fetching the channel from API
                if ch is None:
                    try:
                        ch = await interaction.client.fetch_channel(self.origin_channel_id)  # type: ignore[attr-defined]
                    except Exception:
                        ch = None
                # If still None but we're in the same channel, use interaction.channel
                if ch is None and interaction.channel_id == self.origin_channel_id:
                    ch = interaction.channel  # type: ignore[assignment]
                if ch is not None and hasattr(ch, 'fetch_message'):
                    msg = await ch.fetch_message(self.origin_message_id)  # type: ignore[attr-defined]
                    self.origin_message = msg
                    return msg
        except Exception:
            return None
        return None

    async def _edit_origin(self, interaction: discord.Interaction, *, embed: discord.Embed | None = None, view: discord.ui.View | None = None, content: str | None = None) -> bool:
        """Edit the origin message in place; if unavailable, send/update a follow-up message.

        Returns True if we successfully showed the update somewhere, False otherwise.
        """
        # Prefer editing the original message in place
        msg = self.origin_message
        try:
            if msg is None:
                msg = await self._resolve_origin_message(interaction)
            if msg is not None:
                await msg.edit(content=content, embed=embed, view=view)
                self.origin_message = msg
                return True
        except Exception as e:
            print(f"[Modal] Failed to edit origin message: {type(e).__name__}: {e}")

        # Fallback: edit previously posted follow-up message if available
        if self._fallback_message is not None:
            try:
                msg2 = self._fallback_message
                if isinstance(msg2, discord.Message):
                    await msg2.edit(content=content, embed=embed, view=view)
                    return True
            except Exception as e:
                print(f"[Modal] Failed to edit fallback message: {type(e).__name__}: {e}")

        # Final fallback: post a follow-up message (requires the interaction to be deferred or responded)
        try:
            # Ensure we've at least acknowledged the interaction
            if not interaction.response.is_done():
                try:
                    await interaction.response.defer()
                except Exception:
                    pass
            # Send follow-up with only the provided fields to satisfy type checking
            if embed is not None and view is not None:
                sent = await interaction.followup.send(content=(content if content is not None else ""), embed=embed, view=view, ephemeral=False)
            elif embed is not None:
                sent = await interaction.followup.send(content=(content if content is not None else ""), embed=embed, ephemeral=False)
            elif view is not None:
                sent = await interaction.followup.send(content=(content if content is not None else ""), view=view, ephemeral=False)
            else:
                sent = await interaction.followup.send(content=(content if content is not None else ""), ephemeral=False)
            # Keep editing this message for future updates
            if isinstance(sent, discord.Message):
                self._fallback_message = sent
            return True
        except Exception as e:
            print(f"[Modal] Failed to send follow-up message: {type(e).__name__}: {e}")
            return False

    async def on_submit(self, interaction: discord.Interaction):
        # Acknowledge the modal submit quickly; we'll edit the original message in place.
        if not interaction.response.is_done():
            try:
                await interaction.response.defer()
            except Exception:
                pass

        owner_name = interaction.user.display_name
        try:
            owner_avatar = str(interaction.user.display_avatar.url)
        except Exception:
            owner_avatar = None

        # 1) Show progress on the original message embed (edit in place)
        progress = discord.Embed(
            title="Creating your business...",
            description="### 🖊️ Scoring your idea. This may take a few seconds.",
            color=discord.Color.orange(),
        )
        progress.add_field(name="Name", value=self.name.value, inline=False)
        if self.desc.value:
            progress.add_field(name="About", value=self.desc.value[:1024], inline=False)
        # Edit the original message; use helper to ensure we only edit in place
        await self._edit_origin(interaction, embed=progress, view=None)

        # 2) Score with Gemini
        difficulty, earning, realistic = await _score_business_with_gemini(self.name.value, self.desc.value)
        total = difficulty + earning + realistic
        # Ensure a minimum income of 1/day if the score totals to 0
        base_income = total if total > 0 else 1
        income_per_day = base_income

        # 3) Persist the business
        data = _load_users()
        user = data.get(self.user_id)
        if user is None:
            # Update the original message if possible; otherwise do nothing (no new messages)
            await self._edit_origin(interaction, content="User record missing.", embed=None, view=None)
            return

        user['slots'][self.slot_index] = {
            'name': self.name.value,
            'desc': self.desc.value,
            'scores': {
                'difficulty': difficulty,
                'earning': earning,
                'realistic': realistic,
                'total': total,
            },
            'income_per_day': income_per_day,
            'base_income_per_day': base_income,
            # Keep difference stable at 0 for new businesses
            'difference': 0,
            'rating': 1.0,
            'wins': 0,
            'losses': 0,
            'created_at': _now(),
            'last_collected_at': _now(),
            'total_earned': 0,
            'products_sold': 0,
            'pending_collect': 0,
        }
        _save_users(data)

        # 4) Final state: show created business and actions
        # Also clear any previous purchased upgrades persisted for this slot (fresh business)
        try:
            purchases = _load_purchases()
            urec = purchases.get(self.user_id, {}) or {}
            if str(self.slot_index) in urec:
                del urec[str(self.slot_index)]
            purchases[self.user_id] = urec
            _save_purchases(purchases)
        except Exception:
            pass
        final_user = data.get(self.user_id) or user
        slot = final_user['slots'][self.slot_index]
        final_embed = _render_business_embed(slot, self.slot_index, final_user, owner_id=self.user_id, owner_name=owner_name, owner_avatar=owner_avatar)
        final_embed.description = (final_embed.description + "\n" if final_embed.description else "") + "### ✅ Business created successfully."
        # 4) Replace with the new business embed + actions
        await self._edit_origin(
            interaction,
            embed=final_embed,
            view=BusinessView(self.user_id, self.slot_index, owner_name=owner_name, owner_avatar=owner_avatar),
        )
        # If edit fails, we do nothing (no new messages)


class SlotSelect(discord.ui.Select):
    def __init__(self, user: Dict[str, Any], owner_id: str, owner_name: str | None = None, owner_avatar: str | None = None):
        options: List[discord.SelectOption] = []
        self.owner_id = owner_id
        self.owner_name = owner_name
        self.owner_avatar = owner_avatar
        for idx, slot in enumerate(user['slots']):
            label = f"Slot {idx + 1}"
            desc = "Empty" if slot is None else slot.get('name', 'Occupied')
            options.append(discord.SelectOption(label=label, description=desc, value=str(idx)))
        options.append(discord.SelectOption(label="Buy new slot", description="Purchase an additional slot", value="buy"))
        super().__init__(placeholder="Choose a slot or buy a new one", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        # Gate interactions to the original user
        if str(interaction.user.id) != self.owner_id:
            await interaction.response.send_message("> ❌ You not the owner, get out.", ephemeral=True)
            return
        # If a modal is already open, ignore further interactions
        if self.view is not None and getattr(self.view, "locked", False):
            await interaction.response.send_message("> ❌ Please complete the open modal first.", ephemeral=True)
            return
        user_id = str(interaction.user.id)
        data = _load_users()
        user = data.get(user_id)
        if user is None:
            await interaction.response.edit_message(embed=_render_passive_embed({'slots': [None], 'purchased_slots': 0, 'balance': 0}, "User not found", owner_id=self.owner_id, owner_name=self.owner_name, owner_avatar=self.owner_avatar), view=self.view)
            return

        notice: str | None = None
        choice = self.values[0]
        if choice == 'buy':
            cost = _next_slot_cost(user)
            if user['balance'] < cost:
                notice = f"### ❌ Not enough funds. Need <:greensl:1409394243025502258>{cost}"
            else:
                user['balance'] -= cost
                user['slots'].append(None)
                user['purchased_slots'] = user.get('purchased_slots', 0) + 1
                _save_users(data)
                notice = f"### ✅ Purchased a new slot for <:greensl:1409394243025502258>{cost}"
            # Re-render regardless of success/failure
            await interaction.response.edit_message(embed=_render_passive_embed(user, notice, owner_id=self.owner_id, owner_name=self.owner_name, owner_avatar=self.owner_avatar), view=SlotView(user, self.owner_id, self.owner_name, self.owner_avatar))
            return

        idx = int(choice)
        slot = user['slots'][idx]
        if slot is not None:
            # Show business details view
            await interaction.response.edit_message(embed=_render_business_embed(slot, idx, user, owner_id=self.owner_id, owner_name=self.owner_name, owner_avatar=self.owner_avatar), view=BusinessView(user_id, idx, owner_name=self.owner_name, owner_avatar=self.owner_avatar))
            return

        # Open modal to create a business in this slot (must respond with a modal, not an edit)
        # Lock the view to block further interactions until modal completes
        if self.view is not None:
            setattr(self.view, "locked", True)
        await interaction.response.send_modal(CreateBusinessModal(
            interaction.user.id,
            idx,
            origin_message=interaction.message,
            origin_channel_id=(interaction.channel.id if interaction.channel else None),
            origin_message_id=(interaction.message.id if interaction.message else None),
        ))


class SlotView(discord.ui.View):
    def __init__(self, user: Dict[str, Any], owner_id: str, owner_name: str | None = None, owner_avatar: str | None = None):
        super().__init__(timeout=120)
        # Lock flag to prevent double interactions while a modal is open
        self.locked = False
        self.add_item(SlotSelect(user, owner_id, owner_name, owner_avatar))


class BusinessView(discord.ui.View):
    def __init__(self, user_id: str, slot_index: int, owner_name: str | None = None, owner_avatar: str | None = None):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.slot_index = slot_index
        self.owner_name = owner_name
        self.owner_avatar = owner_avatar

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("> ❌ nuh uh", ephemeral=True)
            return
        data = _load_users()
        user = data.get(str(interaction.user.id)) or {'balance': 0, 'slots': [None], 'purchased_slots': 0}
        await interaction.response.edit_message(
            embed=_render_passive_embed(user, owner_id=self.user_id, owner_name=self.owner_name, owner_avatar=self.owner_avatar),
            view=SlotView(user, self.user_id, self.owner_name, self.owner_avatar)
        )

    @discord.ui.button(label="Collect", style=discord.ButtonStyle.success)
    async def collect(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Only the owner can collect
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("> ❌ This isn't your business.", ephemeral=True)
            return
        data = _load_users()
        user = data.get(str(interaction.user.id))
        if not user:
            await interaction.response.edit_message(embed=_render_passive_embed({'balance': 0, 'slots': [None], 'purchased_slots': 0}, "User not found", owner_id=self.user_id, owner_name=self.owner_name, owner_avatar=self.owner_avatar), view=None)
            return
        # Validate slot
        if self.slot_index >= len(user['slots']) or user['slots'][self.slot_index] is None:
            await interaction.response.edit_message(embed=_render_passive_embed(user, "Slot is empty", owner_id=self.user_id, owner_name=self.owner_name, owner_avatar=self.owner_avatar), view=SlotView(user, self.user_id, self.owner_name, self.owner_avatar))
            return
        slot = user['slots'][self.slot_index]
        amount = _calc_accrued_for_slot(slot, self.user_id, self.slot_index)
        if amount <= 0:
            await interaction.response.send_message("> ℹ️ Nothing to collect yet.", ephemeral=True)
            return
        # Apply collection
        user['balance'] = int(user.get('balance', 0)) + int(amount)
        slot['last_collected_at'] = _now()
        slot['pending_collect'] = 0
        slot['total_earned'] = int(slot.get('total_earned', 0)) + int(amount)
        _save_users(data)
        # Re-render business details with confirmation
        try:
            owner_avatar = str(interaction.user.display_avatar.url)
        except Exception:
            owner_avatar = None
        embed = _render_business_embed(slot, self.slot_index, user, owner_id=self.user_id, owner_name=interaction.user.display_name, owner_avatar=owner_avatar)
        embed.description = (embed.description + "\n" if embed.description else "") + f"### ✅ Collected <:greensl:1409394243025502258>{amount}"
        await interaction.response.edit_message(embed=embed, view=BusinessView(self.user_id, self.slot_index, owner_name=self.owner_name, owner_avatar=self.owner_avatar))

    @discord.ui.button(label="Sell", style=discord.ButtonStyle.danger)
    async def sell(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.user_id:
            await interaction.response.send_message("> ❌ bro trying to sabotage XDDD", ephemeral=True)
            return
        data = _load_users()
        user = data.get(str(interaction.user.id))
        if not user:
            await interaction.response.edit_message(embed=_render_passive_embed({'balance': 0, 'slots': [None], 'purchased_slots': 0}, "User not found", owner_id=self.user_id, owner_name=self.owner_name, owner_avatar=self.owner_avatar), view=None)
            return
        if self.slot_index >= len(user['slots']) or user['slots'][self.slot_index] is None:
            await interaction.response.edit_message(embed=_render_passive_embed(user, "Slot is empty", owner_id=self.user_id, owner_name=self.owner_name, owner_avatar=self.owner_avatar), view=SlotView(user, self.user_id, self.owner_name, self.owner_avatar))
            return
        slot = user['slots'][self.slot_index]
        # Enforce a 10 mins hold before selling
        created_at_val = int(slot.get('created_at', 0))
        if created_at_val > 0:
            elapsed = max(0, _now() - created_at_val)
            if elapsed < 600:
                remaining = 600 - elapsed
                mins = remaining // 60
                secs = remaining % 60
                msg = f"> ❌ You can sell this business after 10 mins of creating a business.\n> **⌚ Time remaining: {mins}m {secs}s**"
                await interaction.response.send_message(msg, ephemeral=True)
                return
        name = slot.get('name', f'Slot {self.slot_index + 1}')
        value = _sell_value(slot, self.user_id, self.slot_index)
        user['balance'] = int(user.get('balance', 0)) + int(value)
        user['slots'][self.slot_index] = None
        _save_users(data)
        # Clear purchased upgrades for this slot
        try:
            purchases = _load_purchases()
            urec = purchases.get(str(self.user_id), {})
            if isinstance(urec, dict) and str(self.slot_index) in urec:
                del urec[str(self.slot_index)]
            purchases[str(self.user_id)] = urec
            _save_purchases(purchases)
        except Exception:
            pass
        await interaction.response.edit_message(embed=_render_passive_embed(user, f"### ✅ Sold {name} for <:greensl:1409394243025502258>{value}", owner_id=self.user_id, owner_name=self.owner_name, owner_avatar=self.owner_avatar), view=SlotView(user, self.user_id, self.owner_name, self.owner_avatar))


class PassiveCommand:
    @staticmethod
    async def setup(tree: app_commands.CommandTree):
        @tree.command(name="passive", description="Manage passive income businesses")
        @app_commands.allowed_contexts(dms=True, guilds=True, private_channels=True)
        async def passive(interaction: discord.Interaction):
            user = _ensure_user(str(interaction.user.id))
            # Normalize existing slots to include pending_collect for older records
            changed = False
            for i, s in enumerate(user.get('slots', [])):
                if s is not None and 'pending_collect' not in s:
                    s['pending_collect'] = 0
                    changed = True
            if changed:
                data = _load_users()
                data[str(interaction.user.id)] = user
                _save_users(data)
            owner_id = str(interaction.user.id)
            owner_name = interaction.user.display_name
            try:
                owner_avatar = str(interaction.user.display_avatar.url)
            except Exception:
                owner_avatar = None
            embed = _render_passive_embed(user, owner_id=owner_id, owner_name=owner_name, owner_avatar=owner_avatar)
            await interaction.response.send_message(embed=embed, view=SlotView(user, owner_id, owner_name, owner_avatar))
