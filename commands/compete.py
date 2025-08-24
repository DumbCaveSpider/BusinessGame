import json
import os
import time
import random
from typing import Dict, Any, Optional

import discord
from discord import app_commands

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
USER_FILE = os.path.join(DATA_DIR, 'users.json')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

try:
    from google import genai  # type: ignore
except Exception:  # pragma: no cover
    genai = None  # type: ignore


# -------------------- Data helpers --------------------

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


def _has_any_business(user: Dict[str, Any]) -> bool:
    return any(bool(s) for s in user.get('slots', []))


def _now() -> int:
    return int(time.time())


# -------------------- Gemini helpers --------------------

_GENAI_CLIENT: Optional[object] = None

# Track ongoing battles by user id -> discord.Message
_ONGOING_BATTLES: dict[str, discord.Message] = {}


def _get_genai_client():
    global _GENAI_CLIENT
    if _GENAI_CLIENT is not None:
        return _GENAI_CLIENT
    if not GEMINI_API_KEY or genai is None:
        return None
    try:
        _GENAI_CLIENT = genai.Client(api_key=GEMINI_API_KEY)  # type: ignore[attr-defined]
    except Exception as e:  # pragma: no cover
        print(f"[Compete] Failed to init google-genai client: {type(e).__name__}: {e}")
        _GENAI_CLIENT = None
    return _GENAI_CLIENT


async def _gemini_generate(prompt: str) -> str:
    if not GEMINI_API_KEY:
        return ''
    if genai is None:
        return ''
    client = _get_genai_client()
    if client is None:
        return ''

    model_name = "gemini-2.5-flash"

    def _call_sync() -> str:
        try:
            resp = client.models.generate_content(model=model_name, contents=prompt)  # type: ignore[attr-defined]
            text = getattr(resp, 'text', None)
            if text:
                return text
            try:
                return json.dumps(resp.to_dict())  # type: ignore[attr-defined]
            except Exception:
                return ''
        except Exception as e:
            print(f"[Compete] generate_content error: {type(e).__name__}: {e}")
            return ''

    import asyncio
    return await asyncio.to_thread(_call_sync)


# -------------------- UI Components --------------------

