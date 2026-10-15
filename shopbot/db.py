"""Google Sheets database layer for the MUNNA MOBILE & Grocery backend.

Sheet layout (1-based columns)::

    Column 1: Product Name    Column 4: Stock Qty       Column 7: Category
    Column 2: MRP             Column 5: EAN Barcode
    Column 3: Selling Price   Column 6: Image URL

Configuration is read from the project-root ``.env`` file (python-dotenv):

    GOOGLE_SHEETS_CREDENTIALS_FILE  service-account JSON key file (required)
    GOOGLE_SHEET_ID                 spreadsheet ID (optional, preferred)
    GOOGLE_SHEET_NAME               spreadsheet title (default: "MUNNA MOBILE & Grocery")
    GOOGLE_SHEET_WORKSHEET          tab name (default: "sheet1")
    GOOGLE_SHEETS_AUTH              "auto" | "oauth2client" | "service_account"

Authentication note
-------------------
The project spec calls for ``oauth2client``. Modern ``gspread`` (>= 6) is built
on ``google-auth``, so this module tries the oauth2client credentials first and
transparently falls back to ``gspread.service_account()`` when the installed
gspread cannot use them (set ``GOOGLE_SHEETS_AUTH`` to force one backend).

All functions are synchronous because gspread blocks. In python-telegram-bot
v20+ call them from plain (non-async) handlers or wrap them with
``await asyncio.to_thread(...)`` from async handlers.
"""

from __future__ import annotations

import logging
import re
import os
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import gspread
from dotenv import load_dotenv
from gspread.exceptions import APIError, SpreadsheetNotFound, WorksheetNotFound
from requests.exceptions import RequestException

load_dotenv()

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Sheet layout (1-based column indexes)
# --------------------------------------------------------------------------- #
COL_NAME = 1
COL_MRP = 2
COL_PRICE = 3
COL_STOCK = 4
COL_EAN = 5
COL_IMAGE = 6
COL_CATEGORY = 7

DEFAULT_SPREADSHEET_NAME = "MUNNA MOBILE & Grocery"
DEFAULT_WORKSHEET = "Sheet1"  # Google's default tab name (case sensitive)
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

MAX_RETRIES = 4
RETRY_BASE_DELAY = 1.5  # seconds; doubled after every retry
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class DatabaseError(Exception):
    """Raised when the Google Sheets database cannot be reached or updated."""


# --------------------------------------------------------------------------- #
# Configuration helpers
# --------------------------------------------------------------------------- #
def _env(name: str, default: str = "") -> str:
    """Read a trimmed environment variable; empty values fall back to default."""
    return (os.getenv(name) or default).strip()


def _credentials_file() -> str:
    path = _env("GOOGLE_SHEETS_CREDENTIALS_FILE")
    if not path:
        raise DatabaseError(
            "GOOGLE_SHEETS_CREDENTIALS_FILE is not set. "
            "Add the service-account JSON path to your .env file."
        )
    if not os.path.isfile(path):
        raise DatabaseError(f"Google service-account file not found: {path}")
    return path


# --------------------------------------------------------------------------- #
# Authentication (oauth2client first, google-auth fallback)
# --------------------------------------------------------------------------- #
def _authorize_oauth2client(path: str) -> gspread.Client:
    from oauth2client.service_account import ServiceAccountCredentials

    credentials = ServiceAccountCredentials.from_json_keyfile_name(path, SCOPES)
    return gspread.authorize(credentials)


def _authorize_service_account(path: str) -> gspread.Client:
    return gspread.service_account(filename=path, scopes=SCOPES)


_AUTH_STRATEGIES: Dict[str, Callable[[str], gspread.Client]] = {
    "oauth2client": _authorize_oauth2client,
    "service_account": _authorize_service_account,
}


def _ordered_strategies() -> List[str]:
    preference = _env("GOOGLE_SHEETS_AUTH", "auto").lower()
    if preference in ("", "auto"):
        return ["oauth2client", "service_account"]
    if preference in _AUTH_STRATEGIES:
        return [preference]
    raise DatabaseError(
        f"Invalid GOOGLE_SHEETS_AUTH value {preference!r}; "
        "use 'auto', 'oauth2client' or 'service_account'."
    )


# --------------------------------------------------------------------------- #
# Connection (cached)
# --------------------------------------------------------------------------- #
_client: Optional[gspread.Client] = None
_worksheet: Optional[gspread.Worksheet] = None
_orders_worksheet: Optional[gspread.Worksheet] = None  # "Orders" tab (cached)


def reset_cache() -> None:
    """Drop the cached client/worksheet (used by tests and reconnect logic)."""
    global _client, _worksheet, _orders_worksheet
    _client = None
    _worksheet = None
    _orders_worksheet = None


def _target_spreadsheet_name() -> str:
    return _env("GOOGLE_SHEET_NAME") or DEFAULT_SPREADSHEET_NAME


