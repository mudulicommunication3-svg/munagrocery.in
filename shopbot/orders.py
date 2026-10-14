"""Shared ordering service for MUNNA MOBILE & Grocery.

All three ordering channels (inline chat cart, Telegram Mini App, Gemini AI
agent) funnel through this module so behaviour is identical everywhere:

* :func:`bill_text`          - itemised bill rendering (HTML for Telegram)
* :func:`admin_alert_text`   - ADMIN_ID alert (name/phone/address/bill/total)
* :func:`parse_order_lines`  - natural-language order parser (English + Odia)
* :func:`match_catalog`      - resolve parsed lines against the live catalog
* :func:`resolve_products`   - catalog lookup for the storefront cart
* :func:`checkout`           - db.place_order + admin alert in one call
* checkout state machine     - address -> phone -> place (per-user states)

Blocking Sheets work is wrapped with ``asyncio.to_thread`` by the callers;
:func:`checkout` itself is async and safe to await from PTB handlers.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import db

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Checkout states (stored in context.user_data["state"])
# --------------------------------------------------------------------------- #
STATE_CHECKOUT_ADDRESS = "CHECKOUT_ADDRESS"
STATE_CHECKOUT_PHONE = "CHECKOUT_PHONE"
STATE_CHECKOUT_LOCATION = "CHECKOUT_LOCATION"
STATE_AI_CONFIRM = "AI_ORDER_CONFIRM"

CHECKOUT_STATES = frozenset(
    {STATE_CHECKOUT_ADDRESS, STATE_CHECKOUT_PHONE, STATE_CHECKOUT_LOCATION}
)

# --------------------------------------------------------------------------- #
# Money formatting
# --------------------------------------------------------------------------- #


def format_money(value: Any) -> str:
    """Compact rupee formatting: 170 -> '170', 48.5 -> '48.5'."""
    try:
        number = float(str(value).replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number.is_integer() else str(round(number, 2))


def _qty_of(value: Any) -> int:
    try:
        return int(float(str(value or 0)))
    except (TypeError, ValueError):
        return 0


def _price_of(value: Any) -> float:
    try:
        return float(str(value or 0).replace(",", ""))
    except (TypeError, ValueError):
        return 0.0
# --------------------------------------------------------------------------- #
# Bill + admin alert rendering (HTML for Telegram)
# --------------------------------------------------------------------------- #


def bill_text(lines: List[Dict[str, Any]], total: Any = None) -> str:
    """Itemised bill, one line per item: ``1. Name x2 - Rs.340``."""
    parts = []
    running = 0.0
    for i, line in enumerate(lines or [], start=1):
        name = html.escape(str(line.get("name") or "Item"))
        qty = _qty_of(line.get("qty"))
        price = _price_of(line.get("price"))
        line_total = line.get("line_total")
        try:
            line_total_f = float(line_total)
        except (TypeError, ValueError):
            line_total_f = price * qty
        running += line_total_f
        parts.append(f"{i}. {name} x{qty} - Rs.{format_money(line_total_f)}")
    grand = total if total is not None else running
    parts.append(f"<b>Total: Rs.{format_money(grand)}</b>")
    return "\n".join(parts)


def admin_alert_text(
    order_id: str,
    customer_name: str,
    phone: str,
    address: str,
    lines: List[Dict[str, Any]],
    total: Any,
    channel: str = "chat",
    telegram_user_id: Any = "",
) -> str:
    """Detailed new-order alert for ADMIN_ID (HTML)."""
    head = [
        "🔔 <b>New order received!</b>",
        f"🧾 Order: <code>{html.escape(str(order_id))}</code>",
        f"👤 Name: {html.escape(str(customer_name) or '-')}",
        f"📞 Phone: {html.escape(str(phone) or '-')}",
        f"📍 Address: {html.escape(str(address) or '-')}",
        f"📦 Channel: {html.escape(str(channel) or 'chat')}",
    ]
    if telegram_user_id not in (None, ""):
        head.append(f"🆔 User: <code>{html.escape(str(telegram_user_id))}</code>")
    head.append("")
    head.append(bill_text(lines, total))
    return "\n".join(head)


def admin_alert_keyboard(order_id: Any) -> Any:
    """Inline ✅ Accept / ❌ Cancel buttons for a new-order admin alert.

    The callback data is exactly what the Admin Panel already handles
    (``admin_ord_ok_<id>`` / ``admin_ord_no_<id>``), so tapping straight from
    the notification accepts or cancels the order and auto-updates the
    customer's live status message. Returns ``None`` when there is no order id
    (Telegram allows max 64 bytes of callback data).
    """
    oid = str(order_id or "").strip()
    if not oid:
        return None
    try:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Accept", callback_data=f"admin_ord_ok_{oid}"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"admin_ord_no_{oid}"),
                ],
                [InlineKeyboardButton("📦 All orders", callback_data="admin_orders")],
            ]
        )
    except Exception:  # noqa: BLE001 - never break the alert over a keyboard
        return None


def admin_ids() -> List[int]:
    """ADMIN_ID env values as ints (empty list when unset/dev-mode)."""
    raw = (os.getenv("ADMIN_ID") or "").strip()
    ids: List[int] = []
    if raw in ("", "0"):
        return ids
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            continue
    return ids


async def notify_admins(context, text: str, reply_markup: Any = None) -> None:
    """Send ``text`` (HTML) to every ADMIN_ID; never raises.

    ``reply_markup`` is optional - the new-order alerts pass an inline
    keyboard with ✅ Accept / ❌ Cancel so the seller can act straight from
    the notification (bot and webapp orders both).
    """
    ids = admin_ids()
    if not ids:
        logger.info("No ADMIN_ID configured; skipping admin alert.")
        return
    for admin_id in ids:
        try:
            await context.bot.send_message(
                chat_id=admin_id, text=text, parse_mode="HTML",
                reply_markup=reply_markup,
            )
        except Exception as exc:  # noqa: BLE001 - one bad id must not block others
            logger.warning("Could not alert admin %s: %s", admin_id, exc)

# --------------------------------------------------------------------------- #
# Natural-language order parsing (English + Odia) + catalog matching
# --------------------------------------------------------------------------- #
_ODIA_DIGITS = str.maketrans("୦୧୨୩୪୫୬୭୮୯", "0123456789")

_STOP_WORDS = frozenset(
    """
    i need want give get please add buy order bring take bottle packet pack
    kg g l ml litre liter pcs piece pieces packet packets oil oils
    and plus
    """.split()
    + """
    ମୋତେ ଦରକାର ଦିଅ ଦିଅନ୍ତୁ ନେବି ନେବା କିଣିବି ଦୟାକରି ଟି ଟା
    ଗୋଟେ ଗୋଟିଏ ଦୁଇ ତିନି ଚାରି ପାଞ୍ଚ ଛଅ ସାତ ଆଠ ନଅ ଦଶ
    କେଜି ଲିଟର ଗ୍ରାମ ପ୍ୟାକେଟ ବୋତଲ ତେଲ ଏବଂ ଓ ସହିତ ସହ କିଛି ଟିକେ
    """.split()
)

_ODIA_NUMBERS = {
    "ଗୋଟେ": 1, "ଗୋଟିଏ": 1, "ଗୋଟାଏ": 1, "ଏକ": 1,
    "ଦୁଇ": 2, "ଦି": 2, "ତିନି": 3, "ଚାରି": 4,
    "ପାଞ୍ଚ": 5, "ଛଅ": 6, "ସାତ": 7, "ଆଠ": 8, "ନଅ": 9, "ଦଶ": 10,
}

_EN_NUMBERS = {
    "one": 1, "a": 1, "an": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def _norm_digits(text: str) -> str:
    return str(text or "").translate(_ODIA_DIGITS)


def parse_order_lines(text: str) -> List[Dict[str, Any]]:
    """Split ``text`` into ``[{"qty": int, "query": str}]`` order lines.

    Handles ``2 Fortune oil and 1 Tata salt``, ``Fortune oil x2``,
    Odia numerals and Odia number words. Never raises.
    """
    raw = _norm_digits(text).strip()
    if not raw:
        return []
    chunks = re.split(r"\s+(?:and|plus|ଏବଂ|ଓ)\s+|\s*[,;+]\s*|\s*\n\s*", raw)
    lines: List[Dict[str, Any]] = []
    for chunk in chunks:
        chunk = chunk.strip(" .!-")
        if not chunk:
            continue
        # strip leading filler verbs ("I need", "ମୋତେ", "please") so a
        # quantity like "2" in "I need 2 Fortune oil" is detected
        words = chunk.split()
        while words and words[0].lower().strip(".,") in _STOP_WORDS:
            words.pop(0)
        chunk = " ".join(words).strip()
        if not chunk:
            continue
        qty = 1
        rest = chunk
        m = re.match(r"^(\d+)\s*[xX×]\s*(.+)$", chunk)
        if m:
            first, second = m.group(1), m.group(2).strip()
            if second and re.match(r"^\d+$", second):
                qty, rest = int(second), first
            elif second:
                qty, rest = int(first), second
            else:
                continue
        else:
            m = re.match(r"^(\d+)\s+(.+)$", chunk)
            if m:
                qty, rest = int(m.group(1)), m.group(2).strip()
            else:
                words = chunk.split()
                first = words[0].lower().strip(".,")
                if first in _EN_NUMBERS:
                    qty, rest = _EN_NUMBERS[first], " ".join(words[1:]).strip()
                elif first in _ODIA_NUMBERS:
                    qty, rest = _ODIA_NUMBERS[first], " ".join(words[1:]).strip()
                else:
                    m2 = re.search(r"[xX×]\s*(\d+)\s*$", chunk)
                    if m2:
                        qty = int(m2.group(1))
                        rest = chunk[: m2.start()].strip()
        tokens = [
            t for t in re.split(r"\s+", rest.lower())
            if t and t not in _STOP_WORDS and len(t) > 1
        ]
        if not tokens:
            continue
        lines.append({"qty": max(1, min(qty, 99)), "query": " ".join(tokens)})
    return lines


def match_catalog(
    lines: List[Dict[str, Any]], catalog: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Resolve parsed lines to catalog rows (out-of-stock rows never match)."""
    matched: List[Dict[str, Any]] = []
    missing: List[str] = []
    for line in lines or []:
        query = str(line.get("query") or "").strip().lower()
        qty = max(1, int(line.get("qty") or 1))
        if not query:
            continue
        qtokens = [t for t in re.split(r"\s+", query) if t]
        best = None
        for prod in catalog or []:
            if _qty_of(prod.get("stock_qty")) <= 0:
                continue
            name = str(prod.get("product_name") or "").lower()
            if name and all(tok in name for tok in qtokens):
                best = prod
                break
        if best is None:
            for prod in catalog or []:
                if _qty_of(prod.get("stock_qty")) <= 0:
                    continue
                name = str(prod.get("product_name") or "").lower()
                if name and any(tok in name for tok in qtokens):
                    best = prod
                    break
        if best is None:
            missing.append(query)
            continue
        price = _price_of(best.get("selling_price"))
        matched.append(
            {
                "ean": str(best.get("ean") or ""),
                "name": str(best.get("product_name") or "Product"),
                "qty": qty,
                "price": price,
                "line_total": price * qty,
                "available": _qty_of(best.get("stock_qty")),
            }
        )
    return matched, missing


