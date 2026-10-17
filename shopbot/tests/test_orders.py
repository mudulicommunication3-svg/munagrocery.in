"""Validation for orders.py + db.py order layer (no network, no Telegram).

Run with:  python tests/test_orders.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
import orders  # noqa: E402

HEADER = [
    "Product Name", "MRP", "Selling Price", "Stock Qty",
    "EAN Barcode", "Image URL", "Category",
]
ROWS = [
    HEADER,
    ["Fortune Mustard Oil 1L", "210", "170", "15", "8901234567890", "http://img/oil.jpg", "Oils"],
    ["Tata Salt 1kg", "28", "25", "5", "8901030595486", "", "Staples"],
    ["Parle-G", "20", "18", "0", "8901719101156", "", "Snacks"],
]

failures = 0


def check(name: str, condition: bool) -> None:
    global failures
    print(("PASS" if condition else "FAIL"), "-", name)
    if not condition:
        failures += 1


class FakeWorksheet:
    def __init__(self, rows):
        self.rows = [list(row) for row in rows]
        self.updated = []
        self.appended = []

    def get_all_values(self):
        return [list(row) for row in self.rows]

    def update(self, values=None, range_name=None, **kwargs):
        self.updated.append((values, range_name))
        if range_name and values:
            import re as _re
            m = _re.match(r"^([A-Z]+)(\d+)(?::([A-Z]+)(\d+))?$", range_name or "")
            if m:
                col = ord(m.group(1)) - ord("A") + 1
                row_n = int(m.group(2))
                while len(self.rows) < row_n:
                    self.rows.append([])
                row = self.rows[row_n - 1]
                while len(row) < col:
                    row.append("")
                row[col - 1] = str(values[0][0])
        return {}

    def append_row(self, values, **kwargs):
        self.appended.append(list(values))
        self.rows.append(list(values))
        return None


class FakeSpreadsheet:
    def __init__(self):
        self.tabs = {}

    def worksheet(self, name):
        if name not in self.tabs:
            from gspread.exceptions import WorksheetNotFound
            raise WorksheetNotFound(f"no tab {name}")
        return self.tabs[name]

    def add_worksheet(self, title, rows=1000, cols=9):
        ws = FakeWorksheet([])
        self.tabs[title] = ws
        return ws


class FakeClient:
    def __init__(self, spreadsheet):
        self.spreadsheet = spreadsheet

    def open_by_key(self, _key):
        return self.spreadsheet

    def open(self, _title):
        return self.spreadsheet


CATALOG = [
    {"product_name": "Fortune Mustard Oil 1L", "mrp": "210",
     "selling_price": "170", "stock_qty": "15", "ean": "8901234567890",
     "image_url": "http://img/oil.jpg", "category": "Oils"},
    {"product_name": "Tata Salt 1kg", "mrp": "28",
     "selling_price": "25", "stock_qty": "5", "ean": "8901030595486",
     "image_url": "", "category": "Staples"},
    {"product_name": "Parle-G", "mrp": "20",
     "selling_price": "18", "stock_qty": "0", "ean": "8901719101156",
     "image_url": "", "category": "Snacks"},
]


def fresh(rows=None):
    db.reset_cache()
    ws = FakeWorksheet(ROWS if rows is None else rows)
    db._worksheet = ws
    sheet = FakeSpreadsheet()
    sheet.tabs["Sheet1"] = ws
    db._client = FakeClient(sheet)
    return ws, sheet


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return None


class FakeContext:
    def __init__(self):
        self.bot = FakeBot()
        self.user_data = {}

def main() -> int:
    db.RETRY_BASE_DELAY = 0

    # --- parser -----------------------------------------------------------
    lines = orders.parse_order_lines("I need 2 Fortune oils and 1 Tata salt")
    check("parser finds 2 lines", len(lines) == 2)
    check("parser qty+query", lines[0]["qty"] == 2 and "fortune" in lines[0]["query"])

    odia = orders.parse_order_lines("ମୋତେ ୨ ଫର୍ଚୁନ ତେଲ ଦରକାର")
    check("parser handles Odia numerals", bool(odia) and odia[0]["qty"] == 2)

    check("parser empty -> []", orders.parse_order_lines("   ") == [])

    # --- matching ---------------------------------------------------------
    matched, missing = orders.match_catalog(
        [{"qty": 2, "query": "fortune oil"}, {"qty": 1, "query": "tata salt"}],
        CATALOG,
    )
    check("matcher resolves both", len(matched) == 2 and not missing)
    check("matcher totals", sum(m["line_total"] for m in matched) == 2 * 170 + 25)

    matched, missing = orders.match_catalog([{"qty": 1, "query": "parle"}], CATALOG)
    check("matcher skips out-of-stock", not matched and missing == ["parle"])

    # --- bill + alert -----------------------------------------------------
    bill = orders.bill_text(
        [{"name": "Fortune Oil", "qty": 2, "price": 170, "line_total": 340}],
        340,
    )
    check("bill renders lines+total", "x2" in bill and "Rs.340" in bill)

    alert = orders.admin_alert_text("MUN-1", "Ravi", "98765", "Main Rd",
                                    [{"name": "Oil", "qty": 1, "price": 170,
                                      "line_total": 170}], 170, "chat", 123)
    for needle in ("Ravi", "98765", "Main Rd", "Rs.170", "MUN-1"):
        check(f"alert carries {needle}", needle in alert)

    # --- deduct_stock -----------------------------------------------------
    ws, _sheet = fresh()
    summary = db.deduct_stock_by_ean("8901234567890", 3)
    check("deduct reduces stock", summary["new_stock"] == 12)
    check("deduct writes D2", ws.updated == [([[12]], "D2")])

    fresh()
    try:
        db.deduct_stock_by_ean("8901030595486", 99)
        check("deduct rejects over-stock", False)
    except ValueError:
        check("deduct rejects over-stock", True)

    # --- place_order (shared checkout path) -------------------------------
    ws, sheet = fresh()
    result = db.place_order(
        [{"ean": "8901234567890", "qty": 2}, {"ean": "8901030595486", "qty": 1}],
        customer_name="Ravi", phone="98765", address="Main Rd",
        channel="chat", telegram_user_id="123",
    )
    check("place_order total", result["total"] == 2 * 170 + 25)
    check("place_order id shape", result["order_id"].startswith("MUN-"))
    check("place_order deducted oil", ws.updated[0] == ([[13]], "D2"))
    check("place_order deducted salt", ws.updated[1] == ([[4]], "D3"))
    orders_ws = sheet.tabs.get("Orders")
    check("place_order created Orders tab", orders_ws is not None)
    check("place_order logged row", len(orders_ws.appended) == 1)
    logged = orders_ws.appended[0]
    check("order row has customer+total",
          logged[2] == "Ravi" and logged[6] == 2 * 170 + 25 and logged[7] == "chat")
    check("order row has 18 unified columns", len(logged) == len(db.ORDERS_HEADERS) == 18)
    check("order row starts Unseen alert", logged[17] == "Unseen")

    fresh()
    try:
        db.place_order([{"ean": "8901030595486", "qty": 99}], channel="ai")
        check("place_order rejects short stock", False)
    except ValueError:
        check("place_order rejects short stock", True)

    # --- get_recent_orders ------------------------------------------------
    _ws, sheet = fresh()
    orders_ws = FakeWorksheet([db.ORDERS_HEADERS])
    sheet.tabs["Orders"] = orders_ws
    db._orders_worksheet = orders_ws
    orders_ws.append_row(["2026-01-01", "MUN-OLD", "A", "1", "x", "Oil x1", 170, "chat", "1"])
    orders_ws.append_row(["2026-01-02", "MUN-NEW", "B", "2", "y", "Salt x2", 50, "ai", "2"])
    recent = db.get_recent_orders(limit=5)
    check("recent newest-first", [o["order_id"] for o in recent] == ["MUN-NEW", "MUN-OLD"])
    check("recent reads 18-column fields", all("delivery_type" in o and "admin_alert_status" in o for o in recent))

    # --- checkout() notifies admins ---------------------------------------
    _ws, _sheet = fresh()
    os.environ["ADMIN_ID"] = "999"
    try:
        ctx = FakeContext()
        summary = asyncio.run(orders.checkout(
            ctx, [{"ean": "8901234567890", "qty": 1}],
            customer_name="Ravi", phone="9", address="Rd",
            channel="miniapp", telegram_user_id="42",
        ))
        check("checkout returns order", summary["order_id"].startswith("MUN-"))
        check("checkout alerted admin",
              len(ctx.bot.sent) == 1 and ctx.bot.sent[0]["chat_id"] == 999)
        check("alert mentions channel", "miniapp" in ctx.bot.sent[0]["text"])
    finally:
        os.environ.pop("ADMIN_ID", None)

    # --- webapp payload shapes --------------------------------------------
    import storefront

    check("webapp list shape",
          storefront._parse_webapp_payload('[{"ean": "111", "qty": 2}]')
          == [{"ean": "111", "qty": 2}])
    check("webapp dict-items shape",
          storefront._parse_webapp_payload('{"items": [{"ean": "111", "qty": 1}]}')
          == [{"ean": "111", "qty": 1}])
    check("webapp cart-map shape",
          storefront._parse_webapp_payload('{"111": 3}') == [{"ean": "111", "qty": 3}])
    try:
        storefront._parse_webapp_payload("not json")
        check("webapp bad json rejected", False)
    except ValueError:
        check("webapp bad json rejected", True)

    # --- cart helpers -----------------------------------------------------
    ctx2 = FakeContext()
    check("empty cart count 0", storefront.cart_count(storefront.get_cart(ctx2)) == 0)
    storefront.get_cart(ctx2)["8901234567890"] = 2
    check("cart count sums qty", storefront.cart_count(storefront.get_cart(ctx2)) == 2)

    print()
    if failures:
        print(f"{failures} check(s) FAILED")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
