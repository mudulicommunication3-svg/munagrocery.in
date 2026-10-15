"""Customer storefront for MUNNA MOBILE & Grocery (Features 1 + 2).

Feature 1 - Inline Chat Ordering (Chat Cart):
  /start shows "Browse Store". Tapping it lists categories from the central
  database; tapping a category sends each product as a photo card with an
  [Add to Cart] button that flips to [- qty +] steppers. A persistent
  [View Cart] button shows the itemised bill and runs the checkout state
  machine (address -> phone -> place via orders.checkout).

Feature 2 - Telegram Mini App (Web App Store):
  /start also shows "Open Shop" (a WebApp button opening WEB_APP_URL, the
  static shop.html Mini App). The Mini App posts its cart back as
  ``web_app_data``; the bot validates it against the live catalog and runs
  the same checkout state machine (address -> phone -> place).

All reads/writes go through db.py + orders.py, the same central database the
admin panel and AI agent use. Blocking Sheets calls run via asyncio.to_thread.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import urllib.parse
from typing import Any, Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

try:
    from telegram import WebAppInfo
except ImportError:  # very old PTB builds - Mini App button degrades gracefully
    WebAppInfo = None  # type: ignore[assignment,misc]

from telegram.constants import ParseMode

try:
    from telegram.error import TelegramBadRequest
except ImportError:
    from telegram.error import BadRequest as TelegramBadRequest  # type: ignore[no-redef]

from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
import orders

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Callbacks + states
# --------------------------------------------------------------------------- #
CB_STORE = "shop_home"
CB_CATEGORIES = "shop_cats"
CB_CAT_PREFIX = "shop_cat_"      # + index into user_data["shop_categories"]
CB_ADD_PREFIX = "shop_add_"      # + ean
CB_INC_PREFIX = "shop_inc_"      # + ean
CB_DEC_PREFIX = "shop_dec_"      # + ean
CB_CART = "shop_cart"
CB_CLEAR = "shop_clear"
CB_CHECKOUT = "shop_checkout"
CB_BACK_CATS = "shop_back_cats"
CB_SEARCH = "shop_search"        # open the search prompt
CB_SEARCH_DONE = "shop_search_done"  # leave the search session
# JM-style cart management dashboard (per-item rows + actions)
CB_CART_DASH = "shop_cart_dash"
CB_COPY_BILL = "shop_copy_bill"           # plain-text bill (long-press copy)
CB_ROW_DEC_PREFIX = "shop_row_dec_"       # dashboard row: qty - 1  (+ ean)
CB_ROW_INC_PREFIX = "shop_row_inc_"       # dashboard row: qty + 1  (+ ean)
CB_ROW_DEL_PREFIX = "shop_row_del_"       # dashboard row: remove    (+ ean)

# chat mode: "ai" = full assistant, "store" = browse/cart only (no AI replies)
CB_CHATMODE = "shop_chatmode"
CHAT_MODE_AI = "ai"
CHAT_MODE_STORE = "store"
DEFAULT_CHAT_MODE = CHAT_MODE_AI


def get_chat_mode(context) -> str:
    """The customer's chat mode ('ai' or 'store'; defaults to 'ai')."""
    try:
        mode = str(context.user_data.get("chat_mode") or "").strip().lower()
    except Exception:  # noqa: BLE001 - fakes without user_data
        return DEFAULT_CHAT_MODE
    return mode if mode in (CHAT_MODE_AI, CHAT_MODE_STORE) else DEFAULT_CHAT_MODE


def chat_mode_button_label(mode: str) -> str:
    """Toggle-button label reflecting the current mode."""
    if mode == CHAT_MODE_AI:
        return "🤖 AI Chat (tap → Store mode)"
    return "🏬 Store Chat (tap → AI mode)"


# saved-address states (explicit name -> address -> phone -> location collect;
# ADDR_CHOOSE = "use the saved address or enter a new one?" prompt at checkout)
STATE_ADDR_CHOOSE = "ADDR_CHOOSE"
STATE_ADDR_NAME = "ADDR_NAME"
STATE_ADDR_TEXT = "ADDR_TEXT"
STATE_ADDR_PHONE = "ADDR_PHONE"
STATE_ADDR_LOC = "ADDR_LOC"
CB_ADDR_USE = "shop_addr_use"
CB_ADDR_NEW = "shop_addr_new"


def _fmt_saved_address(addr: Dict[str, Any]) -> str:
    bits = []
    if addr.get("name"):
        bits.append(f"Name: {html.escape(str(addr['name']))}")
    if addr.get("address"):
        bits.append(f"Address: {html.escape(str(addr['address']))}")
    if addr.get("phone"):
        bits.append(f"Phone: {html.escape(str(addr['phone']))}")
    return "\n".join(bits) if bits else "(empty)"


def _address_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Use this address", callback_data=CB_ADDR_USE)],
            [InlineKeyboardButton("Enter a new address", callback_data=CB_ADDR_NEW)],
        ]
    )


async def _send_choice_prompt(query, message, text: str, reply_markup=None) -> None:
    if query is not None:
        try:
            await query.answer()
        except Exception:
            pass
        await _edit_or_reply(query, text, reply_markup)
        return
    if message is not None:
        try:
            await message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        except TelegramBadRequest:
            try:
                await message.reply_text(text, reply_markup=reply_markup)
            except TelegramBadRequest:
                pass


async def ask_address_choice(chat_id: int, context, query=None, message=None) -> None:
    """Ask 'use saved address?' at checkout; remembers where to continue."""
    try:
        saved = await asyncio.to_thread(db.get_address, context.user_data.get("_uid"))
    except Exception:
        saved = None
    if not saved:
        try:
            context.user_data["state"] = STATE_ADDR_NAME
            context.user_data["addr_collect"] = dict(context.user_data.get("addr_collect") or {})
        except Exception:
            pass
        await _send_choice_prompt(
            query, message,
            "No saved address yet.\n\nWhat's your <b>name</b>? (this will be saved as your default)",
        )
        return
    try:
        context.user_data["state"] = STATE_ADDR_CHOOSE
        context.user_data["addr_collect"] = dict(context.user_data.get("addr_collect") or {})
    except Exception:
        pass
    await _send_choice_prompt(
        query, message,
        f"<b>Use your saved default address?</b>\n\n{_fmt_saved_address(saved)}",
        _address_choice_keyboard(),
    )