def resolve_products(
    cart: Dict[str, int], catalog: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Resolve a ``{ean: qty}`` cart to bill lines + missing-EAN list."""
    by_ean = {str(p.get("ean") or ""): p for p in catalog or []}
    lines: List[Dict[str, Any]] = []
    missing: List[str] = []
    for ean, qty in (cart or {}).items():
        prod = by_ean.get(str(ean))
        if prod is None:
            missing.append(str(ean))
            continue
        price = _price_of(prod.get("selling_price"))
        qty_n = max(1, int(qty or 1))
        lines.append(
            {
                "ean": str(ean),
                "name": str(prod.get("product_name") or "Product"),
                "qty": qty_n,
                "price": price,
                "line_total": price * qty_n,
                "available": _qty_of(prod.get("stock_qty")),
            }
        )
    return lines, missing


# --------------------------------------------------------------------------- #
# Shared checkout: db.place_order + admin alert in one call
# --------------------------------------------------------------------------- #


async def checkout(
    context,
    items: List[Dict[str, Any]],
    customer_name: str = "",
    phone: str = "",
    address: str = "",
    channel: str = "chat",
    telegram_user_id: Any = "",
    location: str = "",
    payment: str = "COD",
) -> Dict[str, Any]:
    """Place an order from ANY channel and alert the admin.

    Wraps :func:`db.place_order` (validates, deducts stock, logs the Orders
    row) and then fires the ADMIN_ID alert. Returns the order summary. Raises
    ``ValueError`` / ``DatabaseError`` on failure (no alert is sent then).
    """
    summary = await asyncio.to_thread(
        db.place_order,
        [{"ean": it.get("ean"), "qty": it.get("qty")} for it in items],
        customer_name,
        phone,
        address,
        channel,
        str(telegram_user_id or ""),
        location=location,
        payment=payment,
    )
    summary["address"] = address
    summary["phone"] = phone
    summary["customer_name"] = customer_name
    alert = admin_alert_text(
        summary["order_id"],
        customer_name,
        phone,
        address,
        summary["lines"],
        summary["total"],
        channel,
        telegram_user_id,
    )
    await notify_admins(context, alert, admin_alert_keyboard(summary.get("order_id")))
    try:
        from ai_agent import clear_cache as _clear_ai_cache

        _clear_ai_cache()  # stock changed -> refresh the RAG catalog cache
    except Exception:  # noqa: BLE001 - cache refresh must never break checkout
        pass
    return summary


# --------------------------------------------------------------------------- #
# Customer live order status (the SAME message auto-updates as the seller works)
# --------------------------------------------------------------------------- #
def order_status_text(summary: Dict[str, Any], status: str = "pending") -> str:
    """Customer-facing HTML status message for an order."""
    label = db.ORDER_STATUS_LABELS.get(status, status)
    oid = html.escape(str((summary or {}).get("order_id") or ""))
    lines = [
        f"🧾 <b>Order <code>{oid}</code></b>",
        f"📊 Status: {label}",
        "",
        bill_text((summary or {}).get("lines") or [], (summary or {}).get("total")),
    ]
    address = str((summary or {}).get("address") or "").strip()
    if address:
        lines.append(f"\n📍 {html.escape(address)}")
    if status not in ("delivered", "cancelled"):
        lines.append("\n<i>This message updates automatically as your order progresses.</i>")
    return "\n".join(lines)


async def send_order_status(
    context,
    chat_id: Any,
    summary: Dict[str, Any],
    status: str = "pending",
):
    """Send the customer a live status message and store its id on the order.

    The stored ``message_id`` (in the Orders sheet) lets the Admin Panel edit
    THIS EXACT message later so the customer sees progress without re-asking.
    """
    text = order_status_text(summary, status)
    sent = None
    try:
        from telegram.constants import ParseMode as _ParseMode

        sent = await context.bot.send_message(
            chat_id=chat_id, text=text, parse_mode=_ParseMode.HTML
        )
    except Exception:  # noqa: BLE001 - fall back to plain text
        try:
            sent = await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not send order status: %s", exc)
            return None
    try:
        db.set_order_status_msg(
            (summary or {}).get("order_id"), getattr(sent, "message_id", None)
        )
    except Exception as exc:  # noqa: BLE001 - never break the order for this
        logger.debug("Could not store status message id: %s", exc)
    return sent


__all__ = [
    "CHECKOUT_STATES",
    "STATE_AI_CONFIRM",
    "STATE_CHECKOUT_ADDRESS",
    "STATE_CHECKOUT_LOCATION",
    "STATE_CHECKOUT_PHONE",
    "admin_alert_keyboard",
    "admin_alert_text",
    "admin_ids",
    "bill_text",
    "checkout",
    "format_money",
    "match_catalog",
    "notify_admins",
    "order_status_text",
    "parse_order_lines",
    "resolve_products",
    "send_order_status",
]

