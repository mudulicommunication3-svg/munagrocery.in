"""Multi-engine AI customer assistant (Grok default + Gemini + Groq) with RAG.

Behaviour:
  * The admin picks the engine from the Admin Panel (persisted in
    bot_settings.json, default "Grok"); ask()/answer() route to it.
  * All engines receive the EXACT same SYSTEM_PROMPT and the EXACT same
    built user prompt (live catalog RAG context + language + question).
  * System prompt (from the project spec) keeps the bot strictly on-topic.
  * Before every answer the current catalog is fetched from Google Sheets
    (cached for CATALOG_TTL_SECONDS so we do not burn API quota) and injected
    into the prompt as context - that is the "RAG" part.
  * The last CHAT_HISTORY_MAX (10) messages per customer are kept in memory
    and injected into the prompt so follow-up questions work; the
    /clearhistory command resets them.
  * Cart actions (add / add-more / remove / view / complete) are resolved
    LOCALLY against the catalog before any AI call - instant and reliable.
  * A circuit breaker skips an engine that just failed fatally (e.g. out of
    credits) for a cool-down window so replies stay fast.
  * Gemini is reached via ``google-generativeai`` (imported lazily).
    Grok (xAI) is reached via the ``openai`` client pointed at
    ``https://api.x.ai/v1``; Groq via the official ``groq`` SDK (both
    imported lazily); bot startup stays fast.

Public API:
    answer(question) -> str        (async, safe for PTB handlers)
    ask(question) -> str           (sync core, routes to the active engine)
    get_active_ai() / set_active_ai() / toggle_active_ai()
    handle_customer_message(...)   (PTB handler)
    register(application)          (attach handler group 0)

Configuration (.env): GEMINI_API_KEY (Gemini), GROK_API_KEY (+ optional
GROK_MODEL, default "grok-2-latest") for Grok, GROQ_API_KEY (+ optional
GROQ_MODEL, auto-detected default "llama-3.3-70b-versatile") for Groq.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

import db  # noqa: E402

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are the official assistant for MUNNA MOBILE & Grocery. "
    "You strictly answer queries regarding store products, prices, and "
    "availability based on the provided database context. "
    "Decline out-of-scope questions politely. "
    "When the customer wants to buy something (e.g. 'I need 2 Fortune oils "
    "and 1 Tata salt'), list the matched items with quantities and prices, "
    "show the total, and ask them to reply 'Confirm' to place the order. "
    "Never invent products or prices - use ONLY the catalog context."
)

# confirm words (English + Odia/Hindi) that accept a pending AI order
CONFIRM_WORDS = frozenset(
    "confirm confirmed yes place order ok okay done han haan ha confirm karo "
    "ହଁ ହଁ ଠିକ ଅଛି କନଫର୍ମ କର ଅର୍ଡର କର ହଁ କର".split()
)

CANCEL_WORDS = frozenset(
    "cancel no stop ବାତିଲ ନା ନାହିଁ ରଦ୍ଦ".split()
)

CATALOG_TTL_SECONDS = 10 * 60  # re-read the sheet every 10 minutes
MAX_CATALOG_LINES = 60  # keep prompts inside sane token limits
DEFAULT_MODEL = "gemini-3.8-flash"  # gemini-2.5-flash was retired for new users

# Grok (xAI) via the OpenAI-compatible API
GROK_BASE_URL = "https://api.x.ai/v1"
DEFAULT_GROK_MODEL = "grok-2-latest"  # override with GROK_MODEL; grok-beta is the fallback

# Groq (fast hosted open models) via the official ``groq`` SDK
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"  # override with GROQ_MODEL; auto-detected otherwise

# --------------------------------------------------------------------------- #
# Active AI engine (Grok <-> Gemini) - persisted in bot_settings.json so the
# admin's panel toggle survives restarts. Default: "Grok".
# --------------------------------------------------------------------------- #
SETTINGS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "bot_settings.json"
)
DEFAULT_ACTIVE_AI = "Grok"
# Cycle order used by the admin panel toggle button.
AI_ENGINES = ("Grok", "Gemini", "Groq")
# accepted aliases -> canonical engine name
_ENGINE_ALIASES = {"grok": "Grok", "gemini": "Gemini", "groq": "Groq"}
_active_ai_cache: Any = None  # in-memory cache; None = not loaded yet


def get_active_ai() -> str:
    """Active engine ('Grok' default, 'Gemini' or 'Groq') from settings, cached."""
    global _active_ai_cache
    if _active_ai_cache is not None:
        return _active_ai_cache
    model = DEFAULT_ACTIVE_AI
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
            saved = str(json.load(fh).get("active_ai_model") or "").strip().lower()
        model = _ENGINE_ALIASES.get(saved, DEFAULT_ACTIVE_AI)
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001 - corrupt file never breaks chat
        logger.warning("Could not read %s: %s", SETTINGS_FILE, exc)
    _active_ai_cache = model
    return model


def set_active_ai(model: Any) -> str:
    """Persist the active engine ('Grok', 'Gemini' or 'Groq'); returns its name."""
    global _active_ai_cache
    name = _ENGINE_ALIASES.get(str(model or "").strip().lower(), DEFAULT_ACTIVE_AI)
    _active_ai_cache = name
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
            json.dump({"active_ai_model": name}, fh, indent=2)
    except Exception as exc:  # noqa: BLE001 - memory-only fallback
        logger.error("Could not persist bot settings: %s", exc)
    return name


def toggle_active_ai() -> str:
    """Cycle Grok -> Gemini -> Groq -> Grok (persisted); returns the new engine."""
    current = get_active_ai()
    try:
        idx = AI_ENGINES.index(current)
    except ValueError:
        idx = 0
    return set_active_ai(AI_ENGINES[(idx + 1) % len(AI_ENGINES)])

OFF_TOPIC_NOTE = (
    "Please note: I can only help with our store's products, prices and "
    "availability."
)


# --------------------------------------------------------------------------- #
# Reply-language preferences (menu: /language)
# --------------------------------------------------------------------------- #
LANGUAGES: Dict[str, str] = {
    "en": "English",
    "or": "Odia",
    "hi": "Hindi",
}
DEFAULT_LANGUAGE = "en"

LANG_INSTRUCTION: Dict[str, str] = {
    "en": "Reply ONLY in English, no matter which language the customer used.",
    "or": "Reply ONLY in Odia (ଓଡ଼ିଆ ଭାଷାରେ), no matter which language the customer used.",
    "hi": "Reply ONLY in Hindi written in the Devanagari script, no matter which language the customer used.",
}

LANG_CONFIRM: Dict[str, str] = {
    "en": "Language set to English. ✅",
    "or": "ଭାଷା ଓଡ଼ିଆରେ ସେଟ୍ ହେଲା। ✅",
    "hi": "भाषा हिन्दी में सेट हो गई। ✅",
}

LANG_BUTTONS = [("en", "English"), ("or", "ଓଡ଼ିଆ"), ("hi", "हिन्दी")]

LANG_CALLBACK_PREFIX = "lang_"
LANG_CALLBACK_PATTERN = r"^lang_(en|or|hi)$"

# telegram user id -> language code ("en" default, resets on restart)
_prefs: Dict[int, str] = {}


def normalize_language(code) -> Optional[str]:
    """Map any reasonable language input to 'en' / 'or' / 'hi' (or None)."""
    if code is None:
        return None
    key = str(code).strip().lower()
    if key in LANGUAGES:
        return key
    aliases = {
        "english": "en", "eng": "en",
        "odia": "or", "oriya": "or", "odi": "or", "ଓଡ଼ିଆ": "or",
        "hindi": "hi", "hin": "hi", "हिन्दी": "hi", "हिंदी": "hi",
    }
    return aliases.get(key)


def set_user_language(user_id, code) -> Optional[str]:
    """Save a customer's reply language; returns the code or None."""
    lang = normalize_language(code)
    if user_id is None or lang is None:
        return None
    _prefs[int(user_id)] = lang
    logger.info("Reply language for %s set to %s", user_id, lang)
    return lang