async def handle_address_choice(query, context) -> bool:
    """'Use saved' / 'new address' buttons during checkout. True when consumed."""
    data = getattr(query, "data", "") or ""
    if data not in (CB_ADDR_USE, CB_ADDR_NEW):
        return False
    if data == CB_ADDR_USE:
        try:
            saved = await asyncio.to_thread(db.get_address, context.user_data.get("_uid"))
        except Exception:
            saved = None
        if not saved:
            try:
                context.user_data["state"] = STATE_ADDR_NAME
                await query.answer("No saved address yet - enter a new one.", show_alert=True)
            except Exception:
                pass
            await _edit_or_reply(
                query,
                "No saved address yet.\n\nWhat's your <b>name</b>? (saved as default)",
            )
            return True
        try:
            context.user_data["checkout_name"] = str(saved.get("name") or "")
            context.user_data["checkout_address"] = str(saved.get("address") or "")
            context.user_data["checkout_phone"] = str(saved.get("phone") or "")
            loc = str(saved.get("location") or "").strip()
            if loc:
                context.user_data["checkout_location"] = loc
            context.user_data["state"] = None
        except Exception:
            pass
        try:
            await query.answer("Saved address applied")
        except Exception:
            pass
        await _edit_or_reply(query, "Saved address applied.")
        await _resume_after_address(query, query.message, context)
        return True
    # CB_ADDR_NEW -> start the collect flow, remember where to continue
    try:
        context.user_data["state"] = STATE_ADDR_NAME
    except Exception:
        pass
    try:
        await query.answer()
    except Exception:
        pass
    await _edit_or_reply(
        query,
        "New address (this will replace your default).\n\nWhat's your <b>name</b>?",
    )
    return True


async def _resume_after_address(query, message, context) -> None:
    """Continue whichever flow waited on the address (ai / modal / chat)."""
    try:
        collect = dict(context.user_data.get("addr_collect") or {})
    except Exception:
        collect = {}
    channel = str(collect.get("channel") or "chat")
    after = collect.get("after")
    if after == "ai_finish" and channel == "ai":
        try:
            from ai_agent import _ai_finish_order as _fin
        except Exception:
            _fin = None
        if _fin is not None:
            if query is not None:
                await _fin(query, context)
            elif message is not None:
                await _fin(message, context)
            return
    if channel == "modal":
        if query is not None:
            await show_cart_dash(query, context)
        return
    if message is not None:
        await finish_checkout(message, context)
    elif query is not None and getattr(query, "message", None) is not None:
        await finish_checkout(query.message, context)


async def _safe_addr_reply(message, text: str) -> None:
    try:
        await message.reply_text(text, parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        try:
            await message.reply_text(text)
        except TelegramBadRequest:
            pass


async def handle_address_text(update, context) -> bool:
    """Name -> address -> phone collect steps. True when consumed."""
    message = update.effective_message
    if message is None:
        return False
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return False
    state = str(context.user_data.get("state") or "")
    if state == STATE_ADDR_CHOOSE:
        return False  # button-only prompt; typed text goes to the next layer
    if state == STATE_ADDR_NAME:
        try:
            collect = dict(context.user_data.get("addr_collect") or {})
            collect["name"] = text
            context.user_data["addr_collect"] = collect
            context.user_data["state"] = STATE_ADDR_TEXT
        except Exception:
            pass
        await _safe_addr_reply(message, "Now send your <b>delivery address</b>:")
        return True
    if state == STATE_ADDR_TEXT:
        try:
            collect = dict(context.user_data.get("addr_collect") or {})
            collect["address"] = text
            context.user_data["addr_collect"] = collect
            context.user_data["state"] = STATE_ADDR_PHONE
        except Exception:
            pass
        await _safe_addr_reply(message, "Now send your <b>phone number</b>:")
        return True
    if state == STATE_ADDR_PHONE:
        try:
            collect = dict(context.user_data.get("addr_collect") or {})
            collect["phone"] = text
            context.user_data["addr_collect"] = collect
            context.user_data["state"] = STATE_ADDR_LOC
        except Exception:
            pass
        await _safe_addr_reply(
            message,
            "Share your <b>location</b> (clip -> <b>Location</b>) for exact delivery,\nor type <b>skip</b>:",
        )
        return True
    if state == STATE_ADDR_LOC:
        await _finish_address_collect(message, context, location="")
        return True
    return False


async def handle_address_location(update, context) -> bool:
    """Telegram location share during the ADDR_LOC collect step."""
    message = update.effective_message
    if message is None:
        return False
    if str(context.user_data.get("state") or "") != STATE_ADDR_LOC:
        return False
    loc = getattr(message, "location", None)
    if loc is None:
        return False
    try:
        lat = float(getattr(loc, "latitude", 0) or 0)
        lng = float(getattr(loc, "longitude", 0) or 0)
    except (TypeError, ValueError):
        lat = lng = 0.0
    await _finish_address_collect(
        message, context, location=(f"{lat:.6f},{lng:.6f}" if (lat or lng) else "")
    )
    return True


async def _finish_address_collect(message, context, location: str = "") -> None:
    """Persist the collected address as the default, then resume the flow."""
    try:
        collect = dict(context.user_data.get("addr_collect") or {})
    except Exception:
        collect = {}
    collect["location"] = location
    try:
        context.user_data["addr_collect"] = collect
    except Exception:
        pass
    # persist: the new address REPLACES the old default (single row per customer)
    uid = None
    try:
        uid = context.user_data.get("_uid")
    except Exception:
        pass
    saved_ok = False
    if uid:
        try:
            await asyncio.to_thread(
                db.save_address, uid,
                collect.get("name", ""), collect.get("address", ""),
                collect.get("phone", ""), location,
            )
            saved_ok = True
        except Exception as exc:
            logger.debug("Could not save address: %s", exc)
    try:
        context.user_data["checkout_name"] = str(collect.get("name") or "")
        context.user_data["checkout_address"] = str(collect.get("address") or "")
        context.user_data["checkout_phone"] = str(collect.get("phone") or "")
        if location:
            context.user_data["checkout_location"] = location
        context.user_data["state"] = None
    except Exception:
        pass
    saved_note = " Saved as your default address." if saved_ok else ""
    await _safe_addr_reply(message, f"<b>Address saved.</b>{saved_note}")
    await _resume_after_address(None, message, context)

# conversation state stored in context.user_data["state"] while the customer
# has the search prompt open (their next typed message is a keyword).
STATE_SEARCH = "SHOP_SEARCH"

CALLBACK_PATTERN = (
    r"^(shop_home|shop_cats|shop_cart|shop_clear|shop_checkout|shop_back_cats|"
    r"shop_search|shop_search_done|shop_cart_dash|shop_copy_bill|shop_chatmode|"
    r"shop_addr_use|shop_addr_new|"
    r"shop_cat_\d+|shop_(add|inc|dec)_.+|shop_row_(dec|inc|del)_.+)$"
)

PAGE_SIZE = 8  # products listed per category page (photo cards need no paging)

# one DNS label: 1-63 chars, letters/digits/hyphen, not starting with '-'.
# (GitHub Pages serves munagrocery.in via CNAME - without this guard a bad
# WEB_APP_URL would make Telegram reject the whole /start menu)
_LABEL_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")


# --------------------------------------------------------------------------- #
# /start menu buttons (used by main.py)
# --------------------------------------------------------------------------- #


def web_app_url() -> str:
    """Public HTTPS URL of the Mini App (empty when not configured)."""
    return (os.getenv("WEB_APP_URL") or "").strip()


def web_app_shop_url(url: str = "") -> str:
    """Mini App URL with the Sheet webhook appended as a ``?sheet=`` param.

    The new Mini App (WEBAPP2.html) reads ``?sheet=<apps-script-url>`` on load
    and persists it, so the catalog auto-syncs from Google Sheets without any
    in-app setup. Returns the base URL unchanged when unset.
    """
    base = (url or web_app_url()).strip()
    hook = (os.getenv("SHEET_WEBHOOK_URL") or "").strip()
    if not base or not hook:
        return base
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}sheet={urllib.parse.quote(hook, safe='')}"


