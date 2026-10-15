"""Webapp <-> Telegram bot database sync (shared Google Sheet Orders tab).

The GitHub Pages Mini App (``index.html`` at munagrocery.in) and the Telegram
bot share ONE
Orders tab with the exact same 13-column format (``db.ORDERS_HEADERS``):

    Timestamp | Order ID | Customer Name | Phone | Address | Items | Total |
    Channel | Telegram User ID | Status | Status Msg ID | Location | Payment

Sync directions handled here (poll loop started from ``main.py``):

* **Webapp -> Telegram**: browser orders (channel ``webapp``) are written by
  the Apps Script webhook. Every such order is alerted to the ADMIN_IDs with
  the same HTML alert as every other channel, and when the customer's
  Telegram user id is known a LIVE status message is sent to them (its id is
  stored in "Status Msg ID", so the Admin Panel can edit it in place).
* **Webapp -> Telegram (status changes)**: when the webapp admin advances the
  stage, the Apps Script updates the Status cell; this loop notices and edits
  the customer's live message.
* **Telegram -> Webapp**: bot orders already land in the same sheet; the web
  app pulls them with its ``GET_ORDERS`` Apps Script action (no work needed).

Alert/status state is persisted in a small JSON file so restarts never
double-alert. Legacy rows use channel ``chat``/``miniapp`` and are skipped -
only rows written by the web app (channel ``webapp``) are processed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import db
import orders

logger = logging.getLogger(__name__)

# Webapp order -> Telegram sync timing. The loop polls the shared Orders tab,
# so the interval is how long a fresh webapp order takes to reach the ADMIN_IDs
# with the Accept/Cancel buttons (and how fast a webapp-admin status change
# reaches the customer's live message). 12s keeps admins notified almost
# instantly while staying well inside Google Sheets + Telegram rate limits;
# override with WEB_SYNC_INTERVAL if needed.
WEBAPP_CHANNEL = "webapp"
DEFAULT_INTERVAL = float(os.getenv("WEB_SYNC_INTERVAL") or 12.0)
DEFAULT_FIRST_DELAY = float(os.getenv("WEB_SYNC_FIRST_DELAY") or 8.0)
STATE_FILE = Path(
    os.getenv("WEB_SYNC_STATE_FILE")
    or Path(__file__).resolve().with_name(".web_sync_state.json")
)

# --------------------------------------------------------------------------- #
# Local state (which orders were alerted + last status we pushed)
# --------------------------------------------------------------------------- #


def load_state() -> Dict[str, Any]:
    """Read the persisted sync state; missing/broken file -> fresh state."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001 - corrupt state must never break the loop
        logger.warning("Could not read %s; starting fresh.", STATE_FILE)
        return {}


def save_state(state: Dict[str, Any]) -> None:
    """Atomically persist the sync state (best effort)."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(str(tmp), str(STATE_FILE))
    except Exception:  # noqa: BLE001
        logger.warning("Could not persist web sync state", exc_info=True)


# --------------------------------------------------------------------------- #
# Unified order helpers (same Items text format as the bot/admin panel)
# --------------------------------------------------------------------------- #
_ITEM_RE = re.compile(r"^(.*?)\s*[xX]\s*(\d+)\s*@\s*Rs\.?\s*(.+)$")


def parse_items_text(text: Any) -> List[Dict[str, Any]]:
    """``"Oil x2 @ Rs.340; Salt x1 @ Rs.25"`` -> ``[{name, qty, price, ...}]``."""
    lines: List[Dict[str, Any]] = []
    for seg in str(text or "").split(";"):
        seg = seg.strip()
        if not seg:
            continue
        match = _ITEM_RE.match(seg)
        if match:
            name = match.group(1).strip()
            qty = int(match.group(2))
            price_raw = match.group(3).strip()
        else:
            parts = seg.rsplit(" x", 1)
            name = parts[0].strip()
            qty = 1
            price_raw = ""
            if len(parts) == 2 and parts[1].strip().isdigit():
                qty = int(parts[1].strip())
        try:
            price = float(str(price_raw).replace(",", "") or 0)
        except (TypeError, ValueError):
            price = 0.0
        lines.append(
            {
                "name": name,
                "qty": qty,
                "price": price,
                "line_total": price * qty,
            }
        )
    return lines


def summary_for(order: Dict[str, Any]) -> Dict[str, Any]:
    """Order dict -> summary accepted by orders.order_status_text()."""
    lines = parse_items_text((order or {}).get("items"))
    total: Any = (order or {}).get("total")
    try:
        total_n = float(str(total).replace(",", "") or 0)
    except (TypeError, ValueError):
        total_n = sum(float(line["line_total"] or 0) for line in lines)
    if float(total_n).is_integer():
        total_n = int(total_n)
    return {
        "order_id": (order or {}).get("order_id"),
        "lines": lines,
        "total": total_n,
        "address": (order or {}).get("address"),
        "phone": (order or {}).get("phone"),
        "customer_name": (order or {}).get("customer_name"),
    }


def is_web_order(order: Dict[str, Any]) -> bool:
    return str((order or {}).get("channel") or "").strip().lower() == WEBAPP_CHANNEL


def _chat_id(raw: Any):
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return text


# --------------------------------------------------------------------------- #
# One poll pass
# --------------------------------------------------------------------------- #
async def push_status(context: Any, order: Dict[str, Any], status: str) -> bool:
    """Edit the customer's live Telegram status message (best effort)."""
    chat_id = _chat_id((order or {}).get("telegram_user_id"))
    message_id = str((order or {}).get("status_msg_id") or "").strip()
    if not chat_id or not message_id or not str(message_id).lstrip("-").isdigit():
        return False
    text = orders.order_status_text(summary_for(order), status)
    try:
        from telegram.constants import ParseMode as _ParseMode

        parse_mode: Any = _ParseMode.HTML
    except Exception:  # noqa: BLE001 - plain text fallback below
        parse_mode = None
    try:
        if parse_mode is not None:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=int(message_id),
                text=text,
                parse_mode=parse_mode,
            )
        else:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=int(message_id), text=text
            )
        return True
    except Exception as exc:  # noqa: BLE001 - stale/identical message
        logger.debug(
            "Could not edit status for %s: %s",
            (order or {}).get("order_id"),
            exc,
        )
        return False


