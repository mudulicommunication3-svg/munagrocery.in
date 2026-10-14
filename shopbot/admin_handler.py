"""Telegram admin auto-stock entry flow for MUNNA MOBILE & Grocery.

Entry format (sent by the admin as plain text)::

    [Product Name], [MRP], [Selling Price], [Qty], [EAN]
    Fortune Mustard Oil 1L, 210, 170, 15, 8901234567890

Flow:
  * EAN already in the sheet -> db.update_stock()
    -> "Item exists. Stock and Price updated."
  * new EAN -> image_search.fetch_images() (5 URLs, paginated)
    -> inline picker [1]-[5] + [Next 5] -> db.add_product()

Register with ``register(application)`` BEFORE the customer/AI handlers.

Blocking work (gspread / Custom Search HTTP) runs in worker threads via
``asyncio.to_thread`` so the asyncio event loop of python-telegram-bot v20+
stays responsive.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode

try:
    from telegram.error import TelegramBadRequest
except ImportError:  # some builds only expose the legacy alias
    from telegram.error import BadRequest as TelegramBadRequest

from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, filters

import db
import jiomart_search
from image_search import MAX_START_INDEX, fetch_images, search_configured

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 15 * 60  # pickers expire after 15 minutes

CALLBACK_NEXT = "next_5"
CALLBACK_SELECT_PREFIX = "sel_"
CALLBACK_RETRY = "img_retry"
CALLBACK_SKIP = "img_skip"
CALLBACK_CANCEL = "img_cancel"
CALLBACK_PATTERN = r"^(next_5|sel_[1-5]|img_retry|img_skip|img_cancel)$"

# --------------------------------------------------------------------------- #
# Admin Panel ("Search & Export to Database") constants
# --------------------------------------------------------------------------- #
# Fallback pincodes - stock is checked against JioMart in this exact order.
PANEL_PINCODES: List[str] = ["753003", "753001", "751010"]
PANEL_TOP_N = 3            # product cards shown per page
PANEL_PAGE_SIZE = 3        # cards per pagination page
PANEL_MAX_FETCH = 15       # total items fetched so we can paginate
PANEL_DEFAULT_QTY = 10     # default Stock Qty for one-click exports

PANEL_CALLBACK_PREFIX = "admin_"
PANEL_CALLBACK_PATTERN = (
    r"^(admin_panel|admin_search|admin_close|admin_home|"
    r"admin_setpin|admin_clearpin|admin_page_\d+|admin_search_done|"
    r"admin_browse|admin_browse_search|admin_cat_\d+|admin_item_\d+|admin_del_\d+|admin_delconfirm_\d+|"
    r"admin_stock_(inc|dec|info)_\d+|admin_editmenu_\d+|admin_backcard_\d+|"
    r"admin_addadmin|admin_rmadmin_\d+|"
    r"admin_ai_toggle|"
    r"admin_restart|"
    r"admin_orders|admin_ord_back|"
    r"admin_ordv_[\w-]+|"
    r"admin_ord_ok_[\w-]+|admin_ord_no_[\w-]+|admin_ord_next_[\w-]+|"
    r"admin_ord_bill_[\w-]+|admin_ord_rm_[\w-]+_\d+|"
    r"admin_manual|admin_manual_newcat|admin_manual_cat_\d+|"
    r"admin_manual_skip_ean|admin_manual_cancel|"
    r"admin_imglink|admin_imglink_more_\d+|admin_imglink_urls|admin_imglink_copy|"
    r"export_item_\d+|"
    r"edit_(name|mrp|price|qty|pack|category)_\d+)$"
)

# conversation states stored in context.user_data["state"]
STATE_SEARCH_PRODUCT = "ADMIN_SEARCH_PRODUCT"
STATE_SET_PINCODE = "ADMIN_SET_PINCODE"
STATE_BROWSE_SEARCH = "ADMIN_BROWSE_SEARCH"
STATE_ADD_ADMIN = "ADMIN_ADD_ADMIN"
# "🖼 Image Link Generator": admin types a keyword -> JioMart image URLs
# (clickable + copyable) for manual products / offer banners.
STATE_IMAGE_LINK = "ADMIN_IMAGE_LINK"
# manual add-product conversation (category -> name -> photo -> prices ->
# qty -> ean), collected in context.user_data["manual"]
STATE_MANUAL_CATEGORY = "ADMIN_MANUAL_CATEGORY"
STATE_MANUAL_NEW_CATEGORY = "ADMIN_MANUAL_NEW_CATEGORY"
STATE_MANUAL_NAME = "ADMIN_MANUAL_NAME"
STATE_MANUAL_PHOTO = "ADMIN_MANUAL_PHOTO"
STATE_MANUAL_PRICES = "ADMIN_MANUAL_PRICES"
STATE_MANUAL_QTY = "ADMIN_MANUAL_QTY"
STATE_MANUAL_EAN = "ADMIN_MANUAL_EAN"
AWAITING_EDIT_PREFIX = "AWAITING_EDIT_"
STATE_EDIT_NAME = AWAITING_EDIT_PREFIX + "NAME"
STATE_EDIT_MRP = AWAITING_EDIT_PREFIX + "MRP"
STATE_EDIT_PRICE = AWAITING_EDIT_PREFIX + "PRICE"
STATE_EDIT_QTY = AWAITING_EDIT_PREFIX + "QTY"
STATE_EDIT_PACK = AWAITING_EDIT_PREFIX + "PACK"
STATE_EDIT_CATEGORY = AWAITING_EDIT_PREFIX + "CATEGORY"

# edit-state -> (field key in the cache, sheet column or None, Odia label)
EDIT_FIELDS: Dict[str, Tuple[str, Optional[int], str]] = {
    STATE_EDIT_NAME: ("name", db.COL_NAME, "Product Name"),
    STATE_EDIT_MRP: ("mrp", db.COL_MRP, "MRP"),
    STATE_EDIT_PRICE: ("price", db.COL_PRICE, "Selling Price"),
    STATE_EDIT_QTY: ("qty", db.COL_STOCK, "Stock Qty"),
    STATE_EDIT_PACK: ("pack", None, "Pack / Weight"),  # no sheet column
    STATE_EDIT_CATEGORY: ("category", db.COL_CATEGORY, "Category"),
}

# uid (JioMart) -> normalized product dict of the last search (per admin
# this cache is global; uid keys are unique so cross-talk is impossible)
_product_cache: Dict[str, Dict[str, Any]] = {}

USAGE_HINT = (
    "Could not read the entry.\n\n"
    "Format:\nProduct Name, MRP, Selling Price, Qty, EAN\n\n"
    "Example:\nFortune Mustard Oil 1L, 210, 170, 15, 8901234567890"
)

# uid -> pending new-product session (one picker per admin at a time)
_pending: Dict[int, Dict[str, Any]] = {}
_bad_admin_id_warned = False

# --------------------------------------------------------------------------- #
# Runtime admins (added from the panel with the "➕ Add Admin" button).
# Primary admins stay env-driven (ADMIN_ID); extras are persisted to a small
# JSON file next to the code so they survive restarts. In development mode
# (ADMIN_ID empty / "0") everyone is already an admin and this list is unused.
# --------------------------------------------------------------------------- #
_ADMINS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "admins.json"
)
_extra_admins: Optional[List[str]] = None  # in-memory cache; None = not loaded


def _env_admin_ids() -> List[str]:
    """Admin ids from the ADMIN_ID env variable (comma-separated allowed)."""
    raw = (os.getenv("ADMIN_ID") or "").strip()
    if raw in ("", "0"):
        return []
    ids: List[str] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(part)
    return ids


def _load_extra_admins() -> List[str]:
    """Persisted extra-admin ids (cached; a corrupt file never breaks auth)."""
    global _extra_admins
    if _extra_admins is not None:
        return _extra_admins
    try:
        with open(_ADMINS_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        uids = [str(u).strip() for u in data.get("admins", [])]
    except FileNotFoundError:
        uids = []
    except Exception as exc:  # noqa: BLE001 - never break auth on bad file
        logger.warning("Could not read %s: %s", _ADMINS_FILE, exc)
        uids = []
    _extra_admins = [u for u in uids if u.isdigit()]
    return _extra_admins


def _save_extra_admins(uids: List[str]) -> None:
    global _extra_admins
    _extra_admins = [str(u).strip() for u in uids]
    with open(_ADMINS_FILE, "w", encoding="utf-8") as fh:
        json.dump({"admins": _extra_admins}, fh, indent=2)


def add_extra_admin(user_id: Any) -> bool:
    """Persist a new admin id. Returns False when it is already an admin."""
    uid = str(user_id or "").strip()
    if not uid.isdigit():
        raise ValueError("Admin id must be numeric.")
    uids = _load_extra_admins()
    if uid in uids:
        return False
    _save_extra_admins(uids + [uid])
    return True


def remove_extra_admin(user_id: Any) -> bool:
    """Revoke a runtime-added admin. Returns False when not present."""
    uid = str(user_id or "").strip()
    uids = _load_extra_admins()
    if uid not in uids:
        return False
    _save_extra_admins([u for u in uids if u != uid])
    return True


def all_admin_ids() -> List[str]:
    """Every known admin id: ADMIN_ID env first, then runtime-added ones."""
    seen: List[str] = []
    for uid in _env_admin_ids() + _load_extra_admins():
        if uid not in seen:
            seen.append(uid)
    return seen


def is_admin(user_id: Optional[int]) -> bool:
    """Authorisation check: ADMIN_ID env + admins added from the panel.

    * ADMIN_ID empty or ``0`` -> development mode, everyone is allowed
    * otherwise the id must match the env list (comma-separated allowed) or
      one of the runtime-added admins stored next to the code
    """
    global _bad_admin_id_warned
    if user_id is None:
        return False
    raw = (os.getenv("ADMIN_ID") or "").strip()
    if raw in ("", "0"):
        return True
    try:
        target = int(user_id)
    except (TypeError, ValueError):
        return False
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if int(part) == target:
                return True
        except ValueError:
            _bad_admin_id_warned = True
    # admins granted from the panel at runtime
    return any(u.isdigit() and int(u) == target for u in _load_extra_admins())


_MONEY_STRIP = re.compile(r"[₹,\s]")  # rupee sign, thousands separators, spaces
_RS_PREFIX = re.compile(r"(?i)^rs\.?")


def parse_stock_entry(text: str) -> Dict[str, Any]:
    """Parse ``Name, MRP, Price, Qty, EAN`` into a validated dict.

    The name itself may contain commas: the last 4 fields are always
    MRP / price / qty / EAN. Raises ValueError with a friendly message
    (including the format example) when the text does not match.
    """
    parts = [part.strip() for part in str(text or "").split(",")]
    if len(parts) < 5:
        raise ValueError(USAGE_HINT)
    name = ", ".join(parts[:-4]).strip()
    mrp_text, price_text, qty_text, ean_text = parts[-4], parts[-3], parts[-2], parts[-1]

    if not name:
        raise ValueError("Product name is empty.\n\n" + USAGE_HINT)
    ean = db.normalize_ean(ean_text)
    if not ean:
        raise ValueError("EAN barcode is empty.\n\n" + USAGE_HINT)

    return {
        "name": name,
        "mrp": _parse_money(mrp_text, "MRP"),
        "price": _parse_money(price_text, "Selling Price"),
        "qty": _parse_quantity(qty_text),
        "ean": ean,
    }


def _parse_money(value: str, field: str) -> float:
    cleaned = _RS_PREFIX.sub("", _MONEY_STRIP.sub("", str(value)))
    try:
        number = float(cleaned)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number (got '{value}').") from None
    if number < 0:
        raise ValueError(f"{field} cannot be negative (got '{value}').")
    return int(number) if number.is_integer() else number


def _parse_quantity(value: str) -> int:
    cleaned = _MONEY_STRIP.sub("", str(value))
    try:
        number = float(cleaned)
    except (TypeError, ValueError):
        raise ValueError(f"Qty must be a whole number (got '{value}').") from None
    if not number.is_integer():
        raise ValueError(f"Qty must be a whole number (got '{value}').")
    if number < 0:
        raise ValueError(f"Qty cannot be negative (got '{value}').")
    return int(number)


# --------------------------------------------------------------------------- #
# Entry filter (keeps non-entry text available for other handlers)
# --------------------------------------------------------------------------- #
class StockEntryFilter(filters.MessageFilter):
    """True for admin text messages that could be a stock entry."""

    def filter(self, message) -> bool:
        if message is None:
            return False
        user = getattr(message, "from_user", None)
        if user is None or not message.text or "," not in message.text:
            return False
        return is_admin(user.id)


ENTRY_FILTER = StockEntryFilter()


# --------------------------------------------------------------------------- #
# Message rendering
# --------------------------------------------------------------------------- #
_MD_V1_SPECIALS = re.compile(r"([\\_*`\[])")


def _escape_markdown(text: str) -> str:
    """Escape legacy-Markdown metacharacters with a backslash."""
    return _MD_V1_SPECIALS.sub(r"\\\1", str(text))


def _format_money(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number.is_integer() else f"{number:.2f}"


def _session_header(session: Dict[str, Any]) -> Tuple[str, str]:
    """(markdown, plain) one-line product summary for this session."""
    md_text = (
        f"*New product:* {_escape_markdown(session['name'])}\n"
        f"MRP: ₹{_format_money(session['mrp'])} | "
        f"Price: ₹{_format_money(session['price'])} | "
        f"Qty: {session['qty']} | EAN: {_escape_markdown(session['ean'])}"
    )
    plain_text = (
        f"New product: {session['name']}\n"
        f"MRP: ₹{_format_money(session['mrp'])} | "
        f"Price: ₹{_format_money(session['price'])} | "
        f"Qty: {session['qty']} | EAN: {session['ean']}"
    )
    return md_text, plain_text


def _render_picker(session: Dict[str, Any]) -> Tuple[str, str]:
    """(markdown, plain) text listing the current batch of image URLs."""
    header_md, header_plain = _session_header(session)
    md_lines: List[str] = []
    plain_lines: List[str] = []
    for index, url in enumerate(session.get("image_urls") or [], 1):
        md_lines.append(f"{index}. [Image {index}]({url})")
        plain_lines.append(f"{index}. {url}")
    footer_md = "\n\nTap a number button below to choose. *Next 5 ➡️* shows more."
    footer_plain = "\n\nTap a number button below to choose. 'Next 5 ➡️' shows more."
    return (
        header_md + "\n\n" + "\n".join(md_lines) + footer_md,
        header_plain + "\n\n" + "\n".join(plain_lines) + footer_plain,
    )


def _render_no_images(session: Dict[str, Any]) -> Tuple[str, str]:
    """Explanation message when the image search produced nothing."""
    header_md, header_plain = _session_header(session)
    if search_configured():
        body_md = "No images found for this product."
        body_plain = body_md
    else:
        body_md = (
            "Image search is not configured - set "
            "`CUSTOM_SEARCH_API_KEY` and `SEARCH_ENGINE_ID` in `.env`."
        )
        body_plain = (
            "Image search is not configured - set CUSTOM_SEARCH_API_KEY "
            "and SEARCH_ENGINE_ID in .env."
        )
    tail_md = "\n\nTap *Retry* to search again or *Skip* to save without an image."
    tail_plain = "\n\nTap 'Retry' to search again or 'Skip' to save without an image."
    return header_md + "\n\n" + body_md + tail_md, header_plain + "\n\n" + body_plain + tail_plain


def _next_allowed(current_start_index: int) -> bool:
    return current_start_index < MAX_START_INDEX


def _picker_keyboard(session: Dict[str, Any]) -> InlineKeyboardMarkup:
    urls = session.get("image_urls") or []
    rows: List[List[InlineKeyboardButton]] = []
    if urls:
        rows.append(
            [
                InlineKeyboardButton(
                    str(index), callback_data=f"{CALLBACK_SELECT_PREFIX}{index}"
                )
                for index in range(1, len(urls) + 1)
            ]
        )
    if _next_allowed(int(session.get("current_start_index") or 1)):
        rows.append([InlineKeyboardButton("Next 5 ➡️", callback_data=CALLBACK_NEXT)])
    rows.append([InlineKeyboardButton("Cancel ❌", callback_data=CALLBACK_CANCEL)])
    return InlineKeyboardMarkup(rows)


def _no_images_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Retry 🔁", callback_data=CALLBACK_RETRY),
                InlineKeyboardButton("Skip (no image)", callback_data=CALLBACK_SKIP),
            ],
            [InlineKeyboardButton("Cancel ❌", callback_data=CALLBACK_CANCEL)],
        ]
    )


# --------------------------------------------------------------------------- #
# Telegram helpers (Markdown with graceful plain-text fallback)
# --------------------------------------------------------------------------- #
async def _send_picker(message, md_text: str, plain_text: str, reply_markup) -> Any:
    """Reply with Markdown; fall back to plain text if Telegram rejects it."""
    try:
        return await message.reply_text(
            md_text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup
        )
    except TelegramBadRequest as exc:
        logger.warning("Markdown rejected by Telegram (%s); sending plain text", exc)
        return await message.reply_text(plain_text, reply_markup=reply_markup)


async def _edit_picker(message, md_text: str, plain_text: str, reply_markup=None) -> None:
    """Edit a message, tolerating inaccessible messages and no-op edits."""
    if message is None or not hasattr(message, "edit_text"):
        return  # inaccessible/old message - nothing to edit
    try:
        await message.edit_text(
            md_text, parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup
        )
    except TelegramBadRequest as exc:
        if "not modified" in str(exc).lower():
            return
        logger.warning("Markdown rejected by Telegram (%s); editing plain text", exc)
        await message.edit_text(plain_text, reply_markup=reply_markup)


async def _edit_to(message, text: str) -> None:
    """Edit a message to a plain status line, ignoring benign errors."""
    if message is None or not hasattr(message, "edit_text"):
        return
    try:
        await message.edit_text(text)
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc).lower():
            logger.debug("Could not edit message: %s", exc)


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
def _callback_message_id(query) -> Optional[int]:
    return getattr(query.message, "message_id", None)


def _session_for_callback(user_id: int, query) -> Optional[Dict[str, Any]]:
    """Return the live session belonging to THIS picker, else None.

    Sessions expire after SESSION_TTL_SECONDS. Buttons on an older picker
    (different message_id) are stale and must not touch the live session.
    """
    session = _pending.get(user_id)
    if session is None:
        return None
    if time.time() - float(session.get("created", 0)) > SESSION_TTL_SECONDS:
        _pending.pop(user_id, None)  # expired
        logger.info("Stock-entry session for %s expired", user_id)
        return None
    if session.get("message_id") != _callback_message_id(query):
        return None  # stale picker from an older entry - keep the live session
    return session


# --------------------------------------------------------------------------- #
# Steps 1-5: the stock entry message handler
# --------------------------------------------------------------------------- #
async def handle_admin_manual_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Manual Add Step 3: capture the product photo.

    Only acts while the photo-waiting state is armed. The photo is forwarded
    to the private DB channel (fast hosting); the channel message's file_id
    becomes the sheet's Image URL. If the channel is unreachable the admin's
    own file_id is kept - it still renders bot-wide.
    """
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or not is_admin(user.id):
        return
    if str(context.user_data.get("state") or "") != STATE_MANUAL_PHOTO:
        return
    if not message.photo:
        return
    file_id = getattr(message.photo[-1], "file_id", "") or ""
    if not file_id:
        await message.reply_text(PANEL_TEXT_MANUAL_BAD, parse_mode=ParseMode.HTML)
        return
    image_ref = file_id
    channel_id = _db_channel_id()
    if channel_id:
        try:
            forwarded = await context.bot.forward_message(
                chat_id=channel_id,
                from_chat_id=message.chat_id,
                message_id=message.message_id,
            )
            hosted = (getattr(forwarded, "photo", None) or [None])[-1]
            hosted_id = getattr(hosted, "file_id", "") or ""
            if hosted_id:
                image_ref = hosted_id
        except Exception as exc:  # noqa: BLE001 - keep the original file_id
            logger.warning(
                "Could not forward photo to channel %s: %s", channel_id, exc
            )
    manual = context.user_data.get("manual") or {}
    manual["image"] = image_ref
    context.user_data["manual"] = manual
    context.user_data["state"] = STATE_MANUAL_PRICES
    await message.reply_text(
        PANEL_TEXT_MANUAL_PRICES, parse_mode=ParseMode.HTML,
        reply_markup=_manual_prompt_keyboard(),
    )