def web_app_shop_url_for_user(user_id, url: str = "") -> str:
    """Mini App URL plus the customer's saved name/phone/address for prefill.

    The Mini App reads ``?name=&phone=&addr=`` and pre-fills its checkout form,
    so a returning customer never retypes their address. Falls back to the
    plain shop URL when nothing is saved or the result would be too long for a
    Telegram ``web_app`` button. Never raises - prefill must not break /start.
    """
    base = web_app_shop_url(url)
    if not base or user_id is None:
        return base
    try:
        saved = db.get_address(user_id) or {}
    except Exception:  # noqa: BLE001 - a Sheets hiccup must never break /start
        logger.warning("Could not load saved address for %s", user_id, exc_info=True)
        return base
    params = {}
    if saved.get("name"):
        params["name"] = saved["name"]
    if saved.get("phone"):
        params["phone"] = saved["phone"]
    if saved.get("address"):
        params["addr"] = saved["address"]
    if not params:
        return base
    sep = "&" if "?" in base else "?"
    out = f"{base}{sep}{urllib.parse.urlencode(params)}"
    # Telegram rejects overly long web_app URLs - keep a safe margin.
    return out if len(out) <= 1500 else base


def _is_valid_webapp_url(url: str) -> bool:
    """Guard against malformed URLs (e.g. host labels ending in '-') that make
    Telegram reject the whole /start menu with ``Button_url_invalid``. Only a
    well-formed ``https://host/...`` URL is accepted as a Mini App target."""
    if not url or WebAppInfo is None:
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.netloc:
        return False
    host = parsed.hostname or ""
    if not host or "." not in host:
        return False
    # every DNS label must be 1-63 chars, alnum + hyphen, not start/end with '-'
    for label in host.split("."):
        if not (1 <= len(label) <= 63):
            return False
        if not _LABEL_RE.match(label):
            return False
    return True


def start_keyboard(is_admin: bool = False, admin_panel_markup=None,
                   chat_mode: str = DEFAULT_CHAT_MODE,
                   include_webapp: bool = True,
                   user_id=None):
    """Row layout for /start: Browse + Search + Cart + Chat Mode + Open Shop.

    The Open Shop (Mini App) button is only added when WEB_APP_URL is a valid
    HTTPS URL, so a bad URL can never break the menu. ``include_webapp=False``
    drops it entirely (used as a fallback when Telegram rejects the URL).
    """
    rows = [
        [InlineKeyboardButton("📦 Browse Store", callback_data=CB_CATEGORIES)],
        [InlineKeyboardButton("🔍 Search", callback_data=CB_SEARCH)],
        [InlineKeyboardButton("🛒 Cart", callback_data=CB_CART_DASH)],
        [InlineKeyboardButton(chat_mode_button_label(chat_mode), callback_data=CB_CHATMODE)],
    ]
    if not include_webapp:
        if is_admin and admin_panel_markup is not None:
            for row in admin_panel_markup.inline_keyboard:
                rows.append(list(row))
        return InlineKeyboardMarkup(rows)
    url = web_app_shop_url_for_user(user_id)
    if url and not _is_valid_webapp_url(url):
        logger.warning(
            "WEB_APP_URL %r is invalid (e.g. bad hostname); hiding the "
            "Open Shop button so /start still works.", url
        )
        url = ""
    if url:
        rows.append(
            [InlineKeyboardButton("🛍️ Open Shop", web_app=WebAppInfo(url=url))]
        )
    if is_admin and admin_panel_markup is not None:
        for row in admin_panel_markup.inline_keyboard:
            rows.append(list(row))
    return InlineKeyboardMarkup(rows)


# --------------------------------------------------------------------------- #
# Cart helpers (per-user cart lives in context.user_data["cart"])
# --------------------------------------------------------------------------- #


def get_cart(context) -> Dict[str, int]:
    cart = context.user_data.get("cart")
    if not isinstance(cart, dict):
        cart = {}
        context.user_data["cart"] = cart
    return cart


def cart_count(cart: Dict[str, int]) -> int:
    return sum(max(0, int(q or 0)) for q in (cart or {}).values())


def _cart_bar(cart: Dict[str, int]) -> List[List[Any]]:
    """Persistent [View Cart (n)] row appended under lists and cards."""
    n = cart_count(cart)
    label = f"🛒 View Cart ({n})" if n else "🛒 View Cart"
    return [[InlineKeyboardButton(label, callback_data=CB_CART)]]


def _qty(value: Any) -> int:
    try:
        return int(float(str(value or 0)))
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- #
# Category list + product cards
# --------------------------------------------------------------------------- #


def _categories_keyboard(categories: List[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"🗂 {cat}", callback_data=f"{CB_CAT_PREFIX}{i}")]
        for i, cat in enumerate(categories)
    ]
    return InlineKeyboardMarkup(rows)


def _card_keyboard(ean: str, qty_in_cart: int) -> InlineKeyboardMarkup:
    if qty_in_cart > 0:
        rows = [
            [
                InlineKeyboardButton("➖", callback_data=f"{CB_DEC_PREFIX}{ean}"),
                InlineKeyboardButton(f"{qty_in_cart}", callback_data=CB_CART),
                InlineKeyboardButton("➕", callback_data=f"{CB_INC_PREFIX}{ean}"),
            ]
        ]
    else:
        rows = [[InlineKeyboardButton("🛒 Add to Cart", callback_data=f"{CB_ADD_PREFIX}{ean}")]]
    return InlineKeyboardMarkup(rows)


def _card_caption(prod: Dict[str, Any]) -> str:
    name = html.escape(str(prod.get("product_name") or "Product"))
    price = orders.format_money(prod.get("selling_price") or 0)
    mrp = orders.format_money(prod.get("mrp") or 0)
    stock = _qty(prod.get("stock_qty"))
    lines = [f"🛒 <b>{name}</b>", f"💰 Rs.{price} (MRP Rs.{mrp})"]
    lines.append("🟢 In stock" if stock > 0 else "🔴 Out of stock")
    return "\n".join(lines)


async def _edit_or_reply(query, text: str, reply_markup=None) -> None:
    message = query.message
    if message is not None and hasattr(message, "edit_text"):
        try:
            await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
            return
        except TelegramBadRequest:
            pass
    try:
        await query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        logger.warning("Could not send storefront message: %s", exc)


async def show_categories(query, context) -> None:
    """List categories from the central database."""
    try:
        categories = await asyncio.to_thread(db.get_categories)
    except Exception as exc:  # noqa: BLE001 - db.DatabaseError / network
        logger.error("Category list failed: %s", exc)
        await query.answer("❌ Could not load categories.", show_alert=True)
        return
    if not categories:
        await query.answer()
        await _edit_or_reply(query, "📂 The store is empty right now. Please check back soon.")
        return
    context.user_data["shop_categories"] = categories
    await query.answer()
    await _edit_or_reply(
        query,
        "🗂 <b>Choose a category:</b>",
        _categories_keyboard(categories),
    )


