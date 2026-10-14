"""Self-contained validation for db.py (no network, no real Google account).

Run with:  python tests/test_db.py

A fake worksheet is injected into db's cache, so only the pure logic is
exercised: EAN duplicate check, stock/price update, row append, validation,
retry/backoff behaviour and error mapping.
"""

from __future__ import annotations

import os
import sys

import requests
from gspread.exceptions import APIError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
from db import DatabaseError  # noqa: E402

HEADER = [
    "Product Name",
    "MRP",
    "Selling Price",
    "Stock Qty",
    "EAN Barcode",
    "Image URL",
]
ROWS = [
    HEADER,
    ["Parle-G Gold", "20", "18", "12", "8901719101156", "http://img.example/pg.jpg"],
    ["Tata Salt", "28", "25", "5", "8901030595486", ""],
]

failures = 0


def check(name: str, condition: bool) -> None:
    global failures
    print(("PASS" if condition else "FAIL"), "-", name)
    if not condition:
        failures += 1


def api_error(status: int, message: str = "boom") -> APIError:
    response = requests.Response()
    response.status_code = status
    response._content = (
        f'{{"error": {{"code": {status}, "message": "{message}"}}}}'
    ).encode()
    response.headers["content-type"] = "application/json"
    return APIError(response)


class FakeWorksheet:
    """Minimal stand-in for gspread.Worksheet."""

    def __init__(self, rows):
        self.rows = [list(row) for row in rows]
        self.updated = []
        self.appended = []
        self.get_calls = 0
        self.fail_once = None  # exception raised on the next call only
        self.always_fail = None  # exception raised on every call

    def get_all_values(self):
        self.get_calls += 1
        if self.always_fail is not None:
            raise self.always_fail
        if self.fail_once is not None:
            exc, self.fail_once = self.fail_once, None
            raise exc
        return [list(row) for row in self.rows]

    def update(self, values=None, range_name=None, **kwargs):
        self.updated.append((values, range_name))
        return {}

    def append_row(self, values, **kwargs):
        self.appended.append(list(values))
        return None


def fresh(rows=None) -> FakeWorksheet:
    db.reset_cache()
    worksheet = FakeWorksheet(ROWS if rows is None else rows)
    db._worksheet = worksheet  # inject fake (bypasses authentication)
    return worksheet


def expect_value_error(name: str, func, *args, **kwargs) -> None:
    try:
        func(*args, **kwargs)
    except ValueError:
        check(name, True)
    else:
        check(name, False)


def main() -> int:
    db.RETRY_BASE_DELAY = 0  # keep retries instant in tests

    # --- EAN duplicate check ------------------------------------------------
    fresh()
    found = db.find_by_ean("8901719101156")
    check("find_by_ean finds existing row 2", found is not None and found[0] == 2)

    fresh()
    found = db.find_by_ean(" 8901719101156 ")
    check("find_by_ean trims whitespace", found is not None and found[0] == 2)

    fresh([HEADER, ["X", "1", "1", "1", 1234567890123.0, ""]])
    found = db.find_by_ean("1234567890123")
    check("find_by_ean matches float-formatted cell", found is not None and found[0] == 2)

    fresh()
    check("find_by_ean returns None for unknown EAN", db.find_by_ean("9999999999999") is None)

    fresh()
    expect_value_error("empty EAN raises ValueError", db.find_by_ean, "   ")

    # --- Duplicate path: stock + price update -------------------------------
    worksheet = fresh()
    found = db.find_by_ean("8901719101156")
    summary = db.update_stock(found, 10, 22, 19.5)
    check(
        "update_stock adds qty (12 + 10 = 22)",
        summary["old_stock"] == 12 and summary["new_stock"] == 22,
    )
    check(
        "update_stock writes MRP/price/qty to B2:D2",
        worksheet.updated == [([[22, 19.5, 22]], "B2:D2")],
    )

    fresh()
    expect_value_error(
        "update_stock_by_ean rejects unknown EAN",
        db.update_stock_by_ean,
        "1111111111111",
        1,
        10,
        9,
    )

    fresh()
    found = db.find_by_ean("8901719101156")
    expect_value_error("negative qty rejected", db.update_stock, found, -5, 20, 18)
    expect_value_error("non-numeric MRP rejected", db.update_stock, found, 5, "abc", 18)

    # --- New product path ---------------------------------------------------
    worksheet = fresh()
    stored = db.add_product(
        "Amul Butter", "50", "48.5", 6, "8901262010012", "http://img.example/butter.jpg"
    )
    check(
        "add_product appends the A-G row (category defaults to General)",
        worksheet.appended
        == [["Amul Butter", 50, 48.5, 6, "8901262010012", "http://img.example/butter.jpg", "General"]],
    )
    check("add_product returns stored values", stored["ean"] == "8901262010012")

    worksheet = fresh()
    db.add_product("Milk", 10, 9, 5, "8901111111111", "u", "Dairy")
    check(
        "add_product stores the given category",
        worksheet.appended == [["Milk", 10, 9, 5, "8901111111111", "u", "Dairy"]],
    )

    fresh()
    expect_value_error("add_product rejects empty name", db.add_product, "  ", 1, 1, 1, "123", "u")
    expect_value_error("add_product rejects bad MRP", db.add_product, "X", "abc", 1, 1, "123", "u")
    expect_value_error("add_product rejects missing EAN", db.add_product, "X", 1, 1, 1, "", "u")
    check("price parsing strips commas", db._to_number("1,299", "MRP") == 1299)

    # --- Retry / error mapping ---------------------------------------------
    worksheet = fresh()
    worksheet.fail_once = api_error(429, "Quota exceeded")
    result = db.find_by_ean("8901030595486")
    check("retries after HTTP 429 and succeeds", result is not None and worksheet.get_calls == 2)

    worksheet = fresh()
    worksheet.always_fail = api_error(429, "Quota exceeded")
    try:
        db.find_by_ean("8901030595486")
    except DatabaseError as exc:
        check("persistent 429 becomes DatabaseError", "rate limit" in str(exc))
    else:
        check("persistent 429 becomes DatabaseError", False)

    worksheet = fresh()
    worksheet.fail_once = api_error(403, "The caller does not have permission")
    try:
        db.find_by_ean("8901030595486")
    except DatabaseError as exc:
        check("HTTP 403 raises DatabaseError with hint", "403" in str(exc) and "share" in str(exc))
    else:
        check("HTTP 403 raises DatabaseError with hint", False)

    worksheet = fresh()
    worksheet.always_fail = requests.exceptions.ConnectionError("no route to host")
    try:
        db.find_by_ean("8901030595486")
    except DatabaseError as exc:
        check("network failure raises DatabaseError after retries", "network" in str(exc))
    else:
        check("network failure raises DatabaseError after retries", False)

    # --- Catalog helper (RAG source) ----------------------------------------
    fresh()
    products = db.get_all_products()
    check("get_all_products skips header row", len(products) == 2)
    check(
        "get_all_products returns parsed fields",
        products[0]["product_name"] == "Parle-G Gold"
        and products[0]["selling_price"] == "18"
        and products[0]["ean"] == "8901719101156",
    )

    # --- Missing credentials -------------------------------------------------
    db.reset_cache()
    os.environ["GOOGLE_SHEETS_CREDENTIALS_FILE"] = "definitely_missing.json"
    try:
        db.get_worksheet()
    except DatabaseError as exc:
        check("missing credentials file raises DatabaseError", "not found" in str(exc))
    else:
        check("missing credentials file raises DatabaseError", False)

    print()
    if failures:
        print(f"{failures} check(s) FAILED")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

