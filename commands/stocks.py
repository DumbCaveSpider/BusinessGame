import os
import json
import time
import random
from typing import Dict, Any, List, Optional

import discord
from discord import app_commands

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
STOCK_FILE = os.path.join(DATA_DIR, 'stocks.json')
USER_FILE = os.path.join(DATA_DIR, 'users.json')
EQUITY_FILE = os.path.join(DATA_DIR, 'equity.json')


def _now() -> int:
    return int(time.time())


def _ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)


def _resolve_name_avatar(interaction: Optional[discord.Interaction], user_id: str) -> tuple[Optional[str], Optional[str]]:
    """Return (display_name, avatar_url) for a given user id using guild member or client cache."""
    name: Optional[str] = None
    avatar: Optional[str] = None
    try:
        if interaction is not None:
            member = None
            if interaction.guild is not None:
                try:
                    member = interaction.guild.get_member(int(user_id))
                except Exception:
                    member = None
            if member is not None:
                name = member.display_name
                try:
                    avatar = str(member.display_avatar.url)
                except Exception:
                    avatar = None
            else:
                try:
                    user_obj = interaction.client.get_user(int(user_id))  # type: ignore[attr-defined]
                    if user_obj is not None:
                        name = getattr(user_obj, 'display_name', None) or getattr(user_obj, 'name', None)
                        try:
                            avatar = str(user_obj.display_avatar.url)
                        except Exception:
                            avatar = None
                except Exception:
                    pass
    except Exception:
        name, avatar = None, None
    return name, avatar


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


