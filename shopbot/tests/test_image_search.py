"""Validation for jiomart_search / image_search (mocked HTTP, no network).

Run with:  python tests/test_image_search.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import image_search  # noqa: E402
import jiomart_search  # noqa: E402

failures = 0
total = 0


def check(name: str, condition: bool) -> None:
    global failures, total
    total += 1
    print(("PASS" if condition else "FAIL"), "-", name)
    if not condition:
        failures += 1


def make_item(name="Oil 1L", uid=1, sellable=True, images=3):
    """A minimal JioMart search item in the real API's shape."""
    return {
        "uid": uid,
        "slug": f"{name.lower().replace(' ', '-')}-{uid}",
        "name": name,
        "sellable": sellable,
        "price": {"effective": {"min": 170, "max": 170},
                  "marked": {"min": 210, "max": 210}},
        "brand": {"name": "Fortune"},
        "sizes": ["OS"],
        "instock_variants": {"sizes": ["OS"]} if sellable else {},
        "seller_id": 1,
        "sku_code": f"SKU{uid}",
        "net-quantity-value": 1,
        "net-quantity-unit": "L",
        "attributes": {"vertical-code": "GROCERIES"},
        "medias": [
            {"url": f"https://cdn.example/{uid}-{i}.jpg", "type": "image"}
            for i in range(images)
        ],
    }


def install_fetch(fake_fetch):
    """Point jiomart_search.fetch_items at a fake; returns restore callable."""
    original = jiomart_search.fetch_items
    jiomart_search.fetch_items = fake_fetch

    def restore():
        jiomart_search.fetch_items = original

    return restore


def main() -> int:
    # --- search_configured / reset_service (compat surface) ----------------
    check("search_configured always True", image_search.search_configured())
    image_search.reset_service()  # must not raise

    # --- search_product: pincode loop --------------------------------------
    # Case 1: in stock at the FIRST pincode -> stops immediately
    calls = []

    def fetch_one(pin):
        def fake(query, pincode, page_size=5):
            calls.append(pincode)
            return [make_item(sellable=(pincode == pin))]
        return fake

    restore = install_fetch(fetch_one("753003"))
    try:
        p = jiomart_search.search_product("oil")
    finally:
        restore()
    check("in-stock first pin -> single call", calls == ["753003"])
    check("result pincode recorded", p.get("pincode") == "753003")
    check("in_stock True", p.get("in_stock") is True)
    check("name extracted", p.get("name") == "Oil 1L")
    check("price extracted", p.get("price") == 170.0)
    check("mrp extracted", p.get("mrp") == 210.0)
    check("main image is medias[0]", p.get("image") == "https://cdn.example/1-0.jpg")

    # Case 2: out of stock at 753003 and 753001, in stock at 751010
    calls = []
    restore = install_fetch(fetch_one("751010"))
    try:
        p = jiomart_search.search_product("oil")
    finally:
        restore()
    check("fallback walks all three pincodes",
          calls == ["753003", "753001", "751010"])
    check("winner pincode is 751010", p.get("pincode") == "751010")
    check("winner is in stock", p.get("in_stock") is True)

    # Case 3: out of stock everywhere -> last result with in_stock=False
    calls = []
    restore = install_fetch(fetch_one("__none__"))
    try:
        p = jiomart_search.search_product("oil")
    finally:
        restore()
    check("all OOS -> all pincodes tried", len(calls) == 3)
    check("all OOS -> in_stock False", p.get("in_stock") is False)
    check("all OOS -> details still returned", p.get("name") == "Oil 1L")

    # Case 4: no results anywhere -> {}
    restore = install_fetch(lambda q, pin, page_size=5: [])
    try:
        p = jiomart_search.search_product("oil")
    finally:
        restore()
    check("no results -> empty dict", p == {})

    # Case 5: empty / whitespace query -> {} and no HTTP
    calls = []
    restore = install_fetch(lambda q, pin, page_size=5: calls.append(pin) or [])
    try:
        check("empty query -> {}",
              jiomart_search.search_product("   ") == {})
    finally:
        restore()
    check("empty query hits no endpoint", calls == [])

    # --- fetch_items: HTTP / network failure -> [] -------------------------
    original_urlopen = jiomart_search.urllib.request.urlopen

    def raise_http(*a, **k):
        raise OSError("network down")

    jiomart_search.urllib.request.urlopen = raise_http
    try:
        check("network failure -> []",
              jiomart_search.fetch_items("oil", "753003") == [])
    finally:
        jiomart_search.urllib.request.urlopen = original_urlopen


    # --- collect_images / fetch_images (admin picker contract) -------------
    items = [make_item(uid=u) for u in range(1, 6)]  # 5 products x 3 images
    restore = install_fetch(lambda q, pin, page_size=5: items)
    try:
        urls = image_search.fetch_images("oil", 1)
        check("first page has 5 URLs", len(urls) == 5)
        check("first URL is first product main image",
              urls[0] == "https://cdn.example/1-0.jpg")
        urls2 = image_search.fetch_images("oil", 6)
        check("second page continues the flattened list",
              urls2[0] == "https://cdn.example/2-2.jpg")
        check("start beyond window -> []",
              image_search.fetch_images("oil", image_search.MAX_START_INDEX + 1) == [])
    finally:
        restore()

    # empty query -> []
    check("empty query -> no images", image_search.fetch_images("   ") == [])

    # dedupe + http(s)-only + type filter in _extract_images
    mixed = {
        "medias": [
            {"url": "https://a.jpg", "type": "image"},
            {"url": "https://a.jpg", "type": "image"},
            {"url": "javascript:alert(1)", "type": "image"},
            {"url": "https://b.jpg", "type": "image"},
            {"url": "https://v.mp4", "type": "video"},
        ]
    }
    got = jiomart_search._extract_images(mixed)
    check("dedupe + http-only + type filter",
          got == ["https://a.jpg", "https://b.jpg"])

    # fetch_images surfaces network failure as []
    jiomart_search.urllib.request.urlopen = raise_http
    try:
        check("fetch_images network failure -> []",
              image_search.fetch_images("oil") == [])
    finally:
        jiomart_search.urllib.request.urlopen = original_urlopen

    # --- header construction (the live-verified contract) ------------------
    h = jiomart_search._build_headers("753003", visitor_id="vid123")
    check("Origin header", h.get("Origin") == "https://www.jiomart.com")
    check("pincode header", h.get("pincode") == "753003")
    check("x-location-detail carries the pincode",
          '"pincode": "753003"' in h.get("x-location-detail", ""))
    check("visitor-id header", h.get("visitor-id") == "vid123")
    check("no auth header without token", "Authorization" not in h)
    h2 = jiomart_search._build_headers("753003", token="Bearer abc")
    check("Bearer prefix not doubled", h2["Authorization"] == "Bearer abc")

    # --- price edge cases (plain number vs {min,max}) ----------------------
    check("plain-number price", jiomart_search._num(29) == 29.0)
    check("min-max price", jiomart_search._num({"min": 74, "max": 79}) == 74.0)
    check("None price -> 0.0", jiomart_search._num(None) == 0.0)

    print()
    print(f"{total - failures}/{total} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