def get_user_language(user_id) -> str:
    """The customer's reply language ('en' by default)."""
    if user_id is None:
        return DEFAULT_LANGUAGE
    return _prefs.get(int(user_id), DEFAULT_LANGUAGE)

_cache: Dict[str, Any] = {"context": None, "ts": 0.0}
_model: Any = None


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def build_catalog_context(products: List[Dict[str, Any]]) -> str:
    """Render the sheet rows as a compact, model-friendly catalog text."""
    if not products:
        return "(The catalog is currently empty - no products listed yet.)"
    lines: List[str] = []
    for item in products[:MAX_CATALOG_LINES]:
        name = item.get("product_name") or "?"
        mrp = item.get("mrp") or "?"
        price = item.get("selling_price") or "?"
        stock_raw = str(item.get("stock_qty") or "0").strip()
        try:
            in_stock = float(stock_raw or 0) > 0
        except ValueError:
            in_stock = False
        status = "IN STOCK" if in_stock else "OUT OF STOCK"
        lines.append(
            f"- {name} | price Rs.{price} (MRP Rs.{mrp}) | {status} ({stock_raw} pcs)"
        )
    truncated = len(products) > MAX_CATALOG_LINES
    text = "\n".join(lines)
    if truncated:
        text += f"\n(...and {len(products) - MAX_CATALOG_LINES} more items)"
    return text


def get_catalog_context(force_refresh: bool = False) -> str:
    """Catalog text with a TTL cache (avoids one Sheets call per chat)."""
    now = time.time()
    if (
        not force_refresh
        and _cache["context"] is not None
        and now - float(_cache["ts"]) < CATALOG_TTL_SECONDS
    ):
        return str(_cache["context"])
    products = db.get_all_products()
    context = build_catalog_context(products)
    _cache["context"] = context
    _cache["ts"] = now
    return context


def clear_cache() -> None:
    """Reset the catalog cache (tests / after bulk edits)."""
    _cache["context"] = None
    _cache["ts"] = 0.0


def _get_model() -> Any:
    """Build (and cache) the Gemini model; heavy import happens here."""
    global _model
    if _model is not None:
        return _model
    api_key = _env("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set in .env")
    import google.generativeai as genai  # lazy: slow import

    genai.configure(api_key=api_key)
    model_name = _env("GEMINI_MODEL", DEFAULT_MODEL)
    _model = genai.GenerativeModel(
        model_name,
        system_instruction=SYSTEM_PROMPT,
    )
    return _model


def reset_model() -> None:
    """Drop the cached model (tests / after key changes)."""
    global _model
    _model = None