async def handle_stock_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Parse ``Name, MRP, Price, Qty, EAN`` and run the auto-stock flow."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    if not is_admin(user.id):  # defence in depth (ENTRY_FILTER already gates)
        return
    # Skip when the Admin Panel (group -1) already handled this message as a
    # search keyword / edit value - avoid a duplicate reply in this group.
    if _was_consumed(message.message_id):
        return

    try:
        entry = parse_stock_entry(message.text or "")
    except ValueError as exc:
        await message.reply_text(str(exc))
        return

    try:
        found = await asyncio.to_thread(db.find_by_ean, entry["ean"])
    except db.DatabaseError as exc:
        logger.error("EAN lookup failed: %s", exc)
        await message.reply_text(f"❌ Database error while checking the EAN: {exc}")
        return

    if found is None:
        await _handle_new_item(message, user.id, entry)
    else:
        await _handle_existing_item(message, entry, found)


async def _handle_existing_item(message, entry: Dict[str, Any], found) -> None:
    """Duplicate EAN: add the new quantity and refresh MRP / price (step 3)."""
    try:
        summary = await asyncio.to_thread(
            db.update_stock, found, entry["qty"], entry["mrp"], entry["price"]
        )
    except db.DatabaseError as exc:
        logger.error("Stock update failed: %s", exc)
        await message.reply_text(f"❌ Database error while updating stock: {exc}")
        return
    except ValueError as exc:
        await message.reply_text(str(exc))
        return
    reply = "✅ Item exists. Stock and Price updated."
    if summary:
        reply += (
            f"\n\n📦 Stock: {summary['old_stock']} → {summary['new_stock']}"
            f"\n💰 MRP: ₹{_format_money(summary['mrp'])} | "
            f"Price: ₹{_format_money(summary['selling_price'])}"
        )
    await message.reply_text(reply)


async def _handle_new_item(message, user_id: int, entry: Dict[str, Any]) -> None:
    """New EAN: fetch images and show the inline picker (steps 4-5)."""
    image_urls = await asyncio.to_thread(fetch_images, entry["name"], 1)
    session: Dict[str, Any] = {
        **entry,
        "current_start_index": 1,
        "image_urls": image_urls,
        "created": time.time(),
        "chat_id": message.chat_id,
        "message_id": None,
    }
    if image_urls:
        text_md, text_plain = _render_picker(session)
        keyboard = _picker_keyboard(session)
    else:
        text_md, text_plain = _render_no_images(session)
        keyboard = _no_images_keyboard()
    try:
        sent = await _send_picker(message, text_md, text_plain, keyboard)
    except TelegramBadRequest as exc:
        logger.error("Could not send the image picker: %s", exc)
        await message.reply_text(f"❌ Could not display the image picker: {exc}")
        return
    session["message_id"] = sent.message_id
    _pending[user_id] = session