class PlayerSelect(discord.ui.Select):
    def __init__(self, owner_id: int, display_name: str, user_data: Dict[str, Any]):
        self.owner_id = str(owner_id)
        options: list[discord.SelectOption] = []
        for idx, slot in enumerate(user_data.get('slots', [])):
            if not slot:
                continue
            name = slot.get('name', f"Slot {idx + 1}")
            inc = int(slot.get('income_per_day', 0))
            rate = float(slot.get('rating', 0))
            options.append(discord.SelectOption(label=name, description=f"💵 ${inc}/day • ⭐ {rate}", value=str(idx)))
        disabled = False
        if not options:
            options = [
                discord.SelectOption(label="No businesses", description="You have nothing to select", value="-1", default=True)
            ]
            disabled = True
        possessive = f"{display_name}'s" if not display_name.endswith('s') else f"{display_name}'"
        super().__init__(
            placeholder=f"{possessive} business",
            options=options,
            min_values=1,
            max_values=1,
            disabled=disabled,
        )

    async def callback(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.owner_id:
            await interaction.response.send_message("Only this player's owner can select.", ephemeral=True)
            return
        if self.values[0] == "-1":
            await interaction.response.send_message("You have no businesses to select.", ephemeral=True)
            return
        view: BattleView = self.view  # type: ignore[assignment]
        idx = int(self.values[0])
        if str(interaction.user.id) == view.a_id:
            view.a_choice = idx
            try:
                slot = view.a_data['slots'][idx]
                view.a_rating = float(slot.get('rating', 1.0) or 1.0)
            except Exception:
                view.a_rating = 1.0
        elif str(interaction.user.id) == view.b_id:
            view.b_choice = idx
            try:
                slot = view.b_data['slots'][idx]
                view.b_rating = float(slot.get('rating', 1.0) or 1.0)
            except Exception:
                view.b_rating = 1.0
        self.disabled = True
        view.update_controls()
        await interaction.response.edit_message(view=view, embed=view.render_embed())


class ArgumentModal(discord.ui.Modal, title="Make Your Case"):
    def __init__(self, owner_id: str):
        super().__init__()
        self.owner_id = owner_id
        self.argument = discord.ui.TextInput(
            label="Why is your business better?",
            style=discord.TextStyle.paragraph,
            placeholder="Convince the customers...",
            max_length=500,
            required=True,
        )
        self.add_item(self.argument)

    async def on_submit(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.owner_id:
            await interaction.response.send_message("> ❌ You not part of this battle.", ephemeral=True)
            return
        view: BattleView = self.parent_view  # type: ignore[attr-defined]
        text = str(self.argument.value or "").strip()
        if not text:
            await interaction.response.send_message("> ❌ Please provide an argument.", ephemeral=True)
            return
        if str(interaction.user.id) == view.a_id:
            view.a_argument = text
        else:
            view.b_argument = text
        try:
            await interaction.response.send_message(
                "> ✅ Argument received!", ephemeral=True
            )
        except Exception:
            pass
        # If only one argument has been submitted so far, update the main embed
        # to show the current round's arguments while waiting for the other player.
        if (view.a_argument and not view.b_argument) or (view.b_argument and not view.a_argument):
            try:
                if view.message is not None:
                    await view.message.edit(embed=view.render_embed(), view=view)
            except Exception:
                pass
        if view.a_argument and view.b_argument:
            await view.judge(interaction)


class BattleView(discord.ui.View):
    def __init__(self, a_id: int, b_id: int, a_data: Dict[str, Any], b_data: Dict[str, Any], a_name: str, b_name: str):
        super().__init__(timeout=300)
        self.a_id = str(a_id)
        self.b_id = str(b_id)
        self.a_data = a_data
        self.b_data = b_data
        self.a_name = a_name
        self.b_name = b_name

        self.a_mention = f"<@{self.a_id}>"
        self.b_mention = f"<@{self.b_id}>"
        self.a_choice: Optional[int] = None
        self.b_choice: Optional[int] = None
        self.a_argument: Optional[str] = None
        self.b_argument: Optional[str] = None
        self.judging: bool = False
        self.battle_over: bool = False
        self.a_rating: float = 1.0
        self.b_rating: float = 1.0
        self.message: Optional[discord.Message] = None
        self.started: bool = False
        self.round: int = 1
        # Keep last round arguments to display as placeholders until replaced
        self.prev_a_argument: Optional[str] = None
        self.prev_b_argument: Optional[str] = None

        # Store selects so they can be removed when the battle starts
        self.a_select: Optional[PlayerSelect] = PlayerSelect(a_id, self.a_name, a_data)
        self.b_select: Optional[PlayerSelect] = PlayerSelect(b_id, self.b_name, b_data)
        self.add_item(self.a_select)
        self.add_item(self.b_select)

        # Buttons wired via callbacks
        self.start_button = discord.ui.Button(label="Start", style=discord.ButtonStyle.success)
        self.submit_button = discord.ui.Button(label="Submit Argument", style=discord.ButtonStyle.primary)
        self.forfeit_button = discord.ui.Button(label="Forfeit", style=discord.ButtonStyle.danger)
        self.add_item(self.start_button)
        self.add_item(self.submit_button)
        self.add_item(self.forfeit_button)

        async def _start_cb(interaction: discord.Interaction):
            await self._start_pressed(interaction)

        async def _submit_cb(interaction: discord.Interaction):
            await self._submit_pressed(interaction)

        async def _forfeit_cb(interaction: discord.Interaction):
            await self._forfeit_pressed(interaction)

        self.start_button.callback = _start_cb  # type: ignore[assignment]
        self.submit_button.callback = _submit_cb  # type: ignore[assignment]
        self.forfeit_button.callback = _forfeit_cb  # type: ignore[assignment]

        self.update_controls()

    # ---- Battle scaling helpers ----
    def current_multiplier(self) -> float:
        """Returns the rating change multiplier that doubles every 5 rounds.
        Rounds 1-4 -> 1x, 5-9 -> 2x, 10-14 -> 4x, etc.
        """
        r = max(1, int(self.round))
        return float(2 ** ((r - 1) // 5))

    def current_delta(self) -> float:
        """Base delta is 0.1, scaled by current multiplier."""
        base = 0.1
        return round(base * self.current_multiplier(), 2)

    @staticmethod
    def _fmt_num(x: float) -> str:
        s = f"{x:.2f}"
        s = s.rstrip('0').rstrip('.')
        return s

    def update_controls(self) -> None:
        both_selected = (self.a_choice is not None and self.b_choice is not None)
        # Enable/disable buttons
        self.start_button.disabled = not (both_selected and not self.started and not self.battle_over)
        self.submit_button.disabled = not (self.started and both_selected and not self.battle_over)
        self.forfeit_button.disabled = not (self.started and both_selected and not self.battle_over)

    def render_embed(self) -> discord.Embed:
        # Title shows current round while battle is running
        title = "Business Battle"
        if self.started and not self.battle_over:
            title = f"Business Battle — Round {self.round}"
        embed = discord.Embed(title=title, color=discord.Color.purple())
        # Always show players
        embed.add_field(name="👥 Players", value=f"{self.a_mention} vs {self.b_mention}", inline=False)
        if self.a_choice is None or self.b_choice is None:
            a_sel = "Not selected" if self.a_choice is None else self.a_data['slots'][self.a_choice].get('name', f"Slot {self.a_choice+1}")
            b_sel = "Not selected" if self.b_choice is None else self.b_data['slots'][self.b_choice].get('name', f"Slot {self.b_choice+1}")
            embed.description = "### 🏢 Select businesses to begin."
            embed.add_field(name=self.a_name, value=f"**Selected:** {a_sel}", inline=True)
            embed.add_field(name=self.b_name, value=f"**Selected:** {b_sel}", inline=True)
        else:
            a_slot = self.a_data['slots'][self.a_choice]
            b_slot = self.b_data['slots'][self.b_choice]
            a_name = a_slot.get('name', f"Slot {self.a_choice+1}")
            b_name = b_slot.get('name', f"Slot {self.b_choice+1}")
            # Use base rate from base_income_per_day if available; fall back to income_per_day
            a_rate = int(a_slot.get('base_income_per_day', a_slot.get('income_per_day', 0)))
            b_rate = int(b_slot.get('base_income_per_day', b_slot.get('income_per_day', 0)))
            a_eff = int(round(a_rate * self.a_rating))
            b_eff = int(round(b_rate * self.b_rating))
            a_diff = a_eff - a_rate
            b_diff = b_eff - b_rate
            a_diff_str = f"+${abs(a_diff)}" if a_diff > 0 else (f"-${abs(a_diff)}" if a_diff < 0 else "$0")
            b_diff_str = f"+${abs(b_diff)}" if b_diff > 0 else (f"-${abs(b_diff)}" if b_diff < 0 else "$0")
            embed.add_field(
                name=self.a_name,
                value=(
                    f"🏢 Business: {a_name}\n"
                    f"📈 Rate: ${a_rate}/day • Base: ${a_eff} ({a_diff_str})"
                ),
                inline=True,
            )
            embed.add_field(
                name=self.b_name,
                value=(
                    f"🏢 Business: {b_name}\n"
                    f"📈 Rate: ${b_rate}/day • Base: ${b_eff} ({b_diff_str})"
                ),
                inline=True,
            )
            # Separate field for ratings
            embed.add_field(
                name="⭐ Ratings",
                value=(
                    f"{self.a_name}: {self.a_rating:.1f}\n"
                    f"{self.b_name}: {self.b_rating:.1f}"
                ),
                inline=False,
            )
            if self.battle_over:
                if self.a_rating > self.b_rating:
                    embed.description = f"### 🏳️ Battle over! {self.a_mention} wins."
                elif self.b_rating > self.a_rating:
                    embed.description = f"### 🏳️ Battle over! {self.b_mention} wins."
                else:
                    embed.description = "### 🏳️ Battle over! It's a tie!"
            else:
                if not self.started:
                    embed.description = "### ✅ Both players' business selected. Press 'Start' to begin the battle."
                else:
                    desc = "### ⚔️ Battle started! Submit arguments each round. First to drop to 0.5 rating loses. Rating drops double every 5 rounds"
                    # Show previous round on top, but hide a player's previous argument once they submit a new one
                    prev_lines: list[str] = []
                    if self.prev_a_argument is not None and self.a_argument is None:
                        prev_lines.append(f"\n> {self.a_mention}: {self.prev_a_argument}")
                    if self.prev_b_argument is not None and self.b_argument is None:
                        prev_lines.append(f"\n> {self.b_mention}: {self.prev_b_argument}")
                    if prev_lines:
                        desc += "\n\n### 🗳️ Previous round:" + "".join(prev_lines)

                    # Always show current round section
                    desc += "\n\n### 🗳️ Current round:"
                    a_curr = self.a_argument if self.a_argument is not None else "⌛"
                    b_curr = self.b_argument if self.b_argument is not None else "⌛"
                    desc += f"\n> {self.a_mention}: {a_curr}"
                    desc += f"\n> {self.b_mention}: {b_curr}"
                    embed.description = desc
        # Always show current multiplier info
        mult = self.current_multiplier()
        delta = self.current_delta()
        embed.add_field(
            name="📈 Current Multiplier",
            value=f"{self._fmt_num(mult)}x (±{self._fmt_num(delta)} rating/round)",
            inline=False,
        )
        return embed

    async def _start_pressed(self, interaction: discord.Interaction):
        if str(interaction.user.id) not in (self.a_id, self.b_id):
            await interaction.response.send_message("You're not part of this battle.", ephemeral=True)
            return
        if self.battle_over:
            await interaction.response.send_message("This battle is already over.", ephemeral=True)
            return
        if self.started:
            await interaction.response.send_message("The battle has already started.", ephemeral=True)
            return
        if self.a_choice is None or self.b_choice is None:
            await interaction.response.send_message("Both players must select a business first.", ephemeral=True)
            return
        self.started = True
        # Remove the select menus once the battle starts
        try:
            if self.a_select is not None:
                self.remove_item(self.a_select)
                self.a_select = None
        except Exception:
            pass
        try:
            if self.b_select is not None:
                self.remove_item(self.b_select)
                self.b_select = None
        except Exception:
            pass
        self.update_controls()
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    async def _submit_pressed(self, interaction: discord.Interaction):
        if str(interaction.user.id) not in (self.a_id, self.b_id):
            await interaction.response.send_message("You're not part of this battle.", ephemeral=True)
            return
        if self.a_choice is None or self.b_choice is None:
            await interaction.response.send_message("Wait until both businesses are selected.", ephemeral=True)
            return
        if not self.started:
            await interaction.response.send_message("Press Start to begin the battle first.", ephemeral=True)
            return
        if (str(interaction.user.id) == self.a_id and self.a_argument) or (str(interaction.user.id) == self.b_id and self.b_argument):
            await interaction.response.send_message("You've already submitted your argument this round.", ephemeral=True)
            return
        modal = ArgumentModal(str(interaction.user.id))
        setattr(modal, 'parent_view', self)
        await interaction.response.send_modal(modal)

    async def _forfeit_pressed(self, interaction: discord.Interaction):
        if str(interaction.user.id) not in (self.a_id, self.b_id):
            await interaction.response.send_message("You're not part of this battle.", ephemeral=True)
            return
        if self.battle_over:
            await interaction.response.send_message("This battle is already over.", ephemeral=True)
            return
        if not self.started:
            await interaction.response.send_message("You can only forfeit an active battle.", ephemeral=True)
            return
        # Determine winner as the opponent
        winner_char = 'B' if str(interaction.user.id) == self.a_id else 'A'
        await self._finalize_battle(interaction, winner_char, forfeited=True)

    async def _finalize_battle(self, interaction: discord.Interaction, winner_char: str, forfeited: bool = False):
        # Build a result summary based on current ratings, and persist outcome
        a_user_mention = f"<@{self.a_id}>"
        b_user_mention = f"<@{self.b_id}>"
        a_slot = self.a_data['slots'][self.a_choice]  # type: ignore[index]
        b_slot = self.b_data['slots'][self.b_choice]  # type: ignore[index]
        nameA = a_slot.get('name', 'Business A')
        nameB = b_slot.get('name', 'Business B')

        # Mark battle over and disable controls
        self.battle_over = True
        self.start_button.disabled = True
        self.submit_button.disabled = True
        self.forfeit_button.disabled = True

        # Persist outcome to storage (apply new income and W/L)
        applied_info = _apply_battle_outcome(
            a_id=self.a_id,
            b_id=self.b_id,
            a_choice=self.a_choice or 0,
            b_choice=self.b_choice or 0,
            a_rating=self.a_rating,
            b_rating=self.b_rating,
            winner_char=winner_char,
        )

        # Create result text summarizing new incomes
        if applied_info:
            a_before, a_after, b_before, b_after = applied_info
        else:
            a_before = a_after = b_before = b_after = None

        if winner_char == 'A':
            base_line = f"🏳️ {a_user_mention} wins{' by forfeit' if forfeited else ''}."
        else:
            base_line = f"🏳️ {b_user_mention} wins{' by forfeit' if forfeited else ''}."

        changes = []
        if a_before is not None and a_after is not None:
            changes.append(f"**{nameA}:** ${a_before}/day → ${a_after}/day")
        if b_before is not None and b_after is not None:
            changes.append(f"**{nameB}:** ${b_before}/day → ${b_after}/day")
        result = base_line + ("\n" + "\n".join(changes) if changes else "")

        embed = self.render_embed()
        embed.add_field(name="Result", value=result, inline=False)
        # Edit the original message; do not create a new one
        try:
            if self.message is None:
                try:
                    self.message = await interaction.original_response()
                except Exception:
                    self.message = None
            if self.message is not None:
                await self.message.edit(embed=embed, view=self)
        except Exception:
            pass
        try:
            _ONGOING_BATTLES.pop(self.a_id, None)
            _ONGOING_BATTLES.pop(self.b_id, None)
        except Exception:
            pass

    async def judge(self, interaction: discord.Interaction):
        if self.judging or self.battle_over:
            return
        self.judging = True
        try:
            data = _load_users()
            a_user = data.get(self.a_id)
            b_user = data.get(self.b_id)
            if not a_user or not b_user:
                try:
                    await interaction.followup.send("User data missing; battle cancelled.", ephemeral=True)
                except Exception:
                    pass
                return
            a_slot = a_user['slots'][self.a_choice]  # type: ignore[index]
            b_slot = b_user['slots'][self.b_choice]  # type: ignore[index]

            nameA = a_slot.get('name', 'Business A')
            nameB = b_slot.get('name', 'Business B')
            rateA = int(a_slot.get('base_income_per_day', a_slot.get('income_per_day', 0)))
            rateB = int(b_slot.get('base_income_per_day', b_slot.get('income_per_day', 0)))
            argA = self.a_argument or ''
            argB = self.b_argument or ''
            mentionA = f"<@{self.a_id}>"
            mentionB = f"<@{self.b_id}>"

            prompt = (
                "Two players present marketing arguments for their businesses. "
                "Choose the stronger argument considering clarity, persuasiveness, and alignment with a plausible business. "
                "Respond with ONLY 'A' or 'B' to indicate the winner.\n\n"
                f"Business A: {nameA} (${rateA}/day)\nArgument A: {argA}\n\n"
                f"Business B: {nameB} (${rateB}/day)\nArgument B: {argB}\n\n"
                "Output: A or B"
            )
            text = await _gemini_generate(prompt)
            winner_char = 'A'
            picked = False
            if text:
                t = text.strip().upper()
                if 'B' == t or t.startswith('B'):
                    winner_char = 'B'
                    picked = True
                elif 'A' == t or t.startswith('A'):
                    winner_char = 'A'
                    picked = True
            if not picked:
                lenA = len((argA or '').split())
                lenB = len((argB or '').split())
                if lenA != lenB:
                    winner_char = 'A' if lenA > lenB else 'B'
                else:
                    winner_char = random.choice(['A', 'B'])

            # Persist last round arguments so they display at the start of the next round
            self.prev_a_argument = argA
            self.prev_b_argument = argB

            # Apply rating changes only; base income remains unchanged during the battle
            delta = self.current_delta()
            delta_str = self._fmt_num(delta)
            if winner_char == 'A':
                prev_a = self.a_rating
                prev_b = self.b_rating
                self.a_rating = round(self.a_rating + delta, 2)
                self.b_rating = round(max(0.0, self.b_rating - delta), 2)
                result = (
                    f"📈 Winner: {mentionA} (+{delta_str} rating) • 📉 Loser: {mentionB} (-{delta_str} rating)\n"
                    f"**{nameA}:** ⭐ Rating {prev_a:.1f} → {self.a_rating:.1f}\n"
                    f"**{nameB}:** ⭐ Rating {prev_b:.1f} → {self.b_rating:.1f}"
                )
            else:
                prev_a = self.a_rating
                prev_b = self.b_rating
                self.b_rating = round(self.b_rating + delta, 2)
                self.a_rating = round(max(0.0, self.a_rating - delta), 2)
                result = (
                    f"📈 Winner: {mentionB} (+{delta_str} rating) • 📉 Loser: {mentionA} (-{delta_str} rating)\n"
                    f"**{nameB}:** ⭐ Rating {prev_b:.1f} → {self.b_rating:.1f}\n"
                    f"**{nameA}:** ⭐ Rating {prev_a:.1f} → {self.a_rating:.1f}"
                )

            # No base income changes are persisted during battle rounds; only the round state (ratings) changes in-memory
            if self.a_rating <= 0.5 or self.b_rating <= 0.5:
                # Determine winner by higher rating
                winner_char = 'A' if self.a_rating > self.b_rating else ('B' if self.b_rating > self.a_rating else random.choice(['A','B']))
                await self._finalize_battle(interaction, winner_char)
            else:
                self.a_argument = None
                self.b_argument = None
                # Advance to next round
                self.round += 1
                self.update_controls()
            if not self.battle_over:
                # Post-round result update in the same message without ending the battle
                embed = self.render_embed()
                embed.add_field(name="Result", value=result, inline=False)
                try:
                    if self.message is None:
                        try:
                            self.message = await interaction.original_response()
                        except Exception:
                            self.message = None
                    if self.message is not None:
                        await self.message.edit(embed=embed, view=self)
                except Exception:
                    pass
        finally:
            self.judging = False

    async def on_timeout(self) -> None:
        if self.battle_over:
            return
        self.battle_over = True
        self.start_button.disabled = True
        self.submit_button.disabled = True
        self.forfeit_button.disabled = True
        # Note: keep last known preview in embed, add timeout result; edit existing message only
        try:
            if self.message is not None:
                embed = self.render_embed()
                embed.add_field(name="Result", value="⏰ Battle timed out.", inline=False)
                await self.message.edit(embed=embed, view=self)
        except Exception:
            pass
        try:
            _ONGOING_BATTLES.pop(self.a_id, None)
            _ONGOING_BATTLES.pop(self.b_id, None)
        except Exception:
            pass


# -------------------- Persistence helpers --------------------

def _apply_battle_outcome(
    *,
    a_id: str,
    b_id: str,
    a_choice: int,
    b_choice: int,
    a_rating: float,
    b_rating: float,
    winner_char: str,
) -> Optional[tuple[int, int, int, int]]:
    """Persist new incomes and wins/losses for both selected businesses.
    Returns a tuple (a_before, a_after, b_before, b_after) or None on failure.
    """
    try:
        data = _load_users()
        a_user = data.get(a_id)
        b_user = data.get(b_id)
        if not a_user or not b_user:
            return None
        a_slot = a_user['slots'][a_choice]
        b_slot = b_user['slots'][b_choice]
        if not a_slot or not b_slot:
            return None

        # Determine fixed base from score total (preferred) or existing fields; and always store it
        a_base = int(a_slot.get('scores', {}).get('total', a_slot.get('base_income_per_day', a_slot.get('income_per_day', 0))))
        b_base = int(b_slot.get('scores', {}).get('total', b_slot.get('base_income_per_day', b_slot.get('income_per_day', 0))))
        a_slot['base_income_per_day'] = a_base
        b_slot['base_income_per_day'] = b_base
        a_slot['wins'] = int(a_slot.get('wins', 0))
        a_slot['losses'] = int(a_slot.get('losses', 0))
        b_slot['wins'] = int(b_slot.get('wins', 0))
        b_slot['losses'] = int(b_slot.get('losses', 0))

        # Apply new incomes based on final ratings
        a_after = max(0, int(round(a_base * a_rating)))
        b_after = max(0, int(round(b_base * b_rating)))
        a_before = int(a_slot.get('income_per_day', a_after))
        b_before = int(b_slot.get('income_per_day', b_after))
        a_slot['income_per_day'] = a_after
        b_slot['income_per_day'] = b_after
        # Update W/L
        if winner_char == 'A':
            a_slot['wins'] = int(a_slot.get('wins', 0)) + 1
            b_slot['losses'] = int(b_slot.get('losses', 0)) + 1
        elif winner_char == 'B':
            b_slot['wins'] = int(b_slot.get('wins', 0)) + 1
            a_slot['losses'] = int(a_slot.get('losses', 0)) + 1
        # Persist final ratings to passive businesses
        a_slot['rating'] = float(max(0.0, a_rating))
        b_slot['rating'] = float(max(0.0, b_rating))
        _save_users(data)
        return a_before, a_after, b_before, b_after
    except Exception:
        return None


# -------------------- Command --------------------

class CompeteCommand:
    @staticmethod
    async def setup(tree: app_commands.CommandTree):
        @tree.command(name="compete", description="Challenge another user to a business battle")
        @app_commands.describe(opponent="User to compete against")
        @app_commands.allowed_contexts(dms=True, guilds=True, private_channels=True)
        async def compete(interaction: discord.Interaction, opponent: discord.User):
            a_id = str(interaction.user.id)
            b_id = str(opponent.id)
            if a_id == b_id:
                await interaction.response.send_message("You cannot compete against yourself.", ephemeral=True)
                return
            # Prevent duplicate battles by either participant
            existing = _ONGOING_BATTLES.get(a_id)
            if existing is not None:
                try:
                    await interaction.response.send_message(
                        f"You're already in an ongoing battle. Jump to it: {existing.jump_url}", ephemeral=True
                    )
                except Exception:
                    await interaction.response.send_message("You're already in an ongoing battle.", ephemeral=True)
                return
            opp_existing = _ONGOING_BATTLES.get(b_id)
            if opp_existing is not None:
                try:
                    await interaction.response.send_message(
                        f"That opponent is already in a battle. See it here: {opp_existing.jump_url}", ephemeral=True
                    )
                except Exception:
                    await interaction.response.send_message("That opponent is already in a battle.", ephemeral=True)
                return
            data = _load_users()
            a_data = data.get(a_id)
            b_data = data.get(b_id)
            if not a_data or not _has_any_business(a_data):
                await interaction.response.send_message("You need at least one business to compete. Use '/passive' to create one.", ephemeral=True)
                return
            if not b_data or not _has_any_business(b_data):
                await interaction.response.send_message("The opponent has no businesses yet.", ephemeral=True)
                return

            title = f"Business Battle"
            embed = discord.Embed(title=title, description="### 🏢 Select businesses to begin.", color=discord.Color.purple())
            view = BattleView(int(a_id), int(b_id), a_data, b_data, interaction.user.display_name, opponent.display_name)
            await interaction.response.send_message(content=f"{interaction.user.mention} vs {opponent.mention}", embed=embed, view=view)
            try:
                msg = await interaction.original_response()
                view.message = msg
                # Register ongoing battle for both users
                _ONGOING_BATTLES[a_id] = msg
                _ONGOING_BATTLES[b_id] = msg
            except Exception:
                view.message = None