# --------------------------------------------------------------------------- #
# Per-customer chat memory (last CHAT_HISTORY_MAX messages, RAM only).
# The recent turns are injected into the prompt so the assistant can follow up
# ("what about the price?" uses the previous turn). Cleared on restart.
# --------------------------------------------------------------------------- #
CHAT_HISTORY_MAX = 10  # keep the last 10 messages (user + assistant) per customer

# telegram user id -> [{"role": "user"|"assistant", "content": str}, ...]
_chat_histories: Dict[int, List[Dict[str, str]]] = {}


def _remember(user_id, role: str, content: str) -> None:
    """Append one message to a customer's memory, keeping only the last N."""
    if user_id is None or not content:
        return
    try:
        key = int(user_id)
    except (TypeError, ValueError):
        return
    history = _chat_histories.setdefault(key, [])
    history.append({"role": role, "content": str(content)})
    if len(history) > CHAT_HISTORY_MAX:
        del history[: len(history) - CHAT_HISTORY_MAX]


def get_chat_history(user_id) -> List[Dict[str, str]]:
    """Copy of a customer's recent turns (empty list when none / unknown)."""
    if user_id is None:
        return []
    try:
        key = int(user_id)
    except (TypeError, ValueError):
        return []
    return list(_chat_histories.get(key, []))


def clear_chat_history(user_id=None) -> None:
    """Clear one customer's memory, or ALL memories when user_id is None."""
    if user_id is None:
        _chat_histories.clear()
        return
    try:
        _chat_histories.pop(int(user_id), None)
    except (TypeError, ValueError):
        pass


def format_chat_history(history) -> str:
    """Render recent turns as a plain 'Customer:/Assistant:' block."""
    lines: List[str] = []
    for msg in history or []:
        role = "Assistant" if str(msg.get("role", "")).lower() == "assistant" else "Customer"
        content = str(msg.get("content", "") or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _build_user_prompt(
    question: str,
    language: str,
    context: str,
    history_text: str = "",
) -> str:
    """Catalog + reply-language rule + recent chat + the question.

    The built prompt is IDENTICAL for Grok/Gemini/Groq; the optional recent
    conversation block lets the assistant resolve follow-up questions.
    """
    recent = ""
    if history_text:
        recent = (
            "RECENT CONVERSATION (use it to understand follow-up questions):\n"
            f"{history_text}\n\n"
        )
    return (
        "STORE CATALOG (live data from the shop's Google Sheet):\n"
        f"{context}\n\n"
        f"IMPORTANT: {LANG_INSTRUCTION[language]}\n\n"
        "Answer the customer's question using ONLY the catalog above.\n"
        "- Prices are in Indian Rupees (Rs.).\n"
        "- If a product is not listed, tell them it is not currently available.\n"
        "- If they ask something unrelated to the store, decline politely.\n\n"
        f"{recent}"
        f"CUSTOMER QUESTION: {question}"
    )


def _ask_gemini(user_prompt: str) -> str:
    """Google Gemini call (the original RAG path - behaviour unchanged)."""
    model = _get_model()
    response = model.generate_content(user_prompt)
    text = getattr(response, "text", "") or ""
    return text.strip()


def _ask_grok(user_prompt: str) -> str:
    """xAI Grok call via the OpenAI-compatible API.

    Sends the EXACT same SYSTEM_PROMPT as a system message and the EXACT
    same user prompt (catalog context + language + question) as Gemini gets.
    """
    api_key = _env("GROK_API_KEY")
    if not api_key:
        raise RuntimeError("GROK_API_KEY is not set in .env")
    from openai import OpenAI  # lazy: heavy import

    client = OpenAI(api_key=api_key, base_url=GROK_BASE_URL, timeout=30.0)
    model_name = _env("GROK_MODEL", DEFAULT_GROK_MODEL)
    last_error: Optional[Exception] = None
    for name in (model_name, "grok-beta"):
        try:
            response = client.chat.completions.create(
                model=name,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
            )
            text = response.choices[0].message.content or ""
            return text.strip()
        except Exception as exc:  # noqa: BLE001 - try the fallback model name
            last_error = exc
            logger.warning("Grok model %r failed: %s", name, exc)
    raise RuntimeError(f"Grok request failed: {last_error}")


# --------------------------------------------------------------------------- #
# Groq (fast hosted Llama/Qwen/Gemma/Mixtral) via the official ``groq`` SDK
# --------------------------------------------------------------------------- #
_groq_client: Any = None  # cached Groq client (heavy import happens once)
_groq_model: Optional[str] = None  # cached auto-detected chat model


def _get_groq_client() -> Any:
    """Build (and cache) the Groq client; heavy import happens here."""
    global _groq_client
    if _groq_client is not None:
        return _groq_client
    api_key = _env("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not set in .env")
    from groq import Groq  # lazy: heavy import

    _groq_client = Groq(api_key=api_key)
    return _groq_client


def _detect_groq_model(client: Any) -> str:
    """Pick a Groq chat model: GROQ_MODEL override, else auto-detect.

    Mirrors the admin's own script: prefer the first non-Whisper/non-guard
    Llama/Qwen/Gemma/Mixtral model the account exposes; fall back to the
    first listed model, then to DEFAULT_GROQ_MODEL when nothing resolves.
    """
    global _groq_model
    override = _env("GROQ_MODEL").strip()
    if override:
        return override
    if _groq_model:
        return _groq_model
    model = DEFAULT_GROQ_MODEL
    try:
        models = client.models.list()
        for item in getattr(models, "data", []) or []:
            mid = str(getattr(item, "id", "") or "")
            if not mid or "whisper" in mid or "guard" in mid:
                continue
            if any(tag in mid for tag in ("llama", "qwen", "gemma", "mixtral")):
                model = mid
                break
        else:
            data = getattr(models, "data", []) or []
            if data:
                model = str(getattr(data[0], "id", "") or model)
    except Exception as exc:  # noqa: BLE001 - detection must never break chat
        logger.debug("Could not auto-detect a Groq model: %s", exc)
    _groq_model = model
    return model


def _ask_groq(user_prompt: str) -> str:
    """Groq call.

    Sends the EXACT same SYSTEM_PROMPT as a system message and the EXACT
    same user prompt (catalog context + language + question) as Gemini/Grok.
    ``max_tokens`` is capped so a long reply can never trip rate limits.
    """
    client = _get_groq_client()
    model_name = _detect_groq_model(client)
    response = client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=800,
    )
    text = response.choices[0].message.content or ""
    return text.strip()