# --------------------------------------------------------------------------- #
# Step 6: callback queries (select / paginate / retry / skip / cancel)
# --------------------------------------------------------------------------- #
async def handle_image_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle image-picker buttons: ``next_5``, ``sel_N``, ``img_*``."""
    query = update.callback_query
    if query is None or not query.data:
        return
    user = query.from_user
    if user is None or not is_admin(user.id):
        await query.answer("⛔ Not authorised to use the admin panel.", show_alert=True)
        return
    data = query.data

    if data == CALLBACK_CANCEL:
        session = _pending.get(user.id)
        if session is not None and session.get("message_id") == _callback_message_id(query):
            _pending.pop(user.id, None)
        await query.answer()
        await _edit_to(query.message, "❌ Entry cancelled.")
        return

    session = _session_for_callback(user.id, query)
    if session is None:
        await query.answer(
            "⌛ This selection has expired. Please send the product entry again.",
            show_alert=True,
        )
        await _edit_to(query.message, "⌛ Selection expired - send the entry again.")
        return

    if data == CALLBACK_NEXT:
        await _handle_next(query, session)
    elif data == CALLBACK_RETRY:
        await _handle_retry(query, session)
    elif data == CALLBACK_SKIP:
        await _handle_select(query, context, session, None)
    elif data.startswith(CALLBACK_SELECT_PREFIX):
        raw = data[len(CALLBACK_SELECT_PREFIX):]
        if not raw.isdigit():
            await query.answer("❌ Invalid selection.", show_alert=True)
            return
        await _handle_select(query, context, session, int(raw))
    else:
        await query.answer("❌ Unknown action.", show_alert=True)


async def _handle_next(query, session: Dict[str, Any]) -> None:
    """Fetch the next 5 images and edit the inline message."""
    new_start = int(session.get("current_start_index") or 1) + 5
    if new_start > MAX_START_INDEX:
        await query.answer("🖼 No more images available.", show_alert=True)
        return

    image_urls = await asyncio.to_thread(fetch_images, session["name"], new_start)
    if not image_urls:
        await query.answer(
            "No more images found for this product.", show_alert=True
        )
        return
    session["current_start_index"] = new_start
    session["image_urls"] = image_urls
    await query.answer()
    text_md, text_plain = _render_picker(session)
    await _edit_picker(
        query.message, text_md, text_plain, _picker_keyboard(session)
    )


async def _handle_retry(query, session: Dict[str, Any]) -> None:
    """Re-run the image search for the first batch."""
    await query.answer("Searching images again...")
    image_urls = await asyncio.to_thread(fetch_images, session["name"], 1)
    session["current_start_index"] = 1
    session["image_urls"] = image_urls
    if image_urls:
        text_md, text_plain = _render_picker(session)
        keyboard = _picker_keyboard(session)
    else:
        text_md, text_plain = _render_no_images(session)
        keyboard = _no_images_keyboard()
    await _edit_picker(query.message, text_md, text_plain, keyboard)


async def _handle_select(query, context, session: Dict[str, Any], index) -> None:
    """Save the product with the chosen image (``None`` = save without one)."""
    urls = session.get("image_urls") or []
    image_url = ""
    if index is not None:
        if not 1 <= index <= len(urls):
            await query.answer("Invalid image number.", show_alert=True)
            return
        image_url = urls[index - 1]

    try:
        await asyncio.to_thread(
            db.add_product,
            session["name"],
            session["mrp"],
            session["price"],
            session["qty"],
            session["ean"],
            image_url,
        )
    except db.DatabaseError as exc:
        logger.error("add_product failed: %s", exc)
        # keep the picker alive so the admin can simply tap again
        await query.answer(f"Could not save the product: {exc}", show_alert=True)
        return
    except ValueError as exc:
        await query.answer(str(exc), show_alert=True)
        return

    _pending.pop(query.from_user.id, None)
    await query.answer()
    chosen = f"Saved with image {index}." if index else "Saved without an image."
    await _edit_to(query.message, f"✅ {chosen}")
    success = "✅ New product added to the database."
    message = query.message
    if message is not None and hasattr(message, "reply_text"):
        try:
            await message.reply_text(success)
            return
        except Exception as exc:
            logger.warning("Could not reply in place (%s); using bot.send_message", exc)
    await context.bot.send_message(chat_id=session["chat_id"], text=success)


# --------------------------------------------------------------------------- #
# Admin Panel: "Search & Export to Database" (Odia UI - admin always chats in
# Odia; English field labels kept where the spec requires them)
# --------------------------------------------------------------------------- #
# Odia UI strings for the whole panel flow.
PANEL_TEXT_HOME = "🛡️ <b>ଆଡମିନ ପ୍ୟାନେଲ୍</b>\n\nଏକ ପ୍ରୋଡକ୍ଟ ସର୍ଚ୍ଚ କରି ଡାଟାବେସ୍‌ରେ ସେଭ୍ କରନ୍ତୁ।"
PANEL_TEXT_SEARCH_PROMPT = (
    "🔍 <b>ପ୍ରୋଡକ୍ଟ ସର୍ଚ୍ଚ</b>\n\n"
    "ଦୟାକରି ଏକ ପ୍ରୋଡକ୍ଟ କିୱାର୍ଡ ପଠାନ୍ତୁ।\n"
    "ଉଦାହରଣ: <code>Fortune Mustard Oil 1L</code>"
)
PANEL_TEXT_SEARCHING = "🔎 ଖୋଜୁଛି... ଦୟାକରି ଅପେକ୍ଷା କରନ୍ତୁ।"
PANEL_TEXT_NO_RESULTS = (
    "❌ ଦୁଃଖିତ, ଏହି କିୱାର୍ଡ ପାଇଁ କୌଣସି ପ୍ରୋଡକ୍ଟ ମିଳିଲା ନାହିଁ।\n"
    "ପୁଣି ଚେଷ୍ଟା କରନ୍ତୁ / ଅନ୍ୟ କିୱାର୍ଡ ପଠାନ୍ତୁ।"
)
PANEL_TEXT_CLOSED = "❌ ପ୍ୟାନେଲ୍ ବନ୍ଦ ହେଲା। /start ରେ ପୁଣି ଖୋଲନ୍ତୁ।"
PANEL_TEXT_SAVED_TOAST = "✅ ସେଭ୍ ହେଲା!"
PANEL_TEXT_DUP_TOAST = "⚠️ ପୂର୍ବରୁ ଡାଟାବେସ୍‌ରେ ଅଛି।"

# edit-state -> Odia prompt for the value we are waiting for
EDIT_PROMPT: Dict[str, str] = {
    STATE_EDIT_NAME: (
        "✏️ <b>ନାଁ ସମ୍ପାଦନା</b>\n\n"
        "ପ୍ରୋଡକ୍ଟ ID <code>{uid}</code> ପାଇଁ ନୂଆ ନାଁ ପଠାନ୍ତୁ:"
    ),
    STATE_EDIT_MRP: (
        "✏️ <b>MRP ସମ୍ପାଦନା</b>\n\n"
        "ପ୍ରୋଡକ୍ଟ ID <code>{uid}</code> ପାଇଁ ନୂଆ MRP ପଠାନ୍ତୁ:"
    ),
    STATE_EDIT_PRICE: (
        "✏️ <b>ବିକ୍ରି ମୂଲ୍ୟ ସମ୍ପାଦନା</b>\n\n"
        "ପ୍ରୋଡକ୍ଟ ID <code>{uid}</code> ପାଇଁ ନୂଆ ବିକ୍ରି ମୂଲ୍ୟ ପଠାନ୍ତୁ:"
    ),
    STATE_EDIT_QTY: (
        "✏️ <b>ଷ୍ଟକ ସଂଖ୍ୟା ସମ୍ପାଦନା</b>\n\n"
        "ପ୍ରୋଡକ୍ଟ ID <code>{uid}</code> ପାଇଁ ନୂଆ Qty ପଠାନ୍ତୁ:"
    ),
    STATE_EDIT_PACK: (
        "✏️ <b>ପ୍ୟାକ / ଓଜନ ସମ୍ପାଦନା</b>\n\n"
        "ପ୍ରୋଡକ୍ଟ ID <code>{uid}</code> ପାଇଁ ନୂଆ ପ୍ୟାକ ଲେଖା ପଠାନ୍ତୁ "
        "(ଯେପରିକି <code>1 L</code>):"
    ),
    STATE_EDIT_CATEGORY: (
        "🗂 <b>ବର୍ଗ ସମ୍ପାଦନା</b>\n\n"
        "ପ୍ରୋଡକ୍ଟ ID <code>{uid}</code> ପାଇଁ ନୂଆ ବର୍ଗ ପଠାନ୍ତୁ "
        "(ଯେପରିକି <code>Edible Oils</code>):"
    ),
}

# edit-state -> Odia success confirmation (inserted value shown)
EDIT_DONE: Dict[str, str] = {
    STATE_EDIT_NAME: "✅ ନାଁ ଅପଡେଟ୍ ହେଲା: <b>{value}</b>",
    STATE_EDIT_MRP: "✅ MRP ଅପଡେଟ୍ ହେଲା: ₹<b>{value}</b>",
    STATE_EDIT_PRICE: "✅ ବିକ୍ରି ମୂଲ୍ୟ ଅପଡେଟ୍ ହେଲା: ₹<b>{value}</b>",
    STATE_EDIT_QTY: "✅ ଷ୍ଟକ ସଂଖ୍ୟା ଅପଡେଟ୍ ହେଲା: <b>{value}</b>",
    STATE_EDIT_PACK: "✅ ପ୍ୟାକ ଅପଡେଟ୍ ହେଲା: <b>{value}</b> <i>(ସିଟ୍‌ରେ ସେଭ୍ ନାହିଁ)</i>",
    STATE_EDIT_CATEGORY: "✅ ବର୍ଗ ଅପଡେଟ୍ ହେଲା: <b>{value}</b>",
}

PANEL_TEXT_EXP_PROMPT = "✏️ <b>ଡାଟାବେସ୍‌ରେ ସେଭ୍</b>\n\n📥 Export ବଟନ୍ ଚାପନ୍ତୁ।"
PANEL_TEXT_EXPIRED = "⌛ ଏହି କାର୍ଡ ପୁରୁଣା ହୋଇଗଲା। ଦୟାକରି ପୁଣି ସର୍ଚ୍ଚ କରନ୍ତୁ।"
PANEL_TEXT_SEARCH_ACTIVE = "🔍 ସର୍ଚ୍ଚ ଚାଲିଛି - କିୱାର୍ଡ ପଠାନ୍ତୁ।"

# Manual pincode (stock is checked against this pin first, then the fallbacks)
PANEL_TEXT_PIN_PROMPT = (
    "📍 <b>ପିନକୋଡ୍ ବଦଳାନ୍ତୁ</b>\n\n"
    "ଷ୍ଟକ ଯାଞ୍ଚ ପାଇଁ ନୂଆ 6-ଅଙ୍କର ପିନକୋଡ୍ ପଠାନ୍ତୁ "
    "(ଉଦାହରଣ: <code>751001</code>)।"
)
PANEL_TEXT_PIN_SAVED = "✅ ପିନକୋଡ୍ <b>{pin}</b> ସେଟ୍ ହେଲା। ଏବେ କିୱାର୍ଡ ପଠାନ୍ତୁ।"
PANEL_TEXT_PIN_BAD = "❌ ଭୁଲ ପିନକୋଡ୍ - ଠିକ୍ 6ଅଙ୍କ ହେବା ଉଚିତ। ପୁଣି ଚେଷ୍ଟା କରନ୍ତୁ।"
PANEL_TEXT_PIN_CURRENT = (
    "📍 ବର୍ତ୍ତମାନର ପିନକୋଡ୍: <b>{pin}</b>\n\n"
    "ଷ୍ଟକ ପ୍ରଥମେ ଏଥିରେ ଯାଞ୍ଚ ହେବ, ତାପରେ ଡିଫଲ୍ଟ ତାଲିକା: "
    "<code>{defaults}</code>"
)

# Add / remove admins (stock-manage access) from the panel
PANEL_TEXT_ADD_ADMIN_PROMPT = (
    "➕ <b>ଆଡମିନ୍ ଯୋଡ଼ନ୍ତୁ</b>\n\n"
    "ଷ୍ଟକ୍ ମ୍ୟାନେଜ୍ କରିବାକୁ ନୂଆ ଆଡମିନ୍ଙ୍କ "
    "ଟେଲିଗ୍ରାମ୍ <b>User ID</b> ପଠାନ୍ତୁ।\n"
    "(ଉଦାହରଣ: <code>123456789</code>)\n\n"
    "ଆଡମିନ୍ ତାଲିକା: <code>{admins}</code>\n\n"
    "<i>ଟିପ୍: User ID ଜାଣିବାକୁ @userinfobot କୁ ନାଁ ପଠାନ୍ତୁ।</i>"
)
PANEL_TEXT_ADD_ADMIN_DONE = "✅ <b>{uid}</b> ଏବେ ଆଡମିନ୍। ସେ ଷ୍ଟକ୍ ମ୍ୟାନେଜ୍ କରିପାରିବେ।"
PANEL_TEXT_ADD_ADMIN_DUP = "⚠️ <b>{uid}</b> ପୂର୍ବରୁ ଆଡମିନ୍ ଅଛନ୍ତି।"
PANEL_TEXT_ADD_ADMIN_BAD = (
    "❌ ଭୁଲ User ID - ଠିକ୍ ୁଇଁ ନମ୍ବର ପଠାନ୍ତୁ (5-15 ଅଙ୍କ)।"
)
PANEL_TEXT_RM_ADMIN_DONE = "🗑 ଆଡମିନ୍ ହଟାଗଲା: <b>{uid}</b>"
PANEL_TEXT_RM_ADMIN_DENY = "⛔ ନିଜକୁ / ମୁଖ୍ୟ ଆଡମିନ୍ଙ୍କୁ ହଟାଇପାରିବେନାହିଁ।"

# Manual add-product flow (sheet rows stay the exact gspread path)
PANEL_TEXT_MANUAL_START = (
    "🗂 <b>ମ୍ୟାନୁଆଲ୍ ପ୍ରୋଡକ୍ଟ ଯୋଡ଼ନ୍ତୁ</b>\n\n"
    "ପ୍ରଥମେ ବର୍ଗ (Category) ବାଛନ୍ତୁ:"
)
PANEL_TEXT_MANUAL_NEW_CAT = "✏️ <b>ନୂଆ ବର୍ଗ</b>\n\nନୂଆ ବର୍ଗର ନାଁ ଟାଇପ୍ କରନ୍ତୁ:"
PANEL_TEXT_MANUAL_NAME = "✏️ <b>ପ୍ରୋଡକ୍ଟ ନାଁ</b>\n\nପ୍ରୋଡକ୍ଟର ନାଁ ପଠାନ୍ତୁ:"
PANEL_TEXT_MANUAL_PHOTO = "📷 <b>ପ୍ରୋଡକ୍ଟ ଫଟୋ</b>\n\nପ୍ରୋଡକ୍ଟର ଫଟୋ ପଠାନ୍ତୁ।"
PANEL_TEXT_MANUAL_PRICES = (
    "💰 <b>ଦାମ୍</b>\n\n"
    "MRP ଏବଂ ବିକ୍ରି ମୂଲ୍ୟ ଏକାଠି ପଠାନ୍ତୁ "
    "(ଉଦାହରଣ: <code>150, 130</code>):"
)
PANEL_TEXT_MANUAL_QTY = (
    "📦 <b>ଷ୍ଟକ୍</b>\n\n"
    "ଷ୍ଟକ୍ ସଂଖ୍ୟା ପଠାନ୍ତୁ (ଉଦାହରଣ: <code>25</code>):"
)
PANEL_TEXT_MANUAL_EAN = (
    "🏷 <b>EAN ବାରକୋଡ୍</b>\n\n"
    "EAN ନମ୍ବର ପଠାନ୍ତୁ କିମ୍ବା ସ୍କିପ୍ କରନ୍ତୁ:"
)
PANEL_TEXT_MANUAL_BAD = "❌ ଭୁଲ ଇନପୁଟ୍ - ପୁଣି ଚେଷ୍ଟା କରନ୍ତୁ।"
PANEL_TEXT_MANUAL_EAN_DUP = (
    "⚠️ EAN <code>{ean}</code> ପୂର୍ବରୁ ଅଛି - ନୂଆ EAN ପଠାନ୍ତୁ।"
)
PANEL_TEXT_MANUAL_DONE = (
    "✅ <b>Product successfully added to the database!</b>\n\n"
    "🛒 <b>{name}</b>\n"
    "💰 MRP: ₹{mrp} | ବିକ୍ରି: ₹{price}\n"
    "📦 ଷ୍ଟକ୍: {qty}\n"
    "🏷 EAN: <code>{ean}</code>\n"
    "🗂 ବର୍ଗ: {category}"
)
PANEL_TEXT_MANUAL_CANCELLED = "❌ ମ୍ୟାନୁଆଲ୍ ଯୋଡ଼ା ବାତିଲ୍।"

# Pagination / done
PANEL_TEXT_PAGE_HEADER = "🗂 <b>ଫଳାଫଳ</b> - ପୃଷ୍ଠା {page} (ମୋଟ {total} ମଧ୍ୟରୁ)"
PANEL_TEXT_PAGE_END = "🏁 ସମସ୍ତ ଫଳାଫଳ ଦେଖାଗଲା।"
PANEL_TEXT_DONE = "✅ ସର୍ଚ୍ଚ ସମାପ୍ତ। /start ରେ ପୁଣି ଆଡମିନ ପ୍ୟାନେଲ୍ ଖୋଲନ୍ତୁ।"

# Browse-by-category (Product > Category > Item) + delete
PANEL_TEXT_BROWSE = "🗂 <b>ବର୍ଗ ଅନୁସାରେ ବ୍ରାଉଜ୍</b>\n\nଏକ ବର୍ଗ ବାଛନ୍ତୁ:"
# 🖼 Image Link Generator (manual product photo / offer banner)
PANEL_TEXT_IMGLINK_PROMPT = (
    "🖼 <b>Image Link Generator</b>\n\n"
    "ଏକ ପ୍ରୋଡକ୍ଟ ନାଁ ଲେଖନ୍ତୁ — ମୁଁ JioMart ରୁ ଫଟୋ ଲିଙ୍କ୍ ଆଣି ଦେବି।\n"
    "ଉଦାହରଣ: <code>Fortune Mustard Oil 1L</code>\n\n"
    "🔍 ଏକ କିୱାର୍ଡ ପଠାନ୍ତୁ।"
)
PANEL_TEXT_IMGLINK_SEARCHING = "🔎 ଫଟୋ ଲିଙ୍କ୍ ଖୋଜୁଛି... ଅପେକ୍ଷା କରନ୍ତୁ।"
PANEL_TEXT_IMGLINK_NONE = "❌ ଏହି ନାଁରେ କୌଣସି ଫଟୋ ମିଳିଲା ନାହିଁ। ଅନ୍ୟ ନାଁ ଚେଷ୍ଟା କରନ୍ତୁ।"
PANEL_TEXT_IMGLINK_BAD = "❌ ଭୁଲ ଇନପୁଟ୍ — ଏକ ପ୍ରୋଡକ୍ଟ ନାଁ ପଠାନ୍ତୁ।"
PANEL_TEXT_IMGLINK_URLS = "📋 <b>ସମସ୍ତ ଫଟୋ ଲିଙ୍କ୍</b> — ଟ୍ୟାପ୍ କରି କପି କରନ୍ତୁ:"
PANEL_TEXT_BROWSE_SEARCH_PROMPT = (
    "🔍 <b>ବ୍ରାଉଜ୍ ସର୍ଚ୍ଚ</b>\n\n"
    "ଡାଟାବେସ୍‌ରେ ଖୋଜିବାକୁ ଏକ ପ୍ରୋଡକ୍ଟ ନାଁ / ବର୍ଗ / EAN ପଠାନ୍ତୁ।"
)
PANEL_TEXT_NO_CATS = "📂 ଡାଟାବେସ୍‌ରେ ଏପର୍ଯ୍ୟନ୍ତ କୌଣସି ବର୍ଗ ନାହିଁ।"
PANEL_TEXT_CAT_HEADER = "🗂 <b>{cat}</b> - {count}ଟି ପ୍ରୋଡକ୍ଟ\n\nଏକଟି ବାଛନ୍ତୁ:"
PANEL_TEXT_CAT_EMPTY = "📂 ଏହି ବର୍ଗରେ କୌଣସି ପ୍ରୋଡକ୍ଟ ନାହିଁ।"
PANEL_TEXT_DEL_ASK = (
    "🗑 <b>ଡିଲିଟ୍ କରିବେ?</b>\n\n"
    "<b>{name}</b> (ID <code>{uid}</code>)\n\n"
    "ଡାଟାବେସ୍‌ରୁ ଏହି ପ୍ରୋଡକ୍ଟ ଡିଲିଟ୍ ହୋଇଯିବ। ନିଶ୍ଚିତ କି?"
)
PANEL_TEXT_DEL_DONE = "🗑 ଡିଲିଟ୍ ହେଲା: <b>{name}</b>"
PANEL_TEXT_DEL_FAIL = "❌ ଡିଲିଟ୍ କରିପାରିଲା ନାହିଁ: {err}"


def panel_keyboard() -> InlineKeyboardMarkup:
    """"🛡️ Admin Panel" button shown to the admin on /start (main.py)."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🛡️ Admin Panel", callback_data="admin_panel")]]
    )


def _active_ai_button_label() -> str:
    """Current engine for the toggle button (never breaks the panel)."""
    try:
        import ai_agent

        return f"🤖 Active AI: {ai_agent.get_active_ai()}"
    except Exception:  # noqa: BLE001 - label must always render
        return "🤖 Active AI: Grok"


def _panel_home_keyboard(pincode: Optional[str] = None) -> InlineKeyboardMarkup:
    pin_label = f"📍 Pincode: {pincode}" if pincode else "📍 Set / Change Pincode"
    rows = [
        [InlineKeyboardButton("➕ Search & Add Product", callback_data="admin_search")],
        [InlineKeyboardButton("➕ Add Product Manually", callback_data="admin_manual")],
        [InlineKeyboardButton("🖼 Image Link Generator", callback_data="admin_imglink")],
        [InlineKeyboardButton("🗂 Browse by Category", callback_data="admin_browse")],
        [InlineKeyboardButton("📦 Orders (manage)", callback_data="admin_orders")],
        [InlineKeyboardButton("👥 Add Admin", callback_data="admin_addadmin")],
        [InlineKeyboardButton(_active_ai_button_label(), callback_data="admin_ai_toggle")],
        [InlineKeyboardButton(pin_label, callback_data="admin_setpin")],
    ]
    if pincode:  # allow resetting back to the default fallback list
        rows.append(
            [InlineKeyboardButton("♻️ Reset Pincode", callback_data="admin_clearpin")]
        )
    rows.append([InlineKeyboardButton("♻️ Restart Bot", callback_data="admin_restart")])
    rows.append([InlineKeyboardButton("❌ Close", callback_data="admin_close")])
    return InlineKeyboardMarkup(rows)


