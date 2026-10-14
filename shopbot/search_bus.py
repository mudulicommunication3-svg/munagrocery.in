"""Bot-side worker for the Sheet search bus (webapp admin JioMart search).

The admin panel on a phone cannot reach JioMart directly (browser CORS, dead
public relays, localhost-only proxy). This module turns the BOT PC — where
the JioMart catalog API is PROVEN to work (see jiomart_search.py) — into the
search backend:

    webapp (phone) --GET_SEARCH--> [SearchQueue tab] --poll--> this loop
       --jiomart_search.fetch_items--> [SearchQueue tab] <--GET_SEARCH-- phone

Design notes (quota-safe, reuses the bot's existing db machinery):
  * ONE tab named ``SearchQueue``: Request ID | Query | Pincode | Status |
    Items JSON | Error | Created At | Updated At. Only the newest N rows are
    kept (bounded growth, no sheet bloat).
  * Status flow: pending -> working -> done | error. Stale pendings (older
    than REQUEST_TTL_SECONDS) are skipped so abandoned requests never pile up.
  * Writes go through :func:`db._call` so retries/rate-limit handling match
    everything else. The loop must never raise: every pass is wrapped.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Dict, List

import db
import jiomart_search

logger = logging.getLogger(__name__)

TAB_NAME = "SearchQueue"
HEADERS: List[str] = [
    "Request ID", "Query", "Pincode", "Status",
    "Items JSON", "Error", "Created At", "Updated At",
]
# Tab column indexes (1-based) for random access in the worker.
COL_REQ_ID = 1
COL_QUERY = 2
COL_PINCODE = 3
COL_STATUS = 4
COL_ITEMS = 5
COL_ERROR = 6
COL_CREATED = 7
COL_UPDATED = 8

STATUS_PENDING = "pending"
STATUS_WORKING = "working"
STATUS_DONE = "done"
STATUS_ERROR = "error"

POLL_FAST_SECONDS = 5.0       # tight loop right after a handled request
POLL_IDLE_SECONDS = 30.0      # relaxed loop when nothing is pending
REQUEST_TTL_SECONDS = 120.0   # requests older than this are skipped
KEEP_ROWS = 15                # cap tab size: newest requests only
MAX_ITEMS_PER_REQUEST = 12    # mirrors the webapp's grid page

# Bump when item shape changes so readers can evolve safely.
PROTOCOL_VERSION = 1


def utc_now() -> str:
    """Sheet-friendly timestamp (India time, same style as Apps Script)."""
    try:
        from datetime import datetime, timedelta

        return (datetime.utcnow() + timedelta(hours=5, minutes=30)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except Exception:  # noqa: BLE001 - clock must never break the loop
        return ""


def pack_items(items: List[Dict[str, Any]]) -> str:
    """Slim the bot's item dicts to the webapp's shape."""
    slim: List[Dict[str, Any]] = []
    for it in (items or [])[:MAX_ITEMS_PER_REQUEST]:
        try:
            imgs = it.get("images") or []
            img = imgs[0] if imgs else (it.get("image") or "")
            slim.append(
                {
                    "title": it.get("name") or "Product",
                    "url": img,
                    "mrp": str(it.get("mrp", "") or ""),
                    "sp": str(it.get("price", "") or ""),
                    "cat": it.get("category") or "Grocery",
                    "sellable": it.get("in_stock", True),
                    "source": "JioMart",
                }
            )
        except Exception:  # noqa: BLE001 - one bad item never kills the batch
            continue
    return json.dumps(slim, ensure_ascii=False)


def new_request_id() -> str:
    return "SQ-" + uuid.uuid4().hex[:10].upper()


def _queue_ws():
    """Open (and create with headers when missing) the SearchQueue tab."""
    client = db._client  # reuse the cached gspread auth, never re-login
    if client is None:
        db.get_worksheet()
        client = db._client
    sheet_id = db._env("GOOGLE_SHEET_ID")
    if sheet_id:
        wb = client.open_by_key(sheet_id)
    else:
        wb = client.open(db._target_spreadsheet_name())
    try:
        ws = wb.worksheet(TAB_NAME)
    except Exception:  # noqa: BLE001 - missing tab is expected once
        ws = wb.add_worksheet(title=TAB_NAME, rows=100, cols=len(HEADERS))
        ws.update("A1", [HEADERS])
        return ws
    try:
        head = ws.row_values(1)
        if [str(v or "").strip() for v in head[: len(HEADERS)]] != HEADERS:
            ws.update("A1", [HEADERS])
    except Exception:  # noqa: BLE001 - header repair is best effort
        pass
    return ws


def _rows(ws) -> List[Dict[str, Any]]:
    """Read every data row with its 1-based sheet row number."""
    try:
        values = db._call("read search queue", ws.get_all_values)
    except db.DatabaseError:
        return []
    out: List[Dict[str, Any]] = []
    for idx, row in enumerate(values[1:], start=2):
        row = list(row or []) + [""] * len(HEADERS)
        out.append(
            {
                "_row": idx,
                "request_id": str(row[COL_REQ_ID - 1] or "").strip(),
                "query": str(row[COL_QUERY - 1] or "").strip(),
                "pincode": str(row[COL_PINCODE - 1] or "").strip(),
                "status": str(row[COL_STATUS - 1] or "").strip(),
                "items_json": str(row[COL_ITEMS - 1] or ""),
                "error": str(row[COL_ERROR - 1] or ""),
                "created": str(row[COL_CREATED - 1] or ""),
            }
        )
    return out


def _set(ws, row: int, status: str, items_json: str = "", error: str = "") -> None:
    now = utc_now()
    db._call(
        "write search result",
        ws.update,
        f"D{row}:H{row}",
        [[status, items_json, error, "", now]],
    )


def _prune(ws, rows: List[Dict[str, Any]]) -> None:
    """Keep only the newest KEEP_ROWS rows so the tab never grows."""
    if len(rows) <= KEEP_ROWS:
        return
    stale = sorted(rows, key=lambda r: (r["created"], r["_row"]))[: len(rows) - KEEP_ROWS]
    # delete bottom-up so row numbers stay valid
    for r in sorted((x["_row"] for x in stale), reverse=True):
        try:
            db._call("prune search queue", ws.delete_rows, r)
        except db.DatabaseError:
            continue


def _fresh(row: Dict[str, Any]) -> bool:
    try:
        from datetime import datetime, timedelta

        created = datetime.strptime(row["created"], "%Y-%m-%d %H:%M:%S")
        now_ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
        return (now_ist - created).total_seconds() <= REQUEST_TTL_SECONDS
    except Exception:  # noqa: BLE001 - unparsable timestamp: still workable
        return True


def process_once() -> int:
    """Answer ONE batch of pending search requests. Returns count handled."""
    try:
        ws = _queue_ws()
    except db.DatabaseError as exc:
        logger.warning("Search bus: queue tab unavailable (%s)", exc)
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("Search bus: unexpected queue error (%s)", exc)
        return 0

    rows = [r for r in _rows(ws) if r["status"] == STATUS_PENDING and r["query"]]
    if not rows:
        return 0

    handled = 0
    for row in rows:
        if not _fresh(row):
            continue
        pin = row["pincode"] or "752106"
        try:
            _set(ws, row["_row"], STATUS_WORKING)
        except db.DatabaseError:
            continue
        try:
            items = jiomart_search.fetch_items(row["query"], pin) or []
            _set(ws, row["_row"], STATUS_DONE, pack_items(items))
            handled += 1
            logger.info(
                "Search bus answered %s (%r @ %s): %d items",
                row["request_id"] or "?",
                row["query"],
                pin,
                len(items or []),
            )
        except Exception as exc:  # noqa: BLE001 - per-request failure only
            try:
                _set(ws, row["_row"], STATUS_ERROR, "", str(exc)[:300])
            except db.DatabaseError:
                pass
            handled += 1
    try:
        _prune(ws, _rows(ws))
    except Exception:  # noqa: BLE001 - pruning must never break the loop
        pass
    return handled


async def search_loop(context: Any = None) -> None:
    """Forever: poll SearchQueue fast after work, slow when idle."""
    import asyncio

    await asyncio.sleep(6.0)  # let the bot finish startup first
    while True:
        try:
            done = await asyncio.to_thread(process_once)
            wait = POLL_FAST_SECONDS if done else POLL_IDLE_SECONDS
            if done:
                logger.info("Search bus handled %s request(s).", done)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop must never die
            logger.error("Search bus pass failed", exc_info=True)
            wait = POLL_IDLE_SECONDS
        await asyncio.sleep(wait)


__all__ = [
    "COL_ERROR",
    "COL_ITEMS",
    "COL_PINCODE",
    "COL_QUERY",
    "COL_REQ_ID",
    "COL_STATUS",
    "HEADERS",
    "KEEP_ROWS",
    "MAX_ITEMS_PER_REQUEST",
    "POLL_FAST_SECONDS",
    "POLL_IDLE_SECONDS",
    "PROTOCOL_VERSION",
    "REQUEST_TTL_SECONDS",
    "STATUS_DONE",
    "STATUS_ERROR",
    "STATUS_PENDING",
    "STATUS_WORKING",
    "TAB_NAME",
    "new_request_id",
    "pack_items",
    "process_once",
    "search_loop",
    "utc_now",
]