def ask(
    question: str,
    lang: str = DEFAULT_LANGUAGE,
    catalog_context: str | None = None,
    model: str | None = None,
    history: List[Dict[str, str]] | None = None,
) -> str:
    """Unified AI call: routes to the active engine (Grok default / Gemini / Groq).

    The customer may ask in ANY language, but the reply is ALWAYS in the
    user's set language (English by default). ``model`` forces one engine
    (used for the cross-engine fallback in :func:`answer`). ``history`` is the
    customer's recent turns (see :func:`get_chat_history`) injected for context.
    """
    language = normalize_language(lang) or DEFAULT_LANGUAGE
    context = (
        catalog_context if catalog_context is not None else get_catalog_context()
    )
    user_prompt = _build_user_prompt(
        question, language, context, format_chat_history(history)
    )
    active = model or get_active_ai()
    if active == "Gemini":
        return _ask_gemini(user_prompt)
    if active == "Groq":
        return _ask_groq(user_prompt)
    return _ask_grok(user_prompt)


# --------------------------------------------------------------------------- #
# Engine health (circuit breaker): skip an engine that just failed fatally
# (out of credits / bad key) for a cool-down window instead of burning a slow
# network round-trip on EVERY message. Keeps replies fast when one engine is
# down; the engine is retried automatically after the cool-down expires.
# --------------------------------------------------------------------------- #
ENGINE_COOLDOWN_SECONDS = 5 * 60
_ENGINE_HEALTH: Dict[str, float] = {}  # engine name -> time.time() to retry at
_FATAL_MARKERS = (
    "permission-denied", "permission denied", "unauthorized", "unauthorised",
    "invalid api key", "incorrect api key", "invalid_api_key", "api key not",
    "used all available credits", "monthly spending limit", "credit balance",
    "insufficient_quota", "invalid authentication", "billing",
)


def _error_is_fatal(exc: Exception) -> bool:
    """True for auth/credit failures that retrying the SAME engine won't fix."""
    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    if status in (401, 403, 404):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _FATAL_MARKERS)


def _engine_is_down(name: str) -> bool:
    until = _ENGINE_HEALTH.get(name)
    return bool(until) and until > time.time()


def _mark_engine_down(name: str) -> None:
    _ENGINE_HEALTH[name] = time.time() + ENGINE_COOLDOWN_SECONDS


def _mark_engine_ok(name: str) -> None:
    _ENGINE_HEALTH.pop(name, None)


def _engine_candidates(active: str) -> List[str]:
    """Engines to try this turn (active first), skipping any currently down."""
    order = [active] + [n for n in AI_ENGINES if n != active]
    healthy = [n for n in order if not _engine_is_down(n)]
    return healthy or list(order)  # if all are down, still try them all


async def answer(
    question: str,
    lang: str = DEFAULT_LANGUAGE,
    user_id: Optional[int] = None,
) -> str:
    """Async wrapper for PTB handlers (runs the blocking call in a thread).

    When ``user_id`` is given, the customer's last CHAT_HISTORY_MAX messages
    are injected into the prompt (so follow-ups work) and each successful
    exchange is remembered for the next turn.

    Speed: engines that just failed fatally are skipped for a cool-down window
    (see ``_engine_candidates``) so a dead engine (e.g. out of credits) never
    slows down every reply. Safeguard: the remaining engines are tried in turn
    before the customer ever sees an apology.
    """
    question = str(question or "").strip()
    if not question:
        return "Please type your question about our products. 🙂"
    history = get_chat_history(user_id) if user_id is not None else None
    active = get_active_ai()
    reply: str | None = None
    last_error: Exception | None = None
    for engine in _engine_candidates(active):
        try:
            reply = await asyncio.to_thread(ask, question, lang, None, engine, history)
            _mark_engine_ok(engine)
            break
        except Exception as exc:  # noqa: BLE001 - quota, network, bad key...
            last_error = exc
            logger.error("%s request failed: %s", engine, exc)
            if _error_is_fatal(exc):
                _mark_engine_down(engine)
    if reply is None:
        kind = type(last_error).__name__ if last_error else "Error"
        return (
            "Sorry, the assistant is temporarily unavailable. "
            f"({kind}) Please try again in a minute."
        )
    # Remember this exchange so the next message can refer back to it.
    if user_id is not None:
        _remember(user_id, "user", question)
        _remember(user_id, "assistant", reply)
    return reply