def _admins_manage_keyboard() -> InlineKeyboardMarkup:
    """List of runtime-added admins with a Remove button each (env-driven
    primary admins cannot be removed from the panel)."""
    rows = [
        [InlineKeyboardButton(f"🗑 Remove {uid}", callback_data=f"admin_rmadmin_{uid}")]
        for uid in _load_extra_admins()
    ]
    rows.append([InlineKeyboardButton("➕ Add Admin", callback_data="admin_addadmin")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)


def _db_channel_id() -> int:
    """Private channel that hosts manual-add product images (0 = disabled)."""
    try:
        return int((os.getenv("DB_CHANNEL_ID") or "").strip() or 0)
    except ValueError:
        return 0


def _manual_categories_keyboard(categories: List[str]) -> InlineKeyboardMarkup:
    """Step-1 picker: sheet categories + Type New + Cancel."""
    rows = [
        [InlineKeyboardButton(f"🗂 {cat}", callback_data=f"admin_manual_cat_{i}")]
        for i, cat in enumerate(categories[:40])
    ]
    rows.append(
        [InlineKeyboardButton("➕ Type New Category", callback_data="admin_manual_newcat")]
    )
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="admin_manual_cancel")])
    return InlineKeyboardMarkup(rows)


def _manual_prompt_keyboard(include_skip_ean: bool = False) -> InlineKeyboardMarkup:
    """Typed-step prompts (Cancel always; Skip only on the EAN step)."""
    rows = []
    if include_skip_ean:
        rows.append(
            [InlineKeyboardButton("⏭️ Skip / Auto-Generate", callback_data="admin_manual_skip_ean")]
        )
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="admin_manual_cancel")])
    return InlineKeyboardMarkup(rows)


def _generate_ean_candidate() -> str:
    """13-digit candidate EAN for products without a barcode."""
    return "2" + str(random.randint(10**11, 10**12 - 1))


async def _start_manual_flow(query, context) -> None:
    """Manual Add: load sheet categories and show the Step-1 picker."""
    await query.answer()
    try:
        categories = await asyncio.to_thread(db.get_categories)
    except (db.DatabaseError, ValueError) as exc:
        logger.error("Manual add: category load failed: %s", exc)
        categories = []
    context.user_data["manual"] = {}
    context.user_data["manual_categories"] = categories
    context.user_data["state"] = STATE_MANUAL_CATEGORY
    await _edit_to_html(
        query.message, PANEL_TEXT_MANUAL_START,
        _manual_categories_keyboard(categories),
    )


async def _cancel_manual(query, context) -> None:
    """Drop the in-progress manual product and return to the panel home."""
    context.user_data.pop("manual", None)
    context.user_data.pop("manual_categories", None)
    context.user_data.pop("state", None)
    await query.answer("❌ Cancelled.")
    await _edit_to_html(
        query.message, PANEL_TEXT_MANUAL_CANCELLED,
        _panel_home_keyboard(context.user_data.get("panel_pincode")),
    )


async def _finish_manual_product(context, reply_message, ean: Optional[str] = None) -> None:
    """Save the collected manual product to the sheet + send the preview card.

    Uses the exact gspread path (``db.add_product``) as every other flow.
    """
    manual = context.user_data.get("manual") or {}
    name = str(manual.get("name") or "").strip()
    category = str(manual.get("category") or "General").strip() or "General"
    image = str(manual.get("image") or "").strip()
    if not name:
        await reply_message.reply_text(PANEL_TEXT_MANUAL_BAD, parse_mode=ParseMode.HTML)
        return
    if ean is None:
        # Skip/ Auto-Generate: pick a 13-digit EAN that is not in the sheet.
        for _ in range(5):
            candidate = _generate_ean_candidate()
            try:
                found = await asyncio.to_thread(db.find_by_ean, candidate)
            except db.DatabaseError as exc:
                logger.error("Manual add: EAN check failed: %s", exc)
                await reply_message.reply_text(f"❌ {exc}")
                return
            if found is None:
                ean = candidate
                break
        else:
            await reply_message.reply_text(PANEL_TEXT_MANUAL_BAD)
            return
    try:
        stored = await asyncio.to_thread(
            db.add_product,
            name, manual.get("mrp"), manual.get("price"),
            manual.get("qty"), ean, image, category,
        )
    except (db.DatabaseError, ValueError) as exc:
        logger.error("Manual add failed for %r: %s", name, exc)
        await reply_message.reply_text(f"❌ {exc}")
        return
    context.user_data.pop("manual", None)
    context.user_data.pop("manual_categories", None)
    context.user_data.pop("state", None)
    done = PANEL_TEXT_MANUAL_DONE.format(
        name=html.escape(str(stored.get("product_name") or "Product")),
        mrp=_format_money(stored.get("mrp") or 0),
        price=_format_money(stored.get("selling_price") or 0),
        qty=stored.get("stock_qty", 0),
        ean=html.escape(str(stored.get("ean") or "")),
        category=html.escape(str(stored.get("category") or "General")),
    )
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Admin Panel", callback_data="admin_panel")]]
    )
    if image:
        try:
            await context.bot.send_photo(
                chat_id=reply_message.chat_id, photo=image,
                caption=done, parse_mode=ParseMode.HTML, reply_markup=keyboard,
            )
            return
        except TelegramBadRequest as exc:
            logger.warning("Manual preview photo failed, sending text: %s", exc)
    await reply_message.reply_text(
        done, parse_mode=ParseMode.HTML, reply_markup=keyboard
    )


def _search_page_keyboard(page: int, total_pages: int) -> InlineKeyboardMarkup:
    """Pagination bar shown under a page of results."""
    row = []
    if page > 1:
        row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin_page_{page - 1}"))
    if page < total_pages:
        row.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin_page_{page + 1}"))
    rows = [row] if row else []
    rows.append([InlineKeyboardButton("✅ Search Done", callback_data="admin_search_done")])
    return InlineKeyboardMarkup(rows)


def _product_keyboard(prod: Dict[str, Any]) -> InlineKeyboardMarkup:
    """Export button (becomes "✅ Saved to Database" after export) + the
    inline edit buttons (Name, MRP, Price, Qty, Pack/Weight, Category)."""
    uid = str(prod.get("uid") or "")
    saved = bool(prod.get("saved"))
    export_label = "✅ Saved to Database" if saved else "📥 Export to Database"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(export_label, callback_data=f"export_item_{uid}")],
            [
                InlineKeyboardButton("✏️ Edit Name", callback_data=f"edit_name_{uid}"),
                InlineKeyboardButton("✏️ Edit Category", callback_data=f"edit_category_{uid}"),
            ],
            [
                InlineKeyboardButton("✏️ Edit MRP", callback_data=f"edit_mrp_{uid}"),
                InlineKeyboardButton("✏️ Edit Price", callback_data=f"edit_price_{uid}"),
            ],
            [
                InlineKeyboardButton("✏️ Edit Qty", callback_data=f"edit_qty_{uid}"),
                InlineKeyboardButton("✏️ Edit Pack/Weight", callback_data=f"edit_pack_{uid}"),
            ],
        ]
    )


def _render_product_card(prod: Dict[str, Any]) -> str:
    """HTML caption for a product card (Odia labels, as the admin chats in
    Odia). Shows name, pack, category, MRP, selling price and per-pincode
    stock."""
    name = html.escape(str(prod.get("name") or "Product"))
    lines = [f"🛒 <b>{name}</b>"]
    pack = str(prod.get("pack") or prod.get("size") or "").strip()
    if pack:
        lines.append(f"📦 Pack: {html.escape(pack)}")
    category = str(prod.get("category") or "").strip()
    if category:
        lines.append(f"🗂 Category: {html.escape(category)}")
    mrp = _format_money(prod.get("mrp") or 0)
    price = _format_money(prod.get("price") or 0)
    lines.append(f"💰 MRP: ₹{mrp} | ବିକ୍ରି ମୂଲ୍ୟ: ₹{price}")
    if prod.get("in_stock"):
        pin = html.escape(str(prod.get("pincode") or ""))
        lines.append(f"🟢 ଷ୍ଟକ ଅଛି" + (f" (PIN {pin})" if pin else ""))
    else:
        lines.append("🔴 ଷ୍ଟକ ନାହିଁ")
    return "\n".join(lines)


def _search_products_sync(
    keyword: str, pincode: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Fetch products for ``keyword``, looping the pincode list.

    ``pincode`` (set manually by the admin) is checked FIRST; if nothing
    in-stock comes back, the default PANEL_PINCODES fallback list is tried in
    order. For every pincode the JioMart catalog API is queried (the
    ``x-location-detail`` header carries the pincode - see jiomart_search).
    The first pincode whose results contain an in-stock item wins; sellable
    items are ordered first and up to PANEL_MAX_FETCH are returned so the
    caller can paginate. If no pincode has stock, the last answered pincode's
    top items are returned (marked out of stock). Never raises.
    """
    order: List[str] = []
    if pincode and pincode not in PANEL_PINCODES:
        order.append(pincode)          # manual pin wins
    order.extend(PANEL_PINCODES)

    fallback: List[Dict[str, Any]] = []
    for pin in order:
        try:
            items = jiomart_search.fetch_items(keyword, pin, page_size=PANEL_MAX_FETCH)
        except Exception as exc:  # noqa: BLE001 - defensive; fetch_items is safe
            logger.warning("Panel search failed for PIN %s: %s", pin, exc)
            continue
        if not items:
            continue
        products: List[Dict[str, Any]] = []
        for raw in items[:PANEL_MAX_FETCH]:
            try:
                prod = jiomart_search._normalize_item(raw)
            except Exception as exc:  # noqa: BLE001 - skip malformed items
                logger.debug("Skipping malformed item: %s", exc)
                continue
            prod["pincode"] = pin
            products.append(prod)
        if not products:
            continue
        in_stock = [p for p in products if p.get("in_stock")]
        if in_stock:
            ordered = in_stock + [p for p in products if not p.get("in_stock")]
            logger.info(
                "Panel search %r: in stock at PIN %s (%d hits)",
                keyword, pin, len(in_stock),
            )
            return ordered[:PANEL_MAX_FETCH]
        fallback = products[:PANEL_MAX_FETCH]
    if fallback:
        logger.info("Panel search %r: no pincode in stock; using last results", keyword)
    return fallback


async def _send_product_cards(message, keyword: str, context) -> None:
    """Search JioMart, cache all results, and post page 1 of photo cards.

    The search session stays active (state == STATE_SEARCH_PRODUCT) so the
    admin can simply type another word to search again; the pagination bar's
    "✅ Search Done" button ends the session.
    """
    status = await message.reply_text(PANEL_TEXT_SEARCHING)
    pincode = context.user_data.get("panel_pincode")
    products = await asyncio.to_thread(_search_products_sync, keyword, pincode)
    context.user_data["state"] = STATE_SEARCH_PRODUCT  # keep searching alive
    context.user_data["panel_chat_id"] = message.chat_id
    _BOT_CACHE[message.chat_id] = context.bot
    if not products:
        try:
            await status.edit_text(PANEL_TEXT_NO_RESULTS, parse_mode=ParseMode.HTML)
        except TelegramBadRequest:
            pass
        return
    try:
        await status.delete()
    except TelegramBadRequest:
        pass
    context.user_data["panel_results"] = products
    context.user_data["panel_keyword"] = keyword
    context.user_data["panel_page"] = 1
    context.user_data["panel_card_ids"] = []
    context.user_data["panel_nav_id"] = None
    await _render_panel_page(context)


async def _render_panel_page(context) -> None:
    """Send the current page of cached results (PANEL_PAGE_SIZE cards) + a
    pagination bar (Prev / Next / Search Done)."""
    products = context.user_data.get("panel_results") or []
    if not products:
        return
    page = int(context.user_data.get("panel_page") or 1)
    total = len(products)
    total_pages = max(1, -(-total // PANEL_PAGE_SIZE))  # ceil division
    page = max(1, min(page, total_pages))
    context.user_data["panel_page"] = page
    start = (page - 1) * PANEL_PAGE_SIZE
    chunk = products[start:start + PANEL_PAGE_SIZE]
    chat_id = context.user_data.get("panel_chat_id")
    bot = context.bot
    sent_ids: List[int] = []
    for prod in chunk:
        uid = str(prod.get("uid") or "")
        if not uid:
            continue
        _product_cache[uid] = prod
        caption = _render_product_card(prod)
        keyboard = _product_keyboard(prod)
        image = str(prod.get("image") or "").strip()
        try:
            if image:
                sent = await bot.send_photo(
                    chat_id=chat_id, photo=image, caption=caption,
                    parse_mode=ParseMode.HTML, reply_markup=keyboard,
                )
            else:
                sent = await bot.send_message(
                    chat_id=chat_id, text=caption,
                    parse_mode=ParseMode.HTML, reply_markup=keyboard,
                )
            prod["card_chat_id"] = chat_id
            prod["card_message_id"] = getattr(sent, "message_id", None)
            _product_cache[uid] = prod
            sent_ids.append(sent.message_id)
        except TelegramBadRequest as exc:
            logger.error("Could not send product card %s: %s", uid, exc)
    # pagination bar under the cards
    header = PANEL_TEXT_PAGE_HEADER.format(page=page, total=total)
    try:
        nav = await bot.send_message(
            chat_id=chat_id, text=header, parse_mode=ParseMode.HTML,
            reply_markup=_search_page_keyboard(page, total_pages),
        )
        context.user_data["panel_nav_id"] = nav.message_id
    except TelegramBadRequest as exc:
        logger.warning("Could not send pagination bar: %s", exc)
    context.user_data["panel_card_ids"] = sent_ids


async def _clear_panel_page(context) -> None:
    """Delete the current page's cards + pagination bar (before paging)."""
    chat_id = context.user_data.get("panel_chat_id")
    bot = context.bot
    ids = list(context.user_data.get("panel_card_ids") or [])
    nav_id = context.user_data.get("panel_nav_id")
    if nav_id:
        ids.append(nav_id)
    for mid in ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=int(mid))
        except Exception:  # noqa: BLE001 - message may already be gone
            pass
    context.user_data["panel_card_ids"] = []
    context.user_data["panel_nav_id"] = None


async def _refresh_card(query, prod: Dict[str, Any]) -> None:
    """Re-render the clicked card's caption + buttons (export/edit rows)."""
    try:
        await query.edit_message_caption(
            caption=_render_product_card(prod),
            parse_mode=ParseMode.HTML,
            reply_markup=_product_keyboard(prod),
        )
    except (TelegramBadRequest, Exception) as exc:  # noqa: BLE001
        # photo->text fallback cards have no caption; try editing the text
        logger.debug("edit_message_caption failed (%s); trying edit_text", exc)
        try:
            await query.edit_message_text(
                _render_product_card(prod),
                parse_mode=ParseMode.HTML,
                reply_markup=_product_keyboard(prod),
            )
        except Exception as exc2:  # noqa: BLE001
            logger.warning("Could not refresh product card: %s", exc2)