async def show_category_products(query, context, index: int) -> None:
    """Send each product of a category as a photo card with cart buttons."""
    categories = context.user_data.get("shop_categories") or []
    if index < 0 or index >= len(categories):
        await query.answer("❌ Category expired. Open Browse Store again.", show_alert=True)
        return
    category = categories[index]
    await query.answer()
    try:
        products = await asyncio.to_thread(db.get_products_by_category, category)
    except Exception as exc:  # noqa: BLE001
        logger.error("Product list failed for %r: %s", category, exc)
        await _edit_or_reply(query, f"❌ Could not load {html.escape(category)}.")
        return
    products = [p for p in products if _qty(p.get("stock_qty")) > 0][:PAGE_SIZE]
    if not products:
        await _edit_or_reply(
            query,
            f"📂 No in-stock items in <b>{html.escape(category)}</b> right now.",
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Categories", callback_data=CB_CATEGORIES)]]
                + _cart_bar(get_cart(context))
            ),
        )
        return
    cart = get_cart(context)
    header = f"🗂 <b>{html.escape(category)}</b> - tap Add to Cart:"
    try:
        await query.message.reply_text(header, parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        pass
    for prod in products:
        ean = str(prod.get("ean") or "")
        if not ean:
            continue
        keyboard = InlineKeyboardMarkup(
            list(_card_keyboard(ean, cart.get(ean, 0)).inline_keyboard)
            + _cart_bar(cart)
        )
        caption = _card_caption(prod)
        image = str(prod.get("image_url") or "").strip()
        try:
            if image:
                await context.bot.send_photo(
                    chat_id=query.message.chat_id, photo=image,
                    caption=caption, parse_mode=ParseMode.HTML, reply_markup=keyboard,
                )
            else:
                await context.bot.send_message(
                    chat_id=query.message.chat_id, text=caption,
                    parse_mode=ParseMode.HTML, reply_markup=keyboard,
                )
        except TelegramBadRequest as exc:
            logger.warning("Could not send product card %s: %s", ean, exc)
    nav = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Categories", callback_data=CB_CATEGORIES)]]
        + _cart_bar(get_cart(context))
    )
    try:
        await context.bot.send_message(
            chat_id=query.message.chat_id, text="—",
            reply_markup=nav,
        )
    except TelegramBadRequest:
        pass


async def _send_search_cards(message, context, keyword: str) -> None:
    """Search the database for ``keyword`` and send each match as a card.

    The search session stays open (``state == STATE_SEARCH``) so the customer
    can type another word to search again; the ``shop_search_done`` button ends
    the session and returns to the category menu.
    """
    cart = get_cart(context)
    try:
        products = await asyncio.to_thread(db.search_products, keyword, PAGE_SIZE)
    except Exception as exc:  # noqa: BLE001 - db.DatabaseError / network
        logger.error("Search failed for %r: %s", keyword, exc)
        await message.reply_text(
            "❌ Sorry, the search is unavailable right now. Please try again.",
            parse_mode=ParseMode.HTML,
        )
        return
    context.user_data["state"] = STATE_SEARCH  # keep searching alive
    products = [p for p in products if _qty(p.get("stock_qty")) > 0]
    if not products:
        await message.reply_text(
            "🔍 <b>No results</b> for "
            f"<b>{html.escape(keyword)}</b>.\n\n"
            "Try another word, or tap 🔎 Search again.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data=CB_CATEGORIES)]]
                + _cart_bar(cart)
            ),
        )
        return
    header = (
        "🔍 <b>Search results</b> for "
        f"<b>{html.escape(keyword)}</b> ({len(products)} found):"
    )
    try:
        await message.reply_text(header, parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        pass
    for prod in products:
        ean = str(prod.get("ean") or "")
        if not ean:
            continue
        keyboard = InlineKeyboardMarkup(
            list(_card_keyboard(ean, cart.get(ean, 0)).inline_keyboard)
            + _cart_bar(cart)
        )
        caption = _card_caption(prod)
        image = str(prod.get("image_url") or "").strip()
        try:
            if image:
                await context.bot.send_photo(
                    chat_id=message.chat_id, photo=image,
                    caption=caption, parse_mode=ParseMode.HTML, reply_markup=keyboard,
                )
            else:
                await context.bot.send_message(
                    chat_id=message.chat_id, text=caption,
                    parse_mode=ParseMode.HTML, reply_markup=keyboard,
                )
        except TelegramBadRequest as exc:
            logger.warning("Could not send search card %s: %s", ean, exc)
    nav = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Categories", callback_data=CB_CATEGORIES)],
         [InlineKeyboardButton("🔎 Search Again", callback_data=CB_SEARCH)]]
        + _cart_bar(get_cart(context))
    )
    try:
        await context.bot.send_message(
            chat_id=message.chat_id, text="—", reply_markup=nav,
        )
    except TelegramBadRequest:
        pass


async def handle_search_text(update, context) -> bool:
    """Consume the customer's typed search keyword (STATE_SEARCH).

    Reached from ai_agent.handle_customer_message, which is where customer
    text actually lands (the storefront text handler is shadowed in group -1
    by the admin panel handler). Returns True when it handled the keyword.
    """
    message = update.effective_message
    if message is None:
        return False
    if str(context.user_data.get("state") or "") != STATE_SEARCH:
        return False
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return False
    # Mirror the admin/storefront consume guard so the AI agent (group 0)
    # does not also answer this keyword.
    try:
        from admin_handler import _mark_consumed as _mc
        _mc(message.message_id)
    except Exception:  # noqa: BLE001 - guard must never break search
        pass
    await _send_search_cards(message, context, text)
    return True


async def _refresh_card_buttons(query, context, ean: str) -> None:
    """Flip Add to Cart <-> stepper after a tap (best effort)."""
    cart = get_cart(context)
    keyboard = _card_keyboard(ean, cart.get(ean, 0))
    try:
        await query.edit_message_reply_markup(reply_markup=keyboard)
    except Exception as exc:  # noqa: BLE001 - text cards / stale messages
        logger.debug("Could not refresh card buttons for %s: %s", ean, exc)


# --------------------------------------------------------------------------- #
# Product search (customer types a keyword -> matching photo cards + cart)
# --------------------------------------------------------------------------- #


async def prompt_search(query, context) -> None:
    """Open the search prompt and arm the search state for the next message."""
    context.user_data["state"] = STATE_SEARCH
    await query.answer()
    text = (
        "🔍 <b>Search products</b>\n\n"
        "Send a product name (or part of it) and I will show the matching items."
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🗂 Browse by Category", callback_data=CB_CATEGORIES)],
            [InlineKeyboardButton("❌ Cancel", callback_data=CB_SEARCH_DONE)],
        ]
    )
    await _edit_or_reply(query, text, keyboard)


# --------------------------------------------------------------------------- #
# Cart view + checkout state machine (address -> phone -> place)
# --------------------------------------------------------------------------- #


