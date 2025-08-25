import os
import json
import time
import random
import asyncio
import re
from typing import Dict, Any, Optional, List, Tuple

import discord
from discord import app_commands


DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
USER_FILE = os.path.join(DATA_DIR, 'users.json')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

try:
    from google import genai  # type: ignore
except Exception:  # pragma: no cover
    genai = None  # type: ignore


# ----- Persistence helpers -----

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


# ----- Gemini helpers -----

_GENAI_CLIENT: Optional[object] = None


def _get_genai_client():
    global _GENAI_CLIENT
    if _GENAI_CLIENT is not None:
        return _GENAI_CLIENT
    if not GEMINI_API_KEY or genai is None:
        return None
    try:
        _GENAI_CLIENT = genai.Client(api_key=GEMINI_API_KEY)  # type: ignore[attr-defined]
    except Exception:
        _GENAI_CLIENT = None
    return _GENAI_CLIENT


async def _gemini_generate(prompt: str) -> str:
    if not GEMINI_API_KEY or genai is None:
        print("[Minigame] _gemini_generate: Missing API key or SDK; returning empty.")
        return ''
    client = _get_genai_client()
    if client is None:
        print("[Minigame] _gemini_generate: No client available; returning empty.")
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
                print("[Minigame] _gemini_generate: Response had no text and to_dict failed; returning empty.")
                return ''
        except Exception as e:
            print(f"[Minigame] _gemini_generate: Exception during model call: {type(e).__name__}: {e}")
            return ''

    return await asyncio.to_thread(_call_sync)


# ----- Minigame components -----

def _list_user_businesses(ud: Dict[str, Any]) -> List[Tuple[int, Dict[str, Any]]]:
    out = []
    for i, s in enumerate(ud.get('slots', []) or []):
        if s:
            out.append((i, s))
    return out


def _persona_seed() -> Dict[str, str]:
    personas = [
        {"name": "Tech-savvy student", "need": "affordable but high-quality", "mood": "curious"},
        {"name": "Busy parent", "need": "convenient and reliable", "mood": "practical"},
        {"name": "Small business owner", "need": "cost-effective with clear ROI", "mood": "results-driven"},
        {"name": "Hobbyist", "need": "fun and customizable", "mood": "enthusiastic"},
        {"name": "Eco-conscious buyer", "need": "sustainable and ethical", "mood": "thoughtful"},
    ]
    return random.choice(personas)


async def _generate_customer_prompt(biz_name: str, biz_desc: str) -> str:
    p = _persona_seed()
    base = (
        f"Roleplay a {p['mood']} customer persona ({p['name']}) interested in a product that is {p['need']}. "
        f"You're considering buying from a business called '{biz_name}'. "
        f"Business about: {biz_desc}. "
        "In 1-2 short sentences, state your need and a hesitation or question."
    )
    text = ''
    try:
        text = (await asyncio.wait_for(_gemini_generate(base), timeout=20.0)).strip()
    except Exception as e:
        print(f"[Minigame] _generate_customer_prompt: Model call failed ({type(e).__name__}: {e}); using fallback template.")
        text = ''
    # Normalize whitespace and quotes/newlines
    text = (text or '').replace('\n', ' ').replace('  ', ' ').strip().strip('"').strip("'")

    # Helper to split sentences cleanly
    def _split_sentences(s: str) -> List[str]:
        parts = re.split(r'(?<=[.!?])\s+', s.strip()) if s else []
        return [x.strip() for x in parts if x and any(ch.isalnum() for ch in x)]

    # Ensure fallback when empty or non-sentential
    sents = _split_sentences(text)
    if not sents:
        print("[Minigame] _generate_customer_prompt: Model returned empty/invalid text; using fallback template.")
        text = (
            f"I'm a {p['name']} looking for something {p['need']}. "
            "Before I buy, how will this help me?"
        )
        sents = _split_sentences(text)

    # Ensure at least two short sentences by appending a brief follow-up if needed
    if len(sents) == 1:
        print("[Minigame] _generate_customer_prompt: Only one sentence from model; appending brief follow-up.")
        follow = "Can you explain briefly how this solves my need?"
        if p.get('need'):
            follow = f"Can you explain briefly how this meets my '{p['need']}' need?"
        if sents[0] and sents[0][-1] not in '.!?':
            sents[0] = sents[0] + '.'
        sents.append(follow)

    # Compose and keep within ~200 chars, preferring whole sentences
    recomposed = ' '.join(sents[:2])
    if len(recomposed) > 200:
        print(f"[Minigame] _generate_customer_prompt: Truncating model text from {len(recomposed)} to 200 chars.")
        first_only = sents[0]
        text = first_only if len(first_only) <= 200 else first_only[:197].rstrip() + '...'
    else:
        text = recomposed

    if text and text[-1] not in '.!?…':
        text += '.'
    if not text:
        print("[Minigame] _generate_customer_prompt: Empty text after processing; using fallback template.")
    return text


