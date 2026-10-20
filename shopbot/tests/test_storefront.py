"""Validation for storefront keyboard construction (no network, no Telegram).

Regression guard for the crash where ``InlineKeyboardMarkup.inline_keyboard``
is a tuple (python-telegram-bot v20+) and was concatenated with a list:

    TypeError: can only concatenate tuple (not "list") to tuple

Run with:  python tests/test_storefront.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storefront  # noqa: E402
from telegram import InlineKeyboardMarkup  # noqa: E402

failures = 0
total = 0


def check(name: str, condition: bool) -> None:
    global failures, total
    total += 1
    print(("PASS" if condition else "FAIL"), "-", name)
    if not condition:
        failures += 1


# --- 1. _cart_bar returns a list -----------------------------------------
bar = storefront._cart_bar({"123": 2})
check("_cart_bar returns list", isinstance(bar, list))
check("_cart_bar rows are lists", all(isinstance(r, list) for r in bar))

# --- 2. _card_keyboard(...).inline_keyboard type -------------------------
card = storefront._card_keyboard("123", 0)
rows = card.inline_keyboard
check(
    "inline_keyboard is tuple or list (both legal)",
    isinstance(rows, (tuple, list)),
)

# --- 3. THE BUG: merging card rows + cart bar must not raise -------------
try:
    merged = list(rows) + storefront._cart_bar({"123": 0})
    check("list(inline_keyboard) + _cart_bar does not raise", True)
except TypeError as exc:
    check(f"list(inline_keyboard) + _cart_bar does not raise ({exc})", False)
    merged = None

if merged is not None:
    check("merged is a list", isinstance(merged, list))
    try:
        markup = InlineKeyboardMarkup(merged)
        check("InlineKeyboardMarkup(merged) builds", True)
        check(
            "markup has all rows",
            len(markup.inline_keyboard) == len(merged),
        )
    except Exception as exc:  # noqa: BLE001
        check(f"InlineKeyboardMarkup(merged) builds ({exc})", False)

# --- 4. Simulate the exact expression from show_category_products --------
cart = {"123": 1}
try:
    keyboard = InlineKeyboardMarkup(
        list(storefront._card_keyboard("123", cart.get("123", 0)).inline_keyboard)
        + storefront._cart_bar(cart)
    )
    check("product-card keyboard builds (fixed expression)", True)
    check(
        "card keyboard includes View Cart row",
        any(
            b.callback_data == storefront.CB_CART
            for row in keyboard.inline_keyboard
            for b in row
        ),
    )
except TypeError as exc:
    check(f"product-card keyboard builds (fixed expression) ({exc})", False)

# --- 5. In-cart variant shows stepper + cart bar -------------------------
try:
    keyboard2 = InlineKeyboardMarkup(
        list(storefront._card_keyboard("123", 3).inline_keyboard)
        + storefront._cart_bar({"123": 3})
    )
    check("in-cart card keyboard builds", True)
    check(
        "in-cart keyboard has qty button",
        any(b.text == "3" for row in keyboard2.inline_keyboard for b in row),
    )
except TypeError as exc:
    check(f"in-cart card keyboard builds ({exc})", False)

# --- 6. Regression: raw (unfixed) expression really does raise -----------
try:
    InlineKeyboardMarkup(card.inline_keyboard + storefront._cart_bar({}))
    # Some PTB builds may allow tuple + tuple; only assert when it's a tuple
    check(
        "raw expression raises TypeError (documents the bug)",
        not isinstance(card.inline_keyboard, tuple),
    )
except TypeError:
    check("raw expression raises TypeError (documents the bug)", True)

# --- 7. Search button on /start ------------------------------------------
import asyncio  # noqa: E402
import re as _re  # noqa: E402

start_kb = storefront.start_keyboard()
start_cbs = [b.callback_data for row in start_kb.inline_keyboard for b in row]
check("/start keyboard has Search button", storefront.CB_SEARCH in start_cbs)
check(
    "/start Search row sits right after Browse Store",
    len(start_kb.inline_keyboard) >= 2
    and start_kb.inline_keyboard[1][0].callback_data == storefront.CB_SEARCH,
)

_pat = _re.compile(storefront.CALLBACK_PATTERN)
check("pattern matches shop_search", bool(_pat.match(storefront.CB_SEARCH)))
check("pattern matches shop_search_done", bool(_pat.match(storefront.CB_SEARCH_DONE)))


# --- fakes for the interactive search flow --------------------------------
class FUser:
    def __init__(self, uid):
        self.id = uid
        self.full_name = "Tester"
        self.first_name = "Tester"
        self.username = None


class FMsg:
    def __init__(self, text="", chat_id=5, message_id=1, user=None):
        self.text = text
        self.chat_id = chat_id
        self.message_id = message_id
        self.from_user = user
        self.replies = []
        self.edits = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, dict(kwargs)))
        return FMsg(chat_id=self.chat_id, message_id=self.message_id + 1000)

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, dict(kwargs)))
        return self


class FQuery:
    def __init__(self, data, message, user=None):
        self.data = data
        self.message = message
        self.from_user = user
        self.answers = []

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.answers.append((text, show_alert))


class FBot:
    def __init__(self):
        self.sent = []

    async def send_photo(self, **kwargs):
        self.sent.append(dict(kwargs, __kind="photo"))

    async def send_message(self, **kwargs):
        self.sent.append(dict(kwargs, __kind="message"))


class FCtx:
    def __init__(self):
        self.user_data = {}
        self.bot = FBot()


class FUpd:
    def __init__(self, message=None, query=None, user=None):
        self.effective_message = message
        self.message = message
        self.callback_query = query
        self.effective_user = user


# --- 8. prompt_search arms the state + shows the cancel button ------------
q = FQuery(storefront.CB_SEARCH, FMsg(text=""))
ctx = FCtx()
asyncio.run(storefront.prompt_search(q, ctx))
check("prompt_search arms STATE_SEARCH",
      ctx.user_data.get("state") == storefront.STATE_SEARCH)
check("prompt_search answered the query", bool(q.answers))
prompt_markup = q.message.edits[0][1].get("reply_markup") if q.message.edits else None
prompt_cbs = (
    [b.callback_data for row in prompt_markup.inline_keyboard for b in row]
    if prompt_markup else []
)
check("prompt has Cancel (shop_search_done)", storefront.CB_SEARCH_DONE in prompt_cbs)
check("prompt has Browse by Category", storefront.CB_CATEGORIES in prompt_cbs)

# --- 9. handle_search_text -> DB search -> cards with Add to Cart ---------
rows = [
    {"product_name": "Fortune Oil 1L", "ean": "111", "selling_price": 170,
     "mrp": 210, "stock_qty": "5", "image_url": "https://img/oil.jpg",
     "category": "General"},
    {"product_name": "Tata Salt 1kg", "ean": "222", "selling_price": 25,
     "mrp": 28, "stock_qty": "9", "image_url": "", "category": "Grocery"},
]
orig_search = storefront.db.search_products
seen = {}


def fake_search(keyword, limit=25):
    seen["keyword"] = keyword
    needle = str(keyword).lower()
    return [dict(r) for r in rows if needle in r["product_name"].lower()]


storefront.db.search_products = fake_search
try:
    msg = FMsg(text="fortune", chat_id=5, message_id=424242, user=FUser(1))
    ctx2 = FCtx()
    ctx2.user_data["state"] = storefront.STATE_SEARCH
    consumed = asyncio.run(
        storefront.handle_search_text(FUpd(message=msg, user=FUser(1)), ctx2)
    )
    check("handle_search_text consumed keyword", consumed is True)
    check("customer search queried our DB", seen.get("keyword") == "fortune")
    check("search session stays alive",
          ctx2.user_data.get("state") == storefront.STATE_SEARCH)
    photos = [s for s in ctx2.bot.sent if s.get("__kind") == "photo"]
    check("one matching photo card sent",
          len(photos) == 1
          and "fortune" in (photos[0].get("caption") or "").lower())
    card_cbs = (
        [b.callback_data for row in photos[0]["reply_markup"].inline_keyboard
         for b in row]
        if photos else []
    )
    check("search card has Add to Cart button",
          f"{storefront.CB_ADD_PREFIX}111" in card_cbs)
    check("search card has View Cart bar", storefront.CB_CART in card_cbs)
    check("non-matching product excluded",
          not any("Tata Salt" in (s.get("caption") or s.get("text") or "")
                  for s in ctx2.bot.sent))
    # No-state text is left for the AI assistant.
    ctx3 = FCtx()
    check("returns False without search state",
          asyncio.run(storefront.handle_search_text(
              FUpd(message=FMsg(text="hello", message_id=9998)), ctx3)) is False)
finally:
    storefront.db.search_products = orig_search

# --- 10. Router: shop_search / shop_search_done --------------------------
orig_cats = storefront.show_categories
called = {}


async def spy_cats(query, context):
    called["yes"] = True


storefront.show_categories = spy_cats
try:
    ctx4 = FCtx()
    ctx4.user_data["state"] = storefront.STATE_SEARCH
    q4 = FQuery(storefront.CB_SEARCH_DONE, FMsg(text=""))
    asyncio.run(storefront.handle_store_callback(FUpd(query=q4), ctx4))
    check("search_done clears the state", "state" not in ctx4.user_data)
    check("search_done returns to categories", called.get("yes") is True)

    ctx5 = FCtx()
    q5 = FQuery(storefront.CB_SEARCH, FMsg(text=""))
    asyncio.run(storefront.handle_store_callback(FUpd(query=q5), ctx5))
    check("router shop_search arms the prompt",
          ctx5.user_data.get("state") == storefront.STATE_SEARCH)
finally:
    storefront.show_categories = orig_cats

# --- 11. ai_agent delegates search + checkout text ------------------------
import ai_agent  # noqa: E402

orig_hst = storefront.handle_search_text
orig_hct = storefront.handle_checkout_text
orig_answer = ai_agent.answer
spy = {"search": 0, "checkout": 0, "ai": 0}


async def spy_search(update, context):
    spy["search"] += 1
    return True


async def spy_checkout(update, context):
    spy["checkout"] += 1
    return True


async def spy_answer(text, lang, **kwargs):
    spy["ai"] += 1
    return "AI-REPLY"


storefront.handle_search_text = spy_search
storefront.handle_checkout_text = spy_checkout
ai_agent.answer = spy_answer
try:
    ctx6 = FCtx()
    ctx6.user_data["state"] = storefront.STATE_SEARCH
    m6 = FMsg(text="oil", message_id=777001, user=FUser(42))
    asyncio.run(ai_agent.handle_customer_message(
        FUpd(message=m6, user=FUser(42)), ctx6))
    check("ai_agent delegates search keyword to storefront", spy["search"] == 1)
    check("Gemini not called for search keyword", spy["ai"] == 0)

    ctx7 = FCtx()
    ctx7.user_data["state"] = "CHECKOUT_ADDRESS"
    m7 = FMsg(text="12 MG Road, Bhubaneswar", message_id=777002, user=FUser(42))
    asyncio.run(ai_agent.handle_customer_message(
        FUpd(message=m7, user=FUser(42)), ctx7))
    check("ai_agent delegates checkout address to storefront",
          spy["checkout"] == 1)
    check("Gemini not called during checkout", spy["ai"] == 0)
finally:
    storefront.handle_search_text = orig_hst
    storefront.handle_checkout_text = orig_hct
    ai_agent.answer = orig_answer


# --- Open Shop URL validation ---------------------------------------------
# A hostname label may NOT start or end with '-' (RFC 1123). Telegram enforces
# this too and raises Button_url_invalid, so the guard must reject a trailing
# '-' so /start never sends a broken button. The bot must also stay alive even
# if a bad URL slips through (cmd_start falls back to a button-less menu).
url_cases = [
    ("https://munagrocery.in", True),
    ("https://munagrocery.in/shop", True),
    ("https://example.com/shop", True),
    ("https://shop.domain.co.in/", True),
    ("http://example.com", False),
    ("https://-bad.com", False),
    ("not a url", False),
    ("", False),
]
for _u, _exp in url_cases:
    check(
        f"shop URL {(_u or '(empty)')!r} -> {_exp}",
        storefront._is_valid_webapp_url(_u) is _exp,
    )

# --- Open Shop is a PLAIN URL button (own HTML shop, NOT a Telegram Mini App)
_start_rows = storefront.start_keyboard().inline_keyboard
_flat = [b for row in _start_rows for b in row]
_shop_btns = [b for b in _flat if getattr(b, "text", "") == "🛍️ Open Shop"]
check(
    "Open Shop button exists on /start",
    len(_shop_btns) == 1,
)
check(
    "Open Shop is a normal url button (no Telegram Mini App)",
    len(_shop_btns) == 1
    and getattr(_shop_btns[0], "url", None) is not None
    and getattr(_shop_btns[0], "web_app", None) is None
    and (storefront.SHOP_SITE_URL in (_shop_btns[0].url or "")),
)
check(
    "no WebAppInfo import (Mini App removed)",
    not hasattr(storefront, "WebAppInfo"),
)

check(
    "chat-mode button exists in router pattern",
    bool(_re.match(storefront.CALLBACK_PATTERN, storefront.CB_CHATMODE)),
)

# --- Per-user Open Shop URL (address prefill) ------------------------------
import urllib.parse as _urlparse

_orig_web_app_url = storefront.web_app_url
_orig_get_addr = storefront.db.get_address
try:
    storefront.web_app_url = lambda: "https://munagrocery.in"

    storefront.db.get_address = lambda uid: {
        "name": "Ramesh",
        "phone": "9876543210",
        "address": "At/Post Rampur, Cuttack 754001",
    }
    _u_saved = storefront.web_app_shop_url_for_user(111)
    _q = _urlparse.parse_qs(_urlparse.urlsplit(_u_saved).query)
    check(
        "per-user URL embeds saved name/phone/address",
        _q.get("addr") == ["At/Post Rampur, Cuttack 754001"]
        and _q.get("phone") == ["9876543210"]
        and _q.get("name") == ["Ramesh"],
    )
    check(
        "per-user URL is still a valid button URL",
        storefront._is_valid_webapp_url(_u_saved) is True,
    )

    storefront.db.get_address = lambda uid: None
    _base_no_hook = storefront.web_app_url()
    if os.getenv("SHEET_WEBHOOK_URL"):
        _base_no_hook += ("&" if "?" in _base_no_hook else "?") + "sheet=" + _urlparse.quote(
            os.getenv("SHEET_WEBHOOK_URL"), safe=""
        )
    check(
        "per-user URL falls back to base when nothing saved",
        storefront.web_app_shop_url_for_user(111) == _base_no_hook,
    )

    def _boom(uid):
        raise RuntimeError("sheet down")

    storefront.db.get_address = _boom
    check(
        "per-user URL never raises when address book fails",
        storefront.web_app_shop_url_for_user(111) == _base_no_hook,
    )
finally:
    storefront.web_app_url = _orig_web_app_url
    storefront.db.get_address = _orig_get_addr

print()
print(f"{total - failures}/{total} checks passed")
sys.exit(1 if failures else 0)