# --------------------------------------------------------------------------- #
# Conversational ordering (Feature 3): local intent -> summary -> Confirm
# --------------------------------------------------------------------------- #
_ORDER_HINT_WORDS = (
    "need want buy order get give bring take add cart bottle packet "
    "ଦରକାର ଦିଅ ନେବି କିଣିବି ଅର୍ଡର ପଠାଅ"
)


def _looks_like_order(text: str) -> bool:
    lowered = str(text or "").lower()
    import re as _re

    if _re.search(r"\d", lowered):
        return True
    return any(word in lowered for word in _ORDER_HINT_WORDS.split())


# --------------------------------------------------------------------------- #
# AI cart management: customers can add / add-more / remove / view / complete
# the cart right in chat - no LLM call, so it is instant and reliable. The cart
# lives in context.user_data["cart"] = {ean: qty}, the SAME structure the
# storefront and checkout use, so a cart built here works everywhere.
# --------------------------------------------------------------------------- #
_CART_REMOVE_WORDS = (
    "remove", "delete", "drop", "minus", "without", "cancel item",
)
_CART_DONE_WORDS = (
    "complete cart", "complete order", "checkout", "check out", "place order",
    "finish order", "proceed", "confirm order", "done", "complete",
)
_CART_VIEW_WORDS = (
    "show cart", "view cart", "my cart", "cart", "basket",
    "what's in my cart", "what is in my cart",
)
_CART_ADD_VERBS = (
    "add", "need", "want", "buy", "order", "get", "give", "bring", "take", "include",
)
_CART_FILLER_PHRASES = (
    "to my cart", "to the cart", "in my cart", "in the cart",
    "from my cart", "from the cart", "to cart", "in cart", "from cart", "cart",
)


def _get_cart(context) -> Dict[str, int]:
    """Per-customer cart {ean: qty} in user_data (shared with the storefront)."""
    try:
        cart = context.user_data.get("cart")
        if not isinstance(cart, dict):
            cart = {}
            context.user_data["cart"] = cart
        return cart
    except Exception:  # noqa: BLE001 - fakes without user_data
        return {}


async def _safe_reply(message, text: str) -> None:
    try:
        await message.reply_text(text)
    except Exception:  # noqa: BLE001 - stale/edited messages
        pass


def _strip_first(text: str, words) -> str:
    """Remove the first action verb so only the product part remains."""
    lowered = text.lower()
    for w in words:
        i = lowered.find(w)
        if i >= 0:
            return (text[:i] + " " + text[i + len(w):]).strip()
    return text


def _strip_cart_filler(text: str) -> str:
    """Drop cart phrases so only 'qty product' reaches the order parser."""
    lowered = text.lower()
    for phrase in _CART_FILLER_PHRASES:
        i = lowered.find(phrase)
        while i >= 0:
            text = (text[:i] + " " + text[i + len(phrase):])
            lowered = text.lower()
            i = lowered.find(phrase)
    return text.strip()


async def _reply_cart(message, context, note: str = "") -> None:
    """Send the itemised cart + a short hint for the next action."""
    import orders as _orders

    cart = _get_cart(context)
    if not cart:
        await _safe_reply(message, (note + "\n" if note else "") + "Your cart is empty.")
        return
    try:
        catalog = await asyncio.to_thread(db.get_all_products)
    except Exception:  # noqa: BLE001
        catalog = []
    lines, _missing = _orders.resolve_products(cart, catalog)
    body = _orders.bill_text(lines)
    count = sum(int(q or 0) for q in cart.values())
    text = (
        (note + "\n" if note else "")
        + "Your cart:\n"
        + body
        + f"\n\nItems: {count}"
        + "\n\nType another product to add more, say 'remove <item>' to delete,"
        + "\nor type 'complete cart' to checkout."
    )
    await _safe_reply(message, text)


