"""JioMart product search fetcher for MUNNA MOBILE & Grocery.

Replaces the broken Google Custom Search image lookup. The endpoint, headers
and response parsing below are extracted verbatim from the working project
``jm1.8.0_3.py`` (see ``search_jiomart_catalog`` / ``extract_product_meta``
there) and were verified live against the API.

Endpoint:
    GET https://www.jiomart.com/api/service/application/catalog/v1.0/products
        ?q={query}&page_size=5&page_no=1

Key header notes (LIVE-VERIFIED in jm1.8.0_3.py):
  * the API reads the pincode from the ``x-location-detail`` JSON header
    (NOT from ``pincode``/``x-pincode`` alone), so per-pincode stock is
    checked simply by rebuilding that header.
  * a ``visitor-id`` (uuid4) must be sent; ``Authorization`` is optional.

Item shape (relevant fields):
    name                     product title
    price.effective.{min,max} selling price (may be a plain number)
    price.marked.{min,max}    MRP
    medias[].url              product images - medias[0].url is the MAIN image
    sellable                  bool - in-stock for the requested pincode
    instock_variants.sizes    sizes/variants currently available
    slug / uid / sku_code     product identifiers

``search_product`` walks the fallback pincode list and returns as soon as a
pincode reports the product "in stock" (sellable=True). When every pincode is
out of stock, the last result is returned with ``in_stock=False``. Every
failure (network, HTTP error, malformed JSON) degrades to ``{}`` - the
function never raises, matching the shopbot convention.

Stdlib only (urllib) so no new dependencies are added to requirements.txt.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Exact fallback order requested for this bot.
FALLBACK_PINCODES: List[str] = ["753003", "753001", "751010"]

SEARCH_URL = (
    "https://www.jiomart.com/api/service/application/catalog/v1.0/products"
)
PAGE_SIZE = 5           # jm1 uses page_size=5, page_no=1
DEFAULT_TIMEOUT = 10.0  # seconds per HTTP request
USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/115.0.0.0 Mobile Safari/537.36"
)

# Keep names in image_search's vocabulary for admin_handler compatibility.
NUM_PER_PAGE = 5
MAX_START_INDEX = 96


def _build_headers(pincode: str, visitor_id: str = "",
                   token: str = "") -> Dict[str, str]:
    """Rebuild the jm1.8.0_3.py catalog-search headers for one pincode."""
    loc_detail = json.dumps(
        {"country": "INDIA", "country_iso_code": "IN", "pincode": str(pincode)}
    )
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.jiomart.com",
        "Referer": "https://www.jiomart.com/",
        "pincode": str(pincode),
        "x-pincode": str(pincode),
        "x-location-detail": loc_detail,
        "visitor-id": visitor_id or str(uuid.uuid4()),
    }
    if token:
        headers["Authorization"] = (
            "Bearer " + str(token).replace("Bearer ", "").strip()
        )
    return headers


def _num(val: Any) -> float:
    """price.effective/marked can be {'min': x, 'max': y} or a plain number."""
    if isinstance(val, dict):
        val = val.get("min", val.get("max", 0))
    try:
        return float(val or 0)
    except (TypeError, ValueError):
        return 0.0


def _brand(brand: Any) -> str:
    if isinstance(brand, dict):
        return str(brand.get("name") or "").strip().title()
    return str(brand or "").strip().title()


def _pack(item: Dict[str, Any]) -> str:
    """Pack size string, e.g. '1 kg' (net-quantity-value + unit)."""
    val = item.get("net-quantity-value")
    unit = item.get("net-quantity-unit") or ""
    if not val:
        return ""
    s = str(val).strip()
    if unit and str(unit).lower() not in s.lower():
        s = f"{s} {unit}".strip()
    return s


def _extract_images(item: Dict[str, Any]) -> List[str]:
    """All http(s) image URLs of an item; medias[0] first (main image)."""
    urls: List[str] = []
    medias = item.get("medias") or []
    if not isinstance(medias, list):
        return urls
    for media in medias:
        if not isinstance(media, dict):
            continue
        if str(media.get("type") or "image").lower() not in ("image", ""):
            continue
        url = str(media.get("url") or "").strip()
        if url.startswith(("http://", "https://")) and url not in urls:
            urls.append(url)
    return urls


# Keyword -> category used only when the API sends no hierarchy/categories.
# Ordered; first match wins. Grocery-oriented defaults the admin can edit.
_CATEGORY_KEYWORDS: List[tuple] = [
    (("milk", "curd", "yogurt", "dahi", "paneer", "cheese", "butter", "ghee",
      "cream", "dairy", "lassi", "buttermilk"), "Dairy"),
    (("bread", "bun", "pav", "bakery", "cake", "rusk", "biscuit", "cookies"),
     "Bakery"),
    (("oil", "mustard", "sunflower", "olive", "refined", "groundnut",
      "coconut oil"), "Oils & Ghee"),
    (("rice", "atta", "flour", "wheat", "dal", "lentil", "pulses", "besan",
      "maida", "suji", "rava", "poha", "grain", "cereal", "oats"), "Staples"),
    (("masala", "spice", "turmeric", "chilli", "mirchi", "haldi", "jeera",
      "cumin", "coriander"), "Spices"),
    (("tea", "coffee", "juice", "cold drink", "soda", "water", "beverage",
      "drink", "shake"), "Beverages"),
    (("chips", "namkeen", "snack", "noodles", "pasta", "macaroni", "popcorn",
      "wafers", "mixture"), "Snacks"),
    (("chocolate", "sweet", "mithai", "toffee", "candy", "halwa", "jam",
      "honey", "sauce", "ketchup", "spread"), "Sweets & Spreads"),
    (("soap", "shampoo", "detergent", "cleaner", "phenyl", "surf", "vim",
      "dishwash", "bleach", "harpic"), "Household"),
    (("face", "body", "lotion", "shave", "razor", "toothpaste", "toothbrush",
      "deodorant", "perfume", "hair", "beauty", "cosmetic", "personal"),
     "Personal Care"),
    (("baby", "diaper", "nappy", "wipes", "formula", "cerelac"), "Baby Care"),
    (("egg", "meat", "chicken", "mutton", "fish", "prawn", "frozen",
      "sausage"), "Meat & Frozen"),
]


def _category(item: Dict[str, Any]) -> str:
    """Best product category label from the JioMart response.

    Preference order: explicit hierarchy l3 -> l2 -> l1 -> department -> first
    entry of ``categories[]``. When the API omits those (common for many
    items), fall back to a keyword guess on the product name + brand so the
    Admin Panel still gets a sensible default bucket. ``"General"`` last.
    """
    hier = item.get("hierarchy")
    if isinstance(hier, dict):
        for key in ("l3_category", "l2_category", "l1_category", "department"):
            node = hier.get(key)
            if isinstance(node, dict):
                name = str(node.get("name") or "").strip()
                if name:
                    return name
    cats = item.get("categories")
    if isinstance(cats, list):
        for cat in cats:
            if isinstance(cat, dict):
                name = str(cat.get("name") or "").strip()
                if name:
                    return name
    # no hierarchy from the API -> guess from the product name + brand
    name = str(item.get("name") or "")
    hay = " ".join(
        p for p in (name.lower(), _brand(item.get("brand")).lower()) if p
    )
    for keywords, label in _CATEGORY_KEYWORDS:
        if any(kw in hay for kw in keywords):
            return label
    return "General"


def _category_path(item: Dict[str, Any]) -> str:
    """Full breadcrumb, e.g. 'Groceries > Cooking Essentials > Edible Oils'."""
    hier = item.get("hierarchy")
    parts: List[str] = []
    if isinstance(hier, dict):
        for key in ("department", "l1_category", "l2_category", "l3_category"):
            node = hier.get(key)
            if isinstance(node, dict):
                name = str(node.get("name") or "").strip()
                if name and name not in parts:
                    parts.append(name)
    return " > ".join(parts)


def _normalize_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Turn one raw JioMart item into the bot's product dict."""
    price_obj = item.get("price") or {}
    attrs = item.get("attributes") or {}
    if not isinstance(attrs, dict):
        attrs = {}
    sizes = [
        str(x) for x in (item.get("sizes") or [])
        if x and str(x).strip()
    ]
    iv = item.get("instock_variants") or {}
    in_stock_sizes = []
    if isinstance(iv, dict):
        in_stock_sizes = [str(x) for x in (iv.get("sizes") or []) if x]
    images = _extract_images(item)
    slug = str(item.get("slug") or "")
    sellable = bool(item.get("sellable"))
    return {
        "uid": item.get("uid") or item.get("id"),
        "slug": slug,
        "name": str(item.get("name") or "Product"),
        "price": _num(price_obj.get("effective")),
        "mrp": _num(price_obj.get("marked")),
        "image": images[0] if images else "",
        "images": images,
        "brand": _brand(item.get("brand")),
        "pack": _pack(item),
        "size": sizes[0] if sizes else str(attrs.get("size") or "OS"),
        "size_variants": sizes or None,
        "in_stock_sizes": in_stock_sizes,
        "sellable": sellable,
        "in_stock": sellable,
        "seller_id": int(item.get("seller_id") or 1),
        "sku_code": str(item.get("sku_code") or item.get("item_code") or ""),
        "vertical": str(attrs.get("vertical-code") or "GROCERIES"),
        "category": _category(item),
        "category_path": _category_path(item),
        "link": f"https://www.jiomart.com/product/{slug}" if slug else "",
    }