def _heuristic_convincing(pitch: str) -> bool:
    s = (pitch or '').lower()
    if len(s.split()) < 8:
        return False
    has_num = any(ch.isdigit() for ch in s)
    has_value = any(k in s for k in [
        "save", "increase", "boost", "improve", "benefit", "value", "roi", "return"
    ])
    has_customer = any(k in s for k in ["you", "your", "customers", "client", "audience"])  # talks to customer
    score = (2 if has_num else 0) + (2 if has_value else 0) + (1 if has_customer else 0) + (1 if len(s) > 60 else 0)
    return score >= 3


async def _judge_pitch(customer_text: str, pitch: str) -> Tuple[bool, str]:
    prompt = (
        "You are judging a short sales pitch in a roleplay. Given a customer statement and the seller's pitch, "
        "respond with ONLY 'YES' if the pitch convincingly addresses the customer's need and likely leads to a purchase, otherwise 'NO'.\n\n"
        f"Customer: {customer_text}\n"
        f"Pitch: {pitch}\n\n"
        "Answer: YES or NO"
    )
    verdict = ''
    try:
        verdict = (await asyncio.wait_for(_gemini_generate(prompt), timeout=20.0)).strip().upper()
    except Exception as e:
        print(f"[Minigame] _judge_pitch: Model call failed ({type(e).__name__}: {e}); using heuristic judge.")
        verdict = ''
    if verdict.startswith('YES'):
        return True, "The pitch convincing."
    if verdict.startswith('NO'):
        return False, "The pitch unconvincing."
    if verdict:
        print(f"[Minigame] _judge_pitch: Unrecognized verdict '{verdict[:80]}'; falling back to heuristic.")
    else:
        print("[Minigame] _judge_pitch: Empty verdict; falling back to heuristic.")
    ok = _heuristic_convincing(pitch)
    return ok, ("Heuristic: convincing." if ok else "Heuristic: not convincing.")