async def try_handle_cart_intent(message, context, lang: str = DEFAULT_LANGUAGE) -> bool:
    """AI cart management: add / add-more / remove / view / complete the cart.

    Runs locally (no LLM call) so it is instant + reliable. Returns True when
    the message was handled as a cart action (otherwise False -> AI answer).
    """
    import orders as _orders

    text = str(message.text or "").strip()
    if not text:
        return False
    lowered = text.lower()
    cart = _get_cart(context)

    # 1) remove / delete an item from the cart ------------------------------
    if any(w in lowered for w in _CART_REMOVE_WORDS):
        parsed = _orders.parse_order_lines(
            _strip_cart_filler(_strip_first(text, _CART_REMOVE_WORDS))
        )
        if parsed:
            try:
                catalog = await asyncio.to_thread(db.get_all_products)
            except Exception as exc:  # noqa: BLE001
                logger.error("Cart catalog read failed: %s", exc)
                catalog = []
            matched, _missing = _orders.match_catalog(parsed, catalog)
            removed = []
            for line in matched:
                ean = str(line["ean"])
                if ean in cart:
                    cart[ean] = max(0, cart[ean] - int(line["qty"]))
                    if cart[ean] == 0:
                        cart.pop(ean, None)
                    removed.append(line["name"])
            if removed:
                context.user_data["cart"] = cart
                await _reply_cart(message, context, "Removed: " + ", ".join(removed) + ".")
                return True

    # 2) complete / checkout -------------------------------------------------
    if cart and any(w in lowered for w in _CART_DONE_WORDS):
        try:
            import storefront as _sf

            context.user_data["checkout_channel"] = "ai"
            context.user_data["addr_collect"] = {"channel": "ai", "after": "ai_finish"}
            user = getattr(message, "from_user", None)
            if user is not None and getattr(user, "id", None):
                context.user_data["_uid"] = int(user.id)
            await _sf.ask_address_choice(
                message.chat_id, context, message=message
            )
        except Exception:  # noqa: BLE001 - fall back to legacy prompt
            context.user_data["checkout_channel"] = "ai"
            context.user_data["state"] = _orders.STATE_CHECKOUT_ADDRESS
            await _safe_reply(
                message,
                "Great, your cart is ready!\n\nPlease send your delivery address:",
            )
        return True

    # 3) view the cart (no digit + no add-verb -> a pure cart query) --------
    if (
        any(w in lowered for w in _CART_VIEW_WORDS)
        and not any(ch.isdigit() for ch in text)
        and not any(v in lowered for v in _CART_ADD_VERBS)
    ):
        if cart:
            await _reply_cart(message, context, "Your cart:")
        else:
            await _safe_reply(
                message,
                "Your cart is empty. Tell me what you'd like, e.g. '2 Fortune oil'.",
            )
        return True

    # 4) add / add another product to the cart --------------------------------
    if _looks_like_order(text):
        parsed = _orders.parse_order_lines(_strip_cart_filler(text))
        if parsed:
            try:
                catalog = await asyncio.to_thread(db.get_all_products)
            except Exception as exc:  # noqa: BLE001
                logger.error("Cart catalog read failed: %s", exc)
                return False
            matched, missing = _orders.match_catalog(parsed, catalog)
            if not matched:
                await _safe_reply(
                    message,
                    "I could not find that in our store. "
                    "Please check the name or tap Browse Store.",
                )
                return True
            added = []
            for line in matched:
                ean = str(line["ean"])
                want = int(cart.get(ean, 0)) + int(line["qty"])
                cap = max(1, int(line["available"] or 1))
                cart[ean] = max(1, min(want, cap))
                added.append(f"{line['name']} x{line['qty']}")
            context.user_data["cart"] = cart
            note = "Added: " + ", ".join(added) + "."
            if missing:
                note += "\nNot found: " + ", ".join(missing) + "."
            await _reply_cart(message, context, note)
            return True
    return False


async def try_handle_order_intent(message, context, lang: str = DEFAULT_LANGUAGE) -> bool:
    """Resolve "I need 2 X and 1 Y" locally; reply with a Confirm summary.

    Returns True when the message was an order (summary sent or a
    no-match notice sent), False when it is plain chit-chat for Gemini.
    """
    import orders as _orders

    text = str(message.text or "").strip()
    if not text or not _looks_like_order(text):
        return False
    try:
        parsed = _orders.parse_order_lines(text)
    except Exception:  # noqa: BLE001
        return False
    if not parsed:
        return False
    try:
        catalog = await asyncio.to_thread(db.get_all_products)
    except Exception as exc:  # noqa: BLE001 - sheet/network failure
        logger.error("AI order catalog read failed: %s", exc)
        return False
    matched, missing = _orders.match_catalog(parsed, catalog)
    if not matched:
        await message.reply_text(
            "😕 I could not find those items in our store. "
            "Please check the name or browse the store with 📦 Browse Store."
        )
        return True
    # clamp to live stock for an honest summary
    short: list = []
    for line in matched:
        if line["qty"] > line["available"]:
            short.append(line["name"])
            line["qty"] = max(1, line["available"])
            line["line_total"] = line["price"] * line["qty"]
    summary_lines = [
        f"{i}. {line['name']} x{line['qty']} - Rs.{_orders.format_money(line['line_total'])}"
        for i, line in enumerate(matched, start=1)
    ]
    total = sum(float(line["line_total"] or 0) for line in matched)
    reply = (
        f"🧾 <b>Found {len(matched)} item(s):</b>\n"
        + "\n".join(summary_lines)
        + f"\n<b>Total: Rs.{_orders.format_money(total)}</b>"
    )
    if missing:
        reply += f"\n\n<i>Not found: {', '.join(missing)}</i>"
    if short:
        reply += "\n<i>Quantity adjusted to available stock.</i>"
    reply += "\n\nReply <b>'Confirm'</b> to place the order."
    try:
        context.user_data["ai_pending_order"] = {
            "items": [{"ean": m["ean"], "qty": m["qty"]} for m in matched],
            "lang": lang,
        }
        context.user_data["state"] = "AI_ORDER_CONFIRM"
    except Exception:  # noqa: BLE001 - fakes without user_data
        pass
    try:
        from telegram.constants import ParseMode as _ParseMode

        await message.reply_text(reply, parse_mode=_ParseMode.HTML)
    except Exception:  # noqa: BLE001
        await message.reply_text(reply)
    return True


