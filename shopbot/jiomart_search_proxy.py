"""JioMart search proxy for MUNNA MOBILE & Grocery (PC, port 8085).

The admin webapp on the phone cannot call JioMart directly (browser CORS +
JioMart bot-blocking). This tiny stdlib-only HTTP server exposes the SAME
proven engine the Telegram bot uses (``jiomart_search.fetch_items``) over the
local network so the phone's Catalog -> Smart Search can show REAL JioMart
products:

    phone browser --GET http://PC-IP:8085/?q=oil&pincode=752106--> this proxy
        --jiomart_search.fetch_items--> jiomart.com --JSON--> phone

Run:  python jiomart_search_proxy.py        (started by run_bot.cmd too)
Open: http://localhost:8085/?q=oil          (this PC)
      http://192.168.1.14:8085/?q=oil       (phone on same Wi-Fi - see .env LAN IP)

Endpoints (CORS: Access-Control-Allow-Origin: * so github.io may call):
    GET /?q=<text>&pincode=<pin>   -> {"status":"success","items":[...]}
    GET /search?q=<text>           -> same shape
    GET /health                    -> {"ok":true,...}

Stdlib only - no new requirements.txt entries. If the port is already taken
(another instance is running) the script exits 0 instead of crashing, so it is
safe to start from run_bot.cmd on every bot launch.
"""

from __future__ import annotations

import json
import socket
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Tuple

import jiomart_search

HOST = "0.0.0.0"   # reachable from the phone over Wi-Fi, not just localhost
PORT = 8085
CACHE_TTL = 90.0     # repeat searches answer instantly, less JioMart load
CACHE_MAX = 200

_cache: Dict[Tuple[str, str], Tuple[float, List[Dict[str, Any]]]] = {}


def _pack_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Slim fetch_items dicts to the webapp grid shape (same as search_bus)."""
    slim: List[Dict[str, Any]] = []
    for it in (items or [])[:12]:
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
    return slim


def _search(query: str, pincode: str) -> List[Dict[str, Any]]:
    """fetch_items with a tiny TTL cache + pincode fallback like the bot."""
    key = (query.lower(), pincode)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]

    pins = [pincode] if pincode else []
    for p in jiomart_search.FALLBACK_PINCODES:  # retry others if first is dry
        if p not in pins:
            pins.append(p)
    items: List[Dict[str, Any]] = []
    for pin in pins:
        try:
            items = jiomart_search.fetch_items(query, pin) or []
        except Exception:  # noqa: BLE001 - proxy never raises
            items = []
        if items:
            break

    slim = _pack_items(items)
    if len(_cache) >= CACHE_MAX:
        _cache.clear()
    _cache[key] = (time.time(), slim)
    return slim



class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # CORS: the webapp lives on github.io / localhost - always allowed.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:  # noqa: BLE001 - client hung up mid-write
            pass

    def do_OPTIONS(self) -> None:  # noqa: N802 - preflight from the browser
        self._send_json({"ok": True}, 204)

    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            query = (qs.get("q") or qs.get("query") or [""])[0].strip()
            pin = (qs.get("pincode") or qs.get("pin") or [""])[0].strip()

            if parsed.path in ("/health", "/ping"):
                self._send_json(
                    {"ok": True, "service": "jiomart-search-proxy",
                     "engine": "jiomart_search.fetch_items", "cached": len(_cache)}
                )
                return
            if not query:  # bare "/" - helpful hint, not an error
                self._send_json(
                    {"ok": True, "hint": "Use /?q=<search text>&pincode=<pin>",
                     "example": "/?q=oil&pincode=752106"}
                )
                return

            items = _search(query, pin)
            print(f"[proxy] q={query!r} pin={pin!r} -> {len(items)} items",
                  flush=True)
            self._send_json({"status": "success", "query": query,
                             "count": len(items), "items": items})
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001 - always answer the browser
            print(f"[proxy] ERROR {type(exc).__name__}: {exc}", flush=True)
            try:
                self._send_json({"status": "error", "error": str(exc),
                                 "items": []}, 500)
            except Exception:  # noqa: BLE001 - connection already gone
                pass

    def log_message(self, fmt: str, *args: Any) -> None:
        # Compact one-line access log into proxy_run.log (no default noise).
        try:
            print(f"[proxy] {self.address_string()} {fmt % args}", flush=True)
        except Exception:  # noqa: BLE001
            pass


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> int:
    if _port_in_use(PORT):
        print(f"[proxy] port {PORT} already in use - assuming proxy is "
              "running; exiting.", flush=True)
        return 0
    server = ThreadingHTTPServer((HOST, PORT), _Handler)
    print(f"[proxy] JioMart search proxy on http://{HOST}:{PORT}/ "
          f"(phone: http://<PC-IP>:{PORT}/?q=oil)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("[proxy] stopped", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