def _load_equity() -> Dict[str, Any]:
    if not os.path.exists(EQUITY_FILE):
        return {}
    with open(EQUITY_FILE, 'r', encoding='utf-8') as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def _save_equity(data: Dict[str, Any]):
    _ensure_dirs()
    with open(EQUITY_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


SELL_MULTIPLIER = 0.5


def _sell_value_for_slot(slot: Dict[str, Any]) -> int:
    """Approximate sell value consistent with passive: effective income/day * 0.5 + accrued.
    Here, effective approximates to current income_per_day scaled by rating.
    """
    try:
        base = float(slot.get('income_per_day', 0))
        rating = float(slot.get('rating', 1.0))
        effective = max(0, int(round(base * rating)))
    except Exception:
        effective = int(slot.get('income_per_day', 0))
    # Accrued since last collection
    try:
        last = int(slot.get('last_collected_at') or slot.get('created_at') or int(time.time()))
        elapsed = max(0, int(time.time()) - last)
        days = elapsed / 86400.0
        accrued = int(days * effective)
    except Exception:
        accrued = 0
    return int(effective * SELL_MULTIPLIER + accrued)

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
        curr = max(0.0, min(200.0, curr + change))
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
    # Factor baseline: 100% => 1.0x
    factor = (stock_pct / 100.0) if stock_pct != 0 else 0.0
    for uid, user in list(data.items()):
        slots = user.get('slots', []) or []
        for idx, slot in enumerate(slots):
            if slot is None:
                continue
            # Use the stored base income as the source for applying stock factor
            base = int(slot.get('base_income_per_day', 0))
            new_income = max(0, int(round(base * factor)))
            if int(slot.get('income_per_day', 0)) != new_income:
                slot['income_per_day'] = new_income
                changed = True
            # Also refresh current income to track stocks so future rates are based on latest value
            # Avoid permanently zeroing the income when stock is 0%
            if factor > 0.0 and int(slot.get('income_per_day', 0)) != new_income:
                slot['income_per_day'] = new_income
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
    # Compute current movement emoji compared to previous historic value
    hist_full: List[Dict[str, Any]] = list(data.get('history', []))
    cur_delta_emoji = ""
    if hist_full:
        prev_pct = float(hist_full[-1].get('pct', curr))
        if curr > prev_pct:
            cur_delta_emoji = " 🟢"
        elif curr < prev_pct:
            cur_delta_emoji = " 🔴"
    embed.add_field(name="💸 Current", value=f"{curr:.1f}%{cur_delta_emoji}", inline=True)
    embed.add_field(name="⌚ Next tick", value=f"in {mins}m {secs}s", inline=True)
    # History (last 12)
    hist = hist_full[-12:]
    if hist:
        lines = []
        start_index = max(0, len(hist_full) - len(hist))
        for i, item in enumerate(hist):
            global_idx = start_index + i
            pct = float(item.get('pct', 0.0))
            # Determine emoji based on movement from previous historical item
            emoji = ""
            if global_idx > 0:
                prev = float(hist_full[global_idx - 1].get('pct', pct))
                if pct > prev:
                    emoji = "🟢 "
                elif pct < prev:
                    emoji = "🔴 "
            t = time.strftime('%m-%d %H:%M', time.localtime(int(item.get('t', now))))
            lines.append(f"{emoji}{t} — {pct:.1f}%")
        embed.add_field(name="📈 Recent", value="\n".join(lines), inline=False)
    embed.set_footer(text="Updates every hour by ±10%")
    return embed


class StocksView(discord.ui.View):
    def __init__(self, interaction: Optional[discord.Interaction] = None):
        super().__init__(timeout=60)
        # Remember who opened the stocks view to restrict sensitive actions
        self.invoker_id: Optional[str] = None
        if interaction is not None and interaction.user is not None:
            try:
                self.invoker_id = str(interaction.user.id)
            except Exception:
                self.invoker_id = None
        # Add Buy Stock select (pass interaction so we can resolve display names)
        self.add_item(BuyStockSelect(interaction))
        # Add Sell Stake select (only shows businesses where the viewer owns a stake)
        self.add_item(SellStakeSelect(interaction))

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = _tick_if_needed()
        # Apply to all businesses so current rate matches display logic
        try:
            _apply_stock_to_all_users(float(data.get('current_pct', 50.0)))
        except Exception:
            pass
        embed = _render_stocks_embed(data)
        await interaction.response.edit_message(embed=embed, view=StocksView(interaction))


class BuyStockSelect(discord.ui.Select):
    def __init__(self, interaction: Optional[discord.Interaction] = None):
        options: list[discord.SelectOption] = []
        data = _load_users()
        equity = _load_equity()
        viewer_id: Optional[str] = None
        if interaction is not None and interaction.user is not None:
            try:
                viewer_id = str(interaction.user.id)
            except Exception:
                viewer_id = None
        # Build a list of all businesses across users
        for uid, u in data.items():
            for idx, slot in enumerate(u.get('slots', []) or []):
                if not slot:
                    continue
                # Skip businesses owned by the viewer
                if viewer_id is not None and uid == viewer_id:
                    continue
                try:
                    name = str(slot.get('name', f"Business {idx+1}"))[:95]
                    sv = _sell_value_for_slot(slot)
                    # Resolve owner's display name if possible
                    owner_name = f"User {uid}"
                    if interaction is not None:
                        member = None
                        try:
                            if interaction.guild is not None:
                                member = interaction.guild.get_member(int(uid))
                        except Exception:
                            member = None
                        if member is not None:
                            owner_name = member.display_name
                        else:
                            try:
                                user_obj = interaction.client.get_user(int(uid))  # type: ignore[attr-defined]
                                if user_obj is not None:
                                    owner_name = getattr(user_obj, 'display_name', None) or user_obj.name
                            except Exception:
                                pass
                    # Compute viewer's ownership and total paid (if any)
                    you_part = ""
                    try:
                        if viewer_id is not None:
                            rec = (equity.get(uid) or {}).get(str(idx)) or []
                            my_pct = 0.0
                            my_paid = 0.0
                            for r in rec:
                                if str(r.get('investor_id')) == viewer_id:
                                    try:
                                        my_pct += float(r.get('pct', 0.0))
                                    except Exception:
                                        pass
                                    try:
                                        my_paid += float(r.get('paid', 0.0))
                                    except Exception:
                                        pass
                            if my_pct > 0.0:
                                you_part = f" • You: {my_pct:.2f}% (paid GL${int(my_paid)})"
                    except Exception:
                        you_part = ""
                    # Show owner display name, value and your ownership
                    desc = f"Value: GL${sv}{you_part}"
                    value = f"{uid}:{idx}"
                    options.append(discord.SelectOption(label=name, description=desc[:100], value=value))
                except Exception:
                    continue
        if not options:
            options = [discord.SelectOption(label="No businesses available", description="Create one with /passive", value="-")]
        super().__init__(placeholder="Buy stock — pick a business", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        # Only original stocks viewer can use the select
        try:
            parent_view = self.view  # type: ignore[attr-defined]
            invoker_id = getattr(parent_view, 'invoker_id', None)
        except Exception:
            invoker_id = None
        if invoker_id is not None and str(interaction.user.id) != invoker_id:
            await interaction.response.send_message(
                "> ❌ Only the original viewer can use this action.", ephemeral=True
            )
            return
        # Disallow placeholder
        choice = self.values[0]
        if choice == "-":
            await interaction.response.send_message("> ℹ️ There are no businesses to buy.", ephemeral=True)
            return
        # Parse owner and slot
        try:
            owner_id, idx_str = choice.split(":", 1)
            slot_index = int(idx_str)
        except Exception:
            await interaction.response.send_message("> ❌ Invalid selection.", ephemeral=True)
            return
        users = _load_users()
        owner = users.get(owner_id)
        if not owner or slot_index < 0 or slot_index >= len(owner.get('slots', [])):
            await interaction.response.send_message("> ❌ Business not found.", ephemeral=True)
            return
        slot = owner['slots'][slot_index]
        if not slot:
            await interaction.response.send_message("> ❌ Business not found.", ephemeral=True)
            return
        # Disallow buying your own business
        if owner_id == str(interaction.user.id):
            await interaction.response.send_message("> ❌ You can't buy your own business.", ephemeral=True)
            return
        # Show a detail embed and a purchase button
        name = slot.get('name', f"Business {slot_index+1}")
        desc = slot.get('desc', 'No description.')
        sell_value = _sell_value_for_slot(slot)
        embed = discord.Embed(title="Buy Stock", color=discord.Color.green())
        try:
            o_name, o_avatar = _resolve_name_avatar(interaction, owner_id)
            embed.set_author(name=o_name or "", icon_url=o_avatar)
        except Exception:
            pass
        embed.add_field(name="🏢 Business", value=name, inline=False)
        # Use mention for owner in embed
        embed.add_field(name="👤 Owner", value=f"<@{owner_id}>", inline=True)
        embed.add_field(name="💰 Sell value", value=f"<:greensl:1409394243025502258>{sell_value}", inline=True)
        # Your ownership summary
        try:
            viewer_id = str(interaction.user.id)
            rec = (_load_equity().get(owner_id) or {}).get(str(slot_index)) or []
            my_pct = 0.0
            my_paid = 0.0
            total_staked = 0.0
            for r in rec:
                try:
                    total_staked += float(r.get('pct', 0.0))
                except Exception:
                    pass
                if str(r.get('investor_id')) == viewer_id:
                    try:
                        my_pct += float(r.get('pct', 0.0))
                    except Exception:
                        pass
                    try:
                        my_paid += float(r.get('paid', 0.0))
                    except Exception:
                        pass
            avail = max(0.0, 100.0 - total_staked)
            if my_pct > 0.0:
                embed.add_field(name="Your ownership", value=f"{my_pct:.2f}% (paid <:greensl:1409394243025502258>{int(my_paid)})", inline=False)
            else:
                embed.add_field(name="Your ownership", value="None", inline=False)
            embed.add_field(name="Available to buy", value=f"{avail:.2f}%", inline=True)
        except Exception:
            pass
        if desc:
            embed.add_field(name="About", value=str(desc)[:1024], inline=False)
        # Build a new view with Confirm button to open modal
        # Restrict next actions to the original invoker of the Stocks view
        invoker_id = None
        try:
            parent_view = self.view  # type: ignore[attr-defined]
            invoker_id = getattr(parent_view, 'invoker_id', None)
        except Exception:
            invoker_id = None
        view = BuyStockConfirmView(owner_id, slot_index, sell_value, invoker_id=invoker_id)
        try:
            await interaction.response.edit_message(embed=embed, view=view)
        except Exception:
            try:
                await interaction.followup.send(embed=embed, view=view)
            except Exception:
                pass


class BuyStockConfirmView(discord.ui.View):
    def __init__(self, owner_id: str, slot_index: int, sell_value: int, invoker_id: Optional[str] = None):
        super().__init__(timeout=90)
        self.owner_id = owner_id
        self.slot_index = slot_index
        self.sell_value = sell_value
        self.invoker_id = invoker_id
        self.add_item(discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary))
        self.children[0].callback = self._back  # type: ignore[assignment]
        self.add_item(discord.ui.Button(label="Buy stake", style=discord.ButtonStyle.success))
        self.children[1].callback = self._buy  # type: ignore[assignment]

    async def _back(self, interaction: discord.Interaction):
        # Only original stocks viewer can navigate back
        if self.invoker_id is not None and str(interaction.user.id) != self.invoker_id:
            await interaction.response.send_message(
                "> ❌ Only the original viewer can use this action.", ephemeral=True
            )
            return
        # Return to main stocks view
        data = _tick_if_needed()
        embed = _render_stocks_embed(data)
        await interaction.response.edit_message(embed=embed, view=StocksView(interaction))

    async def _buy(self, interaction: discord.Interaction):
        # Only original stocks viewer can proceed to buy modal
        if self.invoker_id is not None and str(interaction.user.id) != self.invoker_id:
            await interaction.response.send_message(
                "> ❌ Only the original viewer can use this action.", ephemeral=True
            )
            return
        # Open modal to enter percentage
        modal = BuyStakeModal(self.owner_id, self.slot_index, self.sell_value)
        await interaction.response.send_modal(modal)


class BuyStakeModal(discord.ui.Modal, title="Buy stake %"):
    def __init__(self, owner_id: str, slot_index: int, sell_value: int):
        super().__init__()
        self.owner_id = owner_id
        self.slot_index = slot_index
        self.sell_value = sell_value
        self.percent = discord.ui.TextInput(
            label="Percentage to buy (0-100)", placeholder="e.g., 10 for 10%", max_length=6
        )
        self.add_item(self.percent)

    async def on_submit(self, interaction: discord.Interaction):
        buyer_id = str(interaction.user.id)
        try:
            pct = float(str(self.percent.value).strip())
        except Exception:
            await interaction.response.send_message("> ❌ Enter a valid number.", ephemeral=True)
            return
        if pct <= 0 or pct > 100:
            await interaction.response.send_message("> ❌ Percentage must be between 0 and 100.", ephemeral=True)
            return
        # Check total stake limit 100%
        equity = _load_equity()
        rec = (equity.get(self.owner_id) or {}).get(str(self.slot_index)) or []
        current_total = 0.0
        for r in rec:
            try:
                current_total += float(r.get('pct', 0.0))
            except Exception:
                continue
        if current_total + pct > 100.0:
            await interaction.response.send_message("> ❌ Not enough available ownership left for this business.", ephemeral=True)
            return
        # Compute cost and show confirmation
        cost = int(round(self.sell_value * (pct / 100.0)))
        users = _load_users()
        buyer = users.get(buyer_id)
        owner = users.get(self.owner_id)
        if not buyer or not owner:
            await interaction.response.send_message("> ❌ User data not found.", ephemeral=True)
            return
        if int(buyer.get('balance', 0)) < cost:
            await interaction.response.send_message(f"> ❌ Not enough funds. Need <:greensl:1409394243025502258>{cost}", ephemeral=True)
            return
        # Prepare confirmation embed
        try:
            owner_slots = owner.get('slots', []) or []
            slot = owner_slots[self.slot_index] if 0 <= self.slot_index < len(owner_slots) else None
            bname = slot.get('name', f"Business {self.slot_index+1}") if slot else f"Business {self.slot_index+1}"
        except Exception:
            bname = f"Business {self.slot_index+1}"
        embed = discord.Embed(title="Confirm Purchase", color=discord.Color.yellow())
        try:
            o_name, o_avatar = _resolve_name_avatar(interaction, self.owner_id)
            embed.set_author(name=o_name or "", icon_url=o_avatar)
        except Exception:
            pass
        embed.add_field(name="🏢 Business", value=bname, inline=False)
        # Use mention for owner in embed
        embed.add_field(name="👤 Owner", value=f"<@{self.owner_id}>", inline=True)
        embed.add_field(name="📊 Percentage", value=f"{pct:.2f}%", inline=True)
        embed.add_field(name="💵 Cost", value=f"<:greensl:1409394243025502258>{cost}", inline=True)
        embed.set_footer(text="Confirm to complete the purchase or cancel to abort")
        view = PurchaseConfirmView(self.owner_id, self.slot_index, buyer_id, pct, cost)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class PurchaseConfirmView(discord.ui.View):
    def __init__(self, owner_id: str, slot_index: int, buyer_id: str, pct: float, cost: int):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.slot_index = slot_index
        self.buyer_id = buyer_id
        self.pct = float(pct)
        self.cost = int(cost)
        self.add_item(discord.ui.Button(label="Confirm", style=discord.ButtonStyle.success))
        self.children[0].callback = self._confirm  # type: ignore[assignment]
        self.add_item(discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary))
        self.children[1].callback = self._cancel  # type: ignore[assignment]

    async def _confirm(self, interaction: discord.Interaction):
        # Only the original buyer can confirm
        if str(interaction.user.id) != self.buyer_id:
            await interaction.response.send_message("> ❌ This confirmation isn’t for you.", ephemeral=True)
            return
        # Re-validate availability and funds
        equity = _load_equity()
        users = _load_users()
        owner = users.get(self.owner_id)
        buyer = users.get(self.buyer_id)
        if not owner or not buyer:
            await interaction.response.edit_message(content="> ❌ User data not found.", embed=None, view=None)
            return
        # Check stake availability
        rec = (equity.get(self.owner_id) or {}).get(str(self.slot_index)) or []
        current_total = 0.0
        for r in rec:
            try:
                current_total += float(r.get('pct', 0.0))
            except Exception:
                continue
        if current_total + self.pct > 100.0:
            await interaction.response.edit_message(content="> ❌ Not enough ownership left anymore. Try a smaller percentage.", embed=None, view=None)
            return
        # Check funds
        if int(buyer.get('balance', 0)) < self.cost:
            await interaction.response.edit_message(content=f"> ❌ Not enough funds. Need <:greensl:1409394243025502258>{self.cost}", embed=None, view=None)
            return
        # Apply transaction
        buyer['balance'] = int(buyer.get('balance', 0)) - self.cost
        owner['balance'] = int(owner.get('balance', 0)) + self.cost
        # Record equity
        out = equity.get(self.owner_id) or {}
        arr = out.get(str(self.slot_index)) or []
        merged = False
        for r in arr:
            if str(r.get('investor_id')) == self.buyer_id:
                r['pct'] = float(r.get('pct', 0.0)) + float(self.pct)
                try:
                    r['paid'] = float(r.get('paid', 0.0)) + float(self.cost)
                except Exception:
                    r['paid'] = float(self.cost)
                merged = True
                break
        if not merged:
            arr.append({'investor_id': self.buyer_id, 'pct': float(self.pct), 'paid': float(self.cost)})
        out[str(self.slot_index)] = arr
        equity[self.owner_id] = out
        _save_users(users)
        _save_equity(equity)
        # Resolve owner display name for the final confirmation message
        owner_name = f"User {self.owner_id}"
        try:
            member = None
            if interaction.guild is not None:
                member = interaction.guild.get_member(int(self.owner_id))
            if member is not None:
                owner_name = member.display_name
            else:
                user_obj = interaction.client.get_user(int(self.owner_id))  # type: ignore[attr-defined]
                if user_obj is not None:
                    owner_name = getattr(user_obj, 'display_name', None) or user_obj.name
        except Exception:
            pass
        # Resolve business name for confirmation message
        try:
            owner_slots = (owner.get('slots', []) or [])
            slot = owner_slots[self.slot_index] if 0 <= self.slot_index < len(owner_slots) else None
            bname = slot.get('name', f"Business {self.slot_index+1}") if slot else f"Business {self.slot_index+1}"
        except Exception:
            bname = f"Business {self.slot_index+1}"
        await interaction.response.edit_message(
            content=(
                f"> ✅ Purchased {self.pct:.2f}% of {bname} (slot {int(self.slot_index)+1}) for "
                f"<:greensl:1409394243025502258>{self.cost}"
            ),
            embed=None,
            view=None,
        )

    async def _cancel(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.buyer_id:
            await interaction.response.send_message("> ❌ This confirmation isn’t for you.", ephemeral=True)
            return
        await interaction.response.edit_message(content="> ❎ Purchase cancelled.", embed=None, view=None)


class SellStakeSelect(discord.ui.Select):
    def __init__(self, interaction: Optional[discord.Interaction] = None):
        options: list[discord.SelectOption] = []
        data = _load_users()
        equity = _load_equity()
        viewer_id: Optional[str] = None
        if interaction is not None and interaction.user is not None:
            try:
                viewer_id = str(interaction.user.id)
            except Exception:
                viewer_id = None
        if viewer_id is not None:
            for owner_id, slots in equity.items():
                for slot_idx_str, arr in (slots or {}).items():
                    try:
                        slot_index = int(slot_idx_str)
                    except Exception:
                        continue
                    # Sum viewer's ownership in this slot
                    my_pct = 0.0
                    if isinstance(arr, list):
                        for r in arr:
                            if str(r.get('investor_id')) == viewer_id:
                                try:
                                    my_pct += float(r.get('pct', 0.0))
                                except Exception:
                                    pass
                    if my_pct <= 0.0:
                        continue
                    # Validate owner/slot exists
                    owner_user = data.get(owner_id)
                    if not owner_user:
                        continue
                    slots_list = owner_user.get('slots', []) or []
                    if slot_index < 0 or slot_index >= len(slots_list):
                        continue
                    slot = slots_list[slot_index]
                    if not slot:
                        continue
                    # Build option
                    name = str(slot.get('name', f"Business {slot_index+1}"))[:95]
                    sv = _sell_value_for_slot(slot)
                    desc = f"You own {my_pct:.2f}% — Value GL${sv}"
                    value = f"{owner_id}:{slot_index}"
                    options.append(discord.SelectOption(label=name, description=desc[:100], value=value))
        if not options:
            options = [discord.SelectOption(label="No stakes to sell", description="Buy one first via Buy stock", value="-")]
        super().__init__(placeholder="Sell stake — pick your business", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        # Only original stocks viewer can use the select
        try:
            parent_view = self.view  # type: ignore[attr-defined]
            invoker_id = getattr(parent_view, 'invoker_id', None)
        except Exception:
            invoker_id = None
        if invoker_id is not None and str(interaction.user.id) != invoker_id:
            await interaction.response.send_message(
                "> ❌ Only the original viewer can use this action.", ephemeral=True
            )
            return
        choice = self.values[0]
        if choice == "-":
            await interaction.response.send_message("> ℹ️ You don't own any stakes you can sell.", ephemeral=True)
            return
        try:
            owner_id, idx_str = choice.split(":", 1)
            slot_index = int(idx_str)
        except Exception:
            await interaction.response.send_message("> ❌ Invalid selection.", ephemeral=True)
            return
        users = _load_users()
        equity = _load_equity()
        owner = users.get(owner_id)
        if not owner or slot_index < 0 or slot_index >= len(owner.get('slots', [])):
            await interaction.response.send_message("> ❌ Business not found.", ephemeral=True)
            return
        slot = owner['slots'][slot_index]
        if not slot:
            await interaction.response.send_message("> ❌ Business not found.", ephemeral=True)
            return
        # Compute viewer ownership
        viewer_id = str(interaction.user.id)
        rec = (equity.get(owner_id) or {}).get(str(slot_index)) or []
        my_pct = 0.0
        for r in rec:
            if str(r.get('investor_id')) == viewer_id:
                try:
                    my_pct += float(r.get('pct', 0.0))
                except Exception:
                    pass
        if my_pct <= 0.0:
            await interaction.response.send_message("> ❌ You don't own any stake in this business.", ephemeral=True)
            return
        name = slot.get('name', f"Business {slot_index+1}")
        desc = slot.get('desc', 'No description.')
        sell_value = _sell_value_for_slot(slot)
        embed = discord.Embed(title="Sell Stake", color=discord.Color.red())
        try:
            o_name, o_avatar = _resolve_name_avatar(interaction, owner_id)
            embed.set_author(name=o_name or "", icon_url=o_avatar)
        except Exception:
            pass
        embed.add_field(name="🏢 Business", value=name, inline=False)
        # Use mention for owner in embed
        embed.add_field(name="👤 Owner", value=f"<@{owner_id}>", inline=True)
        embed.add_field(name="💰 Sell value", value=f"<:greensl:1409394243025502258>{sell_value}", inline=True)
        embed.add_field(name="Your ownership", value=f"{my_pct:.2f}%", inline=False)
        if desc:
            embed.add_field(name="About", value=str(desc)[:1024], inline=False)
        # Restrict next actions to the original invoker of the Stocks view
        invoker_id = None
        try:
            parent_view = self.view  # type: ignore[attr-defined]
            invoker_id = getattr(parent_view, 'invoker_id', None)
        except Exception:
            invoker_id = None
        view = SellStakeConfirmView(owner_id, slot_index, sell_value, my_pct, invoker_id=invoker_id)
        try:
            await interaction.response.edit_message(embed=embed, view=view)
        except Exception:
            try:
                await interaction.followup.send(embed=embed, view=view)
            except Exception:
                pass


class SellStakeConfirmView(discord.ui.View):
    def __init__(self, owner_id: str, slot_index: int, sell_value: int, owned_pct: float, invoker_id: Optional[str] = None):
        super().__init__(timeout=90)
        self.owner_id = owner_id
        self.slot_index = slot_index
        self.sell_value = sell_value
        self.owned_pct = float(owned_pct)
        self.invoker_id = invoker_id
        self.add_item(discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary))
        self.children[0].callback = self._back  # type: ignore[assignment]
        self.add_item(discord.ui.Button(label="Sell stake", style=discord.ButtonStyle.danger))
        self.children[1].callback = self._sell  # type: ignore[assignment]

    async def _back(self, interaction: discord.Interaction):
        # Only original stocks viewer can navigate back
        if self.invoker_id is not None and str(interaction.user.id) != self.invoker_id:
            await interaction.response.send_message(
                "> ❌ Only the original viewer can use this action.", ephemeral=True
            )
            return
        data = _tick_if_needed()
        embed = _render_stocks_embed(data)
        await interaction.response.edit_message(embed=embed, view=StocksView(interaction))

    async def _sell(self, interaction: discord.Interaction):
        # Only original stocks viewer can proceed to sell modal
        if self.invoker_id is not None and str(interaction.user.id) != self.invoker_id:
            await interaction.response.send_message(
                "> ❌ Only the original viewer can use this action.", ephemeral=True
            )
            return
        modal = SellStakeModal(self.owner_id, self.slot_index, self.sell_value, self.owned_pct)
        await interaction.response.send_modal(modal)


class SellStakeModal(discord.ui.Modal, title="Sell stake %"):
    def __init__(self, owner_id: str, slot_index: int, sell_value: int, owned_pct: float):
        super().__init__()
        self.owner_id = owner_id
        self.slot_index = slot_index
        self.sell_value = sell_value
        self.owned_pct = float(owned_pct)
        self.percent = discord.ui.TextInput(
            label="Percentage to sell (0-100)", placeholder="e.g., 5 for 5%", max_length=6
        )
        self.add_item(self.percent)

    async def on_submit(self, interaction: discord.Interaction):
        seller_id = str(interaction.user.id)
        try:
            pct = float(str(self.percent.value).strip())
        except Exception:
            await interaction.response.send_message("> ❌ Enter a valid number.", ephemeral=True)
            return
        if pct <= 0 or pct > 100:
            await interaction.response.send_message("> ❌ Percentage must be between 0 and 100.", ephemeral=True)
            return
        if pct - self.owned_pct > 1e-6:
            await interaction.response.send_message("> ❌ You can't sell more than you own.", ephemeral=True)
            return
        payout = int(round(self.sell_value * (pct / 100.0)))
        users = _load_users()
        seller = users.get(seller_id)
        owner = users.get(self.owner_id)
        if not seller or not owner:
            await interaction.response.send_message("> ❌ User data not found.", ephemeral=True)
            return
        if int(owner.get('balance', 0)) < payout:
            await interaction.response.send_message(
                f"> ❌ Owner doesn't have enough funds to buy back your stake. Needs <:greensl:1409394243025502258>{payout}",
                ephemeral=True,
            )
            return
        # Confirmation embed
        try:
            owner_slots = (owner.get('slots', []) or [])
            slot = owner_slots[self.slot_index] if 0 <= self.slot_index < len(owner_slots) else None
            bname = slot.get('name', f"Business {self.slot_index+1}") if slot else f"Business {self.slot_index+1}"
        except Exception:
            bname = f"Business {self.slot_index+1}"
        embed = discord.Embed(title="Confirm Sale", color=discord.Color.orange())
        try:
            o_name, o_avatar = _resolve_name_avatar(interaction, self.owner_id)
            embed.set_author(name=o_name or "", icon_url=o_avatar)
        except Exception:
            pass
        embed.add_field(name="🏢 Business", value=bname, inline=False)
        embed.add_field(name="👤 Owner", value=f"<@{self.owner_id}>", inline=True)
        embed.add_field(name="📊 Percentage", value=f"{pct:.2f}%", inline=True)
        embed.add_field(name="💵 Payout", value=f"<:greensl:1409394243025502258>{payout}", inline=True)
        embed.set_footer(text="Confirm to complete the sale or cancel to abort")
        view = SellConfirmView(self.owner_id, self.slot_index, seller_id, pct, payout)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class SellConfirmView(discord.ui.View):
    def __init__(self, owner_id: str, slot_index: int, seller_id: str, pct: float, payout: int):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.slot_index = slot_index
        self.seller_id = seller_id
        self.pct = float(pct)
        self.payout = int(payout)
        self.add_item(discord.ui.Button(label="Confirm", style=discord.ButtonStyle.success))
        self.children[0].callback = self._confirm  # type: ignore[assignment]
        self.add_item(discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary))
        self.children[1].callback = self._cancel  # type: ignore[assignment]

    async def _confirm(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.seller_id:
            await interaction.response.send_message("> ❌ This confirmation isn’t for you.", ephemeral=True)
            return
        equity = _load_equity()
        users = _load_users()
        owner = users.get(self.owner_id)
        seller = users.get(self.seller_id)
        if not owner or not seller:
            await interaction.response.edit_message(content="> ❌ User data not found.", embed=None, view=None)
            return
        # Validate current ownership and funds
        rec = (equity.get(self.owner_id) or {}).get(str(self.slot_index)) or []
        my_pct = 0.0
        target_idx = None
        for i, r in enumerate(rec):
            if str(r.get('investor_id')) == self.seller_id:
                try:
                    my_pct += float(r.get('pct', 0.0))
                    target_idx = i
                except Exception:
                    pass
        if my_pct < self.pct - 1e-6 or target_idx is None:
            await interaction.response.edit_message(content="> ❌ You no longer own that much to sell.", embed=None, view=None)
            return
        if int(owner.get('balance', 0)) < self.payout:
            await interaction.response.edit_message(
                content=(
                    f"> ❌ Owner doesn't have enough funds anymore. Needs <:greensl:1409394243025502258>{self.payout}"
                ),
                embed=None,
                view=None,
            )
            return
        # Apply transaction
        owner['balance'] = int(owner.get('balance', 0)) - self.payout
        seller['balance'] = int(seller.get('balance', 0)) + self.payout
        # Reduce equity
        r = rec[target_idx]
        new_pct = float(r.get('pct', 0.0)) - float(self.pct)
        if new_pct <= 1e-6:
            # remove record
            rec.pop(target_idx)
        else:
            r['pct'] = new_pct
        # save back
        out = equity.get(self.owner_id) or {}
        out[str(self.slot_index)] = rec
        equity[self.owner_id] = out
        _save_users(users)
        _save_equity(equity)
        # Resolve owner display name for the final message (non-embed)
        owner_name = f"User {self.owner_id}"
        try:
            member = None
            if interaction.guild is not None:
                member = interaction.guild.get_member(int(self.owner_id))
            if member is not None:
                owner_name = member.display_name
            else:
                user_obj = interaction.client.get_user(int(self.owner_id))  # type: ignore[attr-defined]
                if user_obj is not None:
                    owner_name = getattr(user_obj, 'display_name', None) or user_obj.name
        except Exception:
            pass
        # Resolve business name for confirmation message
        try:
            owner_slots = (owner.get('slots', []) or [])
            slot = owner_slots[self.slot_index] if 0 <= self.slot_index < len(owner_slots) else None
            bname = slot.get('name', f"Business {self.slot_index+1}") if slot else f"Business {self.slot_index+1}"
        except Exception:
            bname = f"Business {self.slot_index+1}"
        await interaction.response.edit_message(
            content=(
                f"> ✅ Sold **{self.pct:.2f}%** of **{bname}** for **<:greensl:1409394243025502258>{self.payout}**"
            ),
            embed=None,
            view=None,
        )

    async def _cancel(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.seller_id:
            await interaction.response.send_message("> ❌ This confirmation isn’t for you.", ephemeral=True)
            return
        await interaction.response.edit_message(content="> ❎ Sale cancelled.", embed=None, view=None)


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
            await interaction.response.send_message(embed=embed, view=StocksView(interaction))