async def _handle_export(query, context, uid: str) -> None:
    """📥 Export to Database: duplicate check then one-click insert."""
    prod = _product_cache.get(uid)
    if prod is None:
        await query.answer(PANEL_TEXT_EXPIRED, show_alert=True)
        return
    if prod.get("saved"):
        await query.answer(PANEL_TEXT_SAVED_TOAST)
        return
    try:
        found = await asyncio.to_thread(db.find_by_ean, uid)
    except (db.DatabaseError, ValueError) as exc:
        logger.error("Export duplicate check failed for %s: %s", uid, exc)
        await query.answer(f"❌ {exc}", show_alert=True)
        return
    if found is not None:
        prod["saved"] = True
        _product_cache[uid] = prod
        await _refresh_card(query, prod)
        await query.answer(PANEL_TEXT_DUP_TOAST, show_alert=True)
        return
    try:
        await asyncio.to_thread(
            db.add_product,
            prod.get("name") or "Product",
            prod.get("mrp") or 0,
            prod.get("price") or 0,
            PANEL_DEFAULT_QTY,           # default Stock Qty = 10
            uid,                         # JioMart uid stored as the EAN/UID
            prod.get("image") or "",
            prod.get("category") or "General",  # auto-detected category
        )
    except (db.DatabaseError, ValueError) as exc:
        logger.error("Export add_product failed for %s: %s", uid, exc)
        await query.answer(f"❌ {exc}", show_alert=True)
        return
    prod["saved"] = True
    prod.setdefault("qty", PANEL_DEFAULT_QTY)
    _product_cache[uid] = prod
    await _refresh_card(query, prod)
    await query.answer(PANEL_TEXT_SAVED_TOAST)


# --------------------------------------------------------------------------- #
# Admin Panel: Browse by Category (Product > Category > Item) + Delete
# --------------------------------------------------------------------------- #
# chat_id -> {category: [product rows]} snapshot so a category tap can list its
# items without re-reading the sheet, and edits/deletes stay in sync.
_category_cache: Dict[int, Dict[str, List[Dict[str, Any]]]] = {}


def _num_safe(value: Any) -> Any:
    """Best-effort number for a sheet cell; returns 0 when unparseable."""
    try:
        cleaned = _MONEY_STRIP.sub("", str(value or ""))
        if not cleaned:
            return 0
        number = float(cleaned)
        return int(number) if number.is_integer() else number
    except (TypeError, ValueError):
        return 0


def _row_to_card(prod: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a db.get_all_products() row into a card dict for the shared
    renderer/keyboard (already saved in the database)."""
    uid = str(prod.get("ean") or "").strip()
    return {
        "uid": uid,
        "name": prod.get("product_name") or "Product",
        "pack": "",
        "mrp": _num_safe(prod.get("mrp")),
        "price": _num_safe(prod.get("selling_price")),
        "qty": _num_safe(prod.get("stock_qty")),
        "category": prod.get("category") or "General",
        "image": prod.get("image_url") or "",
        "in_stock": _num_safe(prod.get("stock_qty")) > 0,
        "pincode": "",
        "saved": True,
        "browse": True,  # saved DB item -> keep the Delete/Back editor keyboard
    }


def _browse_categories_keyboard(categories: List[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"🗂 {cat}", callback_data=f"admin_cat_{i}")]
        for i, cat in enumerate(categories)
    ]
    rows.append([InlineKeyboardButton("🔍 Search", callback_data="admin_browse_search")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)


def _browse_items_keyboard(uids: List[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🛒 Open item", callback_data=f"admin_item_{uid}")]
        for uid in uids
    ]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="admin_browse")])
    return InlineKeyboardMarkup(rows)


def _browse_item_keyboard(uid: str) -> InlineKeyboardMarkup:
    """Full editor for a saved item: all edit fields + a Delete button."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏️ Edit Name", callback_data=f"edit_name_{uid}"),
                InlineKeyboardButton("✏️ Edit Category", callback_data=f"edit_category_{uid}"),
            ],
            [
                InlineKeyboardButton("✏️ Edit MRP", callback_data=f"edit_mrp_{uid}"),
                InlineKeyboardButton("✏️ Edit Price", callback_data=f"edit_price_{uid}"),
            ],
            [
                InlineKeyboardButton("✏️ Edit Qty", callback_data=f"edit_qty_{uid}"),
                InlineKeyboardButton("✏️ Edit Pack/Weight", callback_data=f"edit_pack_{uid}"),
            ],
            [
                InlineKeyboardButton("🗑 Delete", callback_data=f"admin_del_{uid}"),
                InlineKeyboardButton("⬅️ Back", callback_data=f"admin_backcard_{uid}"),
            ],
        ]
    )


def _render_browse_card(prod: Dict[str, Any]) -> str:
    """HTML caption for a saved product card in Browse-by-Category.

    Shows Product Name, Category, MRP, Selling Price and the live Stock Qty.
    A qty of 0 is labelled Out of stock (the row is kept in the database).
    """
    name = html.escape(str(prod.get("name") or "Product"))
    lines = [f"🛒 <b>{name}</b>"]
    category = str(prod.get("category") or "").strip()
    if category:
        lines.append(f"🗂 Category: {html.escape(category)}")
    mrp = _format_money(prod.get("mrp") or 0)
    price = _format_money(prod.get("price") or 0)
    lines.append(f"💰 MRP: ₹{mrp} | 💰 Selling: ₹{price}")
    qty = _num_safe(prod.get("qty"))
    try:
        qty_int = int(qty)
    except (TypeError, ValueError):
        qty_int = 0
    lines.append(f"📦 Stock Qty: <b>{qty_int}</b>")
    lines.append("🟢 In stock" if qty_int > 0 else "🔴 Out of stock")
    return "\n".join(lines)


def _browse_stock_keyboard(uid: str, qty: Any) -> InlineKeyboardMarkup:
    """Stock controls for one saved product card:
    [➖] [Current Qty] [➕]  +  [✏️ Edit Details]  +  [🗑️ Delete Item]."""
    try:
        qty_int = int(_num_safe(qty))
    except (TypeError, ValueError):
        qty_int = 0
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➖", callback_data=f"admin_stock_dec_{uid}"),
                InlineKeyboardButton(str(qty_int), callback_data=f"admin_stock_info_{uid}"),
                InlineKeyboardButton("➕", callback_data=f"admin_stock_inc_{uid}"),
            ],
            [
                InlineKeyboardButton("✏️ Edit Details", callback_data=f"admin_editmenu_{uid}"),
            ],
            [
                InlineKeyboardButton("🗑️ Delete Item", callback_data=f"admin_del_{uid}"),
                InlineKeyboardButton("⬅️ Categories", callback_data="admin_browse"),
            ],
        ]
    )


async def _show_delete_confirm(query, uid: str) -> None:
    """Ask for confirmation before deleting a saved product."""
    prod = _product_cache.get(uid) or {}
    name = html.escape(str(prod.get("name") or "Product"))
    text = PANEL_TEXT_DEL_ASK.format(name=name, uid=html.escape(uid))
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🗑 Yes, delete", callback_data=f"admin_delconfirm_{uid}")],
            [InlineKeyboardButton("⬅️ Cancel", callback_data=f"admin_backcard_{uid}")],
        ]
    )
    await _edit_to_html(query.message, text, keyboard)


async def _do_delete(query, context, uid: str) -> None:
    """Delete the product row from the database and confirm."""
    prod = _product_cache.get(uid) or {}
    name = str(prod.get("name") or "Product")
    try:
        await asyncio.to_thread(db.delete_product_by_ean, uid)
    except (db.DatabaseError, ValueError) as exc:
        logger.error("Delete failed for %s: %s", uid, exc)
        await query.answer(PANEL_TEXT_DEL_FAIL.format(err=exc), show_alert=True)
        return
    _product_cache.pop(uid, None)
    # keep the browse snapshot in sync so the category list stays accurate
    chat_id = getattr(query.message, "chat_id", None)
    if chat_id is not None:
        snapshot = _category_cache.get(int(chat_id))
        if snapshot:
            for cat in list(snapshot.keys()):
                snapshot[cat] = [p for p in snapshot[cat] if str(p.get("ean") or "") != uid]
    await query.answer()
    await _edit_to_html(
        query.message,
        PANEL_TEXT_DEL_DONE.format(name=html.escape(name)),
        _panel_home_keyboard(context.user_data.get("panel_pincode")),
    )


async def _load_categories(context) -> List[str]:
    """Fetch distinct categories + their products from the sheet into cache."""
    chat_id = context.user_data.get("panel_chat_id")
    categories = await asyncio.to_thread(db.get_categories)
    products = await asyncio.to_thread(db.get_all_products)
    snapshot: Dict[str, List[Dict[str, Any]]] = {cat: [] for cat in categories}
    for prod in products:
        cat = str(prod.get("category") or "General")
        snapshot.setdefault(cat, []).append(prod)
    if chat_id is not None:
        _category_cache[int(chat_id)] = snapshot
    return categories


async def _send_browse_search_cards(message, context, keyword: str) -> None:
    """Search our database and post each match as a stock-manage card.

    Same cards as Browse-by-Category (photo + caption + [+/-] stock controls,
    Edit Details, Delete), so the admin can manage stock from search results
    directly. The search session stays alive (state == STATE_BROWSE_SEARCH) so
    the admin can type another keyword.
    """
    try:
        products = await asyncio.to_thread(db.search_products, keyword, 25)
    except (db.DatabaseError, ValueError) as exc:
        logger.error("Browse search failed for %r: %s", keyword, exc)
        await message.reply_text(
            PANEL_TEXT_DEL_FAIL.format(err=exc), parse_mode=ParseMode.HTML
        )
        return
    context.user_data["state"] = STATE_BROWSE_SEARCH  # keep searching alive
    context.user_data["panel_chat_id"] = message.chat_id
    _BOT_CACHE[message.chat_id] = context.bot
    if not products:
        await message.reply_text(
            PANEL_TEXT_NO_RESULTS, parse_mode=ParseMode.HTML,
            reply_markup=_browse_categories_keyboard(
                list(_category_cache.get(int(message.chat_id), {}).keys())
            ),
        )
        return
    chat_id = message.chat_id
    header = (
        "🔍 <b>ସର୍ଚ୍ଚ ଫଳାଫଳ</b> - "
        f"<b>{html.escape(keyword)}</b> ({len(products)}ଟି):"
    )
    try:
        await message.reply_text(header, parse_mode=ParseMode.HTML)
    except TelegramBadRequest:
        pass
    for prod in products:
        uid = str(prod.get("ean") or "").strip()
        if not uid:
            continue
        card = _row_to_card(prod)
        card["card_chat_id"] = chat_id
        _product_cache[uid] = card
        image = str(card.get("image") or "").strip()
        caption = _render_browse_card(card)
        keyboard = _browse_stock_keyboard(uid, card.get("qty"))
        try:
            if image:
                sent = await context.bot.send_photo(
                    chat_id=chat_id, photo=image, caption=caption,
                    parse_mode=ParseMode.HTML, reply_markup=keyboard,
                )
            else:
                sent = await context.bot.send_message(
                    chat_id=chat_id, text=caption,
                    parse_mode=ParseMode.HTML, reply_markup=keyboard,
                )
        except TelegramBadRequest as exc:
            logger.warning("Could not send browse-search card %s: %s", uid, exc)
            continue
        card["card_message_id"] = getattr(sent, "message_id", None)
        _product_cache[uid] = card
    nav = InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Browse by Category", callback_data="admin_browse")]]
    )
    try:
        await context.bot.send_message(
            chat_id=chat_id, text="—", reply_markup=nav,
        )
    except TelegramBadRequest:
        pass


async def _adjust_stock(query, context, uid: str, delta: int) -> None:
    """Apply a +1/-1 stock change from a browse card's stepper buttons.

    Writes the new Stock Qty to the central database in real time. A quantity
    of 0 is kept as an Out of Stock row (never auto-deleted); only the explicit
    [🗑️ Delete Item] button removes it. The card caption + buttons re-render
    so the admin always sees the live count.
    """
    await query.answer()
    prod = _product_cache.get(uid)
    if prod is None:
        await query.answer(PANEL_TEXT_EXPIRED, show_alert=True)
        return
    try:
        summary = await asyncio.to_thread(db.adjust_stock_by_ean, uid, delta)
    except (db.DatabaseError, ValueError) as exc:
        logger.error("Stock adjust failed for %s: %s", uid, exc)
        await query.answer(f"❌ {exc}", show_alert=True)
        return
    new_qty = summary.get("new_stock", 0)
    prod["qty"] = new_qty
    prod["in_stock"] = new_qty > 0
    _product_cache[uid] = prod
    # keep the browse snapshot in sync so the category list stays accurate
    chat_id = getattr(query.message, "chat_id", None)
    if chat_id is not None:
        snapshot = _category_cache.get(int(chat_id))
        if snapshot:
            for cat_items in snapshot.values():
                for p in cat_items:
                    if str(p.get("ean") or "") == uid:
                        p["stock_qty"] = new_qty
    # re-render this card in place (caption + stock keyboard)
    try:
        await query.edit_message_caption(
            caption=_render_browse_card(prod),
            parse_mode=ParseMode.HTML,
            reply_markup=_browse_stock_keyboard(uid, new_qty),
        )
    except Exception as exc:  # noqa: BLE001 - text cards have no caption
        logger.debug("edit_message_caption failed for %s (%s); trying edit_text", uid, exc)
        try:
            await query.edit_message_text(
                _render_browse_card(prod),
                parse_mode=ParseMode.HTML,
                reply_markup=_browse_stock_keyboard(uid, new_qty),
            )
        except Exception as exc2:  # noqa: BLE001
            logger.warning("Could not refresh browse card %s: %s", uid, exc2)
    if new_qty == 0:
        await query.answer("🔴 Out of stock (row kept)", show_alert=True)


# --------------------------------------------------------------------------- #
# Orders workflow: seller accepts -> packs -> delivers; customer msg auto-updates
# ---------------------------------------------------------------------------
import re as _re_order