async def show_cart(query, context, checkout_hint: bool = True) -> None:
    """Itemised bill + checkout/clear buttons (empty-cart aware)."""
    cart = get_cart(context)
    if not cart:
        await query.answer()
        await _edit_or_reply(query, "🛒 Your cart is empty. Browse the store to add items!")
        return
    try:
        catalog = await asyncio.to_thread(db.get_all_products)
    except Exception as exc:  # noqa: BLE001
        logger.error("Cart catalog read failed: %s", exc)
        await query.answer("❌ Could not load the cart.", show_alert=True)
        return
    lines, missing = orders.resolve_products(cart, catalog)
    if missing:
        for ean in missing:
            cart.pop(ean, None)
    if not lines:
        await query.answer()
        await _edit_or_reply(query, "🛒 Your cart is empty (items no longer available).")
        return
    for line in lines:
        if line["qty"] > line["available"] and line["available"] > 0:
            line["qty"] = line["available"]
            line["line_total"] = line["price"] * line["qty"]
            cart[line["ean"]] = line["qty"]
    text = "🧾 <b>Your cart:</b>\n\n" + orders.bill_text(lines)
    if missing:
        text += "\n\n<i>Some unavailable items were removed.</i>"
    rows = [
        [InlineKeyboardButton("✅ Checkout", callback_data=CB_CHECKOUT)],
        [InlineKeyboardButton("🧹 Clear cart", callback_data=CB_CLEAR)],
        [InlineKeyboardButton("⬅️ Continue shopping", callback_data=CB_CATEGORIES)],
    ]
    await query.answer()
    await _edit_or_reply(query, text, InlineKeyboardMarkup(rows))


# --------------------------------------------------------------------------- #
# Cart management dashboard (JM-style): summary table + per-item qty rows
# --------------------------------------------------------------------------- #


def _dashboard_table(lines: List[Dict[str, Any]]) -> str:
    """JM-style monospace rows: ``Name   qty x price = line_total``."""
    if not lines:
        return "Cart is empty."
    name_w = min(24, max([10] + [len(str(l.get("name") or "Item")) for l in lines]))
    rows = []
    for line in lines:
        name = str(line.get("name") or "Item")[:name_w]
        qty = int(line.get("qty") or 1)
        price = float(line.get("price") or 0)
        line_total = float(line.get("line_total") or price * qty)
        rows.append(
            f"{name:<{name_w}}  {f'{qty} x {price:.2f}':>14} = {line_total:>9.2f}"
        )
    return "\n".join(rows)


def _dashboard_body(context, lines: List[Dict[str, Any]]) -> str:
    """Plain-text cart dashboard: running "Cart & Bill Calculator" + table
    + totals + delivery info (JM layout)."""

    def money(v: Any) -> str:
        try:
            n = float(v or 0)
        except (TypeError, ValueError):
            n = 0.0
        return str(int(n)) if n.is_integer() else str(round(n, 2))

    total_qty = sum(int(l.get("qty") or 0) for l in lines)
    total = sum(float(l.get("line_total") or 0) for l in lines)
    address = str(context.user_data.get("checkout_address") or "").strip()
    phone = str(context.user_data.get("checkout_phone") or "").strip()

    # ---- 📟 Cart & Bill Calculator ----
    calc_lines = [
        "🧮 କାର୍ଟ ଏଣ୍ଡ ବିଲ୍ କାଲକୁଲେଟର୍",
        "──────────────────────────────────────",
    ]
    for l in lines:
        calc_lines.append(
            f"  {l.get('name', '?'):<14} Qty {int(l.get('qty') or 0):<4} "
            f"@ {money(l.get('price'))} = {money(l.get('line_total'))}"
        )
    calc_lines += [
        "──────────────────────────────────────",
        f"  {'Total Items:':<16} {total_qty:>6}",
        f"  {'Subtotal:':<16} Rs.{money(total):>10}",
    ]

    # ---- 📦 Item list ----
    dash = "-" * 34
    body_lines = ["📋 ଅର୍ଡର୍ ପୋଇନ୍ଟ୍", dash]
    body_lines.append(_dashboard_table(lines))
    body_lines += [dash, ""] + calc_lines + [dash, ""]

    if address:
        body_lines.append(f"Home: {address[:60]}")
        if phone:
            body_lines.append(f"Phone: {phone}")
    else:
        body_lines.append("Not set yet - tap Address")
    return "\n".join(body_lines)


def _dashboard_text(context, lines: List[Dict[str, Any]]) -> str:
    """HTML-wrapped monospace cart dashboard (rendered on the card)."""
    return f"📊 <b>Your Cart</b>\n\n<pre>{html.escape(_dashboard_body(context, lines))}</pre>"


def _dashboard_keyboard(cart: Dict[str, int], lines: List[Dict[str, Any]]):
    """JM cart-management rows [➖ name qty ➕ 🗑] + action buttons."""
    rows: List[List[Any]] = []
    for line in lines:
        ean = str(line.get("ean") or "")
        if not ean:
            continue
        name = str(line.get("name") or "Item")[:20]
        qty = int(line.get("qty") or 1)
        rows.append(
            [
                InlineKeyboardButton("➖", callback_data=f"{CB_ROW_DEC_PREFIX}{ean}"),
                InlineKeyboardButton(name, callback_data=CB_CART_DASH),
                InlineKeyboardButton(f"{qty}", callback_data=CB_CART_DASH),
                InlineKeyboardButton("➕", callback_data=f"{CB_ROW_INC_PREFIX}{ean}"),
                InlineKeyboardButton("🗑", callback_data=f"{CB_ROW_DEL_PREFIX}{ean}"),
            ]
        )
    rows += [
        [
            InlineKeyboardButton("🏠 Address", callback_data=CB_CHECKOUT),
            InlineKeyboardButton("🔍 Search Products", callback_data=CB_SEARCH),
        ],
        [
            InlineKeyboardButton("📋 Copy Bill", callback_data=CB_COPY_BILL),
            InlineKeyboardButton("🗑 Clear Cart", callback_data=CB_CLEAR),
        ],
        [InlineKeyboardButton("✅ Checkout", callback_data=CB_CHECKOUT)],
        [InlineKeyboardButton("⬅️ Back", callback_data=CB_CATEGORIES)],
    ]
    return InlineKeyboardMarkup(rows)


def _empty_cart_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔍 Search Products", callback_data=CB_SEARCH)],
            [InlineKeyboardButton("📦 Browse Store", callback_data=CB_CATEGORIES)],
        ]
    )


async def _resolve_lines(context) -> List[Dict[str, Any]]:
    """Resolve the cart to bill lines, pruning unavailable EANs (best effort).

    Shared by the dashboard renderer and the copy-bill action so both always
    show the same items/prices as the checkout.
    """
    cart = get_cart(context)
    if not cart:
        return []
    try:
        catalog = await asyncio.to_thread(db.get_all_products)
    except Exception as exc:  # noqa: BLE001
        logger.error("Dashboard catalog read failed: %s", exc)
        return []
    lines, missing = orders.resolve_products(cart, catalog)
    if missing:
        for ean in missing:
            cart.pop(ean, None)
    for line in lines:
        if line["qty"] > line["available"] and line["available"] > 0:
            line["qty"] = line["available"]
            line["line_total"] = line["price"] * line["qty"]
            cart[line["ean"]] = line["qty"]
    return lines