async def _confirm_ai_order(message, context, lang: str = DEFAULT_LANGUAGE) -> None:
    """'Confirm' received: run address -> phone -> shared checkout."""
    import orders as _orders

    try:
        pending = context.user_data.get("ai_pending_order") or {}
    except Exception:  # noqa: BLE001
        pending = {}
    items = pending.get("items") if isinstance(pending, dict) else None
    if not items:
        try:
            context.user_data["state"] = None
        except Exception:  # noqa: BLE001
            pass
        await message.reply_text("⌛ That order expired. Please tell me what you need again!")
        return
    try:
        context.user_data["cart"] = {
            str(it.get("ean")): int(it.get("qty") or 1) for it in items
        }
        context.user_data["checkout_channel"] = "ai"
        context.user_data["addr_collect"] = {"channel": "ai", "after": "ai_finish"}
        user2 = getattr(message, "from_user", None)
        if user2 is not None and getattr(user2, "id", None):
            context.user_data["_uid"] = int(user2.id)
        try:
            import storefront as _sf2

            await _sf2.ask_address_choice(message.chat_id, context, message=message)
        except Exception:  # noqa: BLE001 - fall back to legacy prompt
            context.user_data["state"] = _orders.STATE_CHECKOUT_ADDRESS
            raise
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        from telegram.constants import ParseMode as _ParseMode

        await message.reply_text(
            "✅ <b>Order confirmed!</b>\n\n📍 Please send your <b>delivery address</b>:",
            parse_mode=_ParseMode.HTML,
        )
    except Exception:  # noqa: BLE001
        await message.reply_text("Order confirmed! Please send your delivery address:")


async def _ai_finish_order(src, context) -> None:
    """Place the AI-mode order once the address has been collected.

    ``src`` is either a CallbackQuery (button resume) or a Message (text/location
    collect). Normalizes so ``message.from_user`` is the CUSTOMER (a query's
    ``.message`` was sent by the bot, which would otherwise store the wrong
    Telegram user id for later status updates), then runs the shared
    ``storefront.finish_checkout`` (bill -> live status message).
    """
    import storefront as _sf

    query = src if getattr(src, "data", None) is not None else None
    if query is not None:
        message = getattr(query, "message", None)
        customer = getattr(query, "from_user", None)
    else:
        message = src
        customer = getattr(src, "from_user", None)
    if message is None:
        logger.error("_ai_finish_order: no message to reply to")
        return
    if customer is not None:
        try:
            message.from_user = customer
        except Exception:  # noqa: BLE001 - best effort; _uid is the fallback
            pass
    try:
        await _sf.finish_checkout(message, context)
    except Exception as exc:  # noqa: BLE001 - never leave the customer hanging
        logger.error("AI finish checkout crashed: %s", exc, exc_info=True)
        try:
            await message.reply_text("Could not place the order. Please try again.")
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- #
# Telegram handler (customers talk to the shop assistant here)
# --------------------------------------------------------------------------- #
async def handle_customer_message(update, context) -> None:
    """Answer any non-command text message with the RAG assistant.

    Order intent (e.g. "I need 2 Fortune oils") is resolved locally against
    the live catalog first: the customer gets an order summary and replies
    'Confirm' to place it via the shared checkout path. Anything else falls
    through to Gemini.
    """
    message = update.effective_message
    if message is None or not message.text or message.text.startswith("/"):
        return
    # Skip when the Admin Panel (group -1) already handled this message as a
    # search keyword / edit value - the admin must not also get an AI reply.
    # The same guard marks checkout / search text the storefront consumed.
    try:
        from admin_handler import _was_consumed

        if _was_consumed(message.message_id):
            return
    except Exception:  # noqa: BLE001 - guard must never break the assistant
        pass
    state = ""
    try:
        state = str(context.user_data.get("state") or "")
    except Exception:  # noqa: BLE001 - fakes without user_data
        state = ""
    if state in ("CHECKOUT_ADDRESS", "CHECKOUT_PHONE", "CHECKOUT_LOCATION"):
        # The storefront text handler is shadowed in group -1 (the admin panel
        # text handler matches every message first), so the address / phone
        # steps must be driven from here - this is where customer text lands.
        try:
            import storefront

            await storefront.handle_checkout_text(update, context)
        except Exception:  # noqa: BLE001 - guard must never break chat
            logger.error("Checkout text delegation crashed", exc_info=True)
        return

    # Storefront search session: the customer tapped 🔍 Search and their next
    # message is a keyword. Customer text lands here (the storefront text
    # handler is shadowed in group -1), so delegate to the storefront searcher
    # and never fall through to the AI assistant.
    try:
        import storefront

        if state == storefront.STATE_SEARCH:
            await storefront.handle_search_text(update, context)
            return
    except Exception:  # noqa: BLE001 - guard must never break chat
        pass

    # Saved-address collect steps (name/address/phone/location) and the
    # use-saved/new-choice lived inside storefront - delegate here too since
    # customer text actually lands in this handler (group -1 shadowing).
    try:
        import storefront as _sf_addr

        if state in (
            _sf_addr.STATE_ADDR_CHOOSE, _sf_addr.STATE_ADDR_NAME,
            _sf_addr.STATE_ADDR_TEXT, _sf_addr.STATE_ADDR_PHONE,
            _sf_addr.STATE_ADDR_LOC,
        ):
            if await _sf_addr.handle_address_text(update, context):
                return
    except Exception:  # noqa: BLE001 - guard must never break chat
        pass

    user = update.effective_user
    lang = get_user_language(user.id if user else None)
    text = (message.text or "").strip()

    # --- pending AI order: Confirm / Cancel --------------------------------
    if state == "AI_ORDER_CONFIRM":
        lowered = text.lower()
        if lowered in CANCEL_WORDS or (
            len(lowered.split()) == 1 and lowered in ("no", "n")
        ):
            try:
                context.user_data["state"] = None
                context.user_data.pop("ai_pending_order", None)
            except Exception:  # noqa: BLE001
                pass
            await message.reply_text("❌ Order cancelled. Tell me what you need anytime! 🙂")
            return
        if lowered in CONFIRM_WORDS or (
            len(lowered.split()) <= 2 and "confirm" in lowered
        ):
            await _confirm_ai_order(message, context, lang)
            return
        # anything else while an order is pending -> fall through to Gemini
        # (but keep the pending order alive for a later "confirm")

    # --- AI cart management (local, no LLM call): add / remove / view / complete
    try:
        handled = await try_handle_cart_intent(message, context, lang)
    except Exception as exc:  # noqa: BLE001 - cart parser must never break chat
        logger.error("Cart-intent parse crashed: %s", exc)
        handled = False
    if handled:
        return

    # Store mode: browse/cart/search/direct-chat only - never the AI reply.
    # The mode toggle lives on /start; default stays AI.
    try:
        import storefront as _sf_mode

        if _sf_mode.get_chat_mode(context) == _sf_mode.CHAT_MODE_STORE:
            await message.reply_text(
                "🏬 <b>Store mode.</b>\n\n"
                "Use 📦 Browse Store, 🔍 Search, 🛒 Cart, or type a product name "
                "(e.g. '2 Fortune oil'). Tap the chat-mode button on /start for 🤖 AI.",
                parse_mode="HTML",
            )
            return
    except Exception:  # noqa: BLE001 - a guard must never silence a reply
        pass

    status = await message.reply_text("🤔 Thinking...")
    try:
        reply = await answer(
            message.text, lang, user_id=(user.id if user else None)
        )
    except Exception as exc:  # noqa: BLE001 - last-resort safety net
        logger.error("AI handler crashed: %s", exc)
        reply = "Sorry, something went wrong. Please try again."
    try:
        await status.edit_text(reply)
    except Exception:
        await message.reply_text(reply)