def _open_spreadsheet(client: gspread.Client):
    """Open the shop spreadsheet (by ID when set, else by title)."""
    sheet_id = _env("GOOGLE_SHEET_ID")
    try:
        if sheet_id:
            return client.open_by_key(sheet_id)
        return client.open(_target_spreadsheet_name())
    except SpreadsheetNotFound as exc:
        raise DatabaseError(
            f"Google Sheet '{sheet_id or _target_spreadsheet_name()}' not found. "
            "Check GOOGLE_SHEET_ID / GOOGLE_SHEET_NAME in .env and share the "
            "sheet with the service-account email."
        ) from exc


def _open_worksheet(client: gspread.Client) -> gspread.Worksheet:
    spreadsheet = _open_spreadsheet(client)
    tab = _env("GOOGLE_SHEET_WORKSHEET") or DEFAULT_WORKSHEET
    try:
        return spreadsheet.worksheet(tab)
    except WorksheetNotFound as exc:
        raise DatabaseError(
            f"Worksheet/tab '{tab}' not found. Check GOOGLE_SHEET_WORKSHEET in .env."
        ) from exc


def get_worksheet() -> gspread.Worksheet:
    """Open (and cache) the product worksheet.

    Tries the oauth2client credentials required by the project spec first and
    falls back to ``gspread.service_account()`` (google-auth) when the installed
    gspread version cannot use them.
    """
    global _client, _worksheet
    if _worksheet is not None:
        return _worksheet

    path = _credentials_file()
    failures: List[str] = []
    for strategy in _ordered_strategies():
        try:
            client = _AUTH_STRATEGIES[strategy](path)
            worksheet = _open_worksheet(client)
        except DatabaseError:
            raise  # sheet/tab genuinely missing -> config problem, not auth
        except Exception as exc:  # noqa: BLE001 - try the next auth backend
            logger.warning(
                "Google Sheets authentication via %s failed: %s", strategy, exc
            )
            failures.append(f"{strategy}: {exc}")
            continue
        _client = client
        _worksheet = worksheet
        logger.info("Connected to Google Sheets via %s", strategy)
        return worksheet
    raise DatabaseError("Could not connect to Google Sheets (%s)" % "; ".join(failures))


# --------------------------------------------------------------------------- #
# Retries / error handling
# --------------------------------------------------------------------------- #
def _status_code(exc: APIError) -> Optional[int]:
    try:
        return int(exc.response.status_code)
    except Exception:
        return None


def _api_message(description: str, status: Optional[int], exc: Exception) -> str:
    if status == 429:
        hint = " Google Sheets API rate limit hit - try again in about a minute."
    elif status in (401, 403):
        hint = " Check that the sheet is shared with the service-account email."
    else:
        hint = ""
    return f"{description} failed (HTTP {status}): {exc}.{hint}"


