"""Validation for web_sync.py (webapp <-> Telegram bot shared-order sync).

Run with:  python tests/test_web_sync.py

No network, no real Telegram, no Google Sheets - order lists are passed in
directly and the bot is faked.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from tempfile import mkdtemp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import web_sync  # noqa: E402

HEADER = [
    "Product Name", "MRP", "Selling Price", "Stock Qty",
    "EAN Barcode", "Image URL", "Category",
]
ROWS = [
    HEADER,
    ["Fortune Mustard Oil 1L", "210", "170", "15", "8901234567890", "", "Oils"],
]

failures = 0


def check(name: str, condition: bool) -> None:
    global failures
    print(("PASS" if condition else "FAIL"), "-", name)
    if not condition:
        failures += 1


class FakeMessage:
    def __init__(self, message_id=555):
        self.message_id = message_id


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edited = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return FakeMessage()

    async def edit_message_text(self, **kwargs):
        self.edited.append(kwargs)
        return FakeMessage()


class FakeContext:
    def __init__(self):
        self.bot = FakeBot()


WEB_ORDER = {
    "timestamp": "2026-10-13 10:00:00",
    "order_id": "MUN-20261013-WEB001",
    "customer_name": "Ravi",
    "phone": "98765",
    "address": "Cinema Hall Road",
    "items": "Fortune Mustard Oil 1L x2 @ Rs.340",
    "total": "340",
    "channel": "webapp",
    "telegram_user_id": "42",
    "status": "pending",
    "status_msg_id": "",
    "location": "20.45,86.02",
    "payment": "COD",
}

CHAT_ORDER = dict(WEB_ORDER, order_id="MUN-20261013-CHAT01", channel="chat",
                  telegram_user_id="77", status_msg_id="")


def main() -> int:
    # --- items text parsing (identical format to the bot) -----------------
    lines = web_sync.parse_items_text("Oil x2 @ Rs.340; Salt x1 @ Rs.25")
    check("items parser: 2 lines", len(lines) == 2)
    check("items parser: qty", lines[0]["qty"] == 2 and lines[1]["qty"] == 1)
    check("items parser: totals",
          lines[0]["line_total"] == 680 and lines[1]["line_total"] == 25)
    summary = web_sync.summary_for(WEB_ORDER)
    check("summary carries total", summary["total"] == 340)
    check("summary carries bill", summary["lines"][0]["name"] == "Fortune Mustard Oil 1L")
    check("summary carries address", summary["address"] == "Cinema Hall Road")

    # --- channel filter ----------------------------------------------------
    check("web order detected", web_sync.is_web_order(WEB_ORDER))
    check("chat order ignored", not web_sync.is_web_order(CHAT_ORDER))

    # --- state helpers -----------------------------------------------------
    web_sync.STATE_FILE = Path(mkdtemp()) / "state.json"
    check("fresh state empty", web_sync.load_state() == {})
    web_sync.save_state({"MUN-1": {"alerted": True, "status": "pending"}})
    check("state persists", web_sync.load_state().get("MUN-1", {}).get("alerted") is True)

    os.environ["ADMIN_ID"] = "999"
    try:
        # --- first pass: alert admin + live customer message ---------------
        ctx = FakeContext()
        processed = asyncio.run(
            web_sync.process_once(ctx, [CHAT_ORDER, WEB_ORDER])
        )
        check("one web order processed", processed == 1)
        check("admin alert + customer message sent", len(ctx.bot.sent) == 2)
        check("admin alert target", str(ctx.bot.sent[0]["chat_id"]) == "999")
        check("admin alert names customer", "Ravi" in ctx.bot.sent[0]["text"])
        check("admin alert shows channel", "webapp" in ctx.bot.sent[0]["text"])
        check("customer got live status message", len(ctx.bot.sent) == 2)
        check("customer target is tg id",
              str(ctx.bot.sent[1]["chat_id"]) == "42")
        check("state marked alerted",
              web_sync.load_state()[WEB_ORDER["order_id"]]["alerted"] is True)

        # --- second pass: NO duplicate alerts ------------------------------
        ctx2 = FakeContext()
        processed2 = asyncio.run(
            web_sync.process_once(ctx2, [CHAT_ORDER, WEB_ORDER])
        )
        check("second pass is a no-op", processed2 == 0 and not ctx2.bot.sent)

        # --- status change: customer's live message edited -----------------
        WEB_ORDER["status"] = "packing"
        WEB_ORDER["status_msg_id"] = "555"
        ctx3 = FakeContext()
        processed3 = asyncio.run(web_sync.process_once(ctx3, [WEB_ORDER]))
        check("status change detected", processed3 == 1)
        check("live message edited", len(ctx3.bot.edited) == 1)
        check("edited for the right chat",
              str(ctx3.bot.edited[0]["chat_id"]) == "42")
        check("edit carries new status", "packing" in ctx3.bot.edited[0]["text"]
              or "Packing" in ctx3.bot.edited[0]["text"])
        check("state stores new status",
              web_sync.load_state()[WEB_ORDER["order_id"]]["status"] == "packing")

        # --- chat-only orders never touch the loop ------------------------
        ctx4 = FakeContext()
        processed4 = asyncio.run(web_sync.process_once(ctx4, [CHAT_ORDER]))
        check("chat orders skipped", processed4 == 0 and not ctx4.bot.sent)
    finally:
        os.environ.pop("ADMIN_ID", None)

    print()
    if failures:
        print(f"{failures} check(s) FAILED")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