async def show_cart_dash(query, context) -> None:
    """Render the JM-style cart-management dashboard (table + qty rows)."""
    await query.answer()
    lines = await _resolve_lines(context)
    if not lines:
        await _edit_or_reply(
            query,
            "📊 <b>Your Cart</b>\n\nYour cart is empty. Search or browse to add items.",
            _empty_cart_keyboard(),
        )
        return
    await _edit_or_reply(
        query,
        _dashboard_text(context, lines),
        _dashboard_keyboard(get_cart(context), lines),
    )


async def _dashboard_row_edit(query, context, ean: str, delta: int) -> None:
    """Apply a +/- qty change from a dashboard row and re-render in place."""
    await query.answer()
    cart = get_cart(context)
    if ean not in cart:
        await query.answer("⏳ Item already removed.", show_alert=True)
        return
    new_qty = int(cart.get(ean, 0)) + delta
    if new_qty <= 0:
        cart.pop(ean, None)
    else:
        cart[ean] = min(new_qty, 99)
    context.user_data["cart"] = cart
    lines = await _resolve_lines(context)
    if not lines:
        await _edit_or_reply(
            query,
            "📊 <b>Your Cart</b>\n\nYour cart is empty. Search or browse to add items.",
            _empty_cart_keyboard(),
        )
        return
    await _edit_or_reply(
        query,
        _dashboard_text(context, lines),
        _dashboard_keyboard(cart, lines),
    )


async def _dashboard_row_delete(query, context, ean: str) -> None:
    """Remove one line from the dashboard and re-render."""
    await query.answer("🗑 Removed.")
    cart = get_cart(context)
    cart.pop(ean, None)
    context.user_data["cart"] = cart
    lines = await _resolve_lines(context)
    if not lines:
        await _edit_or_reply(
            query,
            "📊 <b>Your Cart</b>\n\nYour cart is empty. Search or browse to add items.",
            _empty_cart_keyboard(),
        )
        return
    await _edit_or_reply(
        query,
        _dashboard_text(context, lines),
        _dashboard_keyboard(cart, lines),
    )


async def _copy_bill(query, context) -> None:
    """Send a plain-text bill the customer can long-press to copy."""
    await query.answer()
    lines = await _resolve_lines(context)
    if not lines:
        await query.message.reply_text("📋 Your cart is empty - nothing to copy.")
        return
    body = _dashboard_body(context, lines)
    await query.message.reply_text(
        f"📋 <b>Copy your bill</b>\n\n<pre>{html.escape(body)}</pre>\n\n"
        "<i>Long-press the bill above to copy it.</i>",
        parse_mode=ParseMode.HTML,
    )


async def start_checkout(query, context) -> None:
    """Begin checkout: saved-address chooser (or fresh collect), then order."""
    if not get_cart(context):
        await query.answer("Your cart is empty.", show_alert=True)
        return
    try:
        context.user_data["checkout_channel"] = "chat"
        context.user_data["addr_collect"] = {"channel": "chat", "after": "chat_finish"}
        user = getattr(query, "from_user", None)
        if user is not None and getattr(user, "id", None):
            context.user_data["_uid"] = int(user.id)
    except Exception:
        pass
    await ask_address_choice(
        getattr(getattr(query, "message", None), "chat_id", 0), context, query=query
    )


async def handle_checkout_text(update, context) -> bool:
    """Address/phone steps of the checkout machine. True when consumed."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return False
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return False
    state = str(context.user_data.get("state") or "")

    def _consume() -> None:
        try:
            from admin_handler import _mark_consumed as _mc
        except Exception:  # noqa: BLE001
            return
        try:
            _mc(message.message_id)
        except Exception:  # noqa: BLE001 - guard must never break checkout
            pass

    if state == orders.STATE_CHECKOUT_ADDRESS:
        context.user_data["checkout_address"] = text
        context.user_data["state"] = orders.STATE_CHECKOUT_PHONE
        try:
            await message.reply_text(
                "📞 Now please send your <b>phone number</b>:",
                parse_mode=ParseMode.HTML,
            )
        except TelegramBadRequest:
            pass
        _consume()
        return True
    if state == orders.STATE_CHECKOUT_PHONE:
        context.user_data["checkout_phone"] = text
        context.user_data["state"] = orders.STATE_CHECKOUT_LOCATION
        _consume()
        try:
            await message.reply_text(
                "📍 Optionally share your <b>location</b> for exact delivery "
                "(📎 clip → <b>Location</b>), or type <b>skip</b>:",
                parse_mode=ParseMode.HTML,
            )
        except TelegramBadRequest:
            pass
        return True
    if state == orders.STATE_CHECKOUT_LOCATION:
        # Any text here means "no location / skip" (or a written landmark) -> place now.
        context.user_data["state"] = None
        _consume()
        await finish_checkout(message, context)
        return True
    return False


async def handle_checkout_location(update, context) -> None:
    """Capture a shared Telegram location during checkout, then place the order."""
    message = update.effective_message
    if message is None:
        return
    state = str(context.user_data.get("state") or "")
    if state != orders.STATE_CHECKOUT_LOCATION:
        return
    loc = getattr(message, "location", None)
    if loc is None:
        return
    try:
        lat = float(getattr(loc, "latitude", 0) or 0)
        lng = float(getattr(loc, "longitude", 0) or 0)
    except (TypeError, ValueError):
        lat = lng = 0.0
    if lat or lng:
        context.user_data["checkout_location"] = f"{lat:.6f},{lng:.6f}"
    context.user_data["state"] = None
    await finish_checkout(message, context)


async def finish_checkout(message, context) -> None:
    """Place the order via the shared path and confirm to the customer."""
    cart = get_cart(context)
    if not cart:
        await message.reply_text("🛒 Your cart is empty - nothing to order.")
        return
    address = str(context.user_data.get("checkout_address") or "").strip()
    phone = str(context.user_data.get("checkout_phone") or "").strip()
    channel = str(context.user_data.get("checkout_channel") or "chat")
    location = str(context.user_data.get("checkout_location") or "").strip()
    user = getattr(message, "from_user", None)
    # Prefer the stored customer id: on a button resume message.from_user is
    # the BOT, which would store the wrong id for later status updates.
    user_id = str(context.user_data.get("_uid") or "").strip() or (
        getattr(user, "id", "") if user else ""
    )
    name = str(context.user_data.get("checkout_name") or "").strip()
    if not name:
        for attr in ("full_name", "first_name", "username"):
            try:
                candidate = str(getattr(user, attr, "") or "").strip()
            except Exception:  # noqa: BLE001
                candidate = ""
            if candidate and candidate.lower() != "none":
                name = candidate if attr != "username" else f"@{candidate}"
                break
    items = [{"ean": ean, "qty": qty} for ean, qty in cart.items()]
    try:
        summary = await orders.checkout(
            context, items,
            customer_name=name, phone=phone, address=address,
            channel=channel, telegram_user_id=user_id, location=location,
        )
    except ValueError as exc:
        await message.reply_text(f"❌ Could not place the order: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 - db.DatabaseError / network
        from db import DatabaseError as _DatabaseError

        if isinstance(exc, _DatabaseError):
            await message.reply_text(f"❌ Database error while ordering: {exc}")
        else:
            logger.error("Checkout crashed: %s", exc)
            await message.reply_text("❌ Something went wrong. Please try again.")
        return
    context.user_data["cart"] = {}
    for key in ("checkout_address", "checkout_phone", "checkout_channel",
                "checkout_location", "ai_pending_order"):
        context.user_data.pop(key, None)
    # Send the LIVE order-status message (it auto-updates as the seller works).
    try:
        sent = await orders.send_order_status(context, message.chat_id, summary, "pending")
    except Exception:  # noqa: BLE001 - never break the order for this
        sent = None
    if sent is None:  # fallback confirmation if the status message could not be sent
        text = (
            f"✅ <b>Order placed!</b>\n"
            f"🧾 Order ID: <code>{html.escape(summary['order_id'])}</code>\n\n"
            + orders.bill_text(summary["lines"], summary["total"])
            + "\n\n🚚 We will deliver soon. Thank you! 🙏"
        )
        try:
            await message.reply_text(text, parse_mode=ParseMode.HTML)
        except TelegramBadRequest:
            await message.reply_text(text)

# --------------------------------------------------------------------------- #
# Callback router (cart buttons)
# --------------------------------------------------------------------------- #


async def cmd_myorders(update, context) -> None:
    """List the caller's recent orders with their live statuses."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    uid = str(user.id)
    try:
        all_orders = await asyncio.to_thread(db.get_recent_orders, 200)
    except Exception as exc:  # noqa: BLE001 - db.DatabaseError / network
        logger.error("My-orders read failed: %s", exc)
        await message.reply_text("❌ Could not load your orders right now.")
        return
    mine = [o for o in all_orders if str(o.get("telegram_user_id") or "") == uid][:10]
    if not mine:
        await message.reply_text("📭 You have no orders yet. Type a product to start!")
        return
    lines = ["📦 <b>My orders</b> (newest first)\n"]
    for o in mine:
        status = o.get("status") or "pending"
        label = db.ORDER_STATUS_LABELS.get(status, status)
        oid = html.escape(str(o.get("order_id") or ""))
        total = html.escape(str(o.get("total") or ""))
        lines.append(f"🧾 <code>{oid}</code> — {label}\n   Rs.{total}")
    lines.append("\n<i>Your live order message updates automatically as the seller works.</i>")
    try:
        await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        await message.reply_text("\n".join(lines))