def _parse_items_text(items_text):
    """Parse 'Name x2 @ Rs.170; Name2 x1 @ Rs.25' back into bill lines."""
    lines = []
    for chunk in str(items_text or "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = _re_order.match(r"^(.*?)\s+x(\d+)\s+@\s*Rs\.?([0-9.]+)\s*$", chunk)
        if m:
            name = m.group(1).strip()
            qty = int(m.group(2))
            price = float(m.group(3))
            lines.append(
                {"name": name, "qty": qty, "price": price,
                 "line_total": round(price * qty, 2)}
            )
    return lines


def _items_text_from_lines(lines):
    return "; ".join(f"{l['name']} x{l['qty']} @ Rs.{l['price']}" for l in lines)


def _order_to_summary(order):
    """Build a summary dict (compatible with orders.order_status_text)."""
    lines = _parse_items_text((order or {}).get("items"))
    total = sum(float(l["line_total"] or 0) for l in lines)
    total = int(total) if float(total).is_integer() else round(total, 2)
    return {
        "order_id": (order or {}).get("order_id"),
        "lines": lines,
        "total": total,
        "address": (order or {}).get("address"),
    }


async def _update_customer_status(context, order, status=None, summary=None):
    """Edit the customer's LIVE status message in place (best effort)."""
    import orders as _orders_mod

    tg = str((order or {}).get("telegram_user_id") or "").strip()
    mid = str((order or {}).get("status_msg_id") or "").strip()
    if not tg or not mid:
        return False
    new_status = status or (order or {}).get("status") or "pending"
    if summary is None:
        summary = _order_to_summary(order)
    summary = dict(summary)
    summary["status"] = new_status
    text = _orders_mod.order_status_text(summary, new_status)
    try:
        from telegram.constants import ParseMode as _PM

        await context.bot.edit_message_text(
            chat_id=int(tg), message_id=int(mid), text=text, parse_mode=_PM.HTML
        )
        return True
    except Exception as exc:  # noqa: BLE001 - stale/identical message
        logger.debug(
            "Could not update customer status for %s: %s",
            (order or {}).get("order_id"), exc,
        )
        return False


def _fetch_order(order_id):
    """Return one order dict by id (newest match), or None."""
    try:
        import db as _db

        for order in _db.get_recent_orders(limit=200):
            if str(order.get("order_id") or "").strip() == str(order_id or "").strip():
                return order
    except Exception as exc:  # noqa: BLE001
        logger.error("Order lookup failed for %s: %s", order_id, exc)
    return None


def _order_status_emoji(status):
    return {
        "pending": "🕐", "accepted": "✅", "packing": "📦",
        "out_for_delivery": "🚚", "delivered": "🎉", "cancelled": "❌",
    }.get(status, "•")


def _orders_list_text(orders):
    if not orders:
        return "📭 No orders yet."
    lines = ["📦 <b>Recent orders</b> (newest first)\n"]
    for o in orders:
        status = o.get("status") or "pending"
        emoji = _order_status_emoji(status)
        name = html.escape(str(o.get("customer_name") or "Customer"))
        oid = html.escape(str(o.get("order_id") or ""))
        total = html.escape(str(o.get("total") or ""))
        lines.append(f"{emoji} <code>{oid}</code> — {name} — Rs.{total}")
    return "\n".join(lines)


def _orders_list_keyboard(orders):
    rows = []
    for o in orders:
        oid = str(o.get("order_id") or "").strip()
        if not oid:
            continue
        emoji = _order_status_emoji(o.get("status") or "pending")
        name = str(o.get("customer_name") or "Customer")[:16]
        rows.append([
            InlineKeyboardButton(
                f"{emoji} {name} • Rs.{o.get('total')}",
                callback_data=f"admin_ordv_{oid}",
            )
        ])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)


def _order_detail_text(order):
    status = (order or {}).get("status") or "pending"
    label = db.ORDER_STATUS_LABELS.get(status, status)
    oid = html.escape(str((order or {}).get("order_id") or ""))
    lines = [
        f"🧾 <b>Order <code>{oid}</code></b>",
        f"📊 Status: {label}",
        f"👤 {html.escape(str((order or {}).get('customer_name') or '-'))}",
        f"📞 {html.escape(str((order or {}).get('phone') or '-'))}",
        f"📍 {html.escape(str((order or {}).get('address') or '-'))}",
    ]
    loc = str((order or {}).get("location") or "").strip()
    if loc:
        lines.append(f"🗺️ <a href='https://maps.google.com/?q={loc}'>Open delivery map</a>")
    lines.append("")
    lines.append(f"🧺 {html.escape(str((order or {}).get('items') or '-'))}")
    lines.append(f"💰 Total: Rs.{html.escape(str((order or {}).get('total') or ''))}")
    return "\n".join(lines)


def _order_detail_keyboard(order):
    status = (order or {}).get("status") or "pending"
    oid = str((order or {}).get("order_id") or "").strip()
    rows = []
    if status == "pending":
        rows.append([
            InlineKeyboardButton("✅ Accept", callback_data=f"admin_ord_ok_{oid}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"admin_ord_no_{oid}"),
        ])
    elif status == "accepted":
        rows.append([
            InlineKeyboardButton("➖ Edit bill", callback_data=f"admin_ord_bill_{oid}"),
            InlineKeyboardButton("📦 Start packing", callback_data=f"admin_ord_next_{oid}"),
        ])
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"admin_ord_no_{oid}")])
    elif status == "packing":
        rows.append([InlineKeyboardButton("🚚 Out for delivery", callback_data=f"admin_ord_next_{oid}")])
        rows.append([InlineKeyboardButton("➖ Edit bill", callback_data=f"admin_ord_bill_{oid}")])
    elif status == "out_for_delivery":
        rows.append([InlineKeyboardButton("✅ Confirm delivered", callback_data=f"admin_ord_next_{oid}")])
    rows.append([InlineKeyboardButton("⬅️ All orders", callback_data="admin_orders")])
    return InlineKeyboardMarkup(rows)


def _editbill_keyboard(order):
    """One 'remove' button per bill line + save/back."""
    oid = str((order or {}).get("order_id") or "").strip()
    lines = _parse_items_text((order or {}).get("items"))
    rows = []
    for i, line in enumerate(lines):
        rows.append([InlineKeyboardButton(
            f"➖ Remove {line['name'][:18]} x{line['qty']}",
            callback_data=f"admin_ord_rm_{oid}_{i}",
        )])
    rows.append([InlineKeyboardButton("⬅️ Back to order", callback_data=f"admin_ordv_{oid}")])
    return InlineKeyboardMarkup(rows)


# Admin Panel: callback queries (panel / search / export / edit_*)
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# 🖼 Image Link Generator (manual product photo / offer banner URL)
# --------------------------------------------------------------------------- #
IMGLINK_BATCH = 3      # photos shown per page
IMGLINK_MAX_START = 60