# --------------------------------------------------------------------------- #
# /clearhistory - wipe the caller's in-memory chat memory
# --------------------------------------------------------------------------- #
async def cmd_clearhistory(update, context) -> None:
    """Reset the caller's recent-chat memory so the next chat starts fresh."""
    message = update.effective_message
    user = update.effective_user
    if message is None:
        return
    clear_chat_history(user.id if user else None)
    await message.reply_text("🧹 Chat history cleared. Let's start fresh!")


# --------------------------------------------------------------------------- #
# Language menu: /language command + inline buttons
# --------------------------------------------------------------------------- #
async def cmd_language(update, context) -> None:
    """Show the 3-button language picker (English / ଓଡ଼ିଆ / हिन्दी)."""
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        "🌐 Choose your reply language / ଭାଷା ବାଛନ୍ତୁ / भाषा चुनें:",
        reply_markup=_language_keyboard(),
    )


async def handle_language_callback(update, context) -> None:
    """Save the tapped language and confirm in THAT language."""
    query = update.callback_query
    if query is None or not query.data:
        return
    user = query.from_user
    try:
        lang = normalize_language(query.data[len(LANG_CALLBACK_PREFIX):])
    except Exception:  # noqa: BLE001 - malformed data
        lang = None
    if lang is None:
        try:
            await query.answer()
        except Exception:  # noqa: BLE001 - stale query, nothing to do
            pass
        return
    set_user_language(user.id if user else None, lang)
    try:
        await query.answer()
    except Exception:  # noqa: BLE001 - stale query, nothing to do
        pass
    await _safe_edit_language(query, lang)


def _language_keyboard():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f"{LANG_CALLBACK_PREFIX}{code}",
                )
                for code, label in LANG_BUTTONS
            ]
        ]
    )


async def _safe_edit_language(query, lang: str) -> None:
    """Confirm the choice (in the new language); never crash the flow."""
    inner_message = getattr(query, "message", None)
    if inner_message is None or not hasattr(inner_message, "edit_text"):
        return
    try:
        await inner_message.edit_text(
            LANG_CONFIRM[lang], reply_markup=_language_keyboard()
        )
    except Exception as exc:  # noqa: BLE001 - old/inaccessible message
        logger.debug("Could not edit language message: %s", exc)


def register(application) -> None:
    """Attach the customer AI handler (register AFTER admin handlers)."""
    from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters

    application.add_handler(CommandHandler("language", cmd_language))
    application.add_handler(CommandHandler("clearhistory", cmd_clearhistory))
    application.add_handler(
        CallbackQueryHandler(handle_language_callback, pattern=LANG_CALLBACK_PATTERN)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_customer_message)
    )


__all__ = [
    "AI_ENGINES",
    "CANCEL_WORDS",
    "CHAT_HISTORY_MAX",
    "CONFIRM_WORDS",
    "DEFAULT_LANGUAGE",
    "LANG_BUTTONS",
    "LANG_CALLBACK_PATTERN",
    "LANG_CALLBACK_PREFIX",
    "LANG_CONFIRM",
    "LANG_INSTRUCTION",
    "LANGUAGES",
    "OFF_TOPIC_NOTE",
    "SYSTEM_PROMPT",
    "_ai_finish_order",
    "_confirm_ai_order",
    "answer",
    "ask",
    "build_catalog_context",
    "clear_cache",
    "clear_chat_history",
    "cmd_clearhistory",
    "cmd_language",
    "format_chat_history",
    "get_active_ai",
    "get_catalog_context",
    "get_chat_history",
    "get_user_language",
    "handle_customer_message",
    "handle_language_callback",
    "normalize_language",
    "register",
    "reset_model",
    "set_active_ai",
    "set_user_language",
    "toggle_active_ai",
    "try_handle_cart_intent",
    "try_handle_order_intent",
]