async def handle_store_callback(update, context) -> None:
    """Route Browse Store / category / cart / stepper taps."""
    query = update.callback_query
    if query is None or not query.data:
        return
    data = query.data
    if await handle_address_choice(query, context):
        return
    if data == CB_CHATMODE:
        current = get_chat_mode(context)
        new_mode = CHAT_MODE_STORE if current == CHAT_MODE_AI else CHAT_MODE_AI
        try:
            context.user_data["chat_mode"] = new_mode
        except Exception:  # noqa: BLE001 - fakes without user_data
            pass
        await query.answer(
            "🤖 AI chat ON" if new_mode == CHAT_MODE_AI else "🏬 Store chat ON"
        )
        rows = [
            [InlineKeyboardButton("📦 Browse Store", callback_data=CB_CATEGORIES)],
            [InlineKeyboardButton("🔍 Search", callback_data=CB_SEARCH)],
            [InlineKeyboardButton("🛒 Cart", callback_data=CB_CART_DASH)],
            [InlineKeyboardButton(chat_mode_button_label(new_mode), callback_data=CB_CHATMODE)],
        ]
        url = web_app_shop_url()
        if url and _is_valid_webapp_url(url) and WebAppInfo is not None:
            rows.append([InlineKeyboardButton("🛍️ Open Shop", web_app=WebAppInfo(url=url))])
        try:
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(rows))
        except Exception:  # noqa: BLE001 - stale/identical markup
            pass
        return
    if data == CB_CATEGORIES or data == CB_STORE:
        context.user_data.pop("state", None)
        await show_categories(query, context)
        return
    if data == CB_SEARCH:
        # Open the search prompt; the customer's next typed message is the
        # keyword (routed via ai_agent -> handle_search_text).
        await prompt_search(query, context)
        return
    if data == CB_SEARCH_DONE:
        context.user_data.pop("state", None)
        await query.answer()
        await show_categories(query, context)
        return
    if data.startswith(CB_CAT_PREFIX):
        idx_text = data[len(CB_CAT_PREFIX):]
        if not idx_text.isdigit():
            await query.answer("❌ Invalid category.", show_alert=True)
            return
        await show_category_products(query, context, int(idx_text))
        return
    if data == CB_CART:
        await show_cart(query, context)
        return
    if data == CB_CLEAR:
        context.user_data["cart"] = {}
        await query.answer("🧹 Cart cleared.")
        await _edit_or_reply(query, "🛒 Your cart is empty. Browse the store to add items!")
        return
    if data == CB_CHECKOUT:
        await start_checkout(query, context)
        return
    if data == CB_BACK_CATS:
        await show_categories(query, context)
        return
    # --- JM-style cart management dashboard -------------------------------
    if data == CB_CART_DASH:
        await show_cart_dash(query, context)
        return
    if data == CB_COPY_BILL:
        await _copy_bill(query, context)
        return
    for prefix in (CB_ROW_DEC_PREFIX, CB_ROW_INC_PREFIX):
        if data.startswith(prefix):
            ean = data[len(prefix):].strip()
            if not ean:
                await query.answer("❌ Invalid item.", show_alert=True)
                return
            delta = 1 if prefix == CB_ROW_INC_PREFIX else -1
            await _dashboard_row_edit(query, context, ean, delta)
            return
    if data.startswith(CB_ROW_DEL_PREFIX):
        ean = data[len(CB_ROW_DEL_PREFIX):].strip()
        if not ean:
            await query.answer("❌ Invalid item.", show_alert=True)
            return
        await _dashboard_row_delete(query, context, ean)
        return
    for prefix, delta in ((CB_ADD_PREFIX, 1), (CB_INC_PREFIX, 1), (CB_DEC_PREFIX, -1)):
        if data.startswith(prefix):
            ean = data[len(prefix):].strip()
            if not ean:
                await query.answer("❌ Invalid item.", show_alert=True)
                return
            cart = get_cart(context)
            new_qty = max(0, int(cart.get(ean, 0)) + delta)
            if new_qty <= 0:
                cart.pop(ean, None)
                new_qty = 0
            else:
                cart[ean] = min(new_qty, 99)
            context.user_data["cart"] = cart
            if delta > 0 and new_qty == 1 and prefix == CB_ADD_PREFIX:
                await query.answer("🛒 Added to cart!")
            else:
                await query.answer()
            await _refresh_card_buttons(query, context, ean)
            return
    await query.answer("❌ Unknown action.", show_alert=True)
# --------------------------------------------------------------------------- #
# Mini App checkout payload (Telegram Web App -> web_app_data)
# --------------------------------------------------------------------------- #


