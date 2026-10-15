"""Bot-side worker: deliver real-login OTP codes over Telegram.

Flow (server-verified, single-use):
    webapp (phone) --REQUEST_OTP--> [LoginOTPs tab: pending]
        --poll (this loop)--> bot sends code to admin -> customer
    webapp (phone) --VERIFY_OTP--> Apps Script checks the SAME code.

Why the bot: the 4-digit code lives ONLY in the shared Sheet (never in
the browser, never in localStorage), so only someone reading that phone's
Telegram can type it back. Design mirrors search_bus.py:
  * ONE tab ``LoginOTPs``: Phone | Code | Status | Attempts | Created At |
    Updated At. Only the newest rows are kept (bounded growth).
  * Status flow: pending -> sent | expired | failed. VERIFY_OTP on the
    Apps Script side marks rows used/blocked/expired.
  * Writes go through :func:`db._call` so retries/rate-limit handling
    match everything else. The loop must never raise.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

import db
import orders

logger = logging.getLogger(__name__)

TAB_NAME = "LoginOTPs"
HEADERS: List[str] = [
    "Phone", "Code", "Status", "Attempts", "Created At", "Updated At",
]
COL_PHONE = 1
COL_CODE = 2
COL_STATUS = 3
COL_CREATED = 5
COL_UPDATED = 6

STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_EXPIRED = "expired"
STATUS_FAILED = "failed"

TERMINAL = frozenset({STATUS_SENT, STATUS_EXPIRED, STATUS_FAILED,
                      "used", "blocked"})
POLL_FAST_SECONDS = 5.0
POLL_IDLE_SECONDS = 30.0
REQUEST_TTL_SECONDS = 300.0
KEEP_ROWS = 60


def utc_now() -> str:
    try:
        from datetime import datetime, timedelta

        return (datetime.utcnow() + timedelta(hours=5, minutes=30)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except Exception:  # noqa: BLE001 - clock must never break the loop
        return ""


def new_request_id() -> str:
    import uuid

    return "OTP" + uuid.uuid4().hex[:8].upper()


_otp_worksheet = None


def get_otp_worksheet():
    """Open (and cache) the ``LoginOTPs`` tab, creating it when missing."""
    global _otp_worksheet
    if _otp_worksheet is not None:
        return _otp_worksheet
    db.get_worksheet()  # ensures db._client is connected (or raises DatabaseError)
    client = db._client
    if client is None:  # pragma: no cover - defensive; get_worksheet sets it
        raise db.DatabaseError("Google Sheets client is not connected.")
    spreadsheet = db._open_spreadsheet(client)
    try:
        from gspread.exceptions import WorksheetNotFound as _WSNF

        try:
            ws = spreadsheet.worksheet(TAB_NAME)
        except _WSNF:
            ws = spreadsheet.add_worksheet(
                title=TAB_NAME, rows=220, cols=len(HEADERS)
            )
            db._call("Writing login OTP header", ws.update,
                     values=[HEADERS], range_name="A1")
    except db.DatabaseError:
        raise
    except Exception as exc:  # noqa: BLE001 - API/driver failure
        raise db.DatabaseError(f"Login OTP tab unreadable: {exc}")
    _otp_worksheet = ws
    return ws


def _otp_ws():
    try:
        return get_otp_worksheet()
    except db.DatabaseError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise db.DatabaseError(f"Login OTP tab unreadable: {exc}")


def _rows(ws) -> List[Dict[str, Any]]:
    try:
        grid = db._call("Reading login OTP queue", ws.get_all_values)
    except db.DatabaseError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise db.DatabaseError(f"Login OTP queue unreadable: {exc}")
    out: List[Dict[str, Any]] = []
    for idx, row in enumerate(grid, start=1):
        if idx == 1:
            continue  # header
        padded = list(row) + [""] * max(0, len(HEADERS) - len(row))
        if not any(str(c or "").strip() for c in padded):
            continue  # blank row
        out.append(
            {
                "_row": idx,
                "phone": str(padded[COL_PHONE - 1] or "").strip(),
                "code": str(padded[COL_CODE - 1] or "").strip(),
                "status": str(padded[COL_STATUS - 1] or "").strip(),
                "created": str(padded[COL_CREATED - 1] or "").strip(),
            }
        )
    return out


def _mark(ws, row_no: int, status: str) -> None:
    db._call(
        "Marking login OTP",
        ws.update_cell,
        row_no,
        COL_STATUS,
        status,
    )
    try:
        db._call("Stamping login OTP", ws.update_cell, row_no, COL_UPDATED, utc_now())
    except db.DatabaseError:
        pass


def _fresh(row: Dict[str, Any]) -> bool:
    try:
        from datetime import datetime, timedelta

        created = datetime.strptime(row["created"], "%Y-%m-%d %H:%M:%S")
        now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
        return (now_ist - created).total_seconds() <= REQUEST_TTL_SECONDS
    except Exception:  # noqa: BLE001 - unparsable timestamp: still workable
        return True


def _prune(ws, rows: List[Dict[str, Any]]) -> None:
    if len(rows) <= KEEP_ROWS:
        return
    stale = sorted(
        ((r["_row"], r["created"]) for r in rows if r["status"] in TERMINAL),
        key=lambda x: x[1],
    )
    drop = [r for r, _c in stale[: max(0, len(rows) - KEEP_ROWS)]]
    for r in sorted(drop, reverse=True):
        try:
            db._call("Pruning login OTP queue", ws.delete_rows, r)
        except db.DatabaseError:
            continue


def process_once(context: Any = None) -> int:
    """Deliver ONE batch of pending login OTP codes. Returns count handled."""
    try:
        ws = _otp_ws()
    except db.DatabaseError as exc:
        logger.warning("Login OTP: queue tab unavailable (%s)", exc)
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("Login OTP: unexpected queue error (%s)", exc)
        return 0

    rows = [
        r
        for r in _rows(ws)
        if r["status"] == STATUS_PENDING and r["phone"] and r["code"]
    ]
    if not rows:
        return 0

    handled = 0
    for row in rows:
        if not _fresh(row):
            try:
                _mark(ws, row["_row"], STATUS_EXPIRED)
            except db.DatabaseError:
                pass
            handled += 1
            continue
        sent = False
        if context is not None:
            sent = _send_code(context, row["phone"], row["code"])
        else:
            logger.info(
                "Login OTP queued %s (no Telegram context yet)", row["phone"]
            )
        try:
            _mark(ws, row["_row"], STATUS_SENT if sent else STATUS_FAILED)
        except db.DatabaseError:
            pass
        handled += 1
    try:
        _prune(ws, _rows(ws))
    except Exception:  # noqa: BLE001 - pruning must never break the loop
        pass
    return handled


def _send_code(context: Any, phone: str, code: str) -> bool:
    """Send the 4-digit code to admins; they forward it to that phone's owner."""
    text = (
        "🔐 <b>Login code</b>\n"
        f"📞 Phone: <code>{phone}</code>\n"
        f"🔢 Code: <code>{code}</code>\n"
        "⏰ ୫ ମିନିଟ୍ ମଧ୍ୟରେ ମିଆଦ ସରିବ — ଗ୍ରାହକଙ୍କୁ ପଠାନ୍ତୁ।"
    )
    try:
        import asyncio as _aio

        async def _go():
            await orders.notify_admins(context, text)

        try:
            loop = _aio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            fut = _aio.run_coroutine_threadsafe(_go(), loop)
            fut.result(timeout=60)
        else:
            _aio.run(_go())
        logger.info("Login OTP delivered for %s", phone)
        return True
    except Exception as exc:  # noqa: BLE001 - delivery failure only
        logger.warning("Login OTP delivery failed for %s (%s)", phone, exc)
        return False


async def otp_loop(context: Any = None) -> None:
    """Forever: poll LoginOTPs fast after work, slow when idle."""
    await asyncio.sleep(6.0)  # let the bot finish startup first
    while True:
        try:
            done = await asyncio.to_thread(process_once, context)
            wait = POLL_FAST_SECONDS if done else POLL_IDLE_SECONDS
            if done:
                logger.info("Login OTP handled %s request(s).", done)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop must never die
            logger.error("Login OTP pass failed", exc_info=True)
            wait = POLL_IDLE_SECONDS
        await asyncio.sleep(wait)


__all__ = [
    "HEADERS",
    "KEEP_ROWS",
    "POLL_FAST_SECONDS",
    "POLL_IDLE_SECONDS",
    "REQUEST_TTL_SECONDS",
    "STATUS_EXPIRED",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_SENT",
    "TAB_NAME",
    "new_request_id",
    "otp_loop",
    "process_once",
    "utc_now",
]