def fetch_items(query: Any, pincode: str,
                page_size: int = PAGE_SIZE) -> List[Dict[str, Any]]:
    """Raw search against ONE pincode. Returns [] on any failure."""
    text = str(query or "").strip()
    if not text:
        return []
    if not pincode:
        pincode = FALLBACK_PINCODES[0]

    qs = urllib.parse.urlencode(
        {"q": text, "page_size": int(page_size) or PAGE_SIZE, "page_no": 1}
    )
    url = f"{SEARCH_URL}?{qs}"
    headers = _build_headers(pincode)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        logger.warning("JioMart search HTTP %s for %r", exc.code, text)
        return []
    except Exception as exc:  # noqa: BLE001 - degrade to "no results"
        logger.warning("JioMart search failed for %r: %s", text, exc)
        return []

    if not isinstance(data, dict):
        return []
    body = data.get("data") if isinstance(data.get("data"), dict) else {}
    items = data.get("items") or body.get("items") or []
    return [i for i in items if isinstance(i, dict)]



def search_product(
    query: Any,
    pincodes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Search JioMart, looping fallback pincodes until one has stock.

    Behaviour (as requested):
      * query JioMart's catalog API for ``query``;
      * walk ``pincodes`` in order (default: 753003 -> 753001 -> 751010);
      * extract name, price and the MAIN image URL from the JSON;
      * the first pincode whose top result is "in stock" (sellable=True)
        wins immediately - the loop stops and returns that result;
      * if every pincode is out of stock, the last product found is
        returned with ``in_stock=False`` so callers can still show details;
      * no results anywhere -> ``{}``.

    The returned dict always carries: ``name``, ``price``, ``mrp``,
    ``image`` (main URL), ``images``, ``slug``, ``link``, ``pincode``,
    ``in_stock``. Never raises.
    """
    pins = [str(p).strip() for p in (pincodes or FALLBACK_PINCODES) if str(p).strip()]
    if not pins:
        pins = list(FALLBACK_PINCODES)

    text = str(query or "").strip()
    if not text:
        logger.debug("search_product called with an empty query")
        return {}

    last: Dict[str, Any] = {}
    for pincode in pins:
        items = fetch_items(text, pincode)
        if not items:
            logger.info("No JioMart results for %r in PIN %s", text, pincode)
            continue
        product = _normalize_item(items[0])
        product["pincode"] = pincode
        last = product
        if product["in_stock"]:
            logger.info(
                "In stock for %r at PIN %s (%s)",
                text, pincode, product["name"],
            )
            return product
        logger.info(
            "Out of stock for %r at PIN %s - trying next pincode",
            text, pincode,
        )

    if last:
        logger.warning(
            "All pincodes out of stock for %r - returning last result",
            text,
        )
        last["in_stock"] = False
        last["sellable"] = False
    return last


def collect_images(query: Any, start_index: int = 1,
                   per_page: int = NUM_PER_PAGE) -> List[str]:
    """Up to ``per_page`` image URLs for ``query`` (1-based ``start_index``).

    Flattens medias[].url across the matched products of the first in-stock
    pincode (falling back to whichever pincode answered), de-duplicated and
    http(s)-only - same contract the admin image picker relied on before.
    Returns [] on ANY failure.
    """
    text = str(query or "").strip()
    if not text:
        return []
    try:
        start = int(start_index)
    except (TypeError, ValueError):
        start = 1
    if start < 1:
        start = 1
    if start > MAX_START_INDEX:
        return []


    # Collect every image the search can give across the fallback pincodes,
    # stopping as soon as one pincode yields enough (in stock preferred).
    all_urls: List[str] = []
    for pincode in FALLBACK_PINCODES:
        items = fetch_items(text, pincode)
        in_stock_first = bool(items) and bool(items[0].get("sellable"))
        for item in items:
            for url in _extract_images(item):
                if url not in all_urls:
                    all_urls.append(url)
        if in_stock_first or len(all_urls) >= start + per_page - 1:
            break

    page = all_urls[start - 1: start - 1 + per_page]
    if not page:
        logger.info("No JioMart images for %r at start=%s", text, start)
    return page


def search_configured() -> bool:
    """Always True - JioMart's catalog API needs no API key."""
    return True


def reset_service() -> None:
    """No-op kept for API compatibility with the old image_search module."""


__all__ = [
    "FALLBACK_PINCODES",
    "MAX_START_INDEX",
    "NUM_PER_PAGE",
    "PAGE_SIZE",
    "SEARCH_URL",
    "collect_images",
    "fetch_items",
    "reset_service",
    "search_configured",
    "search_product",
]

