import os
import json
import time
import asyncio
from typing import Dict, Any, List, Optional, Tuple

import discord
from discord import app_commands

# Data locations
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
MARKET_FILE = os.path.join(DATA_DIR, 'market.json')
USER_FILE = os.path.join(DATA_DIR, 'users.json')
PURCHASED_FILE = os.path.join(DATA_DIR, 'purchased_upgrades.json')

GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

# Try official Google Gemini client
try:
    from google import genai  # type: ignore
except Exception:  # pragma: no cover
    genai = None  # type: ignore


# --------------- Persistence helpers ---------------

def _ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)


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


def _load_market() -> Dict[str, Any]:
    if not os.path.exists(MARKET_FILE):
        return {"upgrades": [], "last_id": 0}
    with open(MARKET_FILE, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {"upgrades": [], "last_id": 0}


def _save_market(data: Dict[str, Any]):
    _ensure_dirs()
    with open(MARKET_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


def _load_purchases() -> Dict[str, Any]:
    if not os.path.exists(PURCHASED_FILE):
        return {}
    with open(PURCHASED_FILE, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def _save_purchases(data: Dict[str, Any]):
    _ensure_dirs()
    with open(PURCHASED_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


def _now() -> int:
    return int(time.time())


# --------------- Gemini helpers ---------------

_GENAI_CLIENT = None


def _mask_key(key: str | None) -> str:
    if not key:
        return "<missing>"
    k = key.strip()
    if len(k) <= 8:
        return "***" + k[-4:]
    return k[:4] + "***" + k[-4:]


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
    model_name = "gemini-2.5-flash"

    def _call_sync() -> str:
        try:
            resp = client.models.generate_content(model=model_name, contents=prompt)
            text = getattr(resp, 'text', None)
            if text:
                return text
            try:
                j = resp.to_dict()  # type: ignore[attr-defined]
            except Exception:
                try:
                    j = resp.__dict__
                except Exception:
                    j = None
            if j is not None:
                return json.dumps(j)
            return ''
        except Exception as e:
            print(f"[Gemini] generate_content error: {type(e).__name__}: {e}")
            return ''

    return await asyncio.to_thread(_call_sync)


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


async def _score_upgrade_with_gemini(name: str, desc: str) -> Tuple[int, int, int, float]:
    """Return (realistic, useful, total20, average10). average10 is used as % boost."""
    prompt = (
        "You are evaluating a game upgrade for a business's passive income. "
        "Return ONLY a JSON object with integer fields 'realistic' and 'useful' (each 0-10).\n\n"
        f"Upgrade Name: {name}\n"
        f"Description: {desc}\n\n"
        "Rules:\n"
        "- realistic: Higher means plausible in real life.\n"
        "- useful: Higher means it meaningfully improves revenue.\n"
        "- Penalize vague/short descriptions (< 2 sentences or < 12 words).\n"
        "Example output: {\"realistic\": 6, \"useful\": 9}"
    )
    text = await _gemini_generate(prompt)
    if not text:
        return (5, 5, 10, 5.0)
    data = _extract_json(text)
    try:
        realistic = int(max(0, min(10, int(data.get('realistic', 5)))))
        useful = int(max(0, min(10, int(data.get('useful', 5)))))
        # Penalize locally if too short
        try:
            simple = desc or ""
            sent_parts = simple.replace('!', '.').replace('?', '.').split('.')
            sentences = sum(1 for p in sent_parts if p.strip())
            words = len([w for w in simple.split() if w.strip()])
            if sentences < 2 or words < 12:
                penalty = 2
                realistic = max(0, realistic - penalty)
                useful = max(0, useful - penalty)
        except Exception:
            pass
        total = realistic + useful  # out of 20
        average = total / 2.0       # out of 10
        return realistic, useful, total, float(average)
    except Exception:
        return (5, 5, 10, 5.0)


# --------------- Rendering ---------------

def _render_market_embed(upgrades: List[Dict[str, Any]], owner_name: Optional[str] = None, owner_avatar: Optional[str] = None) -> discord.Embed:
    embed = discord.Embed(title="Market", description="Create or buy community-made upgrades.", color=discord.Color.green())
    if owner_avatar:
        embed.set_author(name=owner_name or "", icon_url=owner_avatar)
    if not upgrades:
        embed.add_field(name="No upgrades yet", value="Click Create to add one.", inline=False)
    else:
        # Show up to 10 items
        for up in upgrades[:10]:
            # Prefer associated business name, fallback to creator for legacy items
            creator = up.get('business_name') or up.get('creator_name', 'Unknown')
            total = int(up.get('rating', {}).get('total', 0))
            avg = float(up.get('rating', {}).get('average', 0.0))
            price = int(up.get('price', 0))
            embed.add_field(
                name=f"{up.get('name', 'Upgrade')} — ${price}",
                value=(
                    f"by **{creator}**\n"
                    f"⭐ Rating: {total}/20 • Income Boost: {avg:.1f}%\n"
                    f"{up.get('desc', '')[:200]}"
                ),
                inline=False,
            )
    embed.set_footer(text="Use the selector below to buy. Create to add your own.")
    return embed


# --------------- UI Components ---------------

class CreateUpgradeModal(discord.ui.Modal, title="Create Upgrade"):
    def __init__(self):
        super().__init__()
        self.name = discord.ui.TextInput(label="Upgrade name", placeholder="e.g., Targeted Ad Campaign", max_length=80)
        self.desc = discord.ui.TextInput(label="What does it do?", style=discord.TextStyle.paragraph, placeholder="Describe the upgrade", max_length=500)
        self.add_item(self.name)
        self.add_item(self.desc)

    async def on_submit(self, interaction: discord.Interaction):
        # Use an ephemeral progress message we can edit safely
        progress = discord.Embed(title="Submitting upgrade...", description="Scoring your idea.", color=discord.Color.orange())
        if not interaction.response.is_done():
            await interaction.response.send_message(embed=progress, ephemeral=True)
            base_msg = await interaction.original_response()
        else:
            base_msg = await interaction.followup.send(embed=progress, ephemeral=True, wait=True)

        # Must have at least one business to list the upgrade under
        users = _load_users()
        user = users.get(str(interaction.user.id))
        slots = (user or {}).get('slots', []) if user else []
        has_any_business = any(s is not None for s in slots)
        if not has_any_business:
            err = discord.Embed(title="Can't create upgrade", description="You need a business first. Use /passive to create one.", color=discord.Color.red())
            await base_msg.edit(embed=err, view=None)
            return
        if not user:
            err = discord.Embed(title="Can't create upgrade", description="Profile not found. Use /passive first.", color=discord.Color.red())
            await base_msg.edit(embed=err, view=None)
            return

        realistic, useful, total, average = await _score_upgrade_with_gemini(self.name.value, self.desc.value)
        boost_pct = average  # Assumption: average (0-10) maps to % boost
        price = 100 + 50 * total  # simple price formula: 100..1100

        # Ask the creator to choose which of their businesses will sell this upgrade
        pick = discord.Embed(title="Choose a business to sell this upgrade", color=discord.Color.blurple())
        pick.add_field(name="Name", value=self.name.value, inline=False)
        pick.add_field(name="Rating", value=f"{total}/20 (avg {average:.1f})", inline=True)
        pick.add_field(name="Boost", value=f"{boost_pct:.1f}%", inline=True)
        pick.add_field(name="Price", value=f"${price}", inline=True)
        if self.desc.value:
            pick.add_field(name="About", value=self.desc.value[:1024], inline=False)
        draft = {
            'name': self.name.value,
            'desc': self.desc.value,
            'creator_id': str(interaction.user.id),
            'creator_name': interaction.user.display_name,
            'created_at': _now(),
            'rating': {
                'realistic': realistic,
                'useful': useful,
                'total': total,
                'average': average,
            },
            'boost_pct': float(boost_pct),
            'price': int(price),
        }
        view = ChooseSellerBusinessView.build_for_user(user, draft, allowed_user_id=str(interaction.user.id))
        await base_msg.edit(embed=pick, view=view)


class UpgradeSelect(discord.ui.Select):
    def __init__(self, upgrades: List[Dict[str, Any]]):
        options: List[discord.SelectOption] = []
        for up in upgrades[:25]:
            label = up.get('name', 'Upgrade')
            total = int(up.get('rating', {}).get('total', 0))
            price = int(up.get('price', 0))
            options.append(discord.SelectOption(label=label[:100], description=f"⭐ {total}/20 • 💵 ${price}", value=str(up.get('id'))))
        super().__init__(placeholder="Select an upgrade to buy", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        market = _load_market()
        upgrades: List[Dict[str, Any]] = market.get('upgrades', [])
        up = next((u for u in upgrades if str(u.get('id')) == self.values[0]), None)
        if not up:
            await interaction.response.send_message("> ❌ Upgrade not found", ephemeral=True)
            return
        # Pre-check user eligibility and funds
        user_id = str(interaction.user.id)
        data = _load_users()
        user = data.get(user_id)
        if not user:
            await interaction.response.send_message("> ❌ You have no profile. Use /passive first.", ephemeral=True)
            return
        # Disallow buying your own product
        if str(up.get('creator_id')) == user_id:
            await interaction.response.send_message("> ❌ You can't buy your own product.", ephemeral=True)
            return
        slots = user.get('slots', []) or []
        has_any = any(s is not None for s in slots)
        if not has_any:
            await interaction.response.send_message("> ❌ You need a business to apply this upgrade. Use /passive.", ephemeral=True)
            return
        price = int(up.get('price', 0))
        if int(user.get('balance', 0)) < price:
            await interaction.response.send_message(f"> ❌ Not enough funds. Need ${price}", ephemeral=True)
            return

        # Show ephemeral embed + select menu to choose the business to apply to
        total = int(up.get('rating', {}).get('total', 0))
        avg = float(up.get('rating', {}).get('average', 0.0))
        embed = discord.Embed(title=f"Apply '{up.get('name')}'", color=discord.Color.blue())
        embed.add_field(name="Rating", value=f"{total}/20 (avg {avg:.1f})", inline=True)
        embed.add_field(name="Boost", value=f"{avg:.1f}%", inline=True)
        embed.add_field(name="Price", value=f"${price}", inline=True)
        embed.add_field(name="About", value=up.get('desc', '')[:1024], inline=False)
        embed.set_footer(text="Select a business to purchase and apply this upgrade.")
        view = ApplyUpgradeView.build_for_user(user, up)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class MarketView(discord.ui.View):
    def __init__(self, upgrades: List[Dict[str, Any]], owner_name: Optional[str] = None, owner_avatar: Optional[str] = None):
        super().__init__(timeout=120)
        # Only add selector if there are upgrades to choose from
        if upgrades:
            self.add_item(UpgradeSelect(upgrades))
        self.owner_name = owner_name
        self.owner_avatar = owner_avatar

    @discord.ui.button(label="Create Upgrade", style=discord.ButtonStyle.success)
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CreateUpgradeModal())

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        market_data = _load_market()
        upgrades: List[Dict[str, Any]] = market_data.get('upgrades', [])
        embed = _render_market_embed(upgrades, owner_name=self.owner_name, owner_avatar=self.owner_avatar)
        view = MarketView(upgrades, owner_name=self.owner_name, owner_avatar=self.owner_avatar)
        await interaction.response.edit_message(embed=embed, view=view)


# Removed BuyView; the selection is shown immediately in the confirm message.


class SlotSelectForApply(discord.ui.Select):
    def __init__(self, user: Dict[str, Any]):
        options: List[discord.SelectOption] = []
        for idx, slot in enumerate(user.get('slots', [])):
            if slot is None:
                continue
            label = f"{slot.get('name', 'Business')}"
            desc = f"Income ${int(slot.get('income_per_day', 0))}/day"
            options.append(discord.SelectOption(label=label[:100], description=desc[:100], value=str(idx)))
        if not options:
            options.append(discord.SelectOption(label="No available businesses", value="-1", description="Create one with /passive"))
        super().__init__(placeholder="Choose a business", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        parent: ApplyUpgradeView = self.view  # type: ignore[assignment]
        await parent.apply_to_slot(interaction, int(self.values[0]))


class ApplyUpgradeView(discord.ui.View):
    def __init__(self, upgrade: Dict[str, Any]):
        super().__init__(timeout=120)
        self.upgrade = upgrade

    async def on_timeout(self) -> None:
        # self.clear_items()  # Optionally clear
        return

    async def apply_to_slot(self, interaction: discord.Interaction, slot_index: int):
        user_id = str(interaction.user.id)
        data = _load_users()
        user = data.get(user_id)
        if not user:
            await interaction.response.edit_message(content="> ❌ User not found.", view=None)
            return
        # Disallow creators from buying/applying their own products
        if str(self.upgrade.get('creator_id')) == user_id:
            await interaction.response.edit_message(content="> ❌ You can't buy your own product.", view=None)
            return
        if slot_index < 0 or slot_index >= len(user.get('slots', [])) or user['slots'][slot_index] is None:
            await interaction.response.edit_message(content="> ❌ Invalid slot.", view=None)
            return
        price = int(self.upgrade.get('price', 0))
        if int(user.get('balance', 0)) < price:
            await interaction.response.edit_message(content=f"> ❌ Not enough funds. Need ${price}", view=None)
            return
        slot = user['slots'][slot_index]
        # Prevent double-apply of same upgrade (support legacy string IDs and new dict entries)
        raw_applied = slot.get('upgrades', []) or []
        applied_ids = set(
            str(x.get('id')) if isinstance(x, dict) else str(x)
            for x in raw_applied
        )
        up_id = str(self.upgrade.get('id'))
        if up_id in applied_ids:
            await interaction.response.edit_message(content="> ℹ️ This upgrade is already applied to that business.", view=None)
            return
        # Apply effect: increase income by boost percentage
        boost_pct = float(self.upgrade.get('boost_pct', 0.0))
        current = int(slot.get('income_per_day', 0))
        new_income = max(0, int(round(current * (1.0 + boost_pct / 100.0))))
        slot['income_per_day'] = new_income
        # Store applied upgrade with metadata so it still displays after market removal
        applied_entry = {
            'id': up_id,
            'name': self.upgrade.get('name', 'Upgrade'),
            'boost_pct': float(boost_pct),
        }
        raw_applied.append(applied_entry)
        slot['upgrades'] = raw_applied
        # Persist to purchases file keyed by user->slot_index array
        try:
            purchases = _load_purchases()
            urec = purchases.get(user_id) or {}
            arr = urec.get(str(slot_index)) or []
            arr.append(applied_entry)
            urec[str(slot_index)] = arr
            purchases[user_id] = urec
            _save_purchases(purchases)
        except Exception:
            pass
        # Deduct balance and remove from market
        user['balance'] = int(user.get('balance', 0)) - price
        market = _load_market()
        ups = market.get('upgrades', [])
        market['upgrades'] = [u for u in ups if str(u.get('id')) != up_id]
        # Increment seller business products_sold counter and credit funds to seller's pending_collect
        try:
            seller_id = str(self.upgrade.get('creator_id'))
            seller_slot = int(self.upgrade.get('seller_slot_index', -1))
            if seller_id and seller_slot >= 0:
                seller = data.get(seller_id)
                if seller and 0 <= seller_slot < len(seller.get('slots', [])) and seller['slots'][seller_slot] is not None:
                    sslot = seller['slots'][seller_slot]
                    sslot['products_sold'] = int(sslot.get('products_sold', 0)) + 1
                    sslot['pending_collect'] = int(sslot.get('pending_collect', 0)) + int(price)
        except Exception:
            pass
        _save_users(data)
        _save_market(market)
        biz_name = slot.get('name', f'Slot {slot_index + 1}')
        up_name = str(self.upgrade.get('name', 'Upgrade'))
        await interaction.response.edit_message(content=f"> ✅ Applied **{up_name}** to **{biz_name}**\n> 📈 New income: **${new_income}/day**", view=None)

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        # Only the caller can apply
        return True

    @staticmethod
    def build_for_user(user: Dict[str, Any], upgrade: Dict[str, Any]) -> "ApplyUpgradeView":
        view = ApplyUpgradeView(upgrade)
        view.add_item(SlotSelectForApply(user))
        return view


class SellerSlotSelect(discord.ui.Select):
    def __init__(self, user: Dict[str, Any]):
        options: List[discord.SelectOption] = []
        for idx, slot in enumerate(user.get('slots', [])):
            if slot is None:
                continue
            label = f"{slot.get('name', 'Business')}"
            desc = f"Income ${int(slot.get('income_per_day', 0))}/day"
            options.append(discord.SelectOption(label=label[:100], description=desc[:100], value=str(idx)))
        if not options:
            options.append(discord.SelectOption(label="No available businesses", value="-1", description="Create one with /passive"))
        super().__init__(placeholder="Choose a business to sell this upgrade", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        parent: ChooseSellerBusinessView = self.view  # type: ignore[assignment]
        await parent.finalize_create(interaction, int(self.values[0]))


class ChooseSellerBusinessView(discord.ui.View):
    def __init__(self, draft: Dict[str, Any], allowed_user_id: str):
        super().__init__(timeout=120)
        self.draft = draft
        self.allowed_user_id = allowed_user_id

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        return str(interaction.user.id) == self.allowed_user_id

    async def on_timeout(self) -> None:
        # self.clear_items()
        return

    async def finalize_create(self, interaction: discord.Interaction, slot_index: int):
        users = _load_users()
        user = users.get(str(interaction.user.id))
        if not user:
            await interaction.response.edit_message(content="> ❌ User not found.", view=None)
            return
        if slot_index < 0 or slot_index >= len(user.get('slots', [])) or user['slots'][slot_index] is None:
            await interaction.response.edit_message(content="> ❌ Invalid business.", view=None)
            return
        slot = user['slots'][slot_index]
        business_name = slot.get('name', f"Slot {slot_index + 1}")
        market = _load_market()
        market['last_id'] = int(market.get('last_id', 0)) + 1
        upgrade_id = str(market['last_id'])
        upgrade = {
            'id': upgrade_id,
            'name': self.draft['name'],
            'desc': self.draft.get('desc', ''),
            'creator_id': self.draft.get('creator_id'),
            'creator_name': self.draft.get('creator_name'),
            'created_at': self.draft.get('created_at', _now()),
            'business_name': business_name,
            'seller_slot_index': slot_index,
            'rating': self.draft.get('rating', {}),
            'boost_pct': float(self.draft.get('boost_pct', 0.0)),
            'price': int(self.draft.get('price', 0)),
            'buyers': [],
        }
        ups = market.get('upgrades', [])
        ups.insert(0, upgrade)
        market['upgrades'] = ups
        _save_market(market)

        done = discord.Embed(title="Upgrade created!", color=discord.Color.green())
        done.add_field(name="Name", value=upgrade['name'], inline=False)
        total = int(upgrade.get('rating', {}).get('total', 0))
        average = float(upgrade.get('rating', {}).get('average', 0.0))
        boost_pct = float(upgrade.get('boost_pct', 0.0))
        price = int(upgrade.get('price', 0))
        done.add_field(name="Rating", value=f"{total}/20 (avg {average:.1f})", inline=True)
        done.add_field(name="Boost", value=f"{boost_pct:.1f}%", inline=True)
        done.add_field(name="Price", value=f"${price}", inline=True)
        done.add_field(name="Seller business", value=business_name, inline=False)
        await interaction.response.edit_message(embed=done, view=None)

    @staticmethod
    def build_for_user(user: Dict[str, Any], draft: Dict[str, Any], allowed_user_id: str) -> "ChooseSellerBusinessView":
        view = ChooseSellerBusinessView(draft, allowed_user_id)
        view.add_item(SellerSlotSelect(user))
        return view


# --------------- Command ---------------

class MarketCommand:
    @staticmethod
    async def setup(tree: app_commands.CommandTree):
        @tree.command(name="market", description="Browse and create custom upgrades")
        @app_commands.allowed_contexts(dms=True, guilds=True, private_channels=True)
        async def market(interaction: discord.Interaction):
            market_data = _load_market()
            upgrades: List[Dict[str, Any]] = market_data.get('upgrades', [])
            try:
                owner_avatar = str(interaction.user.display_avatar.url)
            except Exception:
                owner_avatar = None
            embed = _render_market_embed(upgrades, owner_name=interaction.user.display_name, owner_avatar=owner_avatar)
            view = MarketView(upgrades, owner_name=interaction.user.display_name, owner_avatar=owner_avatar)
            await interaction.response.send_message(embed=embed, view=view)