async def process_once(
    context: Any, order_list: Optional[List[Dict[str, Any]]] = None
) -> int:
    """Handle new/changed webapp orders once. Returns orders processed.

    * first sight  -> admin alert + (optional) customer live status message
    * status change -> edit the customer's live status message
    """
    if order_list is None:
        try:
            order_list = await asyncio.to_thread(db.get_recent_orders, 200)
        except Exception as exc:  # noqa: BLE001 - DatabaseError / network
            logger.warning("Web sync could not load orders: %s", exc)
            return 0

    state = load_state()
    changed_state = False
    processed = 0

    for order in reversed(order_list):  # oldest first
        if not is_web_order(order):
            continue
        order_id = str((order or {}).get("order_id") or "").strip()
        if not order_id:
            continue
        status = str((order or {}).get("status") or "pending").strip() or "pending"
        entry = state.setdefault(order_id, {"alerted": False, "status": ""})
        if not isinstance(entry, dict):
            entry = {"alerted": False, "status": ""}
            state[order_id] = entry

        if not entry.get("alerted"):
            entry["alerted"] = True
            entry["status"] = status
            changed_state = True
            processed += 1

            # 1) Admin alert (identical wording to chat/miniapp/ai channels),
            #    with inline ✅ Accept / ❌ Cancel so the seller can act on a
            #    webapp order straight from the notification.
            try:
                alert = orders.admin_alert_text(
                    order_id,
                    (order or {}).get("customer_name") or "",
                    (order or {}).get("phone") or "",
                    (order or {}).get("address") or "",
                    parse_items_text((order or {}).get("items")),
                    (order or {}).get("total"),
                    WEBAPP_CHANNEL,
                    (order or {}).get("telegram_user_id") or "",
                )
                await orders.notify_admins(
                    context, alert, orders.admin_alert_keyboard(order_id)
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Admin alert failed for %s", order_id, exc_info=True
                )

            # 2) Live customer status message (Telegram users only).
            chat_id = _chat_id((order or {}).get("telegram_user_id"))
            already_sent = str((order or {}).get("status_msg_id") or "").strip()
            if chat_id and not already_sent:
                try:
                    await orders.send_order_status(
                        context, chat_id, summary_for(order), status
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Customer status message failed for %s",
                        order_id,
                        exc_info=True,
                    )

        elif str(entry.get("status") or "") != status:
            entry["status"] = status
            changed_state = True
            processed += 1
            if status == "cancelled":
                # Seller (or the customer notification) dropped the order --
                # put the shelf quantity back right now, not at the next skip.
                db.restore_order_stock(order_id)
            await push_status(context, order, status)

    if changed_state:
        save_state(state)
    return processed


async def sync_loop(context: Any, interval: Optional[float] = None) -> None:
    """Forever: poll the shared Orders tab for webapp orders + status changes."""
    wait = DEFAULT_INTERVAL if interval is None else float(interval)
    await asyncio.sleep(DEFAULT_FIRST_DELAY)
    while True:
        try:
            processed = await process_once(context)
            if processed:
                logger.info("Web sync handled %s order update(s).", processed)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop must never die
            logger.error("Web sync pass failed", exc_info=True)
        await asyncio.sleep(wait)


__all__ = [
    "DEFAULT_FIRST_DELAY",
    "DEFAULT_INTERVAL",
    "WEBAPP_CHANNEL",
    "is_web_order",
    "load_state",
    "parse_items_text",
    "process_once",
    "push_status",
    "save_state",
    "summary_for",
    "sync_loop",
]

