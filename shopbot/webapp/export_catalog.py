"""Export the live Google Sheets catalog into webapp/index.html for the Mini App.

Usage:  python webapp/export_catalog.py

Reads every product via db.get_all_products() and embeds it inline into
``index.html`` (``window.STORE_CATALOG = [...]``) so the deployed Mini App is
self-contained. Re-run (or cron it) whenever the catalog changes.

Needs the same .env as the bot (GOOGLE_SHEETS_CREDENTIALS_FILE, ...).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
HTML_PATH = BASE_DIR / "index.html"


def build_catalog() -> list:
    items = []
    for prod in db.get_all_products():
        ean = str(prod.get("ean") or "").strip()
        name = str(prod.get("product_name") or "").strip()
        if not ean or not name:
            continue
        items.append(
            {
                "ean": ean,
                "product_name": name,
                "mrp": str(prod.get("mrp") or "0"),
                "selling_price": str(prod.get("selling_price") or "0"),
                "stock_qty": str(prod.get("stock_qty") or "0"),
                "image_url": str(prod.get("image_url") or ""),
                "category": str(prod.get("category") or "General") or "General",
            }
        )
    return items


def _inject_into_html(items: list, html_path: Path) -> None:
    """Embed the catalog JSON inline into index.html.

    Replaces the existing ``window.STORE_CATALOG = [...]`` block so the Mini
    App stays self-contained (no external catalog file needed).
    """
    data = json.dumps(items, ensure_ascii=False)
    replacement = f"window.STORE_CATALOG = {data};"
    html = html_path.read_text(encoding="utf-8")
    start = html.find("window.STORE_CATALOG =")
    end = html.find("];", start)
    if start != -1 and end != -1:
        html = html[:start] + replacement + html[end + 2:]
    else:
        raise SystemExit(f"STORE_CATALOG block not found in {html_path.name}")
    html_path.write_text(html, encoding="utf-8")


def main() -> int:
    try:
        items = build_catalog()
    except db.DatabaseError as exc:
        print(f"ERROR: {exc}")
        return 1
    _inject_into_html(items, HTML_PATH)
    print(f"Exported {len(items)} products -> {HTML_PATH.name} (inline)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