def _parse_webapp_payload(raw: str) -> List[Dict[str, Any]]:
    """Parse the Mini App cart JSON into ``[{"ean": qty}]`` items.

    Accepted shapes: ``[{"ean": ..., "qty": ...}]``,
    ``{"items": [...]}`` or ``{"cart": {ean: qty}}``. Raises ValueError.
    """
    try:
        payload = json.loads(raw or "")
    except (TypeError, ValueError) as exc:
        raise ValueError("Could not read the Mini App cart.") from exc
    if isinstance(payload, dict):
        wrapped = payload.get("items", payload.get("cart", payload.get("products")))
        if wrapped is None and all(
            not isinstance(v, (dict, list)) for v in payload.values()
        ):
            wrapped = [{"ean": k, "qty": v} for k, v in payload.items()]
        payload = wrapped
    if not isinstance(payload, list) or not payload:
        raise ValueError("The Mini App cart is empty.")
    items: List[Dict[str, Any]] = []
    for entry in payload:
        if isinstance(entry, dict):
            ean = entry.get("ean", entry.get("id", entry.get("uid", "")))
            qty = entry.get("qty", entry.get("quantity", entry.get("count", 1)))
        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
            ean, qty = entry[0], entry[1]
        else:
            continue
        ean = db.normalize_ean(ean)
        try:
            qty_n = int(float(qty))
        except (TypeError, ValueError):
            qty_n = 1
        if ean and qty_n > 0:
            items.append({"ean": ean, "qty": min(qty_n, 99)})
    if not items:
        raise ValueError("The Mini App cart has no valid items.")
    return items


async def handle_webapp_data(update, context) -> None:
    """Mini App payload (SAVE_ADDRESS / ORDER). Orders are placed immediately.

    The Mini App collects the whole checkout (items + address + payment) and
    posts it back here. We place the order straight through db.place_order so it
    completes in one step (no multi-step chat dance) and confirm with the order
    id. SAVE_ADDRESS just refreshes the saved address book entry for the user.
    """
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    web_data = getattr(message, "web_app_data", None)
    raw = getattr(web_data, "data", "") if web_data is not None else ""
    if not raw:
        return

    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        parsed = None
    action = ""
    name = phone = address = payment = order_id = location = ""
    if isinstance(parsed, dict):
        action = str(parsed.get("action") or "").strip().upper()
        name = str(parsed.get("customer") or parsed.get("name") or "").strip()
        phone = str(parsed.get("phone") or "").strip()
        address = str(parsed.get("address") or "").strip()
        payment = str(parsed.get("payment") or "COD").strip() or "COD"
        order_id = str(parsed.get("orderId") or "").strip()
        location = str(parsed.get("location") or "").strip()

    # Address book sync from the Mini App (no order placed).
    if action == "SAVE_ADDRESS":
        try:
            await asyncio.to_thread(
                db.save_address, user.id, name, address, phone, location
            )
            await message.reply_text("Address saved. (\u091c\u093e \u0928\u093e\u0939\u093f\u0902)")
        except Exception:  # noqa: BLE001
            logger.warning("Could not save address for %s", user.id, exc_info=True)
            await message.reply_text("Could not save the address right now.")
        return

    # Place the Mini App order immediately.
    try:
        items = _parse_webapp_payload(raw)
    except ValueError as exc:
        await message.reply_text(f"Order could not be placed: {exc}")
        return

    try:
        result = await asyncio.to_thread(
            db.place_order,
            items,
            name,
            phone,
            address,
            "miniapp",
            user.id,
            order_id or None,
            location=location,
            payment=payment,
        )
    except ValueError as exc:
        await message.reply_text(f"Order could not be placed: {exc}")
        return
    except Exception:  # noqa: BLE001 - DB/API failure
        logger.error("Mini App order failed for %s", user.id, exc_info=True)
        await message.reply_text(
            "Something went wrong placing your order. Please try again."
        )
        return

    # Remember the address so the next checkout is prefilled.
    if name and phone and address:
        try:
            await asyncio.to_thread(
                db.save_address, user.id, name, address, phone, location
            )
        except Exception:  # noqa: BLE001
            logger.warning("Could not save address for %s", user.id, exc_info=True)

    # Keep the chat cart in sync with the Mini App order.
    context.user_data.pop("cart", None)
    context.user_data.pop("state", None)

    oid = result.get("order_id", "")
    total = result.get("total", 0)

    # Live customer status message - the SAME message the Admin Panel edits
    # in place as the seller advances the order (shared with all channels).
    summary = dict(result)
    summary["address"] = address
    summary["phone"] = phone
    summary["customer_name"] = name
    sent = await orders.send_order_status(
        context, user.id, summary, result.get("status") or "pending"
    )
    if sent is None:
        # Fallback: plain confirmation when the status message could not send.
        try:
            await message.reply_text(
                f"Your order is placed!\n\n"
                f"Order ID: {oid}\n"
                f"Total: Rs.{total}\n"
                f"Payment: {payment}\n\n"
                f"Track it under My Orders. Thank you!"
            )
        except Exception:  # noqa: BLE001
            logger.warning("Could not confirm Mini App order to %s", user.id)

    # Best-effort admin notification (same alert format as every channel).
    try:
        alert = orders.admin_alert_text(
            oid,
            name,
            phone,
            address,
            result.get("lines") or [],
            total,
            "miniapp",
            user.id,
        )
        await orders.notify_admins(context, alert)
    except Exception:  # noqa: BLE001
        pass



async def handle_store_text(update, context) -> bool:
    """Checkout text steps for store customers. True when consumed.

    NOTE: this handler is registered in group -1 AFTER the admin panel text
    handler, which matches every text message first - so it never fires.
    Customer text actually lands in ``ai_agent.handle_customer_message``,
    which delegates address/phone steps to ``handle_checkout_text`` (and
    search keywords to ``handle_search_text``). Kept for registration order
    safety and direct/programmatic use.
    """
    return await handle_checkout_text(update, context)


async def handle_store_location(update, context) -> None:
    """Route location shares: address-collect pin wins, then checkout pin."""
    if await handle_address_location(update, context):
        return
    await handle_checkout_location(update, context)


def register(application) -> None:
    """Attach storefront callbacks, Mini App data and checkout text."""
    application.add_handler(
        CallbackQueryHandler(handle_store_callback, pattern=CALLBACK_PATTERN),
        group=-1,
    )
    application.add_handler(
        MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data),
        group=-1,
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_store_text),
        group=-1,
    )
    # Shared-location messages during checkout (exact-delivery pin).
    application.add_handler(
        MessageHandler(filters.LOCATION, handle_store_location)
    )


__all__ = [
    "CALLBACK_PATTERN",
    "CB_ADD_PREFIX",
    "CB_BACK_CATS",
    "CB_CART",
    "CB_CATEGORIES",
    "CB_CAT_PREFIX",
    "CB_CHECKOUT",
    "CB_CLEAR",
    "CB_DEC_PREFIX",
    "CB_INC_PREFIX",
    "CB_SEARCH",
    "CB_SEARCH_DONE",
    "CB_STORE",
    "STATE_SEARCH",
    "finish_checkout",
    "get_cart",
    "handle_checkout_text",
    "handle_search_text",
    "handle_store_callback",
    "handle_store_text",
    "handle_webapp_data",
    "prompt_search",
    "register",
    "show_cart",
    "show_categories",
    "show_category_products",
    "start_checkout",
    "start_keyboard",
    "web_app_url",
]