def _imglink_keyboard(start_index: int) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    if start_index + IMGLINK_BATCH <= IMGLINK_MAX_START:
        rows.append(
            [InlineKeyboardButton("More ➡️", callback_data=f"admin_imglink_more_{start_index + IMGLINK_BATCH}")]
        )
    rows.append([InlineKeyboardButton("📋 All URLs (copy)", callback_data="admin_imglink_urls")])
    rows.append([InlineKeyboardButton("⬅️ Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)


async def _send_imglink_photos(message, context, keyword: str, start_index: int = 1) -> None:
    """Search JioMart and post photo cards whose captions carry the image URL.

    Each caption has BOTH a clickable link and the raw URL in a ``code`` block
    (Telegram's tap-to-copy bar), so the admin can copy the URL for a manual
    product or an offer banner. Results are cached in ``context.user_data`` so
    "All URLs" / "More" can reuse them.
    """
    keyword = str(keyword or "").strip()
    if not keyword:
        await message.reply_text(
            PANEL_TEXT_IMGLINK_BAD, parse_mode=ParseMode.HTML,
            reply_markup=_panel_home_keyboard(context.user_data.get("panel_pincode")),
        )
        return
    urls: List[str] = await asyncio.to_thread(
        fetch_images, keyword, start_index
    )
    urls = [u for u in (urls or []) if isinstance(u, str) and u.startswith("http")][:IMGLINK_BATCH]
    if not urls:
        await message.reply_text(PANEL_TEXT_IMGLINK_NONE, parse_mode=ParseMode.HTML)
        return  # stay in STATE_IMAGE_LINK so the admin can type another keyword
    context.user_data["imglink_urls"] = urls
    context.user_data["imglink_start"] = start_index
    context.user_data["imglink_keyword"] = keyword
    context.user_data["state"] = STATE_IMAGE_LINK
    for index, url in enumerate(urls):
        caption = (
            f"🖼 <b>{html.escape(keyword)}</b> — ଫଟୋ {start_index + index}\n"
            f"🔗 <a href='{url}'>{url}</a>\n"
            f"<code>{url}</code>"
        )
        try:
            await context.bot.send_photo(
                chat_id=message.chat_id, photo=url, caption=caption,
                parse_mode=ParseMode.HTML, reply_markup=_imglink_keyboard(start_index),
            )
        except TelegramBadRequest as exc:
            logger.warning("Image link card failed for %s: %s", url, exc)
            try:
                await message.reply_text(
                    caption, parse_mode=ParseMode.HTML,
                    reply_markup=_imglink_keyboard(start_index),
                )
            except TelegramBadRequest:
                pass


async def handle_admin_panel_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Catch ``admin_*``, ``export_item_<uid>`` and ``edit_*_<uid>`` taps."""
    query = update.callback_query
    if query is None or not query.data:
        return
    user = query.from_user
    if user is None or not is_admin(user.id):
        await query.answer("⛔ Not authorised.", show_alert=True)
        return
    data = query.data

    if data == "admin_panel":
        await query.answer()
        await _edit_to_html(
            query.message, PANEL_TEXT_HOME,
            _panel_home_keyboard(context.user_data.get("panel_pincode")),
        )
        return

    if data == "admin_close":
        await query.answer()
        context.user_data.pop("state", None)
        context.user_data.pop("edit_uid", None)
        await _edit_to_html(query.message, PANEL_TEXT_CLOSED)
        return

    if data == "admin_setpin":
        context.user_data["state"] = STATE_SET_PINCODE
        context.user_data.pop("edit_uid", None)
        await query.answer()
        await _edit_to_html(query.message, PANEL_TEXT_PIN_PROMPT)
        return

    if data == "admin_addadmin":
        # Grant stock-manage access: type the new admin's Telegram User ID.
        context.user_data["state"] = STATE_ADD_ADMIN
        context.user_data.pop("edit_uid", None)
        await query.answer()
        await _edit_to_html(
            query.message,
            PANEL_TEXT_ADD_ADMIN_PROMPT.format(
                admins=html.escape(", ".join(all_admin_ids()) or "-")
            ),
            _admins_manage_keyboard(),
        )
        return

    if data.startswith("admin_rmadmin_"):
        uid = data[len("admin_rmadmin_"):].strip()
        acting = str(getattr(query.from_user, "id", "") or "")
        if not uid.isdigit() or uid not in _load_extra_admins():
            await query.answer(PANEL_TEXT_EXPIRED, show_alert=True)
            return
        if uid == acting:  # never let an admin lock themselves out
            await query.answer(PANEL_TEXT_RM_ADMIN_DENY, show_alert=True)
            return
        try:
            await asyncio.to_thread(remove_extra_admin, uid)
        except OSError as exc:
            logger.error("Remove admin failed for %s: %s", uid, exc)
            await query.answer(f"❌ {exc}", show_alert=True)
            return
        context.user_data["state"] = STATE_ADD_ADMIN
        await query.answer(PANEL_TEXT_RM_ADMIN_DONE.format(uid=uid))
        await _edit_to_html(
            query.message,
            PANEL_TEXT_ADD_ADMIN_PROMPT.format(
                admins=html.escape(", ".join(all_admin_ids()) or "-")
            ),
            _admins_manage_keyboard(),
        )
        return

    if data == "admin_ai_toggle":
        # Flip the active AI engine (Grok <-> Gemini) and refresh the button.
        try:
            import ai_agent

            new_model = ai_agent.toggle_active_ai()
        except Exception as exc:  # noqa: BLE001 - toggle must never crash
            logger.error("AI toggle failed: %s", exc)
            await query.answer(f"❌ {exc}", show_alert=True)
            return
        await query.answer(f"🤖 Active AI: {new_model}")
        await _edit_to_html(
            query.message, PANEL_TEXT_HOME,
            _panel_home_keyboard(context.user_data.get("panel_pincode")),
        )
        return

    if data == "admin_manual":
        # Guided manual add: Step 1 (Smart Category) comes from the sheet.
        await _start_manual_flow(query, context)
        return

    # ---- 🖼 Image Link Generator (manual product photo / offer banner) -----
    if data == "admin_imglink":
        context.user_data["state"] = STATE_IMAGE_LINK
        context.user_data.pop("imglink_urls", None)
        await query.answer()
        await _edit_to_html(query.message, PANEL_TEXT_IMGLINK_PROMPT)
        return

    if data.startswith("admin_imglink_more_"):
        # "More ➡️" -> next batch of image URLs for the same keyword.
        offset_text = data[len("admin_imglink_more_"):].strip()
        try:
            offset = int(offset_text)
        except ValueError:
            offset = 1
        keyword = str(context.user_data.get("imglink_keyword") or "").strip()
        if not keyword:
            await query.answer(PANEL_TEXT_EXPIRED, show_alert=True)
            return
        await query.answer("🔎 More images...")
        reply_message = query.message
        if reply_message is None or not hasattr(reply_message, "reply_text"):
            return
        await _send_imglink_photos(reply_message, context, keyword, max(1, offset))
        return

    if data == "admin_imglink_urls":
        # "📋 All URLs (copy)" -> one message listing the URLs as tappable
        # code blocks (Telegram shows a copy button under each).
        urls = [u for u in (context.user_data.get("imglink_urls") or []) if isinstance(u, str)]
        if not urls:
            await query.answer(PANEL_TEXT_EXPIRED, show_alert=True)
            return
        await query.answer()
        reply_message = query.message
        if reply_message is None or not hasattr(reply_message, "reply_text"):
            return
        lines = [f"{i}. <code>{html.escape(u)}</code>" for i, u in enumerate(urls, 1)]
        try:
            await reply_message.reply_text(
                PANEL_TEXT_IMGLINK_URLS + "\n\n" + "\n".join(lines),
                parse_mode=ParseMode.HTML,
                reply_markup=_panel_home_keyboard(context.user_data.get("panel_pincode")),
            )
        except TelegramBadRequest as exc:
            logger.warning("Could not send image URLs: %s", exc)
        return

    if data == "admin_manual_newcat":
        context.user_data["state"] = STATE_MANUAL_NEW_CATEGORY
        await query.answer()
        await _edit_to_html(
            query.message, PANEL_TEXT_MANUAL_NEW_CAT,
            _manual_prompt_keyboard(),
        )
        return

    if data.startswith("admin_manual_cat_"):
        idx_text = data[len("admin_manual_cat_"):]
        cats = context.user_data.get("manual_categories") or []
        if not idx_text.isdigit() or int(idx_text) >= len(cats):
            await query.answer(PANEL_TEXT_EXPIRED, show_alert=True)
            return
        manual = context.user_data.get("manual") or {}
        manual["category"] = cats[int(idx_text)]
        context.user_data["manual"] = manual
        context.user_data["state"] = STATE_MANUAL_NAME
        await query.answer()
        await _edit_to_html(
            query.message, PANEL_TEXT_MANUAL_NAME,
            _manual_prompt_keyboard(),
        )
        return

    if data == "admin_manual_skip_ean":
        # Step 6 shortcut: save with an auto-generated unique EAN.
        if str(context.user_data.get("state") or "") != STATE_MANUAL_EAN:
            await query.answer(PANEL_TEXT_EXPIRED, show_alert=True)
            return
        await query.answer()
        await _finish_manual_product(context, query.message, ean=None)
        return

    if data == "admin_manual_cancel":
        await _cancel_manual(query, context)
        return

    if data == "admin_clearpin":
        context.user_data.pop("panel_pincode", None)
        await query.answer("♻️ Pincode reset to default.")
        await _edit_to_html(
            query.message, PANEL_TEXT_HOME, _panel_home_keyboard(None)
        )
        return

    if data.startswith("admin_page_"):
        m = re.match(r"^admin_page_(\d+)$", data)
        if not m:
            await query.answer("❌ Invalid page.", show_alert=True)
            return
        await query.answer()
        await _clear_panel_page(context)
        context.user_data["panel_page"] = int(m.group(1))
        await _render_panel_page(context)
        return

    if data == "admin_search_done":
        await query.answer()
        await _clear_panel_page(context)
        context.user_data["state"] = None
        context.user_data.pop("panel_results", None)
        context.user_data.pop("panel_page", None)
        context.user_data.pop("edit_uid", None)
        await _edit_to_html(
            query.message, PANEL_TEXT_DONE,
            _panel_home_keyboard(context.user_data.get("panel_pincode")),
        )
        return

    if data == "admin_search":
        context.user_data["state"] = STATE_SEARCH_PRODUCT
        context.user_data.pop("edit_uid", None)
        await query.answer()
        pin = context.user_data.get("panel_pincode")
        prompt = PANEL_TEXT_SEARCH_PROMPT
        if pin:
            prompt += f"\n\n📍 ପିନକୋଡ୍: <b>{html.escape(str(pin))}</b>"
        await _edit_to_html(
            query.message, prompt,
            _panel_home_keyboard(pin),
        )
        return

    if data == "admin_home":
        await query.answer()
        await _edit_to_html(
            query.message, PANEL_TEXT_HOME,
            _panel_home_keyboard(context.user_data.get("panel_pincode")),
        )
        return

    if data == "admin_browse":
        # Product > Category : list distinct categories from the database
        await query.answer()
        context.user_data.pop("state", None)  # leave any search session
        if query.message is not None:
            context.user_data["panel_chat_id"] = query.message.chat_id
        try:
            categories = await _load_categories(context)
        except (db.DatabaseError, ValueError) as exc:
            await _edit_to_html(query.message, PANEL_TEXT_DEL_FAIL.format(err=exc))
            return
        if not categories:
            await _edit_to_html(query.message, PANEL_TEXT_NO_CATS,
                                _panel_home_keyboard(context.user_data.get("panel_pincode")))
            return
        await _edit_to_html(query.message, PANEL_TEXT_BROWSE,
                            _browse_categories_keyboard(categories))
        return

    if data == "admin_browse_search":
        # Search our own database (not JioMart) from within Browse-by-Category.
        await query.answer()
        if query.message is not None:
            context.user_data["panel_chat_id"] = query.message.chat_id
        context.user_data["state"] = STATE_BROWSE_SEARCH
        context.user_data.pop("edit_uid", None)
        await _edit_to_html(
            query.message, PANEL_TEXT_BROWSE_SEARCH_PROMPT,
            _browse_categories_keyboard(
                list(_category_cache.get(
                    int(context.user_data.get("panel_chat_id") or 0), {}
                ).keys())
            ),
        )
        return

    if data.startswith("admin_cat_"):
        # Category > Item : list the items inside one category
        idx_text = data[len("admin_cat_"):]
        await query.answer()
        if not idx_text.isdigit():
            await query.answer("❌ Invalid category.", show_alert=True)
            return
        snapshot = _category_cache.get(int(context.user_data.get("panel_chat_id") or 0), {})
        categories = list(snapshot.keys())
        idx = int(idx_text)
        if idx >= len(categories):
            await query.answer(PANEL_TEXT_EXPIRED, show_alert=True)
            return
        cat = categories[idx]
        items = snapshot.get(cat) or []
        context.user_data["browse_category"] = cat
        if not items:
            await _edit_to_html(query.message, PANEL_TEXT_CAT_EMPTY,
                                _browse_categories_keyboard(categories))
            return
        uids = []
        chat_id = query.message.chat_id
        _BOT_CACHE[int(chat_id)] = context.bot
        for prod in items:
            uid = str(prod.get("ean") or "").strip()
            if not uid:
                continue
            card = _row_to_card(prod)
            card["card_chat_id"] = chat_id
            _product_cache[uid] = card
            uids.append(uid)
        header = PANEL_TEXT_CAT_HEADER.format(
            cat=html.escape(cat), count=len(uids)
        )
        # Replace the category-list message with a short header, then send one
        # product card per item (photo + caption + stock controls), exactly
        # like the customer storefront does.
        nav = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back to Categories", callback_data="admin_browse")]]
        )
        await _edit_to_html(query.message, header, nav)
        context.user_data["browse_card_ids"] = []
        for uid in uids:
            card = _product_cache.get(uid) or {}
            image = str(card.get("image") or "").strip()
            caption = _render_browse_card(card)
            keyboard = _browse_stock_keyboard(uid, card.get("qty"))
            sent = None
            try:
                if image:
                    sent = await context.bot.send_photo(
                        chat_id=chat_id, photo=image, caption=caption,
                        parse_mode=ParseMode.HTML, reply_markup=keyboard,
                    )
                else:
                    sent = await context.bot.send_message(
                        chat_id=chat_id, text=caption,
                        parse_mode=ParseMode.HTML, reply_markup=keyboard,
                    )
            except TelegramBadRequest as exc:
                logger.warning("Could not send browse card %s: %s", uid, exc)
            if sent is not None:
                card["card_message_id"] = getattr(sent, "message_id", None)
                _product_cache[uid] = card
                context.user_data["browse_card_ids"].append(
                    getattr(sent, "message_id", None)
                )
        return

    if data.startswith("admin_item_"):
        # Item > full editor card (name/category/mrp/price/qty + delete)
        uid = data[len("admin_item_"):].strip()
        await query.answer()
        prod = _product_cache.get(uid)
        if prod is None:
            await query.answer(PANEL_TEXT_EXPIRED, show_alert=True)
            return
        prod["card_chat_id"] = query.message.chat_id
        prod["card_message_id"] = query.message.message_id
        _product_cache[uid] = prod
        image = str(prod.get("image") or "").strip()
        caption = _render_product_card(prod)
        keyboard = _browse_item_keyboard(uid)
        # the tapped message is a text list; replace it with the item card
        try:
            if image and hasattr(query.message, "reply_photo"):
                await query.message.reply_photo(
                    photo=image, caption=caption, parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
                await _delete_quiet(context, query.message)
            else:
                await query.message.edit_text(
                    caption, parse_mode=ParseMode.HTML, reply_markup=keyboard
                )
        except TelegramBadRequest as exc:
            logger.debug("Could not open browse item %s: %s", uid, exc)
        return

    if data.startswith("admin_delconfirm_"):
        uid = data[len("admin_delconfirm_"):].strip()
        await _do_delete(query, context, uid)
        return

    if data.startswith("admin_del_"):
        uid = data[len("admin_del_"):].strip()
        await query.answer()
        await _show_delete_confirm(query, uid)
        return

    if data.startswith("admin_stock_inc_"):
        uid = data[len("admin_stock_inc_"):].strip()
        await _adjust_stock(query, context, uid, +1)
        return

    if data.startswith("admin_stock_dec_"):
        uid = data[len("admin_stock_dec_"):].strip()
        await _adjust_stock(query, context, uid, -1)
        return

    if data.startswith("admin_stock_info_"):
        uid = data[len("admin_stock_info_"):].strip()
        prod = _product_cache.get(uid)
        if prod is None:
            await query.answer(PANEL_TEXT_EXPIRED, show_alert=True)
            return
        qty = _num_safe(prod.get("qty"))
        await query.answer(f"📦 Stock Qty: {qty}", show_alert=True)
        return

    if data.startswith("admin_editmenu_"):
        # Swap the card's buttons to the full editor (name/category/mrp/price
        # /qty/pack + delete) without sending a new message.
        uid = data[len("admin_editmenu_"):].strip()
        prod = _product_cache.get(uid)
        if prod is None:
            await query.answer(PANEL_TEXT_EXPIRED, show_alert=True)
            return
        prod["browse"] = True
        prod["card_chat_id"] = query.message.chat_id
        prod["card_message_id"] = query.message.message_id
        _product_cache[uid] = prod
        await query.answer()
        try:
            await query.edit_message_reply_markup(
                reply_markup=_browse_item_keyboard(uid)
            )
        except TelegramBadRequest as exc:
            logger.debug("Could not open edit menu for %s: %s", uid, exc)
        return

    if data.startswith("admin_backcard_"):
        # Return an edited / confirmed card to its browse caption + [+/-] stock
        # controls. Restores BOTH caption and buttons so it also works as the
        # Cancel target of the delete dialog on a photo card.
        uid = data[len("admin_backcard_"):].strip()
        prod = _product_cache.get(uid)
        if prod is None:
            await query.answer(PANEL_TEXT_EXPIRED, show_alert=True)
            return
        prod["browse"] = True
        _product_cache[uid] = prod
        await query.answer()
        await _edit_to_html(
            query.message,
            _render_browse_card(prod),
            _browse_stock_keyboard(uid, prod.get("qty")),
        )
        return

    if data.startswith("export_item_"):
        uid = data[len("export_item_"):].strip()
        if not uid.isdigit():
            await query.answer("❌ Invalid item.", show_alert=True)
            return
        await _handle_export(query, context, uid)
        return

    if data.startswith("edit_"):
        m = re.match(r"^edit_(name|mrp|price|qty|pack|category)_(\d+)$", data)
        if not m:
            await query.answer("❌ Unknown action.", show_alert=True)
            return
        field_key, uid = m.group(1), m.group(2)
        state = f"{AWAITING_EDIT_PREFIX}{field_key.upper()}"
        if state not in EDIT_FIELDS:
            await query.answer("❌ Unknown field.", show_alert=True)
            return
        prod = _product_cache.get(uid)
        if prod is None:
            await query.answer(PANEL_TEXT_EXPIRED, show_alert=True)
            return
        context.user_data["state"] = state
        context.user_data["edit_uid"] = uid
        await query.answer()
        prompt = EDIT_PROMPT[state].format(uid=uid)
        await _send_or_edit(query, prompt)
        return

    # ---- Manual restart (Admin Panel button) --------------------------------
    if data == "admin_restart":
        await query.answer("♻️ Restarting the bot...")
        try:
            await _edit_to_html(
                query.message,
                "♻️ <b>Restarting the bot...</b>\n"
                "Starting a fresh process. I'll be back in a few seconds.",
            )
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(1)
        try:
            import selfrestart

            selfrestart.spawn_successor_and_exit(0)
        except Exception as exc:  # noqa: BLE001 - only reached if spawn failed
            logger.error("Manual restart failed: %s", exc)
            await query.answer(f"❌ Restart failed: {exc}", show_alert=True)
        return

    # ---- Orders workflow (list / accept / cancel / advance / edit bill) ----
    if data in ("admin_orders", "admin_ord_back"):
        await query.answer()
        try:
            orders_list = db.get_recent_orders(limit=15)
        except Exception as exc:  # noqa: BLE001
            await _edit_to_html(query.message, f"❌ Could not load orders: {exc}")
            return
        await _edit_to_html(
            query.message, _orders_list_text(orders_list),
            _orders_list_keyboard(orders_list),
        )
        return

    if data.startswith("admin_ordv_"):
        oid = data[len("admin_ordv_"):].strip()
        order = _fetch_order(oid)
        if order is None:
            await query.answer(PANEL_TEXT_EXPIRED, show_alert=True)
            return
        await query.answer()
        await _edit_to_html(
            query.message, _order_detail_text(order), _order_detail_keyboard(order)
        )
        return

    async def _transition(oid, new_status):
        order = _fetch_order(oid)
        if order is None:
            await query.answer(PANEL_TEXT_EXPIRED, show_alert=True)
            return None
        try:
            db.set_order_status(oid, new_status)
        except Exception as exc:  # noqa: BLE001
            await query.answer(f"❌ {exc}", show_alert=True)
            return None
        order = dict(order)
        order["status"] = new_status
        await _update_customer_status(context, order, new_status)
        return order

    if data.startswith("admin_ord_ok_"):
        oid = data[len("admin_ord_ok_"):].strip()
        order = await _transition(oid, "accepted")
        if order is None:
            return
        await query.answer("✅ Order accepted")
        await _edit_to_html(
            query.message, _order_detail_text(order), _order_detail_keyboard(order)
        )
        return

    if data.startswith("admin_ord_no_"):
        oid = data[len("admin_ord_no_"):].strip()
        order = await _transition(oid, "cancelled")
        if order is None:
            return
        await query.answer("❌ Order cancelled")
        await _edit_to_html(
            query.message, _order_detail_text(order), _order_detail_keyboard(order)
        )
        return

    if data.startswith("admin_ord_next_"):
        oid = data[len("admin_ord_next_"):].strip()
        order = _fetch_order(oid)
        if order is None:
            await query.answer(PANEL_TEXT_EXPIRED, show_alert=True)
            return
        nxt = db.ORDER_STATUS_NEXT.get(order.get("status") or "pending")
        if not nxt:
            await query.answer("This order is already finished.", show_alert=True)
            return
        order = await _transition(oid, nxt)
        if order is None:
            return
        await query.answer(f"➡️ {db.ORDER_STATUS_LABELS.get(nxt, nxt)}")
        await _edit_to_html(
            query.message, _order_detail_text(order), _order_detail_keyboard(order)
        )
        return

    if data.startswith("admin_ord_bill_"):
        oid = data[len("admin_ord_bill_"):].strip()
        order = _fetch_order(oid)
        if order is None:
            await query.answer(PANEL_TEXT_EXPIRED, show_alert=True)
            return
        await query.answer()
        text = "➖ <b>Edit bill</b> - remove any unavailable item:\n\n" + _order_detail_text(order)
        await _edit_to_html(query.message, text, _editbill_keyboard(order))
        return

    if data.startswith("admin_ord_rm_"):
        rest = data[len("admin_ord_rm_"):]
        oid, _, idx_text = rest.rpartition("_")
        order = _fetch_order(oid)
        if order is None:
            await query.answer(PANEL_TEXT_EXPIRED, show_alert=True)
            return
        lines = _parse_items_text(order.get("items"))
        try:
            idx = int(idx_text)
        except ValueError:
            await query.answer("❌ Invalid item.", show_alert=True)
            return
        if idx < 0 or idx >= len(lines):
            await query.answer("❌ Invalid item.", show_alert=True)
            return
        removed = lines.pop(idx)
        new_items = _items_text_from_lines(lines)
        new_total = sum(float(l["line_total"] or 0) for l in lines)
        new_total = int(new_total) if float(new_total).is_integer() else round(new_total, 2)
        try:
            db.update_order_bill(oid, new_items, new_total)
        except Exception as exc:  # noqa: BLE001
            await query.answer(f"❌ {exc}", show_alert=True)
            return
        order = dict(order)
        order["items"] = new_items
        order["total"] = str(new_total)
        summary = _order_to_summary(order)
        await _update_customer_status(
            context, order, order.get("status") or "pending", summary
        )
        await query.answer(f"🗑 Removed {removed['name']}")
        text = "➖ <b>Edit bill</b> - remove any unavailable item:\n\n" + _order_detail_text(order)
        await _edit_to_html(query.message, text, _editbill_keyboard(order))
        return

    await query.answer("❌ Unknown action.", show_alert=True)


async def _edit_to_html(message, text: str, reply_markup=None) -> None:
    """Edit a message to HTML text; swallow stale-message errors.

    Browse-by-Category cards are photos, and a photo's text lives in its
    caption - ``edit_text`` always fails on media messages, so route those to
    ``edit_caption`` instead. Without this the delete confirm / completion
    would silently vanish for any product that has an image.
    """
    if message is None:
        return
    is_media = bool(getattr(message, "photo", None)) or (
        getattr(message, "caption", None) is not None
    )
    if is_media and hasattr(message, "edit_caption"):
        try:
            await message.edit_caption(
                caption=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
            )
        except Exception as exc:  # noqa: BLE001 - message may be gone/edited
            logger.debug("Could not edit caption: %s", exc)
        return
    try:
        await message.edit_text(
            text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
        )
    except TelegramBadRequest as exc:
        logger.debug("Could not edit message: %s", exc)


async def _delete_quiet(context, message) -> None:
    """Delete a helper message, ignoring 'message to delete not found' errors."""
    if message is None:
        return
    try:
        await context.bot.delete_message(
            chat_id=message.chat_id, message_id=message.message_id
        )
    except Exception as exc:  # noqa: BLE001 - already deleted / too old
        logger.debug("Could not delete message %s: %s",
                     getattr(message, "message_id", "?"), exc)


async def _send_or_edit(query, text: str) -> None:
    """Reply to the callback with the prompt (edits the card when possible)."""
    message = query.message
    if message is not None and hasattr(message, "edit_text"):
        try:
            await message.edit_text(text, parse_mode=ParseMode.HTML)
            return
        except TelegramBadRequest:
            pass  # e.g. photo message - fall back to a fresh reply
    try:
        await query.message.reply_text(text, parse_mode=ParseMode.HTML)
    except TelegramBadRequest as exc:
        logger.warning("Could not send edit prompt: %s", exc)


# --------------------------------------------------------------------------- #
# Admin Panel: text messages while in ADMIN_SEARCH_PRODUCT / AWAITING_EDIT_*
# --------------------------------------------------------------------------- #
# bot cache: chat_id -> bot, filled when cards are sent so later edits can
# re-render the caption without an Update object.
_BOT_CACHE: Dict[int, Any] = {}

# message_id -> time.time(); panel-consumed messages are skipped by the
# group-0 handlers (legacy stock entry + customer AI) to avoid double replies.
_consumed_message_ids: Dict[int, float] = {}


def _mark_consumed(message_id: Optional[int]) -> None:
    if not message_id:
        return
    _consumed_message_ids[message_id] = time.time()
    cutoff = time.time() - 30
    for mid, ts in list(_consumed_message_ids.items()):
        if ts < cutoff:
            _consumed_message_ids.pop(mid, None)


def _was_consumed(message_id: Optional[int]) -> bool:
    """True when the Admin Panel already handled this text message."""
    if not message_id:
        return False
    return message_id in _consumed_message_ids


async def handle_admin_panel_text(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Admin plain text: route by panel state (search keyword / edit value /
    manual pincode).

    * state ADMIN_SEARCH_PRODUCT -> JioMart search + product cards (the
      search session stays alive, so typing any word searches again)
    * state ADMIN_SET_PINCODE    -> store the manual pincode, then keep
      prompting for a keyword
    * state AWAITING_EDIT_*      -> apply the inline edit to Sheet + cache
    * no state                   -> fall through (legacy flow untouched)
    """
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or not is_admin(user.id):
        return
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return

    state = str(context.user_data.get("state") or "")

    if state == STATE_SET_PINCODE:
        _mark_consumed(message.message_id)
        pin = text.strip()
        if not pin.isdigit() or len(pin) != 6:
            await message.reply_text(PANEL_TEXT_PIN_BAD, parse_mode=ParseMode.HTML)
            return  # stay in STATE_SET_PINCODE to retry
        context.user_data["panel_pincode"] = pin
        # after setting the pin, drop straight into the search prompt
        context.user_data["state"] = STATE_SEARCH_PRODUCT
        await message.reply_text(
            PANEL_TEXT_PIN_SAVED.format(pin=html.escape(pin)),
            parse_mode=ParseMode.HTML,
            reply_markup=_panel_home_keyboard(pin),
        )
        return

    if state == STATE_BROWSE_SEARCH:
        # Browse-by-Category search: query OUR database (not JioMart). The
        # session stays alive so another keyword searches again.
        _BOT_CACHE[message.chat_id] = context.bot
        _mark_consumed(message.message_id)
        await _send_browse_search_cards(message, context, text)
        return

    if state == STATE_IMAGE_LINK:
        # 🖼 Image Link Generator: the typed text is the product keyword.
        _mark_consumed(message.message_id)
        await _send_imglink_photos(message, context, text)
        return

    if state == STATE_ADD_ADMIN:
        # "👥 Add Admin": the typed text is the new admin's Telegram User ID.
        _mark_consumed(message.message_id)
        uid = text.strip()
        if not (uid.isdigit() and 5 <= len(uid) <= 15):
            await message.reply_text(
                PANEL_TEXT_ADD_ADMIN_BAD, parse_mode=ParseMode.HTML,
                reply_markup=_admins_manage_keyboard(),
            )
            return  # stay in STATE_ADD_ADMIN to retry
        try:
            added = await asyncio.to_thread(add_extra_admin, uid)
        except (OSError, ValueError) as exc:
            logger.error("Add admin failed for %s: %s", uid, exc)
            await message.reply_text(f"❌ {exc}")
            return
        context.user_data.pop("state", None)
        if not added:
            await message.reply_text(
                PANEL_TEXT_ADD_ADMIN_DUP.format(uid=uid), parse_mode=ParseMode.HTML,
            )
            return
        await message.reply_text(
            PANEL_TEXT_ADD_ADMIN_DONE.format(uid=uid),
            parse_mode=ParseMode.HTML,
            reply_markup=_panel_home_keyboard(
                context.user_data.get("panel_pincode")
            ),
        )
        return

    if state == STATE_MANUAL_NEW_CATEGORY:
        # Manual Add Step 1b: admin typed a brand-new category name.
        _mark_consumed(message.message_id)
        category = text.strip()
        if not category or len(category) > 40:
            await message.reply_text(
                PANEL_TEXT_MANUAL_BAD, parse_mode=ParseMode.HTML,
                reply_markup=_manual_prompt_keyboard(),
            )
            return  # stay in STATE_MANUAL_NEW_CATEGORY to retry
        manual = context.user_data.get("manual") or {}
        manual["category"] = category
        context.user_data["manual"] = manual
        context.user_data["state"] = STATE_MANUAL_NAME
        await message.reply_text(
            PANEL_TEXT_MANUAL_NAME, parse_mode=ParseMode.HTML,
            reply_markup=_manual_prompt_keyboard(),
        )
        return

    if state == STATE_MANUAL_NAME:
        # Manual Add Step 2: product name.
        _mark_consumed(message.message_id)
        name = text.strip()
        if not name or len(name) > 120:
            await message.reply_text(
                PANEL_TEXT_MANUAL_BAD, parse_mode=ParseMode.HTML,
                reply_markup=_manual_prompt_keyboard(),
            )
            return  # stay in STATE_MANUAL_NAME to retry
        manual = context.user_data.get("manual") or {}
        manual["name"] = name
        context.user_data["manual"] = manual
        context.user_data["state"] = STATE_MANUAL_PHOTO
        await message.reply_text(
            PANEL_TEXT_MANUAL_PHOTO, parse_mode=ParseMode.HTML,
            reply_markup=_manual_prompt_keyboard(),
        )
        return

    if state == STATE_MANUAL_PRICES:
        # Manual Add Step 4: "MRP, Selling Price" (e.g. "150, 130").
        _mark_consumed(message.message_id)
        m = re.match(r"^\s*([\d.,]+)\s*[,/;]\s*([\d.,]+)\s*$", text)
        try:
            mrp = _parse_money(m.group(1), "MRP") if m else None
            price = _parse_money(m.group(2), "Selling Price") if m else None
            if mrp is None or price is None:
                raise ValueError("Send both prices, e.g. 150, 130.")
        except ValueError as exc:
            await message.reply_text(
                f"❌ {exc}\n\n" + PANEL_TEXT_MANUAL_PRICES,
                parse_mode=ParseMode.HTML,
                reply_markup=_manual_prompt_keyboard(),
            )
            return  # stay in STATE_MANUAL_PRICES to retry
        manual = context.user_data.get("manual") or {}
        manual["mrp"], manual["price"] = mrp, price
        context.user_data["manual"] = manual
        context.user_data["state"] = STATE_MANUAL_QTY
        await message.reply_text(
            PANEL_TEXT_MANUAL_QTY, parse_mode=ParseMode.HTML,
            reply_markup=_manual_prompt_keyboard(),
        )
        return

    if state == STATE_MANUAL_QTY:
        # Manual Add Step 5: stock quantity.
        _mark_consumed(message.message_id)
        try:
            qty = _parse_quantity(text)
            if qty < 0:
                raise ValueError("Quantity cannot be negative.")
        except ValueError as exc:
            await message.reply_text(
                f"❌ {exc}\n\n" + PANEL_TEXT_MANUAL_QTY,
                parse_mode=ParseMode.HTML,
                reply_markup=_manual_prompt_keyboard(),
            )
            return  # stay in STATE_MANUAL_QTY to retry
        manual = context.user_data.get("manual") or {}
        manual["qty"] = qty
        context.user_data["manual"] = manual
        context.user_data["state"] = STATE_MANUAL_EAN
        await message.reply_text(
            PANEL_TEXT_MANUAL_EAN, parse_mode=ParseMode.HTML,
            reply_markup=_manual_prompt_keyboard(include_skip_ean=True),
        )
        return

    if state == STATE_MANUAL_EAN:
        # Manual Add Step 6: typed EAN barcode (skip = auto-generate).
        _mark_consumed(message.message_id)
        ean = db.normalize_ean(text)
        if not (ean.isdigit() and 4 <= len(ean) <= 20):
            await message.reply_text(
                PANEL_TEXT_MANUAL_BAD + "\n\n" + PANEL_TEXT_MANUAL_EAN,
                parse_mode=ParseMode.HTML,
                reply_markup=_manual_prompt_keyboard(include_skip_ean=True),
            )
            return  # stay in STATE_MANUAL_EAN to retry
        try:
            found = await asyncio.to_thread(db.find_by_ean, ean)
        except db.DatabaseError as exc:
            logger.error("Manual add: EAN check failed: %s", exc)
            await message.reply_text(f"❌ {exc}")
            return
        if found is not None:
            await message.reply_text(
                PANEL_TEXT_MANUAL_EAN_DUP.format(ean=html.escape(ean)),
                parse_mode=ParseMode.HTML,
                reply_markup=_manual_prompt_keyboard(include_skip_ean=True),
            )
            return  # stay in STATE_MANUAL_EAN to retry
        await _finish_manual_product(context, message, ean=ean)
        return

    if state == STATE_SEARCH_PRODUCT:
        # keep the search session alive (state stays STATE_SEARCH_PRODUCT) so
        # any word the admin types next triggers another search.
        _BOT_CACHE[message.chat_id] = context.bot
        _mark_consumed(message.message_id)
        await _send_product_cards(message, text, context)
        return

    if state in EDIT_FIELDS:
        _mark_consumed(message.message_id)
        await _apply_panel_edit(update, context, state, text)
        return
    # No panel state -> do nothing; the legacy stock-entry / AI handlers
    # registered after this one still see the message.


async def _apply_panel_edit(
    update: Update, context, state: str, text: str
) -> None:
    """Apply a pending inline edit: Sheet row + local cache + card caption."""
    message = update.effective_message
    uid = str(context.user_data.get("edit_uid") or "")
    # Keep the search session alive afterwards so the admin can keep typing
    # keywords / tapping buttons; "✅ Search Done" is the only way to stop it.
    context.user_data["state"] = STATE_SEARCH_PRODUCT
    context.user_data.pop("edit_uid", None)

    prod = _product_cache.get(uid)
    if prod is None:
        await message.reply_text(PANEL_TEXT_EXPIRED, parse_mode=ParseMode.HTML)
        return

    field, column, label = EDIT_FIELDS[state]
    value: Any = text
    try:
        if state in (STATE_EDIT_MRP, STATE_EDIT_PRICE):
            value = _parse_money(text, label)
        elif state == STATE_EDIT_QTY:
            value = _parse_quantity(text)
        elif state == STATE_EDIT_NAME:
            if not text.strip():
                raise ValueError("Name cannot be empty.")
            value = text.strip()
    except ValueError as exc:
        await message.reply_text(f"❌ {exc}")
        return

    # 1. Sheet update (pack has no column - cache/card only, noted in reply)
    if column is not None:
        try:
            await asyncio.to_thread(db.update_field_by_ean, uid, column, value)
        except (db.DatabaseError, ValueError) as exc:
            logger.error("Panel edit failed for %s/%s: %s", uid, field, exc)
            await message.reply_text(f"❌ Database error: {exc}")
            return

    # 2. Local cache
    prod[field] = value
    _product_cache[uid] = prod

    # 3. Confirmation (Odia) with the new value
    shown = _format_money(value) if field in ("mrp", "price", "qty") else html.escape(str(value))
    done = EDIT_DONE[state].format(value=shown)
    await message.reply_text(done, parse_mode=ParseMode.HTML)
    await _refresh_cached_cards(uid, prod)


async def _refresh_cached_cards(uid: str, prod: Dict[str, Any]) -> None:
    """Best-effort re-render of the product card that initiated this edit."""
    chat_id = prod.get("card_chat_id")
    message_id = prod.get("card_message_id")
    if not chat_id or not message_id:
        return
    bot = _BOT_CACHE.get(int(chat_id))
    if bot is None:
        return
    browse = bool(prod.get("browse"))
    if browse:
        # Browse-by-Category card: live stock caption + the [+/-] controls.
        keyboard = _browse_stock_keyboard(uid, prod.get("qty"))
        text = _render_browse_card(prod)
    else:
        keyboard = _product_keyboard(prod)
        text = _render_product_card(prod)
    try:
        await bot.edit_message_caption(
            chat_id=int(chat_id),
            message_id=int(message_id),
            caption=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except Exception as exc:  # noqa: BLE001 - text cards have no caption
        logger.debug("edit_message_caption failed for %s (%s); trying edit_text", uid, exc)
        try:
            await bot.edit_message_text(
                chat_id=int(chat_id),
                message_id=int(message_id),
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        except Exception as exc2:  # noqa: BLE001
            logger.debug("Could not refresh card for %s: %s", uid, exc2)


def register(application) -> None:
    """Attach the admin auto-stock handlers.

    Call this BEFORE registering customer/AI handlers so admin entries are
    consumed first.
    """
    # Admin Panel ("Search & Export") in an EARLIER group: when the admin has
    # no panel state the handler returns without acting, and PTB still runs
    # the group-0 handlers (legacy stock entry + customer AI) afterwards.
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_panel_text),
        group=-1,
    )
    # Manual-add product photos (only consumed in STATE_MANUAL_PHOTO).
    application.add_handler(
        MessageHandler(filters.PHOTO, handle_admin_manual_photo),
        group=-1,
    )
    application.add_handler(
        CallbackQueryHandler(handle_admin_panel_callback, pattern=PANEL_CALLBACK_PATTERN),
        group=-1,
    )
    application.add_handler(
        MessageHandler(ENTRY_FILTER & ~filters.COMMAND, handle_stock_entry)
    )
    application.add_handler(
        CallbackQueryHandler(handle_image_callback, pattern=CALLBACK_PATTERN)
    )


__all__ = [
    "CALLBACK_PATTERN",
    "EDIT_FIELDS",
    "ENTRY_FILTER",
    "PANEL_CALLBACK_PATTERN",
    "PANEL_PINCODES",
    "STATE_ADD_ADMIN",
    "STATE_BROWSE_SEARCH",
    "STATE_IMAGE_LINK",
    "STATE_MANUAL_CATEGORY",
    "STATE_MANUAL_NEW_CATEGORY",
    "STATE_MANUAL_NAME",
    "STATE_MANUAL_PHOTO",
    "STATE_MANUAL_PRICES",
    "STATE_MANUAL_QTY",
    "STATE_MANUAL_EAN",
    "STATE_SEARCH_PRODUCT",
    "add_extra_admin",
    "all_admin_ids",
    "handle_admin_manual_photo",
    "handle_admin_panel_callback",
    "handle_admin_panel_text",
    "handle_image_callback",
    "handle_stock_entry",
    "is_admin",
    "panel_keyboard",
    "parse_stock_entry",
    "register",
    "remove_extra_admin",
]




