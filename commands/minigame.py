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
STOCK_FILE = os.path.join(DATA_DIR, 'stocks.json')
MARKET_FILE = os.path.join(DATA_DIR, 'market.json')
PURCHASED_FILE = os.path.join(DATA_DIR, 'purchased_upgrades.json')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

# Track active minigames per user to prevent duplicates and provide jump links
ACTIVE_MINIGAMES: Dict[str, Dict[str, Any]] = {}

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


# ----- Shared loaders for stock/upgrades (to match passive disp_inc) -----

def _load_stocks() -> Dict[str, Any]:
    if not os.path.exists(STOCK_FILE):
        return {"current_pct": 50.0}
    try:
        with open(STOCK_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {"current_pct": 50.0}


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


def _total_boost_pct(slot: Dict[str, Any], owner_id: str | None, slot_index: int | None) -> float:
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


def _effective_income_per_day(slot: Dict[str, Any], owner_id: str | None, slot_index: int | None) -> int:
    try:
        base = float(slot.get('income_per_day', 0))
        rating = float(slot.get('rating', 1.0))
        boost_pct = _total_boost_pct(slot, owner_id, slot_index)
        mult = (1.0 + float(boost_pct) / 100.0)
        return max(0, int(round(base * rating * mult)))
    except Exception:
        return int(slot.get('income_per_day', 0) or 0)


def _disp_inc(slot: Dict[str, Any], owner_id: str | None, slot_index: int | None, stock_factor: float) -> int:
    inc = _effective_income_per_day(slot, owner_id, slot_index)
    return int(round(inc * (stock_factor if stock_factor else 0.0)))


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


# (AI-text detection removed)


# ----- Minigame components -----

def _list_user_businesses(ud: Dict[str, Any]) -> List[Tuple[int, Dict[str, Any]]]:
    out = []
    for i, s in enumerate(ud.get('slots', []) or []):
        if s:
            out.append((i, s))
    return out


async def _generate_customer_prompt(biz_name: str, biz_desc: str) -> str:
    base = (
        "You are a customer and wanted to buy a product from the business."
        "You ask what is the business all about and what do they have to offer then ask something that the business has to offer."
        "Use any persona that might fit with the business to have some personality as a customer."
        "Keep it 1-3 short sentences. "
        f"Business name: '{biz_name}'. Brief about: {biz_desc}. "
        "Examples of acceptable questions: 'What's the price and how does it work?' or 'What are the key features and turnaround time?'"
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
        print("[Minigame] _generate_customer_prompt: Model returned empty/invalid text; using neutral fallback.")
        text = (
            "What's the price and how does it work?"
        )
        sents = _split_sentences(text)

    # Ensure at least two short sentences by appending a brief follow-up if needed
    if len(sents) == 1:
        print("[Minigame] _generate_customer_prompt: Only one sentence from model; appending neutral follow-up.")
        follow = "What are the key features?"
        if sents[0] and sents[0][-1] not in '.!?':
            sents[0] = sents[0] + '.'
        sents.append(follow)

    if text and text[-1] not in '.!?…':
        text += '.'
    if not text:
        print("[Minigame] _generate_customer_prompt: Empty text after processing; using fallback template.")
    return text


def _heuristic_convincing(pitch: str) -> bool:
    s = (pitch or '').lower()
    # Make it easier: require fewer words
    if len(s.split()) < 4:
        return False
    has_num = any(ch.isdigit() for ch in s)
    has_customer = any(k in s for k in ["you", "your", "customers", "client", "audience"])  # talks to customer
    has_social = any(k in s for k in ["reviews", "testimonials", "trusted", "rated"])  # social proof
    longish = len(s) > 40
    # Easier scoring: lower threshold and multiple simple win conditions
    score = (1 if has_num else 0) + (2 if has_customer else 0) + (1 if longish else 0) + (1 if has_social else 0)
    return score >= 2


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
        reason = await _decline_reason_model(customer_text, pitch)
        return False, reason
    if verdict:
        print(f"[Minigame] _judge_pitch: Unrecognized verdict '{verdict[:80]}'; falling back to heuristic.")
    else:
        print("[Minigame] _judge_pitch: Empty verdict; falling back to heuristic.")
    ok = _heuristic_convincing(pitch)
    if ok:
        return True, "Heuristic: convincing."
    reason = await _decline_reason_model(customer_text, pitch)
    return False, reason


async def _decline_reason_model(customer_text: str, pitch: str) -> str:
    """Use the model to generate a short reason for decline using both customer prompt and user's pitch.
    Falls back to heuristic if the model isn't available or returns an empty response.
    """
    guide = (
        "In one short sentence (max 140 chars), explain why the customer would decline or why the pitch isn't convincing. "
        "Base it on the customer's statement and the seller's pitch. Be specific (price, reliability, ROI, time, fit). "
        "No preamble; just the reason."
    )
    prompt = (
        f"Customer: {customer_text}\n"
        f"Pitch: {pitch}\n\n"
        f"{guide}"
    )
    try:
        text = (await asyncio.wait_for(_gemini_generate(prompt), timeout=12.0)).strip()
        # Normalize and constrain
        text = (text or '').replace('\n', ' ').strip().strip('"').strip("'")
    except Exception as e:
        print(f"[Minigame] _decline_reason_model: Model call failed ({type(e).__name__}: {e}); using heuristic reason.")
    # Fallback
    return _decline_reason(customer_text, pitch)


def _decline_reason(customer_text: str, pitch: str) -> str:
    """Explain likely reason for a decline, based on the customer's text and the pitch content.
    Returns a short, actionable sentence.
    """
    ct = (customer_text or '').lower()
    p = (pitch or '').lower()

    # Basic quality checks
    if len(p.split()) < 5:
        return "Declined: your pitch is too short and vague. Add specifics and benefits."

    has_num = any(ch.isdigit() for ch in p)
    talks_to_customer = any(k in p for k in ["you", "your", "customers", "client", "audience"])
    mentions_benefit = any(k in p for k in [
        "save", "increase", "boost", "improve", "benefit", "return", "free", "trial", "discount",
        "results", "growth", "profit", "revenue"
    ])

    # Thematic concerns from customer text and whether pitch addresses them
    def _missing(topic_words: List[str], reply_words: List[str]) -> bool:
        return any(w in ct for w in topic_words) and not any(w in p for w in reply_words)

    if not talks_to_customer:
        return "Declined: the pitch doesn't speak to the customer's needs ('you/your') or context."
    if not mentions_benefit:
        return "Declined: benefits aren't clear—explain what's in it for them."
    if not has_num:
        return "Declined: no concrete numbers or examples—add metrics, timelines, or guarantees."

    # Fallback generic reason
    return "They didn't see how your pitch connects clearly to their stated need. Tie benefits directly to it."


async def _feedback_for_pitch(customer_text: str, pitch: str, accepted: bool) -> str:
    """Return one short, specific feedback sentence from the customer's perspective.
    Uses the model if available, with a fast timeout, otherwise falls back to a heuristic.
    """
    ct = (customer_text or '').strip()
    pv = (pitch or '').strip()
    if not pv:
        return "No pitch to respond to."
    if not GEMINI_API_KEY or genai is None:
        # Heuristic fallback
        if accepted:
            return "Clear fit and benefits made the choice easy."
        # Leverage decline reason to craft feedback
        why = _decline_reason(ct, pv)
        return f"I'd need this addressed: {why.replace('Declined: ', '')}"
    try:
        intent = (
            "You accepted the offer. In ONE short sentence (<=120 chars), say what convinced you."
            if accepted else
            "You declined the offer. In ONE short sentence (<=120 chars), say what would have convinced you."
        )
        prompt = (
            "From the customer's perspective, give feedback about the seller's pitch. "
            "Be specific and actionable.\n\n"
            f"Customer: {ct}\n"
            f"Pitch: {pv}\n\n"
            f"{intent}"
        )
        try:
            fb = (await asyncio.wait_for(_gemini_generate(prompt), timeout=8.0)).strip()
        except Exception:
            fb = ''
        fb = (fb or '').replace('\n', ' ').strip().strip('"').strip("'")
        if not fb:
            if accepted:
                return "Clear fit and benefits made the choice easy."
            why = _decline_reason(ct, pv)
            return f"I'd need this addressed: {why.replace('Declined: ', '')}"
        return fb
    except Exception:
        if accepted:
            return "Clear fit and benefits made the choice easy."
        why = _decline_reason(ct, pv)
        return f"I'd need this addressed: {why.replace('Declined: ', '')}"


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
    def __init__(self, owner_id: int, user_data: Dict[str, Any], owner_name: Optional[str] = None, owner_avatar: Optional[str] = None):
        super().__init__(timeout=300)
        self.owner_id = str(owner_id)
        self.user_data = user_data
        self.owner_name = owner_name
        self.owner_avatar = owner_avatar
        self.goal = random.randint(1, 6)
        self.lives = 3
        self.wins = 0
        self.fails = 0
        self.total_gained = 0
        self.business_index: Optional[int] = None
        self.customer_num = 0
        self.customer_text: Optional[str] = None
        self.last_pitch: Optional[str] = None
        self.last_result: Optional[Tuple[bool, str]] = None
        self.last_feedback: Optional[str] = None
        self.message_id: Optional[int] = None
        self.thinking = False
        self.ready_for_next = False
        self.over = False
        # Track starting metrics for end-of-game summary
        self.start_rating: Optional[float] = None
        self.start_income: Optional[int] = None

        # Business select
        options: List[discord.SelectOption] = []
        # Compute stock factor once for display income
        try:
            stocks = _load_stocks()
            stock_pct = float((stocks or {}).get('current_pct', 50.0))
            stock_factor = (stock_pct / 50.0) if stock_pct != 0 else 0.0
        except Exception:
            stock_factor = 1.0
        for idx, slot in _list_user_businesses(user_data):
            name = slot.get('name', f"Slot {idx+1}")
            # Use passive's disp_inc (rating × base × boosts × stock)
            inc = _disp_inc(slot, self.owner_id, idx, stock_factor)
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
            # Capture starting rating and income for summary (income uses passive's disp_inc)
            try:
                slot0 = self._biz() or {}
                self.start_rating = float(slot0.get('rating', 3.0))
                stocks = _load_stocks()
                stock_pct = float((stocks or {}).get('current_pct', 50.0))
                stock_factor = (stock_pct / 50.0) if stock_pct != 0 else 0.0
                # Compute at selection time using current rating and boosts
                self.start_income = _disp_inc(slot0, self.owner_id, self.business_index, stock_factor)
            except Exception:
                self.start_rating = self.start_rating or 3.0
                self.start_income = self.start_income or 0
            self.selector.disabled = True
            # Respond immediately while generating the prompt
            self.customer_num += 1
            self.customer_text = "Waiting for a customer..."
            self.last_pitch = None
            self.last_result = None
            self.last_feedback = None
            # (AI detection removed)
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
                        self.end_button.disabled = False
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
        self.end_button = discord.ui.Button(label="End Day", style=discord.ButtonStyle.danger, disabled=True)

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
            self.last_result = (False, "Skipped customer.")
            # (AI detection removed)
            self.thinking = False
            # If the game is over after skipping, end; otherwise instantly generate the next customer
            if self.wins >= self.goal or self.fails >= 3:
                self.ready_for_next = False
                self.next_button.disabled = True
                await self._advance_or_end()
                # Deactivate UI on finished game
                try:
                    self.stop()
                except Exception:
                    pass
                await interaction.response.edit_message(embed=self.render_embed(), view=None)
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
            self.last_feedback = None
            # (AI detection removed)
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
            self.last_feedback = None
            # (AI detection removed)
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

        async def _end_cb(interaction: discord.Interaction):
            if str(interaction.user.id) != self.owner_id:
                await interaction.response.send_message("> ❌ Not your minigame.", ephemeral=True)
                return
            # Immediately end the game without extra bonuses/penalties
            self.over = True
            self.thinking = False
            try:
                self.pitch_button.disabled = True
                self.skip_button.disabled = True
                self.next_button.disabled = True
                self.end_button.disabled = True
            except Exception:
                pass
            # Clear active session and stop view so message is no longer interactive
            try:
                ACTIVE_MINIGAMES.pop(self.owner_id, None)
            except Exception:
                pass
            try:
                self.stop()
            except Exception:
                pass
            try:
                await interaction.response.edit_message(embed=self.render_embed(), view=None)
            except Exception:
                try:
                    await interaction.edit_original_response(embed=self.render_embed(), view=None)
                except Exception:
                    pass

        self.pitch_button.callback = _pitch_cb  # type: ignore[assignment]
        self.skip_button.callback = _skip_cb  # type: ignore[assignment]
        self.next_button.callback = _next_cb  # type: ignore[assignment]
        self.end_button.callback = _end_cb  # type: ignore[assignment]
        self.add_item(self.pitch_button)
        self.add_item(self.skip_button)
        self.add_item(self.next_button)
        self.add_item(self.end_button)

    def _biz(self) -> Optional[Dict[str, Any]]:
        try:
            if self.business_index is None:
                return None
            return self.user_data['slots'][self.business_index]
        except Exception:
            return None

    def _adjust_rating(self, delta: float) -> None:
        """Adjust current business rating by delta and persist (minimum 0.0, no upper cap)."""
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
            current = float(slot.get('rating', 1.0))
            new_val = max(0.0, current + delta)
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
        self.last_feedback = None
        # Enable pitching controls
        self.pitch_button.disabled = False
        self.skip_button.disabled = False
        self.end_button.disabled = False

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
            try:
                ACTIVE_MINIGAMES.pop(self.owner_id, None)
            except Exception:
                pass
            # Disable controls
            self.pitch_button.disabled = True
            self.skip_button.disabled = True
            self.next_button.disabled = True
            try:
                self.end_button.disabled = True
            except Exception:
                pass
            try:
                self.stop()
            except Exception:
                pass
            return
        if self.fails >= 3:
            self.over = True
            try:
                ACTIVE_MINIGAMES.pop(self.owner_id, None)
            except Exception:
                pass
            # Disable controls
            self.pitch_button.disabled = True
            self.skip_button.disabled = True
            self.next_button.disabled = True
            try:
                self.end_button.disabled = True
            except Exception:
                pass
            try:
                self.stop()
            except Exception:
                pass
            return
        # Continue to next customer
        await self._next_customer()

    async def apply_result_and_advance(self, interaction: discord.Interaction):
        ok, reason = self.last_result if isinstance(self.last_result, tuple) else (False, "")
        # Generate customer feedback for the pitch
        try:
            self.last_feedback = (await asyncio.wait_for(
                _feedback_for_pitch(self.customer_text or '', self.last_pitch or '', ok), timeout=9.0
            )).strip()
        except Exception:
            self.last_feedback = None
        # Compute reward/penalty and ensure user exists
        data_check = _load_users()
        ud_check = data_check.get(self.owner_id)
        if not ud_check:
            try:
                await interaction.response.send_message("> ❌ User data missing.", ephemeral=True)
            except Exception:
                pass
            return
        biz = self._biz()
        income = int((biz or {}).get('income_per_day', 0))
        reward = 0
        penalty = 0
        if ok:
            reward = int(income + random.randint(10, 500))
            self.wins += 1
            self.total_gained += reward
            # Rating increases on a win
            try:
                self._adjust_rating(+0.1)
            except Exception:
                pass
        else:
            penalty = int(random.randint(10, 200))
            self.fails += 1
            self.lives -= 1
            # Rating drops on a loss
            try:
                self._adjust_rating(-0.1)
            except Exception:
                pass
        # Persist balance changes AFTER rating adjust to avoid overwriting rating from a stale copy
        try:
            data2 = _load_users()
            ud2 = data2.get(self.owner_id) or {}
            cur_bal = int(ud2.get('balance', 0))
            if ok:
                ud2['balance'] = cur_bal + reward
            else:
                ud2['balance'] = max(0, cur_bal - penalty)
            data2[self.owner_id] = ud2
            _save_users(data2)
        except Exception:
            pass

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
            await interaction.edit_original_response(embed=self.render_embed(), view=None if self.over else self)
            return
        except Exception:
            pass
        # If not available, try editing the interaction response (component path)
        try:
            await interaction.response.edit_message(embed=self.render_embed(), view=None if self.over else self)
            return
        except Exception:
            pass
        # Fallback: followup with stored message id or send ephemeral
        try:
            if self.message_id:
                await interaction.followup.edit_message(message_id=self.message_id, embed=self.render_embed(), view=None if self.over else self)
            else:
                if self.over:
                    await interaction.followup.send(embed=self.render_embed(), ephemeral=True)
                else:
                    await interaction.followup.send(embed=self.render_embed(), view=self, ephemeral=True)
        except Exception:
            pass

    async def _check_end_and_prepare_next(self):
        # Deprecated in favor of _advance_or_end; keep for compatibility if referenced elsewhere
        await self._advance_or_end()

    def render_embed(self) -> discord.Embed:
        title = "Minigame — Sale Pitch"
        color = discord.Color.green() if not self.over else (discord.Color.gold() if self.wins >= self.goal else discord.Color.red())
        embed = discord.Embed(title=title, color=color)
        # Set author to invoking player's name/avatar if available
        try:
            if self.owner_name or self.owner_avatar:
                embed.set_author(name=self.owner_name or "", icon_url=self.owner_avatar)
        except Exception:
            pass
        embed.add_field(name="🎯 Goal", value=f"Close {self.goal} sales", inline=True)
        embed.add_field(name="📊 Progress", value=f"✅ {self.wins} • ❌ {self.fails}", inline=True)
        if self.business_index is None:
            embed.description = "### 🏢 Pick a business to start."
            return embed
        biz = self._biz() or {}
        embed.add_field(name="🏢 Business", value=biz.get('name', 'Business'), inline=True)
        try:
            rating = float(biz.get('rating', 1.0))
        except Exception:
            rating = 1.0
        embed.add_field(name="⭐ Rating", value=f"{rating:.1f}", inline=True)
    # Current customer
        if not self.over:
            # Customer field
            embed.add_field(name=f"🛃 Customer #{self.customer_num}", value=self.customer_text or "…", inline=False)
            # User's pitch as a dedicated field under the customer field
            if self.last_pitch:
                pitch_field = self.last_pitch
                if self.thinking:
                    pitch_field += "\n\n*🤔 Customer is thinking…*"
                elif self.last_result is not None:
                    ok, _reason = self.last_result
                    verdict = "*✅ Customer accepted the offer.*" if ok else "*❌ Customer declined the offer.*"
                    pitch_field += f"\n\n{verdict}"
                embed.add_field(name="🗣️ Your pitch", value=pitch_field, inline=False)
            # Result details
            if self.last_pitch is not None and self.last_result is not None:
                ok, reason = self.last_result
                status = "✅ Success" if ok else "❌ Fail"
                lr = f"{status} — {reason}"
                if self.last_feedback:
                    lr += f"\n🗨️ {self.last_feedback}"
                embed.add_field(name="📋 Last result", value=lr, inline=False)
        else:
            # End-of-game summary
            try:
                curr = self._biz() or {}
                curr_rating = float(curr.get('rating', 1.0))
            except Exception:
                curr = {}
                curr_rating = 1.0
            start_rating = self.start_rating if self.start_rating is not None else curr_rating
            # Show passive-style disp_inc: includes rating, boosts, and stock factor
            stocks = _load_stocks()
            stock_pct = float((stocks or {}).get('current_pct', 50.0))
            stock_factor = (stock_pct / 50.0) if stock_pct != 0 else 0.0
            # Prefer captured start_income if available; otherwise compute from current slot as a fallback
            before_income = int(self.start_income) if self.start_income is not None else _disp_inc(curr, self.owner_id, self.business_index, stock_factor)
            after_income = _disp_inc(curr, self.owner_id, self.business_index, stock_factor)
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
            ACTIVE_MINIGAMES.pop(self.owner_id, None)
        except Exception:
            pass
        try:
            self.pitch_button.disabled = True
            self.skip_button.disabled = True
            self.next_button.disabled = True
            self.end_button.disabled = True
        except Exception:
            pass
        try:
            self.stop()
        except Exception:
            pass


class MinigameCommand:
    @staticmethod
    async def setup(tree: app_commands.CommandTree):
        @tree.command(name="minigame", description="Play a business-themed minigame")
        @app_commands.describe(category="Choose a minigame")
        @app_commands.choices(
            category=[
                app_commands.Choice(name="Sale Pitch", value="selling"),
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
            # If an active minigame exists, jump to it
            try:
                existing = ACTIVE_MINIGAMES.get(uid)
            except Exception:
                existing = None
            if existing and isinstance(existing, dict):
                try:
                    link = existing.get('link')
                    if link:
                        await interaction.response.send_message(
                            f"> ℹ️ You already have an active minigame. Jump back: {link}", ephemeral=True
                        )
                        return
                except Exception:
                    pass
            if category.value == 'selling':
                # Resolve display name and avatar for author
                try:
                    owner_name = interaction.user.display_name
                except Exception:
                    owner_name = None  # type: ignore[assignment]
                try:
                    owner_avatar = str(interaction.user.display_avatar.url)
                except Exception:
                    owner_avatar = None  # type: ignore[assignment]
                view = SellingView(interaction.user.id, ud, owner_name=owner_name, owner_avatar=owner_avatar)
                embed = view.render_embed()
                await interaction.response.send_message(embed=embed, view=view, ephemeral=False)
                # Store the created message id for later edits from modal submissions
                try:
                    msg = await interaction.original_response()
                    view.message_id = msg.id
                    # Record as active with a jump link
                    try:
                        ACTIVE_MINIGAMES[uid] = {
                            'message_id': msg.id,
                            'channel_id': msg.channel.id if msg.channel else None,
                            'guild_id': getattr(msg.guild, 'id', None),
                            'link': msg.jump_url,
                        }
                    except Exception:
                        pass
                except Exception:
                    pass
            else:
                await interaction.response.send_message("> ❌ Unknown minigame.", ephemeral=True)
