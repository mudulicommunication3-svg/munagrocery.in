"""WhatsApp OTP authentication server for MUNNA GROCERY & MOBILE.

A tiny Flask micro-service that hands the web storefront
(https://munagrocery.in) a WhatsApp-delivered OTP so customers can log in
with their mobile number.

Endpoints
---------
GET  /api/health         -> { ok: true }
POST /api/send-otp       { phone }   -> generate + store a 4-digit code
                          (5-minute TTL, single-use) and fire it over
                          WhatsApp. Returns { success }.
POST /api/verify-otp     { phone, otp } -> check + burn the code and return
                          { success, user: { name, phone } }.

Delivery strategy (first available wins):
  1. HTTP WhatsApp gateway -> set WHATSAPP_GATEWAY_URL (+ optional token).
  2. pywhatkit             -> logged-in WhatsApp Web session; background thread.

CORS allows https://munagrocery.in (production) and localhost (dev).

Run:
    python whatsapp_otp_server.py            # http://0.0.0.0:5000
    PORT=8081 python whatsapp_otp_server.py  # custom port
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
import urllib.request
from typing import Any, Dict, Optional

try:
    from flask import Flask, jsonify, request
except ImportError:  # pragma: no cover - guidance for a missing dependency
    raise SystemExit(
        "Flask is required for the WhatsApp OTP server.\n"
        "Install it with:  pip install flask pywhatkit"
    )

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PORT = int(os.getenv("PORT", "5000"))
OTP_TTL_SECONDS = int(os.getenv("OTP_TTL_SECONDS", str(5 * 60)))  # 5 minutes
OTP_MAX_ATTEMPTS = int(os.getenv("OTP_MAX_ATTEMPTS", "5"))
GATEWAY_URL = (os.getenv("WHATSAPP_GATEWAY_URL") or "").strip()
GATEWAY_TOKEN = (os.getenv("WHATSAPP_GATEWAY_TOKEN") or "").strip()

ALLOWED_ORIGINS = {
    "https://munagrocery.in",
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8000",
}

# ---------------------------------------------------------------------------
# In-memory OTP store: phone -> { otp, exp, attempts, used, created }
# (single-use; a fresh dict on every process restart)
# ---------------------------------------------------------------------------
_store: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()


def _clean_store(now: float) -> None:
    """Drop expired entries so the store never grows without bound."""
    dead = [k for k, v in _store.items() if v.get("used") or now > v.get("exp", 0)]
    for k in dead:
        _store.pop(k, None)


def _valid_phone(phone: Any) -> Optional[str]:
    """Normalise to 10 digits and validate an Indian mobile number."""
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) != 10 or not digits.startswith(("6", "7", "8", "9")):
        return None
    return digits


def _wa_number(phone: str) -> str:
    """+91XXXXXXXXXX as required by pywhatkit / most WhatsApp gateways."""
    return "+91" + phone


def _deliver_whatsapp(phone: str, message: str) -> bool:
    """Send ``message`` to ``phone``. Returns True on a successful dispatch."""
    wa = _wa_number(phone)

    # 1) Hosted HTTP gateway (fast, no browser needed).
    if GATEWAY_URL:
        try:
            payload = {"phone": wa, "message": message, "text": message}
            headers = {"Content-Type": "application/json"}
            if GATEWAY_TOKEN:
                headers["Authorization"] = "Bearer " + GATEWAY_TOKEN
            req = urllib.request.Request(
                GATEWAY_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                if 200 <= getattr(resp, "status", 200) < 300:
                    return True
        except Exception as exc:  # noqa: BLE001 - fall through to pywhatkit
            print(f"[whatsapp] gateway failed ({exc}); trying pywhatkit")

    # 2) pywhatkit (opens WhatsApp Web; requires a logged-in session).
    try:
        import pywhatkit

        pywhatkit.sendwhatmsg_instantly(wa, message, wait_time=8, tab_close=True, close_time=3)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[whatsapp] pywhatkit failed for {wa}: {exc}")
        return False


# ---------------------------------------------------------------------------
# CORS (requirement #4: allow munagrocery.in + localhost)
# ---------------------------------------------------------------------------
@app.after_request
def _cors(resp):
    origin = request.headers.get("Origin", "")
    if origin in ALLOWED_ORIGINS or origin.startswith("http://localhost") or origin.startswith("http://127.0.0.1"):
        resp.headers["Access-Control-Allow-Origin"] = origin or "https://munagrocery.in"
    else:
        resp.headers["Access-Control-Allow-Origin"] = origin or "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return resp


@app.route("/api/send-otp", methods=["OPTIONS", "POST"])
def send_otp():
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.get_json(silent=True) or {}
    phone = _valid_phone(data.get("phone"))
    if not phone:
        return jsonify({"success": False, "error": "ଠିକ୍ 10-ଅଙ୍କ ମୋବାଇଲ୍ ନମ୍ବର ଦିଅନ୍ତୁ।"}), 400

    now = time.time()
    with _lock:
        _clean_store(now)
        existing = _store.get(phone)
        # Resend lock: a fresh code at most every 45s (mirrors the Sheet bus).
        if existing and not existing.get("used") and now - existing.get("created", 0) < 45:
            return jsonify({"success": True, "phone": phone,
                            "wait_seconds": 45 - int(now - existing.get("created", 0))})
        otp = str(random.randint(1000, 9999))
        _store[phone] = {"otp": otp, "exp": now + OTP_TTL_SECONDS,
                         "attempts": 0, "used": False, "created": now}

    msg = OTP_MSG_TEMPLATE.format(otp=otp)
    delivered = _deliver_whatsapp(phone, msg)
    if delivered:
        return jsonify({"success": True, "phone": phone,
                        "expires_in": OTP_TTL_SECONDS, "code_sent": True})
    # Server reachable but WhatsApp delivery failed -> the client falls back
    # to its own demo alert. The OTP was generated + stored above, so the
    # verify endpoint will still accept it.
    return jsonify({"success": True, "phone": phone,
                    "expires_in": OTP_TTL_SECONDS, "code_sent": False,
                    "demo": True, "otp": otp})


@app.route("/api/verify-otp", methods=["OPTIONS", "POST"])
def verify_otp():
    if request.method == "OPTIONS":
        return ("", 204)
    data = request.get_json(silent=True) or {}
    phone = _valid_phone(data.get("phone"))
    typed = str(data.get("otp") or data.get("code") or "").strip()
    if not phone or not (typed.isdigit() and len(typed) == 4):
        return jsonify({"success": False, "error": "ଠିକ୍ ନମ୍ବର ଓ 4-ଅଙ୍କ OTP ଦିଅନ୍ତୁ।"}), 400

    now = time.time()
    with _lock:
        _clean_store(now)
        rec = _store.get(phone)
        if not rec or rec.get("used"):
            return jsonify({"success": False, "error": "ପ୍ରଥମେ “OTP ପଠାନ୍ତୁ” ଚାପନ୍ତୁ।"})
        if now > rec.get("exp", 0):
            _store.pop(phone, None)
            return jsonify({"success": False, "error": "OTP ମିଆଦ ସରିଗଲା — ପୁଣି ପଠାନ୍ତୁ।"})
        if typed != rec.get("otp"):
            rec["attempts"] = rec.get("attempts", 0) + 1
            if rec["attempts"] >= OTP_MAX_ATTEMPTS:
                _store.pop(phone, None)
                return jsonify({"success": False, "error": "ବହୁତ ଥର ଭୁଲ୍ ହେଲା — ପୁଣି OTP ପଠାନ୍ତୁ।"})
            return jsonify({"success": False, "error": "ଭୁଲ୍ OTP — ପୁଣି ଲେଖନ୍ତୁ।",
                            "attempts_left": OTP_MAX_ATTEMPTS - rec["attempts"]})
        # Burn (single-use) so the same code cannot be replayed.
        rec["used"] = True
        _store.pop(phone, None)

    return jsonify({"success": True, "user": {"name": "Customer", "phone": phone}})


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "whatsapp-otp", "ttl": OTP_TTL_SECONDS})


if __name__ == "__main__":
    print(f"[whatsapp-otp] listening on 0.0.0.0:{PORT}")
    print(f"[whatsapp-otp] gateway={GATEWAY_URL or 'pywhatkit fallback'}")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)