class PitchModal(discord.ui.Modal, title="Your Sales Pitch"):
    def __init__(self, owner_id: str):
        super().__init__()
        self.owner_id = owner_id
        self.pitch = discord.ui.TextInput(
            label="Why should they buy?",
            style=discord.TextStyle.paragraph,
            placeholder="Speak to the customer's need, be specific, include benefits and numbers.",
            max_length=500,
            required=True,
        )
        self.add_item(self.pitch)

    async def on_submit(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self.owner_id:
            await interaction.response.send_message("> ❌ Only the original player may submit a pitch.", ephemeral=True)
            return
        view: SellingView = self.parent_view  # type: ignore[attr-defined]
        if view.over:
            await interaction.response.send_message("> ℹ️ This minigame has ended.", ephemeral=True)
            return
        view.last_pitch = str(self.pitch.value or '').strip()
        if not view.last_pitch:
            await interaction.response.send_message("> ❌ Please enter a pitch.", ephemeral=True)
            return
        # Defer to avoid timeout and show thinking state
        try:
            await interaction.response.defer()
        except Exception:
            pass
        try:
            view.thinking = True
            # Disable controls while thinking
            try:
                view.pitch_button.disabled = True
                view.skip_button.disabled = True
            except Exception:
                pass
            await interaction.edit_original_response(embed=view.render_embed(), view=view)
        except Exception:
            pass
        # Judge pitch
        ok, reason = await _judge_pitch(view.customer_text or '', view.last_pitch)
        view.last_result = (ok, reason)
        try:
            view.thinking = False
            await interaction.edit_original_response(embed=view.render_embed(), view=view)
        except Exception:
            pass
        await view.apply_result_and_advance(interaction)


class SellingView(discord.ui.View):
    def __init__(self, owner_id: int, user_data: Dict[str, Any]):
        super().__init__(timeout=300)
        self.owner_id = str(owner_id)
        self.user_data = user_data
        self.goal = random.randint(5, 10)
        self.lives = 3
        self.wins = 0
        self.fails = 0
        self.total_gained = 0
        self.business_index: Optional[int] = None
        self.customer_num = 0
        self.customer_text: Optional[str] = None
        self.last_pitch: Optional[str] = None
        self.last_result: Optional[Tuple[bool, str]] = None
        self.message_id: Optional[int] = None
        self.thinking = False
        self.ready_for_next = False
        self.over = False
        # Track starting metrics for end-of-game summary
        self.start_rating: Optional[float] = None
        self.start_income: Optional[int] = None

        # Business select
        options: List[discord.SelectOption] = []
        for idx, slot in _list_user_businesses(user_data):
            name = slot.get('name', f"Slot {idx+1}")
            inc = int(slot.get('income_per_day', 0))
            rating = float(slot.get('rating', 3.0))
            desc = f"⭐ {rating:.1f} • 💵 GL${inc}/day"
            options.append(discord.SelectOption(label=name[:100], description=desc[:100], value=str(idx)))
        if not options:
            options = [discord.SelectOption(label="No businesses", description="Create one with /passive", value="-1", default=True)]
        self.selector = discord.ui.Select(
            placeholder="Choose your business",
            min_values=1,
            max_values=1,
            options=options,
            disabled=(len(options) == 1 and options[0].value == '-1'),
        )

        async def _sel_cb(interaction: discord.Interaction):
            if str(interaction.user.id) != self.owner_id:
                await interaction.response.send_message("> ❌ Not your minigame.", ephemeral=True)
                return
            if self.selector.values[0] == '-1':
                await interaction.response.send_message("> ❌ You have no businesses.", ephemeral=True)
                return
            self.business_index = int(self.selector.values[0])
            # Capture starting rating and income for summary
            try:
                slot0 = self._biz() or {}
                self.start_rating = float(slot0.get('rating', 3.0))
                self.start_income = int(slot0.get('income_per_day', 0))
            except Exception:
                self.start_rating = self.start_rating or 3.0
                self.start_income = self.start_income or 0
            self.selector.disabled = True
            # Respond immediately while generating the prompt
            self.customer_num += 1
            self.customer_text = "Waiting for a customer..."
            self.last_pitch = None
            self.last_result = None
            # Buttons may not exist yet; guard accesses until after creation
            try:
                self.pitch_button.disabled = True
                self.skip_button.disabled = True
            except Exception:
                pass
            await interaction.response.edit_message(embed=self.render_embed(), view=self)

            async def _gen_and_update():
                try:
                    biz = self._biz() or {}
                    self.customer_text = await _generate_customer_prompt(
                        biz.get('name', 'Business'), biz.get('desc', '')
                    )
                    try:
                        self.pitch_button.disabled = False
                        self.skip_button.disabled = False
                    except Exception:
                        pass
                    try:
                        await interaction.edit_original_response(embed=self.render_embed(), view=self)
                    except Exception:
                        pass
                except Exception:
                    try:
                        await interaction.followup.send(
                            "> ⚠️ Failed to generate customer. Try again.", ephemeral=True
                        )
                    except Exception:
                        pass

            asyncio.create_task(_gen_and_update())

        self.selector.callback = _sel_cb  # type: ignore[assignment]
        self.add_item(self.selector)

        # Buttons
        self.pitch_button = discord.ui.Button(label="Make pitch", style=discord.ButtonStyle.primary, disabled=True)
        self.skip_button = discord.ui.Button(label="Skip customer", style=discord.ButtonStyle.secondary, disabled=True)
        self.next_button = discord.ui.Button(label="Next customer", style=discord.ButtonStyle.success, disabled=True)

        async def _pitch_cb(interaction: discord.Interaction):
            if str(interaction.user.id) != self.owner_id:
                await interaction.response.send_message("> ❌ Not your minigame.", ephemeral=True)
                return
            modal = PitchModal(self.owner_id)
            setattr(modal, 'parent_view', self)
            await interaction.response.send_modal(modal)

        async def _skip_cb(interaction: discord.Interaction):
            if str(interaction.user.id) != self.owner_id:
                await interaction.response.send_message("> ❌ Not your minigame.", ephemeral=True)
                return
            self.fails += 1
            self.lives -= 1
            self.last_result = (False, "Skipped customer.")
            self.thinking = False
            # Rating drops on a loss
            try:
                self._adjust_rating(-0.1)
            except Exception:
                pass
            # If the game is over after skipping, end; otherwise instantly generate the next customer
            if self.wins >= self.goal or self.fails >= 3:
                self.ready_for_next = False
                self.next_button.disabled = True
                await self._advance_or_end()
                await interaction.response.edit_message(embed=self.render_embed(), view=self)
                return

            # Prepare immediate next customer generation
            self.ready_for_next = False
            self.next_button.disabled = True
            try:
                self.pitch_button.disabled = True
                self.skip_button.disabled = True
            except Exception:
                pass
            # Show generating placeholder
            self.customer_num += 1
            self.customer_text = "*Waiting for a customer...*"
            self.last_pitch = None
            self.last_result = None
            await interaction.response.edit_message(embed=self.render_embed(), view=self)

            async def _gen_after_skip():
                try:
                    biz = self._biz() or {}
                    self.customer_text = await _generate_customer_prompt(
                        biz.get('name', 'Business'), biz.get('desc', '')
                    )
                    try:
                        self.pitch_button.disabled = False
                        self.skip_button.disabled = False
                    except Exception:
                        pass
                    try:
                        await interaction.edit_original_response(embed=self.render_embed(), view=self)
                    except Exception:
                        pass
                except Exception:
                    try:
                        await interaction.followup.send(
                            "> ⚠️ Failed to generate customer. Try again.", ephemeral=True
                        )
                    except Exception:
                        pass

            asyncio.create_task(_gen_after_skip())

        async def _next_cb(interaction: discord.Interaction):
            if str(interaction.user.id) != self.owner_id:
                await interaction.response.send_message("> ❌ Not your minigame.", ephemeral=True)
                return
            if not self.ready_for_next and not self.over:
                await interaction.response.send_message("> ℹ️ Make a pitch or skip first.", ephemeral=True)
                return
            # Prepare next customer generation
            self.ready_for_next = False
            # Disable all controls while generating
            self.next_button.disabled = True
            try:
                self.pitch_button.disabled = True
                self.skip_button.disabled = True
            except Exception:
                pass
            # Defer to avoid interaction timeout during generation
            try:
                await interaction.response.defer()
            except Exception:
                pass
            # Show generating placeholder
            self.customer_num += 1
            self.customer_text = "*Waiting for a customer...*"
            self.last_pitch = None
            self.last_result = None
            self.thinking = False
            try:
                await interaction.edit_original_response(embed=self.render_embed(), view=self)
            except Exception:
                pass

            async def _gen_next():
                try:
                    biz = self._biz() or {}
                    self.customer_text = await _generate_customer_prompt(
                        biz.get('name', 'Business'), biz.get('desc', '')
                    )
                    # Enable pitch/skip for the new customer
                    try:
                        self.pitch_button.disabled = False
                        self.skip_button.disabled = False
                    except Exception:
                        pass
                    try:
                        await interaction.edit_original_response(embed=self.render_embed(), view=self)
                    except Exception:
                        pass
                except Exception:
                    try:
                        await interaction.followup.send(
                            "> ⚠️ Failed to generate customer. Try again.", ephemeral=True
                        )
                    except Exception:
                        pass

            asyncio.create_task(_gen_next())

        self.pitch_button.callback = _pitch_cb  # type: ignore[assignment]
        self.skip_button.callback = _skip_cb  # type: ignore[assignment]
        self.next_button.callback = _next_cb  # type: ignore[assignment]
        self.add_item(self.pitch_button)
        self.add_item(self.skip_button)
        self.add_item(self.next_button)

    @staticmethod
    def _income_with_rating(base_income: int, rating: float) -> int:
        """Compute effective income/day from base and rating.
        Baseline at 3.0 = 1.0x. Each ±0.1 rating ≈ ±1%.
        Clamped to [0.2x, 2.0x]."""
        try:
            mult = 1.0 + (float(rating) - 3.0) * 0.1
            mult = max(0.2, min(2.0, mult))
            return int(round(int(base_income) * mult))
        except Exception:
            return int(base_income)

    def _biz(self) -> Optional[Dict[str, Any]]:
        try:
            if self.business_index is None:
                return None
            return self.user_data['slots'][self.business_index]
        except Exception:
            return None

    def _adjust_rating(self, delta: float) -> None:
        """Adjust current business rating by delta and persist (clamped 0.0–5.0)."""
        try:
            if self.business_index is None:
                return
            data = _load_users()
            ud = data.get(self.owner_id)
            if not ud:
                return
            slots = ud.get('slots') or []
            if not (0 <= self.business_index < len(slots)):
                return
            slot = slots[self.business_index] or {}
            current = float(slot.get('rating', 3.0))
            new_val = max(0.0, min(5.0, current + delta))
            slot['rating'] = round(new_val, 1)
            slots[self.business_index] = slot
            ud['slots'] = slots
            data[self.owner_id] = ud
            _save_users(data)
            # Keep local cache in sync
            try:
                self.user_data['slots'][self.business_index]['rating'] = slot['rating']
            except Exception:
                pass
        except Exception:
            pass

    async def _next_customer(self):
        biz = self._biz()
        if not biz:
            return
        self.customer_num += 1
        self.customer_text = await _generate_customer_prompt(biz.get('name', 'Business'), biz.get('desc', ''))
        self.last_pitch = None
        self.last_result = None
        # Enable pitching controls
        self.pitch_button.disabled = False
        self.skip_button.disabled = False

    async def _advance_or_end(self):
        if self.wins >= self.goal:
            # Victory bonus awarded immediately
            biz = self._biz() or {}
            base_bonus = int((biz.get('income_per_day', 0) or 0) * 1) + random.randint(200, 600)
            data = _load_users()
            ud = data.get(self.owner_id) or {}
            ud['balance'] = int(ud.get('balance', 0)) + base_bonus
            _save_users(data)
            self.total_gained += base_bonus
            self.over = True
            # Disable controls
            self.pitch_button.disabled = True
            self.skip_button.disabled = True
            self.next_button.disabled = True
            return
        if self.fails >= 3:
            self.over = True
            # Disable controls
            self.pitch_button.disabled = True
            self.skip_button.disabled = True
            self.next_button.disabled = True
            return
        # Continue to next customer
        await self._next_customer()

    async def apply_result_and_advance(self, interaction: discord.Interaction):
        ok, reason = self.last_result if isinstance(self.last_result, tuple) else (False, "")
        # Compute reward/penalty
        data = _load_users()
        ud = data.get(self.owner_id)
        if not ud:
            try:
                await interaction.response.send_message("> ❌ User data missing.", ephemeral=True)
            except Exception:
                pass
            return
        biz = self._biz()
        income = int((biz or {}).get('income_per_day', 0))
        if ok:
            reward = int(income + random.randint(10, 500))
            ud['balance'] = int(ud.get('balance', 0)) + reward
            self.wins += 1
            self.total_gained += reward
            # Rating increases on a win
            try:
                self._adjust_rating(+0.1)
            except Exception:
                pass
        else:
            penalty = int(random.randint(10, 200))
            ud['balance'] = max(0, int(ud.get('balance', 0)) - penalty)
            self.fails += 1
            self.lives -= 1
            # Rating drops on a loss
            try:
                self._adjust_rating(-0.1)
            except Exception:
                pass
        _save_users(data)

        # Disable pitch/skip until player chooses to advance
        self.pitch_button.disabled = True
        self.skip_button.disabled = True
        # After a pitch, don't auto-advance; enable Next unless the game is over
        self.ready_for_next = True
        if self.wins >= self.goal or self.fails >= 3:
            self.ready_for_next = False
            self.next_button.disabled = True
            await self._advance_or_end()
        else:
            self.next_button.disabled = False
        # Prefer editing the original response (works after modal defer)
        try:
            await interaction.edit_original_response(embed=self.render_embed(), view=self)
            return
        except Exception:
            pass
        # If not available, try editing the interaction response (component path)
        try:
            await interaction.response.edit_message(embed=self.render_embed(), view=self)
            return
        except Exception:
            pass
        # Fallback: followup with stored message id or send ephemeral
        try:
            if self.message_id:
                await interaction.followup.edit_message(message_id=self.message_id, embed=self.render_embed(), view=self)
            else:
                await interaction.followup.send(embed=self.render_embed(), view=self, ephemeral=True)
        except Exception:
            pass

    async def _check_end_and_prepare_next(self):
        # Deprecated in favor of _advance_or_end; keep for compatibility if referenced elsewhere
        await self._advance_or_end()

    def render_embed(self) -> discord.Embed:
        title = "Minigame — Selling to customers"
        color = discord.Color.green() if not self.over else (discord.Color.gold() if self.wins >= self.goal else discord.Color.red())
        embed = discord.Embed(title=title, color=color)
        embed.add_field(name="🎯 Goal", value=f"Close {self.goal} sales", inline=True)
        embed.add_field(name="📊 Progress", value=f"✅ {self.wins} • ❌ {self.fails}", inline=True)
        if self.business_index is None:
            embed.description = "### 🏢 Pick a business to start."
            return embed
        biz = self._biz() or {}
        embed.add_field(name="🏢 Business", value=biz.get('name', 'Business'), inline=True)
        try:
            rating = float(biz.get('rating', 3.0))
        except Exception:
            rating = 3.0
        # Show how much rating has been lost this session (from starting rating)
        loss_note = ""
        try:
            base = self.start_rating if self.start_rating is not None else rating
            delta = float(rating) - float(base)
            if delta < 0:
                loss_note = f" ({abs(delta):.1f} lost)"
        except Exception:
            loss_note = ""
        embed.add_field(name="⭐ Rating", value=f"{rating:.1f}{loss_note}", inline=True)
    # Current customer
        if not self.over:
            # Show pitch and thinking/result in description when available
            if self.last_pitch:
                if self.thinking:
                    embed.description = f"### Your pitch\n{self.last_pitch}\n\n🤔 Customer is thinking…"
                elif self.last_result is not None:
                    ok, _reason = self.last_result
                    verdict = "✅ Customer accepted the offer." if ok else "❌ Customer declined the offer."
                    embed.description = f"### Your pitch\n{self.last_pitch}\n\n{verdict}"
            embed.add_field(name=f"Customer #{self.customer_num}", value=self.customer_text or "…", inline=False)
            if self.last_pitch is not None and self.last_result is not None:
                ok, reason = self.last_result
                status = "✅ Success" if ok else "❌ Fail"
                embed.add_field(name="Last result", value=f"{status} — {reason}", inline=False)
        else:
            # End-of-game summary
            try:
                curr = self._biz() or {}
                curr_rating = float(curr.get('rating', 3.0))
            except Exception:
                curr = {}
                curr_rating = 3.0
            start_rating = self.start_rating if self.start_rating is not None else curr_rating
            start_income = self.start_income if self.start_income is not None else int(curr.get('income_per_day', 0))
            before_income = self._income_with_rating(start_income, start_rating)
            after_income = self._income_with_rating(start_income, curr_rating)
            delta_rating = curr_rating - float(start_rating)

            if self.wins >= self.goal:
                embed.description = f"### 🏆 You won! Total gained: <:greensl:1409394243025502258>{self.total_gained}"
            else:
                embed.description = f"### 🪦 You lost. Customers reached: {self.wins}/{self.goal}"
            # Append rating and income change info
            embed.add_field(name="📈 Rating change", value=f"⭐ {start_rating:.1f} → ⭐ {curr_rating:.1f} ({delta_rating:+.1f})", inline=False)
            embed.add_field(name="💵 Income", value=f"<:greensl:1409394243025502258>{before_income}/day → <:greensl:1409394243025502258>{after_income}/day", inline=False)
        return embed

    async def on_timeout(self) -> None:
        # Auto-end on timeout
        self.over = True
        try:
            self.pitch_button.disabled = True
            self.skip_button.disabled = True
        except Exception:
            pass


class MinigameCommand:
    @staticmethod
    async def setup(tree: app_commands.CommandTree):
        @tree.command(name="minigame", description="Play a business-themed minigame")
        @app_commands.describe(category="Choose a minigame")
        @app_commands.choices(
            category=[
                app_commands.Choice(name="Selling to customers", value="selling"),
            ]
        )
        @app_commands.allowed_contexts(dms=True, guilds=True, private_channels=True)
        async def minigame(interaction: discord.Interaction, category: app_commands.Choice[str]):
            # Verify user has a business
            users = _load_users()
            uid = str(interaction.user.id)
            ud = users.get(uid)
            if not ud or not any(bool(s) for s in ud.get('slots', [])):
                await interaction.response.send_message("> ❌ You need a business to play. Use /passive to create one.", ephemeral=True)
                return
            if category.value == 'selling':
                view = SellingView(interaction.user.id, ud)
                embed = view.render_embed()
                await interaction.response.send_message(embed=embed, view=view, ephemeral=False)
                # Store the created message id for later edits from modal submissions
                try:
                    msg = await interaction.original_response()
                    view.message_id = msg.id
                except Exception:
                    pass
            else:
                await interaction.response.send_message("> ❌ Unknown minigame.", ephemeral=True)