def _call(
    description: str, func: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    """Run one gspread call, retrying rate-limits (429/5xx) and network errors.

    Non-retryable API errors and unexpected exceptions are re-raised as
    :class:`DatabaseError` so callers only need to handle one error type.
    """
    delay = RETRY_BASE_DELAY
    last: Optional[BaseException] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return func(*args, **kwargs)
        except DatabaseError:
            raise
        except (SpreadsheetNotFound, WorksheetNotFound) as exc:
            raise DatabaseError(f"{description} failed: {exc}") from exc
        except APIError as exc:
            status = _status_code(exc)
            last = exc
            if status not in RETRYABLE_STATUS_CODES:
                raise DatabaseError(_api_message(description, status, exc)) from exc
            logger.warning(
                "%s: retryable HTTP %s (attempt %d/%d)",
                description,
                status,
                attempt,
                MAX_RETRIES,
            )
        except RequestException as exc:
            last = exc
            logger.warning(
                "%s: network error: %s (attempt %d/%d)",
                description,
                exc,
                attempt,
                MAX_RETRIES,
            )
        except Exception as exc:
            raise DatabaseError(f"{description} failed: {exc}") from exc
        if attempt < MAX_RETRIES:
            time.sleep(delay)
            delay *= 2

    if isinstance(last, APIError):
        raise DatabaseError(_api_message(description, _status_code(last), last)) from last
    raise DatabaseError(
        f"{description} failed after {MAX_RETRIES} attempts "
        f"(network timeout/connection): {last}"
    ) from last


# --------------------------------------------------------------------------- #
# Parsing / normalisation helpers
# --------------------------------------------------------------------------- #
def normalize_ean(raw: Any) -> str:
    """Normalise a cell value to a comparable EAN string.

    Handles numbers stored as floats (``5901234123457.0``) and stray whitespace.
    """
    if raw is None:
        return ""
    if isinstance(raw, float) and raw.is_integer():
        return str(int(raw))
    text = str(raw).strip()
    if text.endswith(".0"):
        head = text[:-2]
        if head.isdigit():
            text = head
    return text


def _require_ean(ean: Any) -> str:
    code = normalize_ean(ean)
    if not code:
        raise ValueError("EAN barcode is required.")
    if len(code) > 64:
        raise ValueError("EAN barcode is too long (max 64 characters).")
    return code


def _cell(row: Sequence[str], column: int) -> str:
    return str(row[column - 1]).strip() if len(row) >= column else ""


def _to_number(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a number, got {value!r}.")
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().replace(",", "")
        try:
            number = float(text)
        except (TypeError, ValueError):
            raise ValueError(f"{field} must be a number, got {value!r}.") from None
    if number < 0:
        raise ValueError(f"{field} cannot be negative.")
    return int(number) if number.is_integer() else number


def _to_int(value: Any, field: str) -> int:
    number = _to_number(value, field)
    if isinstance(number, float) and not number.is_integer():
        raise ValueError(f"{field} must be a whole number, got {value!r}.")
    return int(number)


# --------------------------------------------------------------------------- #
# EAN duplicate check (core of the admin auto-stock flow)
# --------------------------------------------------------------------------- #
def find_by_ean(ean: Any) -> Optional[Tuple[int, List[str]]]:
    """Look up a product by its EAN barcode (column 5).

    Returns ``(row_number, row_values)`` (1-based row number, full row) for the
    first matching row, or ``None`` when the EAN is not in the sheet yet.
    Raises :class:`ValueError` for an empty EAN and :class:`DatabaseError` on
    API/network failures.
    """
    target = _require_ean(ean)
    worksheet = get_worksheet()
    grid: List[List[str]] = _call("EAN lookup", worksheet.get_all_values)
    for index, row in enumerate(grid):
        if normalize_ean(_cell(row, COL_EAN)) == target:
            return index + 1, list(row)
    return None


def update_stock(
    found: Tuple[int, List[str]], qty_to_add: Any, mrp: Any, selling_price: Any
) -> Dict[str, Any]:
    """Existing-EAN path: add ``qty_to_add`` to the stock and refresh prices.

    ``found`` is the ``(row_number, row_values)`` pair returned by
    :func:`find_by_ean`, so no second lookup is needed. Writes columns B:D
    (MRP, Selling Price, Stock Qty) of that row.

    Returns a summary dict: ``row``, ``old_stock``, ``new_stock``, ``mrp``,
    ``selling_price``.
    """
    row_number, row = found
    old_stock = _to_int(_cell(row, COL_STOCK) or 0, "Stock Qty")
    increment = _to_int(qty_to_add, "Quantity")
    if increment < 0:
        raise ValueError("Quantity cannot be negative.")
    new_mrp = _to_number(mrp, "MRP")
    new_price = _to_number(selling_price, "Selling Price")

    worksheet = get_worksheet()
    range_name = f"B{row_number}:D{row_number}"
    _call(
        "Updating stock row",
        worksheet.update,
        values=[[new_mrp, new_price, old_stock + increment]],
        range_name=range_name,
    )
    return {
        "row": row_number,
        "old_stock": old_stock,
        "new_stock": old_stock + increment,
        "mrp": new_mrp,
        "selling_price": new_price,
    }


def update_stock_by_ean(
    ean: Any, qty_to_add: Any, mrp: Any, selling_price: Any
) -> Dict[str, Any]:
    """Convenience wrapper: duplicate check + :func:`update_stock` in one call.

    Raises ``ValueError`` when the EAN does not exist yet (the caller should
    then run the new-product flow instead).
    """
    found = find_by_ean(ean)
    if found is None:
        raise ValueError(f"Product with EAN '{normalize_ean(ean)}' does not exist yet.")
    return update_stock(found, qty_to_add, mrp, selling_price)


_OrderItemRE = re.compile(r"^(.*?)\s+x(\d+)\s+@\s*Rs\.?([0-9.]+)\s*$")


def _parse_items_bill(items_text: Any) -> List[Dict[str, Any]]:
    """Parse ``"Name x2 @ Rs.170; Name2 x1 @ Rs.25"`` back into bill lines.

    Returns ``[{name, qty, price, line_total}]``.
    """
    lines: List[Dict[str, Any]] = []
    for chunk in str(items_text or "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = _OrderItemRE.match(chunk)
        if m:
            qty = int(m.group(2))
            price = float(m.group(3))
            lines.append(
                {
                    "name": m.group(1).strip(),
                    "qty": qty,
                    "price": price,
                    "line_total": round(price * qty, 2),
                }
            )
    return lines
    """Subtract ``qty`` from a product's Stock Qty (column D).

    Used by the checkout path of every ordering channel so shelves stay
    accurate. Raises ``ValueError`` when the EAN is unknown, the quantity is
    invalid, or there is not enough stock. Returns a summary dict with
    ``row``, ``product_name``, ``old_stock``, ``new_stock`` and ``ean``.
    """
    wanted = _to_int(qty, "Quantity")
    if wanted <= 0:
        raise ValueError("Quantity must be at least 1.")
    found = find_by_ean(ean)
    if found is None:
        raise ValueError(f"Product with EAN '{normalize_ean(ean)}' does not exist yet.")
    row_number, row = found
    old_stock = _to_int(_cell(row, COL_STOCK) or 0, "Stock Qty")
    if old_stock < wanted:
        raise ValueError(
            f"Not enough stock for '{_cell(row, COL_NAME)}' "
            f"(available {old_stock}, wanted {wanted})."
        )
    worksheet = get_worksheet()
    range_name = f"{_COL_LETTERS[COL_STOCK]}{row_number}"
    _call(
        "Deducting stock",
        worksheet.update,
        values=[[old_stock - wanted]],
        range_name=range_name,
    )
    return {
        "row": row_number,
        "product_name": _cell(row, COL_NAME),
        "ean": normalize_ean(ean),
        "old_stock": old_stock,
        "new_stock": old_stock - wanted,
    }


def adjust_stock_by_ean(ean: Any, delta: Any) -> Dict[str, Any]:
    """Add ``delta`` (positive or negative) to a product's Stock Qty (column D).

    Used by the admin Browse-by-Category +/− stock controls. The new value is
    clamped at 0: reaching 0 keeps the row and simply marks it Out of Stock -
    the row is only removed by an explicit delete. Only column D is written, so
    the name / MRP / selling price / image / category are untouched. Raises
    ``ValueError`` when the EAN is unknown or ``delta`` is not a whole number.
    Returns a summary dict with ``row``, ``product_name``, ``ean``,
    ``old_stock`` and ``new_stock``.
    """
    step = _to_int(delta, "Quantity")
    found = find_by_ean(ean)
    if found is None:
        raise ValueError(f"Product with EAN '{normalize_ean(ean)}' does not exist yet.")
    row_number, row = found
    old_stock = _to_int(_cell(row, COL_STOCK) or 0, "Stock Qty")
    new_stock = max(0, old_stock + step)
    worksheet = get_worksheet()
    range_name = f"{_COL_LETTERS[COL_STOCK]}{row_number}"
    _call(
        "Adjusting stock",
        worksheet.update,
        values=[[new_stock]],
        range_name=range_name,
    )
    return {
        "row": row_number,
        "product_name": _cell(row, COL_NAME),
        "ean": normalize_ean(ean),
        "old_stock": old_stock,
        "new_stock": new_stock,
    }


# 1-based column -> spreadsheet column letter (A-F for the product sheet)
_COL_LETTERS: Dict[int, str] = {
    COL_NAME: "A",
    COL_MRP: "B",
    COL_PRICE: "C",
    COL_STOCK: "D",
    COL_EAN: "E",
    COL_IMAGE: "F",
    COL_CATEGORY: "G",
}


def update_field_by_ean(ean: Any, column: int, value: Any) -> Dict[str, Any]:
    """Update ONE cell of the product row identified by ``ean``.

    ``column`` is one of the module-level ``COL_*`` constants. The value is
    validated according to the column (name: non-empty text; MRP/price:
    non-negative number; qty: whole number). Returns a summary dict with the
    1-based ``row``, the ``column`` and the stored ``value``.
    """
    col = int(column)
    if col not in _COL_LETTERS:
        raise ValueError(f"Unsupported column {column!r}.")
    if col == COL_NAME:
        stored: Any = str(value or "").strip()
        if not stored:
            raise ValueError("Product Name is required.")
    elif col in (COL_MRP, COL_PRICE):
        stored = _to_number(
            value, "MRP" if col == COL_MRP else "Selling Price"
        )
    elif col == COL_STOCK:
        stored = _to_int(value, "Stock Qty")
        if stored < 0:
            raise ValueError("Stock Qty cannot be negative.")
    elif col == COL_EAN:
        stored = _require_ean(value)
    elif col == COL_CATEGORY:
        stored = str(value or "").strip()
    else:  # COL_IMAGE
        stored = str(value or "").strip()

    found = find_by_ean(ean)
    if found is None:
        raise ValueError(
            f"Product with EAN '{normalize_ean(ean)}' does not exist yet."
        )
    row_number, _ = found
    worksheet = get_worksheet()
    range_name = f"{_COL_LETTERS[col]}{row_number}"
    _call(
        "Updating product field",
        worksheet.update,
        values=[[stored]],
        range_name=range_name,
    )
    return {"row": row_number, "column": col, "value": stored}


def add_product(
    product_name: Any,
    mrp: Any,
    selling_price: Any,
    qty: Any,
    ean: Any,
    image_url: Any,
    category: Any = "",
) -> Dict[str, Any]:
    """Append a brand-new product row (columns A-G). Returns the stored values.

    The caller is responsible for the duplicate check (:func:`find_by_ean`)
    and for supplying the image URL. ``category`` defaults to "General" when
    empty so every row has a label for category-wise browsing.
    """
    name = str(product_name or "").strip()
    if not name:
        raise ValueError("Product Name is required.")
    category_text = str(category or "").strip() or "General"
    stored = {
        "product_name": name,
        "mrp": _to_number(mrp, "MRP"),
        "selling_price": _to_number(selling_price, "Selling Price"),
        "stock_qty": _to_int(qty, "Stock Qty"),
        "ean": _require_ean(ean),
        "image_url": str(image_url or "").strip(),
        "category": category_text,
    }
    worksheet = get_worksheet()
    row = [
        stored["product_name"],
        stored["mrp"],
        stored["selling_price"],
        stored["stock_qty"],
        stored["ean"],
        stored["image_url"],
        stored["category"],
    ]
    _call("Adding new product", worksheet.append_row, row)
    return stored


def get_all_products() -> List[Dict[str, Any]]:
    """Return every product row as a dict (header row excluded).

    Useful as the catalog source for the Gemini RAG assistant.
    """
    worksheet = get_worksheet()
    grid: List[List[str]] = _call("Loading catalog", worksheet.get_all_values)
    products: List[Dict[str, Any]] = []
    for index, row in enumerate(grid):
        padded = list(row) + [""] * max(0, COL_CATEGORY - len(row))
        if index == 0 and _cell(padded, COL_NAME).lower() in ("product name", "name"):
            continue  # header row
        if not any(str(cell).strip() for cell in padded):
            continue  # blank row
        products.append(
            {
                "row": index + 1,
                "product_name": _cell(padded, COL_NAME),
                "mrp": _cell(padded, COL_MRP),
                "selling_price": _cell(padded, COL_PRICE),
                "stock_qty": _cell(padded, COL_STOCK),
                "ean": normalize_ean(padded[COL_EAN - 1]),
                "image_url": _cell(padded, COL_IMAGE),
                "category": _cell(padded, COL_CATEGORY) or "General",
            }
        )
    return products


def get_categories() -> List[str]:
    """Distinct, non-empty categories across the catalog (sorted)."""
    names = {
        str(p.get("category") or "").strip() or "General"
        for p in get_all_products()
    }
    return sorted(names, key=str.lower)


def get_products_by_category(category: Any) -> List[Dict[str, Any]]:
    """All products whose Category column matches ``category`` (case-insensitive)."""
    target = str(category or "").strip().lower()
    return [
        p for p in get_all_products()
        if str(p.get("category") or "General").strip().lower() == target
    ]


def search_products(keyword: Any, limit: int = 25) -> List[Dict[str, Any]]:
    """Case-insensitive search over Product Name, Category and EAN.

    Used by both the customer storefront (🔍 Search) and the Admin Panel
    (Browse-by-Category 🔍 Search). Matches are ordered so the strongest
    name hits come first, then category hits, then EAN hits. ``limit``
    caps the result list (0/None = no cap). Never raises for empty input;
    an empty sheet simply yields ``[]``.
    """
    needle = str(keyword or "").strip().lower()
    if not needle:
        return []
    name_hits: List[Dict[str, Any]] = []
    cat_hits: List[Dict[str, Any]] = []
    ean_hits: List[Dict[str, Any]] = []
    for prod in get_all_products():
        name = str(prod.get("product_name") or "").lower()
        category = str(prod.get("category") or "").lower()
        ean = str(prod.get("ean") or "").lower()
        if needle in name:
            name_hits.append(prod)
        elif needle in category:
            cat_hits.append(prod)
        elif needle in ean:
            ean_hits.append(prod)
    ordered = name_hits + cat_hits + ean_hits
    if limit and limit > 0:
        ordered = ordered[:limit]
    return ordered



def delete_product_by_ean(ean: Any) -> Dict[str, Any]:
    """Delete the product row identified by ``ean``. Returns a summary dict.

    Raises ``ValueError`` when the EAN does not exist.
    """
    found = find_by_ean(ean)
    if found is None:
        raise ValueError(
            f"Product with EAN '{normalize_ean(ean)}' does not exist yet."
        )
    row_number, row = found
    worksheet = get_worksheet()
    _call("Deleting product row", worksheet.delete_rows, row_number)
    return {
        "row": row_number,
        "product_name": _cell(row, COL_NAME),
        "ean": normalize_ean(ean),
    }


# --------------------------------------------------------------------------- #
# Orders tab (one row per placed order, shared by all ordering channels)
# --------------------------------------------------------------------------- #
ORDERS_TAB_NAME = "Orders"
ORDERS_HEADERS: List[str] = [
    "Timestamp",
    "Order ID",
    "Customer Name",
    "Phone",
    "Address",
    "Items",
    "Total",
    "Channel",
    "Telegram User ID",
    "Status",
    "Status Msg ID",
    "Location",
    "Payment",
]
# column indexes (0-based) for the status/workflow fields
COL_ORDER_STATUS = 9
COL_ORDER_STATUS_MSG = 10
COL_ORDER_LOCATION = 11
COL_ORDER_PAYMENT = 12

# order lifecycle (seller advances these from the Admin Panel)
ORDER_STATUSES: List[str] = [
    "pending",
    "accepted",
    "packing",
    "out_for_delivery",
    "delivered",
    "cancelled",
]
# customer-facing labels + emoji for each status
ORDER_STATUS_LABELS = {
    "pending": "🕐 Pending (awaiting seller)",
    "accepted": "✅ Order accepted",
    "packing": "📦 Packing",
    "out_for_delivery": "🚚 Out for delivery",
    "delivered": "🎉 Delivered",
    "cancelled": "❌ Cancelled",
}
# which step comes next in the happy path (accept -> packing -> ... -> done)
ORDER_STATUS_NEXT = {
    "pending": "accepted",
    "accepted": "packing",
    "packing": "out_for_delivery",
    "out_for_delivery": "delivered",
}


def get_orders_worksheet():
    """Open (and cache) the ``Orders`` tab, creating it when missing."""
    global _orders_worksheet
    if _orders_worksheet is not None:
        return _orders_worksheet
    get_worksheet()  # ensures _client is connected (or raises DatabaseError)
    client = _client
    if client is None:  # pragma: no cover - defensive; get_worksheet sets it
        raise DatabaseError("Google Sheets client is not connected.")
    spreadsheet = _open_spreadsheet(client)
    try:
        worksheet = spreadsheet.worksheet(ORDERS_TAB_NAME)
    except WorksheetNotFound:
        try:
            worksheet = spreadsheet.add_worksheet(
                title=ORDERS_TAB_NAME, rows=1000, cols=len(ORDERS_HEADERS)
            )
        except Exception as exc:  # noqa: BLE001 - API/driver failure
            raise DatabaseError(f"Could not create the Orders tab: {exc}") from exc
        _call(
            "Writing Orders header",
            worksheet.update,
            values=[ORDERS_HEADERS],
            range_name=f"A1:{chr(ord('A') + len(ORDERS_HEADERS) - 1)}1",
        )
    # migrate older sheets: make sure Status / Status Msg ID / Location headers exist
    try:
        existing_header = _call("Reading Orders header", worksheet.row_values, 1)
    except Exception:  # noqa: BLE001 - header read failure must not block orders
        existing_header = []
    if len(existing_header) < len(ORDERS_HEADERS):
        _call(
            "Updating Orders header",
            worksheet.update,
            values=[ORDERS_HEADERS],
            range_name=f"A1:{chr(ord('A') + len(ORDERS_HEADERS) - 1)}1",
        )
    _orders_worksheet = worksheet
    return worksheet


def make_order_id() -> str:
    """Unique order id, e.g. ``MUN-20261010-AB12CD``."""
    import random
    import string
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d")
    rand = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    return f"MUN-{stamp}-{rand}"


def place_order(
    items,
    customer_name="",
    phone="",
    address="",
    channel="chat",
    telegram_user_id="",
    order_id=None,
    timestamp=None,
    location="",
    payment="COD",
):
    """Validate, deduct stock and log an order - the ONE checkout path.

    ``items`` is a list of ``{"ean": ..., "qty": ...}`` dicts. Every ordering
    channel (inline chat cart, Mini App, AI agent) must call this so stock
    deduction and logging are identical everywhere.

    Returns ``{"order_id": ..., "total": ..., "lines": ..., "timestamp": ...}``
    where each line carries name/qty/price/line_total. Raises ``ValueError``
    for bad input or insufficient stock, :class:`DatabaseError` on API
    failures.
    """
    from datetime import datetime

    cleaned = []
    for entry in items or []:
        if not isinstance(entry, dict):
            raise ValueError("Each order item must carry an EAN and a quantity.")
        ean = normalize_ean(entry.get("ean"))
        if not ean:
            raise ValueError("Order item is missing its EAN.")
        qty = _to_int(entry.get("qty"), "Quantity")
        if qty <= 0:
            raise ValueError("Order quantity must be at least 1.")
        cleaned.append({"ean": ean, "qty": qty})
    if not cleaned:
        raise ValueError("The order has no items.")

    # Resolve each line against the live catalog first (fail before deducting).
    catalog = get_all_products()
    by_ean = {str(p.get("ean") or ""): p for p in catalog}
    lines = []
    for entry in cleaned:
        prod = by_ean.get(entry["ean"])
        if prod is None:
            raise ValueError(
                f"Product with EAN '{entry['ean']}' does not exist yet."
            )
        try:
            price = _to_number(prod.get("selling_price") or 0, "Selling Price")
        except ValueError:
            price = 0
        try:
            available = _to_int(prod.get("stock_qty") or 0, "Stock Qty")
        except ValueError:
            available = 0
        if available < entry["qty"]:
            raise ValueError(
                f"Not enough stock for '{prod.get('product_name')}' "
                f"(available {available}, wanted {entry['qty']})."
            )
        line_total = price * entry["qty"] if isinstance(price, (int, float)) else 0
        lines.append(
            {
                "ean": entry["ean"],
                "name": str(prod.get("product_name") or "Product"),
                "qty": entry["qty"],
                "price": price,
                "line_total": line_total,
            }
        )
    total = sum(float(line["line_total"] or 0) for line in lines)
    total = int(total) if float(total).is_integer() else round(total, 2)

    # Deduct stock row by row (each call re-reads the sheet row).
    for line in lines:
        deduct_stock_by_ean(line["ean"], line["qty"])

    oid = str(order_id or "").strip() or make_order_id()
    ts = str(timestamp or "").strip() or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    items_text = "; ".join(
        f"{line['name']} x{line['qty']} @ Rs.{line['price']}" for line in lines
    )
    row = [
        ts,
        oid,
        str(customer_name or "").strip(),
        str(phone or "").strip(),
        str(address or "").strip(),
        items_text,
        total,
        str(channel or "chat").strip() or "chat",
        str(telegram_user_id or "").strip(),
        "pending",  # Status - the seller advances this from the Admin Panel
        "",         # Status Msg ID - filled once the customer status msg is sent
        str(location or "").strip(),
        str(payment or "COD").strip() or "COD",
    ]
    worksheet = get_orders_worksheet()
    _call("Logging order", worksheet.append_row, row)
    return {
        "order_id": oid,
        "total": total,
        "lines": lines,
        "timestamp": ts,
        "status": "pending",
    }


def get_recent_orders(limit=20):
    """Newest-first order rows from the Orders tab (header excluded)."""
    try:
        limit_n = max(1, int(limit))
    except (TypeError, ValueError):
        limit_n = 20
    try:
        worksheet = get_orders_worksheet()
    except DatabaseError:
        return []
    grid = _call("Loading orders", worksheet.get_all_values)
    orders = []
    for index, row in enumerate(grid):
        padded = list(row) + [""] * max(0, len(ORDERS_HEADERS) - len(row))
        if index == 0 and str(padded[0]).strip().lower() in ("timestamp", "date", "time"):
            continue  # header row
        if not any(str(cell).strip() for cell in padded):
            continue  # blank row
        orders.append(
            {
                "row": index + 1,
                "timestamp": str(padded[0]).strip(),
                "order_id": str(padded[1]).strip(),
                "customer_name": str(padded[2]).strip(),
                "phone": str(padded[3]).strip(),
                "address": str(padded[4]).strip(),
                "items": str(padded[5]).strip(),
                "total": str(padded[6]).strip(),
                "channel": str(padded[7]).strip(),
                "telegram_user_id": str(padded[8]).strip(),
                # older rows pre-date these columns -> default sensibly
                "status": str(padded[COL_ORDER_STATUS]).strip() or "pending",
                "status_msg_id": str(padded[COL_ORDER_STATUS_MSG]).strip(),
                "location": str(padded[COL_ORDER_LOCATION]).strip(),
                "payment": str(padded[COL_ORDER_PAYMENT]).strip() or "COD",
            }
        )
    orders.reverse()  # newest first (append-only log)
    return orders[:limit_n]


# --------------------------------------------------------------------------- #
# Order workflow (seller advances status from the Admin Panel)
# --------------------------------------------------------------------------- #
def _order_row_by_id(order_id):
    """Locate an order by id; returns ``(sheet_row, order_dict)`` or ``(None, None)``."""
    target = str(order_id or "").strip()
    if not target:
        return None, None
    for order in get_recent_orders(limit=2000):
        if str(order.get("order_id") or "").strip() == target:
            return int(order.get("row") or 0), order
    return None, None


def set_order_status(order_id: Any, status: Any) -> str:
    """Write the Status cell for an order; returns the normalised status."""
    normalised = str(status or "").strip()
    if normalised not in ORDER_STATUSES:
        raise ValueError(f"Unknown order status: {status}")
    row, _order = _order_row_by_id(order_id)
    if row is None:
        raise ValueError(f"Order '{order_id}' not found.")
    worksheet = get_orders_worksheet()
    _call(
        "Updating order status",
        worksheet.update_cell,
        row,
        COL_ORDER_STATUS + 1,
        normalised,
    )
    # When a seller cancels an order, the shelf quantity the customer took out
    # must go back. Same rule applies whether the cancel came from the bot
    # panel, the webapp or the customer notification.
    if normalised == "cancelled":
        logger.info("Order %s cancelled - restoring stock.", order_id)
        restore_order_stock(order_id)
    return normalised


def restore_order_stock(order_id: Any) -> Dict[str, Any]:
    """Put the shelf quantity back that a cancelled order took out.

    The shared Orders tab keeps items as free text (``"Name x2 @ Rs.170;
    Name2 x1 @ Rs.25"``). This reads that text, matches each product name to
    the live Sheet1 catalog, and calls :func:`adjust_stock_by_ean` to put the
    stock back. Best-effort: the shelf stock moves only when a real catalog
    row can be matched by name.

    Returns a summary dict with ``restored`` (rows adjusted), ``missing``
    (names that did not match a product) and ``order_id``.
    """
    row, order = _order_row_by_id(order_id)
    if row is None:
        return {"restored": 0, "missing": [], "order_id": order_id}

    items_text = (order or {}).get("items") or ""
    if not str(items_text or "").strip():
        return {"restored": 0, "missing": [], "order_id": order_id}

    lines = _parse_items_bill(items_text)
    catalog = get_all_products()
    by_name: Dict[str, List[Dict[str, Any]]] = {}
    for prod in catalog:
        name = str(prod.get("product_name") or "").strip().lower()
        if name:
            by_name.setdefault(name, []).append(prod)

    restored = 0
    missing: List[str] = []
    for line in lines:
        try:
            qty = int(line.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        name = str(line.get("name") or "").strip().lower()
        prods = by_name.get(name)
        if not prods:
            missing.append(name)
            continue
        ean = str(prods[0].get("ean") or "").strip()
        if not ean:
            missing.append("%s@unknown-ean" % name)
            continue
        try:
            adjust_stock_by_ean(ean, qty)
            restored += 1
        except Exception as exc:  # noqa: BLE001 - keep the shelf moving
            logger.warning("Restore stock for %s failed (%s): %s", ean, name, exc)
            missing.append("%s@%s" % (name, ean))

    return {"restored": restored, "missing": missing, "order_id": order_id}


def set_order_status_msg(order_id: Any, message_id: Any) -> bool:
    """Store the customer's status message id so it can be edited in place."""
    row, _order = _order_row_by_id(order_id)
    if row is None:
        return False
    worksheet = get_orders_worksheet()
    _call(
        "Updating order status message id",
        worksheet.update_cell,
        row,
        COL_ORDER_STATUS_MSG + 1,
        str(message_id or ""),
    )
    return True


def update_order_bill(order_id: Any, items_text: Any, total: Any) -> None:
    """Replace the Items + Total cells after the seller edits the bill."""
    row, _order = _order_row_by_id(order_id)
    if row is None:
        raise ValueError(f"Order '{order_id}' not found.")
    worksheet = get_orders_worksheet()
    _call("Updating order items", worksheet.update_cell, row, 6, str(items_text or ""))
    _call("Updating order total", worksheet.update_cell, row, 7, total)


# --------------------------------------------------------------------------- #
# Addresses tab (one DEFAULT address per customer - auto-fills checkout)
# --------------------------------------------------------------------------- #
ADDRESSES_TAB_NAME = "Addresses"
ADDRESSES_HEADERS: List[str] = [
    "Telegram User ID",
    "Name",
    "Address",
    "Phone",
    "Location",
    "Updated At",
]
_addresses_worksheet = None


def get_addresses_worksheet():
    """Open (and cache) the ``Addresses`` tab, creating it when missing."""
    global _addresses_worksheet
    if _addresses_worksheet is not None:
        return _addresses_worksheet
    get_worksheet()  # ensures _client is connected (or raises DatabaseError)
    client = _client
    if client is None:  # pragma: no cover - defensive; get_worksheet sets it
        raise DatabaseError("Google Sheets client is not connected.")
    spreadsheet = _open_spreadsheet(client)
    try:
        worksheet = spreadsheet.worksheet(ADDRESSES_TAB_NAME)
    except WorksheetNotFound:
        try:
            worksheet = spreadsheet.add_worksheet(
                title=ADDRESSES_TAB_NAME, rows=500, cols=len(ADDRESSES_HEADERS)
            )
        except Exception as exc:  # noqa: BLE001 - API/driver failure
            raise DatabaseError(f"Could not create the Addresses tab: {exc}") from exc
        _call(
            "Writing Addresses header",
            worksheet.update,
            values=[ADDRESSES_HEADERS],
            range_name=f"A1:{chr(ord('A') + len(ADDRESSES_HEADERS) - 1)}1",
        )
    _addresses_worksheet = worksheet
    return worksheet


def get_address(user_id: Any) -> Optional[Dict[str, str]]:
    """Return the customer's saved default address, or ``None``."""
    if user_id is None:
        return None
    key = str(user_id).strip()
    try:
        worksheet = get_addresses_worksheet()
    except DatabaseError:
        return None
    grid = _call("Loading address", worksheet.get_all_values)
    for index, row in enumerate(grid):
        if index == 0:
            continue
        padded = list(row) + [""] * max(0, len(ADDRESSES_HEADERS) - len(row))
        if str(padded[0]).strip() == key:
            return {
                "user_id": key,
                "name": str(padded[1]).strip(),
                "address": str(padded[2]).strip(),
                "phone": str(padded[3]).strip(),
                "location": str(padded[4]).strip(),
            }
    return None


def save_address(
    user_id: Any,
    name: str = "",
    address: str = "",
    phone: str = "",
    location: str = "",
) -> List[str]:
    """Create or REPLACE the customer's default address (one row per user).

    Replacing keeps a single default address per customer - changing the
    address removes the old one and stores the new one as the default.
    """
    if user_id is None:
        raise ValueError("Cannot save an address without a user id.")
    key = str(user_id).strip()
    from datetime import datetime

    updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = [
        key,
        str(name or "").strip(),
        str(address or "").strip(),
        str(phone or "").strip(),
        str(location or "").strip(),
        updated,
    ]
    worksheet = get_addresses_worksheet()
    grid = _call("Reading addresses", worksheet.get_all_values)
    for index, existing in enumerate(grid):
        if index == 0:
            continue
        padded = list(existing) + [""] * max(0, len(ADDRESSES_HEADERS) - len(existing))
        if str(padded[0]).strip() == key:
            _call(
                "Updating address",
                worksheet.update,
                values=[row],
                range_name=f"A{index + 1}:F{index + 1}",
            )
            return row
    _call("Saving address", worksheet.append_row, row)
    return row


__all__ = [
    "COL_NAME",
    "COL_MRP",
    "COL_PRICE",
    "COL_STOCK",
    "COL_EAN",
    "COL_IMAGE",
    "COL_CATEGORY",
    "COL_ORDER_LOCATION",
    "COL_ORDER_PAYMENT",
    "COL_ORDER_STATUS",
    "COL_ORDER_STATUS_MSG",
    "DatabaseError",
    "ORDER_STATUSES",
    "ORDER_STATUS_LABELS",
    "ORDER_STATUS_NEXT",
    "ORDERS_HEADERS",
    "add_product",
    "adjust_stock_by_ean",
    "deduct_stock_by_ean",
    "delete_product_by_ean",
    "find_by_ean",
    "get_address",
    "get_addresses_worksheet",
    "get_all_products",
    "get_categories",
    "get_orders_worksheet",
    "get_products_by_category",
    "get_recent_orders",
    "get_worksheet",
    "make_order_id",
    "normalize_ean",
    "place_order",
    "reset_cache",
    "save_address",
    "set_order_status",
    "set_order_status_msg",
    "update_field_by_ean",
    "update_order_bill",
    "update_stock",
    "update_stock_by_ean",
]




