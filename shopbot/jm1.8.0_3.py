import asyncio
import datetime
import html
import json
import logging
import os
import re
import time
import uuid
from dotenv import dotenv_values
from urllib.parse import unquote
import aiohttp
from dotenv import dotenv_values
from playwright.async_api import async_playwright
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ==================== VERSION & LOGGING SETUP ====================
BOT_VERSION = "jm2.3.0"
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ==================== CONFIGURATION ====================
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(CURRENT_DIR, ".env")
env_config = dotenv_values(ENV_PATH)
TOKEN = env_config.get("BOT_TOKEN")


BASE_DIR = (
    os.path.join("D:\\", "JioMartBot")
    if os.name == "nt" and os.path.exists("D:\\JioMartBot")
    else CURRENT_DIR
)
CONFIG_DB = os.path.join(BASE_DIR, "bot_database.json")
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

FIXED_VIEWPORT = {"width": 360, "height": 640}
MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/115.0.0.0 Mobile Safari/537.36"
)

# ==================== IN-MEMORY (RAM) STORAGE ====================
RAM_CACHE = {}             # {chat_id: {token, visitor_id, cart_id, address_id, phone, store_id, cart_price, cookies, ...}}
USER_ACTIVE_SESSIONS = {}  # {chat_id: [session_1, session_2, ...]}
PENDING_PAYMENTS = {}      # {chat_id: {idx: {order_id, trx_id, amount, session_data, ...}}}
SELECTED_PAYMENTS = {}     # {chat_id: set(selected_indices)}
USER_ORDER_COUNTS = {}     # {chat_id: count}
USER_BROWSERS = {}         # {chat_id: {"browser", "ctx", "page", ...}}
USER_SEARCH_STATE = {}     # {chat_id: True=session-search | "prod_search"=product-search} - waiting for keyword
FORCE_STOP_FLAG = {}       # {chat_id: True} - set by 🛑 Force Stop; long loops abort
RUNNING_TASKS = {}         # {chat_id: set(asyncio.Task)} - cancellable by Force Stop
ONLINE_CART_VIEW = {}      # {chat_id: {"items": [...], "cart_id": str, "mid": int}} - live online cart view

PRESET_COUNTS = [1, 5, 10, 20, 30, 50]

# ==================== HELPERS ====================
def find_pgloader_id(node, exclude_ids=(), order_hex_prefix=""):
    """Recursively locate the JioOnePay gateway session id (24-hex) in a checkout payload.

    The genuine gateway id shares its leading hex with the order id
    (JM6A8DCC49... -> 6a8dcc4944142ab8db01bf14), so prefix-matched candidates win.
    """
    exclude = {str(x) for x in exclude_ids if x}
    prefix = (order_hex_prefix or "").lower()

    def _walk(n):
        if isinstance(n, dict):
            for k, v in n.items():
                if isinstance(v, str) and re.fullmatch(r"[0-9a-f]{24}", v) \
                        and v not in exclude \
                        and any(t in k.lower() for t in ("pay", "txn", "trans", "jop", "gateway", "loader")):
                    return v
            for v in n.values():
                found = _walk(v)
                if found:
                    return found
        elif isinstance(n, list):
            for sub in n:
                found = _walk(sub)
                if found:
                    return found
        elif isinstance(n, str) and re.fullmatch(r"[0-9a-f]{24}", n) and n not in exclude:
            return n
        return ""

    def _walk_prefixed(n):
        if isinstance(n, dict):
            for v in n.values():
                found = _walk_prefixed(v)
                if found:
                    return found
        elif isinstance(n, list):
            for sub in n:
                found = _walk_prefixed(sub)
                if found:
                    return found
        elif isinstance(n, str) and re.fullmatch(r"[0-9a-f]{24}", n) \
                and n not in exclude and n.startswith(prefix):
            return n
        return ""

    return _walk_prefixed(node) or _walk(node)

# ==================== DATABASE HELPERS ====================
def load_configs():
    if os.path.exists(CONFIG_DB):
        try:
            with open(CONFIG_DB, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_configs(data):
    try:
        with open(CONFIG_DB, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"save_configs error: {e}")
        return False

def get_all_session_keys():
    if not os.path.exists(SESSIONS_DIR):
        return []
    return sorted([f for f in os.listdir(SESSIONS_DIR) if f.endswith(".json")])

def get_active_account(chat_id: str):
    configs = load_configs()
    user_conf = configs.get(str(chat_id), {})
    active_key = user_conf.get("active_key", "")
    all_keys = get_all_session_keys()

    if active_key:
        local_path = os.path.join(SESSIONS_DIR, active_key)
        if os.path.exists(local_path):
            return active_key, local_path

    if all_keys:
        chosen = all_keys[0]
        user_conf["active_key"] = chosen
        save_configs(configs)
        return chosen, os.path.join(SESSIONS_DIR, chosen)

    return None, None

def set_active_session_key(chat_id: str, key_name: str):
    configs = load_configs()
    if str(chat_id) not in configs:
        configs[str(chat_id)] = {}
    configs[str(chat_id)]["active_key"] = key_name
    save_configs(configs)

def get_cookies_from_state(state: dict) -> dict:
    if not isinstance(state, dict):
        return {}
    return {c["name"]: c["value"] for c in state.get("cookies", []) if isinstance(c, dict)}

# ==================== APIREADY1 LOGIN MODULE ====================
# 🔥 Date-Based Session Naming (apiready1): Jio<Mobile>T<HH.MM>D<DD.MM.YY>
def generate_date_session_key(mobile):
    from datetime import datetime as _dt
    now = _dt.now()
    time_str = now.strftime("%H.%M")
    date_str = now.strftime("%d.%m.%y")
    return f"Jio{mobile}T{time_str}D{date_str}"

LOGIN_TEMP = {}   # {chat_id: {"pw","browser","ctx","page","phone"}} - active browser-login flow

def save_session_apiready1(chat_id: str, secret_key: str, state: dict):
    """apiready1 session save format:
    - File: SESSIONS_DIR/<secret_key>.json (Playwright storage_state)
    - configs[chat_id]["saved_keys"][secret_key] = full file path
    - configs[chat_id]["active_key"] = filename (with .json so jm get_active_account works)
    Returns the saved file path."""
    filename = f"{secret_key}.json"
    filepath = os.path.join(SESSIONS_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    configs = load_configs()
    if str(chat_id) not in configs:
        configs[str(chat_id)] = {"saved_keys": {}, "active_key": ""}
    if "saved_keys" not in configs[str(chat_id)]:
        configs[str(chat_id)]["saved_keys"] = {}
    configs[str(chat_id)]["saved_keys"][secret_key] = filepath
    configs[str(chat_id)]["active_key"] = filename
    save_configs(configs)
    return filepath

def _login_preset_name(chat_id: str) -> str:
    """apiready1: preset_name from configs (default 'JEMS') for new-account setup."""
    try:
        return str(load_configs().get(str(chat_id), {}).get("preset_name", "JEMS")).strip() or "JEMS"
    except Exception:
        return "JEMS"

async def smart_location_popup_handler(page):
    """apiready1-style: bypass the JioMart location popup (multiple strategies)."""
    try:
        enable_btn = page.locator("button:has-text('Enable Location'), button:has-text('Select Location Manually'), button:has-text('Allow'), button:has-text('Allow access')").first
        if await enable_btn.is_visible():
            await enable_btn.click()
            await asyncio.sleep(2)
            return True
    except Exception:
        pass
    try:
        skip_btn = page.locator("button:has-text('Skip'), button:has-text('Not Now'), [aria-label='Close']").first
        if await skip_btn.is_visible():
            await skip_btn.click()
            await asyncio.sleep(1)
            return True
    except Exception:
        pass
    return False

async def purge_login_otp_inputs(page):
    """apiready1 purge_otp_inputs_right_to_left: clear all inputs before re-typing OTP."""
    try:
        await page.evaluate("""() => {
            const inputs = Array.from(document.querySelectorAll('input'));
            for (let i = inputs.length - 1; i >= 0; i--) {
                inputs[i].focus();
                inputs[i].value = '';
                inputs[i].dispatchEvent(new Event('input', { bubbles: true }));
            }
        }""")
        inputs_count = await page.locator("input").count()
        for idx in reversed(range(inputs_count)):
            try:
                cur = page.locator("input").nth(idx)
                await cur.click()
                await page.keyboard.press("Backspace")
                await page.keyboard.press("Delete")
            except Exception:
                pass
        await page.wait_for_timeout(300)
    except Exception as e:
        logger.info(f"login OTP purge error: {e}")

async def _login_finalize(chat_id: str, session_obj: dict, load_msg) -> bool:
    """Shared login finisher: wait for jiomart redirect -> location popup bypass
    -> storage_state -> apiready1 session save -> cleanup -> success message."""
    page = session_obj["page"]
    ctx = session_obj["ctx"]
    browser = session_obj["browser"]
    pw = session_obj["pw"]
    phone = session_obj["phone"]
    # wait for redirect back to jiomart.com (max 30s)
    for _ in range(10):
        u = page.url
        if "jiomart.com" in u and "relianceretail" not in u and "/auth/login" not in u and "retail-auth" not in u:
            break
        await asyncio.sleep(3)
    await asyncio.sleep(2)
    # location popup bypass (apiready1 execute_direct_profile_stay_sequence step)
    try:
        popup_ok = await smart_location_popup_handler(page)
        if popup_ok:
            logger.info(f"[Login] location popup bypassed for {phone}")
    except Exception:
        pass
    state = await ctx.storage_state()
    # apiready1 date-based naming + save format (saved_keys + active_key)
    secret_key = generate_date_session_key(phone)
    filepath = save_session_apiready1(chat_id, secret_key, state)
    # PRESERVED: do NOT wipe RAM — sync data survives a new login save.
    await browser.close()
    await pw.stop()
    LOGIN_TEMP.pop(chat_id, None)
    AWAITING_INPUT.pop(chat_id, None)
    await load_msg.edit_text(
        f"🎉 <b>Login Successful!</b>\n\n"
        f"🔑 Key: <code>{secret_key}</code>\n"
        f"📁 Path: <code>{filepath}</code>\n"
        f"⭐ Set as active session!\n\n"
        f"👉 Click <b>🔄 2-Step Sync</b> to capture all IDs.",
        reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")
    return True

def get_quickcommerce_cookies(chat_id: str) -> dict:
    """Load app_geolocation / app_location_details cookies that carry the
    Quick-Commerce polygon ids (TE*_QC_*, FR*_QC_*).

    These cookies pin delivery to QUICK. Prefer the active account's file,
    then fall back to scanning every saved session for one whose app_geolocation
    exposes a '_QC_' polygon id (e.g. FR63_QC_af4abb75 / TE58_QC_8fbfd1a8).
    Fresh sync browser cookies sometimes carry a default (e.g. Mumbai, empty
    polygons) which silently downgrades to standard delivery.
    """
    def _geo_of(path):
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            return None
        geo = {}
        for ck in st.get("cookies", []):
            n = ck.get("name", "")
            if n in ("app_geolocation", "app_location_details"):
                geo[n] = ck.get("value", "")
        return geo or None

    # 1) active account
    _, path = get_active_account(chat_id)
    g = _geo_of(path)
    if g:
        return g
    # 2) first saved session whose geolocation is QUICK (has _QC_ polygon)
    try:
        files = sorted(os.listdir(SESSIONS_DIR))
    except Exception:
        files = []
    for f in files:
        if not f.endswith(".json"):
            continue
        g = _geo_of(os.path.join(SESSIONS_DIR, f))
        if g and "_QC_" in str(g.get("app_geolocation", "")):
            return g
    # 3) any saved session
    for f in files:
        if not f.endswith(".json"):
            continue
        g = _geo_of(os.path.join(SESSIONS_DIR, f))
        if g:
            return g
    return {}

# ==================== QC LOCATION HELPERS (ported from od35 quick mechanism) ====================
POLY_RE = re.compile(r"[A-Z][A-Z0-9]{1,7}_QC_[0-9a-fA-F]{4,16}")

def _qc_extract_from_state_obj(state) -> dict:
    """Read the QC polygon id + fulfillment store out of a Playwright storage_state.

    Sources (same as od35):
      1. cookies app_geolocation / app_location_details -> '_QC_' polygon ids
         (e.g. FR63_QC_af4abb75 / TE58_QC_8fbfd1a8)
      2. localStorage jio_qc_stores / jio_serviceable_stores /
         nms_delivery_config_info -> authoritative browser-bound store
    Returns {"polygon_id": str, "store_id": str, "store_code": str}.
    """
    out = {"polygon_id": "", "store_id": "", "store_code": ""}
    if not isinstance(state, dict):
        return out

    # 1) QC polygon from geolocation cookies
    try:
        blob = ""
        for ck in state.get("cookies", []):
            n = (ck.get("name", "") or "") if isinstance(ck, dict) else ""
            if n in ("app_geolocation", "app_location_details"):
                blob += " " + unquote(str(ck.get("value", "")))
        m = POLY_RE.search(blob)
        if m:
            out["polygon_id"] = m.group(0)
    except Exception:
        pass
    return out


def extract_qc_identity(chat_id: str) -> dict:
    """Best-effort QC identity (polygon) for this chat from any saved session
    file: active account first, else first session carrying a _QC_ polygon."""
    def _load(path):
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return None
        return None

    _, path = get_active_account(chat_id)
    st = _load(path)
    if st:
        info = _qc_extract_from_state_obj(st)
        if info.get("polygon_id"):
            return info
    try:
        files = sorted(os.listdir(SESSIONS_DIR))
    except Exception:
        files = []
    for f in files:
        if f.endswith(".json"):
            st = _load(os.path.join(SESSIONS_DIR, f))
            if st:
                info = _qc_extract_from_state_obj(st)
                if info.get("polygon_id"):
                    return info
    return {"polygon_id": "", "store_id": "", "store_code": ""}


def build_location_headers(pincode, token="", visitor_id="", lat=None, lon=None,
                           store_id="", ch="MOB", polygon_id=None):
    """OD35-style location headers - THIS is what pins JioMart delivery to QUICK.

    The live browser sends an X-Geolocation header whose polygon_ids list carries
    the quick-commerce polygon (e.g. FR63_QC_xxx) plus an X-Location-Detail and
    glo_pincode on every bag/shipment/checkout call. Without them the server
    silently falls back to standard ('shop all') delivery even when the cart was
    created under a QC pincode.
    """
    loc_detail = {"country": "INDIA", "country_iso_code": "IN", "pincode": str(pincode)}
    gps = False
    lat_f = lon_f = None
    if lat is not None and lon is not None:
        try:
            lat_f = float(lat)
            lon_f = float(lon)
            if abs(lat_f) > 0 and abs(lon_f) > 0:
                gps = True
            else:
                lat_f = lon_f = None
        except (TypeError, ValueError):
            lat_f = lon_f = None

    poly_ids = [str(polygon_id)] if polygon_id else []
    if gps:
        geolocation = {"latitude": lat_f, "longitude": lon_f, "polygon_ids": poly_ids, "type": "GPS"}
        loc_detail["latitude"] = lat_f
        loc_detail["longitude"] = lon_f
    else:
        geolocation = {"polygon_ids": poly_ids}

    clean_token = token.replace("Bearer ", "").strip() if token else ""
    auth_header = f"Bearer {clean_token}" if clean_token else ""

    v_id = visitor_id or str(uuid.uuid4())
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.jiomart.com",
        "Referer": "https://www.jiomart.com/cart/bag",
        "User-Agent": MOBILE_USER_AGENT,
        "sec-ch-ua-platform": '"Android"',
        "pincode": str(pincode),
        "x-pincode": str(pincode),
        "glo_pincode": str(pincode),
        "x-glo-pincode": str(pincode),
        "X-Location-Detail": json.dumps(loc_detail),
        "X-Geolocation": json.dumps(geolocation),
        "x-visitor-id": v_id,
        "visitor-id": v_id,
        "visitor_id": v_id,
        "X-Visitor-Id": v_id,
        "x-channel": ch or "MOB",
        "clientId": "mweb",
    }
    INVALID_STORE_IDS = {"259630"}
    store_id = str(store_id or "")
    if store_id and store_id not in INVALID_STORE_IDS:
        h["store_id"] = store_id
    if auth_header:
        h["Authorization"] = auth_header
    if gps:
        h["latitude"] = str(lat_f)
        h["longitude"] = str(lon_f)
        h["x-latitude"] = str(lat_f)
        h["x-longitude"] = str(lon_f)
    return h


def get_user_count(chat_id: str) -> int:
    return USER_ORDER_COUNTS.get(str(chat_id), 1)

def set_user_count(chat_id: str, count: int):
    count = max(1, min(100, count))
    USER_ORDER_COUNTS[str(chat_id)] = count
    return count

# ---- 🛑 FORCE STOP helpers (apiready1 pattern) ----
def register_task(chat_id, task):
    """Track a running asyncio.Task so 🛑 Force Stop can cancel it."""
    RUNNING_TASKS.setdefault(str(chat_id), set()).add(task)
    task.add_done_callback(lambda t, c=str(chat_id): RUNNING_TASKS.get(c, set()).discard(t))

def is_force_stopped(chat_id) -> bool:
    return bool(FORCE_STOP_FLAG.get(str(chat_id)))

def clear_force_stop(chat_id):
    FORCE_STOP_FLAG.pop(str(chat_id), None)


async def send_safe_html(message_obj, text: str, reply_markup=None):
    if len(text) <= 3500:
        try:
            await message_obj.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception as e:
            # Retry without HTML parsing if Telegram rejects the markup (400 BadRequest)
            if "not modified" not in str(e).lower():
                try:
                    await message_obj.edit_text(text, reply_markup=reply_markup)
                except Exception:
                    try:
                        await message_obj.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)
                    except Exception:
                        try:
                            await message_obj.reply_text(text, reply_markup=reply_markup)
                        except Exception:
                            pass
    else:
        for i in range(0, len(text), 3500):
            chunk = text[i : i + 3500]
            sent = False
            try:
                await message_obj.reply_text(chunk, parse_mode="HTML")
                sent = True
            except Exception:
                pass
            if not sent:
                try:
                    await message_obj.reply_text(chunk)
                except Exception:
                    pass
        if reply_markup is not None:
            try:
                await message_obj.reply_text("Options:", reply_markup=reply_markup)
            except Exception:
                pass

async def safe_edit_html(message_obj, text: str, **kwargs):
    if message_obj is None:
        return
    try:
        await message_obj.edit_text(text, **kwargs)
    except Exception as e:
        if "not modified" in str(e).lower():
            return
        # Retry without HTML parsing if Telegram rejects the markup (400 BadRequest)
        if kwargs.get("parse_mode") == "HTML":
            try:
                clean_kwargs = {k: v for k, v in kwargs.items() if k != "parse_mode"}
                await message_obj.edit_text(text, **clean_kwargs)
                return
            except Exception as e2:
                if "not modified" in str(e2).lower():
                    return
        try:
            await message_obj.reply_text(text, **kwargs)
        except Exception:
            # Last resort: plain text, no parse mode, no keyboard
            try:
                plain_kwargs = {k: v for k, v in kwargs.items() if k != "parse_mode"}
                await message_obj.reply_text(text, **plain_kwargs)
            except Exception:
                pass

# ==================== ADMIN SYSTEM ====================
def get_admin_ids() -> list:
    """Admin list: .env ADMIN_ID + configs['_admins'] (added via Admin Panel)."""
    admins = []
    env_admin = str(env_config.get("ADMIN_ID") or "").strip()
    if env_admin and env_admin.isdigit() and env_admin != "0":
        admins.append(env_admin)
    try:
        cfg_admins = load_configs().get("_admins", [])
        if isinstance(cfg_admins, list):
            admins.extend(str(a) for a in cfg_admins)
    except Exception:
        pass
    seen, out = set(), []
    for a in admins:
        if a and a not in seen:
            seen.add(a)
            out.append(a)
    return out

def is_admin(chat_id) -> bool:
    return str(chat_id) in get_admin_ids()

def add_admin_user(user_id: str) -> bool:
    user_id = str(user_id).strip()
    if not user_id.isdigit():
        return False
    configs = load_configs()
    admins = configs.get("_admins", [])
    if not isinstance(admins, list):
        admins = []
    if user_id in admins or user_id in get_admin_ids():
        return False
    admins.append(user_id)
    configs["_admins"] = admins
    save_configs(configs)
    return True

def remove_admin_user(user_id: str) -> bool:
    user_id = str(user_id).strip()
    configs = load_configs()
    admins = configs.get("_admins", [])
    if not isinstance(admins, list) or user_id not in admins:
        return False
    admins.remove(user_id)
    configs["_admins"] = admins
    save_configs(configs)
    return True

def get_user_saved_keys(chat_id: str) -> dict:
    """USER-WISE keys: this user's own saved session keys {key: filepath}."""
    c = load_configs().get(str(chat_id), {})
    sk = c.get("saved_keys") or {}
    if isinstance(sk, dict) and sk:
        return dict(sk)
    # fallback: old sessions saved before saved_keys existed
    ak = str(c.get("active_key") or "")
    if ak and os.path.exists(os.path.join(SESSIONS_DIR, ak)):
        return {ak: os.path.join(SESSIONS_DIR, ak)}
    return {}

def get_all_user_keys() -> dict:
    """ALL users' keys: {owner_chat_id: {"active": key, "keys": [key, ...]}}."""
    cfg = load_configs()
    out = {}
    for cid, c in cfg.items():
        if not isinstance(c, dict) or cid.startswith("_"):
            continue
        sk = c.get("saved_keys") or {}
        keys = [k for k in sk.keys()] if isinstance(sk, dict) else []
        ak = str(c.get("active_key") or "")
        if ak and ak not in keys:
            keys.append(ak)
        if keys:
            out[cid] = {"active": ak, "keys": keys}
    return out

ADMIN_KEY_CACHE = {}   # {chat_id: [(owner, key), ...]} - index-based callbacks (64B safe)

def build_admin_panel(chat_id: str, page: int = 0):
    """Admin panel: all user session keys (admin can adopt any) + add/del admin."""
    if not is_admin(chat_id):
        return "🚫 <b>Admin access only!</b>", InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔙 Main Menu", callback_data="back_home")]])
    admins = get_admin_ids()
    user_map = get_all_user_keys()
    flat = []
    for owner in sorted(user_map.keys()):
        info = user_map[owner]
        for k in info["keys"]:
            flat.append((owner, k, k == info["active"]))
    ADMIN_KEY_CACHE[str(chat_id)] = flat
    per_page = 20
    total_pages = max(1, (len(flat) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    chunk = flat[start:start + per_page]
    lines = [f"🛡️ <b>Admin Panel</b> [{BOT_VERSION}]", "",
             f"👑 <b>Admins:</b> {len(admins)} | 👥 <b>Users w/ keys:</b> {len(user_map)} | 🔑 <b>Total keys:</b> {len(flat)}",
             "", "🔑 <b>All User Session Keys</b> (tap to USE a key):"]
    kb = []
    for i, (owner, key, is_active) in enumerate(chunk):
        idx = start + i
        oshort = owner[-4:] if len(owner) > 4 else owner
        star = " ⭐" if is_active else ""
        kb.append([InlineKeyboardButton(f"📁 ...{oshort}: {key[:28]}{star}",
                                        callback_data=f"admuse_{idx}")])
    if not chunk:
        lines.append("ℹ️ No user session keys found yet.")
    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"admks_{page-1}"))
        nav.append(InlineKeyboardButton(f"📄 {page+1}/{total_pages}", callback_data="prod_noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"admks_{page+1}"))
        kb.append(nav)
    kb.append([InlineKeyboardButton("➕ Add Admin", callback_data="adm_add"),
               InlineKeyboardButton("🗑 Delete Admin", callback_data="adm_del")])
    kb.append([InlineKeyboardButton("🔄 Refresh", callback_data="admin_panel"),
               InlineKeyboardButton("♻️ Restart Bot", callback_data="adm_restart")])
    kb.append([InlineKeyboardButton("🔙 Main Menu", callback_data="back_home")])
    return "\n".join(lines), InlineKeyboardMarkup(kb)

def build_my_keys_view(chat_id: str):
    """USER-WISE view: every user sees HIS OWN login keys (active starred)."""
    my = get_user_saved_keys(chat_id)
    active_key, _ = get_active_account(chat_id)
    lines = [f"👤 <b>My Session Keys</b> — {len(my)} key(s)", ""]
    kb = []
    for k in sorted(my.keys()):
        star = " ⭐ ACTIVE" if k == active_key else ""
        lines.append(f"🔑 <code>{html.escape(k)}</code>{star}")
        if k != active_key:
            kb.append([InlineKeyboardButton(f"⭐ Use: {k[:36]}", callback_data=f"mykey_{k}")])
    if not my:
        lines.append("ℹ️ No keys yet. Use ➕ New Login or 2-Step Sync first.")
    kb.append([InlineKeyboardButton("➕ New Login", callback_data="sess_new_login")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="open_session_manager")])
    return "\n".join(lines), InlineKeyboardMarkup(kb)

# ==================== VIEW PROFILE (BROWSER) ====================
async def open_view_profile(chat_id: str, message=None):
    if chat_id in USER_BROWSERS:
        if message:
            await safe_edit_html(message, "⚠️ Browser is already active! Close it first.", parse_mode="HTML")
        return False

    active_key, state_path = get_active_account(chat_id)
    if not active_key or not state_path:
        if message:
            await safe_edit_html(message, "❌ No saved session found! Add a session first.", parse_mode="HTML")
        return False

    try:
        if message:
            await safe_edit_html(message, f"👤 <b>Opening Profile in Browser ({active_key})...</b>", parse_mode="HTML")

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )

        ctx_kwargs = {
            "viewport": FIXED_VIEWPORT,
            "is_mobile": True,
            "has_touch": True,
            "user_agent": MOBILE_USER_AGENT,
        }
        if os.path.exists(state_path):
            ctx_kwargs["storage_state"] = state_path

        ctx = await browser.new_context(**ctx_kwargs)
        page = await ctx.new_page()

        USER_BROWSERS[chat_id] = {
            "pw": pw,
            "browser": browser,
            "ctx": ctx,
            "page": page,
            "active_key": active_key,
            "state_path": state_path,
        }

        await page.goto("https://www.jiomart.com/profile", wait_until="domcontentloaded", timeout=60000)

        kb = [
            [InlineKeyboardButton("💾 Save Session & Close", callback_data="action_save_close_browser")],
            [InlineKeyboardButton("❌ Close Browser (No Save)", callback_data="action_close_browser")],
        ]
        if message:
            await safe_edit_html(
                message,
                f"👤 <b>Profile Opened in Browser [{BOT_VERSION}]</b>\n\n"
                f"📁 <b>Session:</b> <code>{active_key}</code>\n\n"
                f"Make changes and choose an option:",
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="HTML",
            )
        return True
    except Exception as e:
        logger.error(f"View Profile Error: {e}")
        if chat_id in USER_BROWSERS:
            try: await USER_BROWSERS[chat_id]["browser"].close()
            except: pass
            try: await USER_BROWSERS[chat_id]["pw"].stop()
            except: pass
            del USER_BROWSERS[chat_id]
        return False

async def handle_save_close_browser(chat_id: str, message=None):
    if chat_id not in USER_BROWSERS:
        return False
    bd = USER_BROWSERS[chat_id]
    active_key = bd.get("active_key")
    state_path = bd.get("state_path")
    try:
        state = await bd["ctx"].storage_state()
        if state_path:
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        if message:
            await safe_edit_html(
                message,
                f"✅ <b>Session Saved & Browser Closed!</b>\n\n📁 Session: <code>{active_key}</code>",
                reply_markup=get_main_keyboard(chat_id),
                parse_mode="HTML",
            )
    except Exception as e:
        logger.error(f"Save & Close Error: {e}")
    try: await bd["browser"].close()
    except: pass
    try: await bd["pw"].stop()
    except: pass
    del USER_BROWSERS[chat_id]
    return True

async def handle_close_browser(chat_id: str, message=None):
    if chat_id not in USER_BROWSERS:
        return False
    bd = USER_BROWSERS[chat_id]
    try: await bd["browser"].close()
    except: pass
    try: await bd["pw"].stop()
    except: pass
    del USER_BROWSERS[chat_id]
    if message:
        await safe_edit_html(message, "❌ Browser closed without saving.", reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")
    return True

# ==================== 2-STEP BROWSER SYNC (10s + 5s) ====================
async def run_2step_browser_sync(chat_id: str, message=None):
    active_key, state_path = get_active_account(chat_id)
    if not active_key or not state_path or not os.path.exists(state_path):
        if message:
            await safe_edit_html(message, "❌ No saved session found! Add/Select a session first.", parse_mode="HTML")
        return False

    RAM_CACHE.pop(str(chat_id), None)
    clear_force_stop(chat_id)
    captured = {
        "token": "",
        "visitor_id": "",
        "phone": "",
        "store_id": "",
        "store_code": "",
        "address_id": "",
        "cart_id": "",
        "cart_price": "0.00",
        "pincode": "754004",
        "polygon_id": "",
        "cookies": {},
        "active_key": active_key,
    }

    m_ph = re.search(r'(\d{10})', active_key)
    if m_ph:
        captured["phone"] = m_ph.group(1)

    pw = None
    browser = None
    try:
        if message:
            await safe_edit_html(
                message,
                f"🔄 <b>Step 1/2: Navigating to Address Page (Wait 10s)...</b>\n"
                f"📁 Session: <code>{active_key}</code>",
                parse_mode="HTML",
            )

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
        )

        ctx = await browser.new_context(
            viewport=FIXED_VIEWPORT,
            is_mobile=True,
            has_touch=True,
            user_agent=MOBILE_USER_AGENT,
            storage_state=state_path,
        )
        # Register browser so 🛑 Force Stop can close it mid-sync
        USER_BROWSERS[str(chat_id)] = {"browser": browser, "ctx": ctx, "pw": pw,
                                       "active_key": active_key, "state_path": state_path}
        page = await ctx.new_page()

        async def inspect_network(response):
            req = response.request
            url = req.url
            headers = req.headers
            auth = headers.get("authorization")
            if auth and auth.startswith("Bearer ") and not captured["token"]:
                captured["token"] = auth.replace("Bearer ", "").strip()
            v_id = headers.get("x-visitor-id") or headers.get("visitor-id")
            if v_id and not captured["visitor_id"]:
                captured["visitor_id"] = v_id

            if "address" in url.lower() and response.status == 200:
                try:
                    res_json = await response.json()
                    addr_list = res_json.get("address") or res_json.get("data", {}).get("address") or (res_json if isinstance(res_json, list) else [])
                    if isinstance(addr_list, list):
                        for a in addr_list:
                            if isinstance(a, dict):
                                aid = str(a.get("id") or a.get("address_id") or "")
                                pin = str(a.get("area_code") or a.get("pincode") or "")
                                ph = str(a.get("phone") or "")
                                if ph and not captured["phone"]: captured["phone"] = ph
                                if pin: captured["pincode"] = pin
                                if aid and (a.get("is_default_address") or not captured["address_id"]):
                                    captured["address_id"] = aid
                                    break
                except Exception:
                    pass

            if any(k in url.lower() for k in ["cart", "get_cart", "bag"]) and response.status == 200:
                try:
                    res_json = await response.json()
                    cid = res_json.get("cart_id") or res_json.get("id") or res_json.get("cart", {}).get("id")
                    if cid and str(cid).lower() != "none":
                        captured["cart_id"] = str(cid)

                    items = res_json.get("items") or res_json.get("cart", {}).get("items") or []
                    for it in items:
                        st = it.get("store") or {}
                        uid = st.get("uid") or st.get("id") or st.get("store_id")
                        if uid and str(uid) not in ("None", "14523", "16546"):
                            captured["store_id"] = str(uid)
                            captured["store_code"] = str(st.get("store_code") or st.get("code") or "")
                            break

                    cart_obj = res_json.get("cart", res_json)
                    pay_sum = cart_obj.get("payment_summary", {})
                    if pay_sum.get("to_pay") is not None and float(pay_sum.get("to_pay") or 0) > 0:
                        captured["cart_price"] = str(pay_sum["to_pay"])
                    else:
                        breakup = cart_obj.get("breakup_values", {}).get("raw", {})
                        if breakup.get("total"):
                            captured["cart_price"] = str(breakup["total"])
                except Exception:
                    pass

        page.on("response", inspect_network)

        # 1. Address Page Navigation & 10s Stabilization
        await page.goto("https://www.jiomart.com/profile/address", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(10)

        if is_force_stopped(chat_id):
            USER_BROWSERS.pop(str(chat_id), None)
            try: await ctx.close()
            except: pass
            try: await browser.close()
            except: pass
            try: await pw.stop()
            except: pass
            if message:
                await safe_edit_html(message, "🛑 <b>FORCE STOP!</b> Sync aborted, browser closed.",
                                     reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")
            return False

        if not captured["address_id"]:
            try:
                page_html = await page.content()
                m = re.search(r'["\'](?:address_id|addressId|id)["\']\s*:\s*["\']?([0-9a-fA-F-]{8,}|[0-9]{5,})["\']?', page_html)
                if m: captured["address_id"] = m.group(1).strip()
            except Exception:
                pass

        if message:
            await safe_edit_html(
                message,
                f"🛒 <b>Step 2/2: Navigating to Cart/Bag Page (Wait 5s)...</b>\n"
                f"📍 Address ID: <code>{captured['address_id'] or 'Searching...'}</code>",
                parse_mode="HTML",
            )

        # 2. Cart Page Navigation & 5s Stabilization
        await page.goto("https://www.jiomart.com/cart/bag", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)

        if is_force_stopped(chat_id):
            USER_BROWSERS.pop(str(chat_id), None)
            try: await ctx.close()
            except: pass
            try: await browser.close()
            except: pass
            try: await pw.stop()
            except: pass
            if message:
                await safe_edit_html(message, "🛑 <b>FORCE STOP!</b> Sync aborted, browser closed.",
                                     reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")
            return False

        live_state = await ctx.storage_state()
        live_cookies = get_cookies_from_state(live_state)
        captured["cookies"] = live_cookies
        # QC DELIVERY: pull the quick-commerce polygon + fulfillment store that
        # the live browser bound (geolocation cookie / jio_qc_stores localStorage).
        try:
            qc_info = _qc_extract_from_state_obj(live_state)
            if qc_info.get("polygon_id"):
                captured["polygon_id"] = qc_info["polygon_id"]
            if qc_info.get("store_id") and not captured.get("store_id"):
                captured["store_id"] = qc_info["store_id"]
                captured["store_code"] = qc_info.get("store_code", "")
        except Exception as _qce:
            logger.warning(f"[QC] state extract warning: {_qce}")
        if not captured["visitor_id"]:
            captured["visitor_id"] = live_cookies.get("_v_id") or str(uuid.uuid4())

        RAM_CACHE[str(chat_id)] = captured
        USER_BROWSERS.pop(str(chat_id), None)   # sync done - unregister browser

        await ctx.close()
        await browser.close()
        await pw.stop()

        if message:
            await safe_edit_html(
                message,
                f"✅ <b>2-Step Sync Complete [{BOT_VERSION}] & Saved to RAM!</b>\n\n"
                f"📁 <b>Session      :</b> <code>{active_key}</code>\n"
                f"📱 <b>Phone        :</b> <code>{captured['phone'] or 'Auto'}</code>\n"
                f"🎫 <b>Bearer Token :</b> <code>{captured['token'][:20]}...</code>\n"
                f"🆔 <b>Visitor ID    :</b> <code>{captured['visitor_id'][:20]}...</code>\n"
                f"📍 <b>Address ID    :</b> <code>{captured['address_id'] or 'N/A'}</code>\n"
                f"🛒 <b>Cart ID       :</b> <code>{captured['cart_id'] or 'N/A'}</code>\n"
                f"🏪 <b>Store ID      :</b> <code>{captured['store_id'] or 'Auto'} {('['+captured['store_code']+']') if captured['store_code'] else ''}</code>\n"
                f"📍 <b>QC Polygon   :</b> <code>{captured.get('polygon_id') or 'N/A'}</code>\n"
                f"💰 <b>Cart Price    :</b> <code>₹{captured['cart_price']}</code>",
                reply_markup=get_main_keyboard(chat_id),
                parse_mode="HTML",
            )
        return True

    except Exception as e:
        logger.error(f"2-Step Sync Error: {e}")
        if browser:
            try: await browser.close()
            except: pass
        if pw:
            try: await pw.stop()
            except: pass
        if message:
            await safe_edit_html(message, f"❌ Sync Failed: {html.escape(str(e))}", reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")
        return False

# ==================== MULTI-SESSION API SYNC ====================
async def run_multi_session_api_sync(chat_id: str, count: int, message=None):
    ram_data = RAM_CACHE.get(str(chat_id))
    if not ram_data or not ram_data.get("token"):
        if message:
            await safe_edit_html(message, "❌ RAM Data missing! Click <b>🔄 2-Step Sync</b> first.", parse_mode="HTML")
        return False

    if message:
        await safe_edit_html(message, f"⚡ <b>Reading {count}x Sessions via API...</b>", parse_mode="HTML")

    token = ram_data["token"]
    cookies = ram_data.get("cookies", {})
    pincode = ram_data.get("pincode", "754004")
    cart_id = ram_data.get("cart_id", "")
    address_id = ram_data.get("address_id", "")
    store_id = ram_data.get("store_id", "")
    phone = ram_data.get("phone", "")
    base_price = ram_data.get("cart_price", "0.00")

    # QC DELIVERY: resolve the quick-commerce polygon (FR63_QC_xxx style) for
    # this account - RAM first (captured during 2-step sync), else scan the
    # saved session files. Without a polygon every order downgrades to standard.
    try:
        polygon_id = (
            ram_data.get("polygon_id")
            or extract_qc_identity(str(chat_id)).get("polygon_id", "")
        )
    except Exception:
        polygon_id = ""
    logger.info(f"[QC] API sync using polygon_id={polygon_id or 'NONE'} pincode={pincode}")

    async def sync_single_thread(idx):
        v_id = str(uuid.uuid4())
        # OD35-style location headers (X-Geolocation polygon_ids + glo_pincode)
        # are what pin each synced session to QUICK-COMMERCE delivery.
        headers = build_location_headers(
            pincode=pincode,
            token=token,
            visitor_id=v_id,
            lat=ram_data.get("latitude"),
            lon=ram_data.get("longitude"),
            store_id=store_id,
            ch="MOB",
            polygon_id=polygon_id,
        )

        # Overlay QC geolocation cookies (pins delivery to Quick-Commerce).
        qc_cookies = get_quickcommerce_cookies(str(chat_id))
        session_cookies = dict(cookies)
        if qc_cookies:
            session_cookies.update(qc_cookies)

        s_info = {
            "index": idx,
            "chat_id": str(chat_id),
            "token": token,
            "visitor_id": v_id,
            "cookies": session_cookies,
            "pincode": pincode,
            "phone": phone,
            "address_id": address_id,
            "cart_id": cart_id,
            "store_id": store_id,
            "store_code": ram_data.get("store_code", ""),
            "polygon_id": polygon_id,
            "cart_price": base_price,
            "item_count": 0,
        }

        url = f"https://www.jiomart.com/api/service/application/cart/v1.0/detail?area_code={pincode}&b=true&i=true"
        try:
            async with aiohttp.ClientSession(cookies=cookies) as sess:
                async with sess.get(url, headers=headers, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        cid = data.get("cart_id") or data.get("id")
                        if cid and str(cid).lower() != "none":
                            s_info["cart_id"] = str(cid)

                        cart_obj = data.get("cart", data)
                        items = cart_obj.get("items") or data.get("items") or []
                        s_info["item_count"] = len(items)
                        logger.info(f"[API sync #{idx}] get_cart OK cart={str(s_info['cart_id'])[:14]} "
                                    f"items={s_info['item_count']}"
                                    + ("  ⚠️ CART EMPTY" if s_info["item_count"] == 0 else ""))

                        # Real fulfillment store assigned per-item by JioMart
                        # (od35 _extract_store_from_cart_data behaviour). Never
                        # let central-warehouse UIDs overwrite a QC store.
                        for it2 in items:
                            st2 = ((it2.get("article") or {}).get("store") if isinstance(it2.get("article"), dict) else None) \
                                or (it2 or {}).get("store") or {}
                            uid2 = st2.get("uid") or st2.get("id") or st2.get("store_id")
                            if uid2 and str(uid2) not in ("None", "14523", "16546", "259630"):
                                s_info["store_id"] = str(uid2)
                                s_info["store_code"] = str(st2.get("store_code") or st2.get("code") or "")
                                break
                        if polygon_id:
                            s_info.setdefault("polygon_id", polygon_id)

                        pay_sum = cart_obj.get("payment_summary", {})
                        try:
                            s_info["delivery_charge"] = float(
                                (cart_obj.get("breakup_values", {}).get("raw", {}) or {}).get("delivery_charge") or 0)
                        except Exception:
                            s_info["delivery_charge"] = 0.0
                        if pay_sum.get("to_pay") is not None and float(pay_sum.get("to_pay") or 0) > 0:
                            s_info["cart_price"] = str(pay_sum["to_pay"])
                        else:
                            breakup = cart_obj.get("breakup_values", {}).get("raw", {})
                            if breakup.get("total"):
                                s_info["cart_price"] = str(breakup["total"])
        except Exception:
            pass
        return s_info

    tasks = [sync_single_thread(i + 1) for i in range(count)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    valid = [r for r in results if isinstance(r, dict) and r.get("cart_id")]

    if valid:
        USER_ACTIVE_SESSIONS[str(chat_id)] = valid
        if message:
            amt = valid[0].get("cart_price", "0.00")
            try:
                tot_val = float(amt) * len(valid)
            except Exception:
                tot_val = 0.0
            total_items = sum(int(r.get("item_count") or 0) for r in valid)
            cart_warn = ("\n\n⚠️ <b>Cart is EMPTY!</b> Add items in JioMart first!" if total_items == 0 else "")
            await safe_edit_html(
                message,
                f"✅ <b>API Sync Complete [{BOT_VERSION}] ({len(valid)} Sessions Ready)</b>\n\n"
                f"📍 Address ID : <code>{address_id or 'N/A'}</code>\n"
                f"🛒 Cart ID    : <code>{valid[0]['cart_id']}</code>\n"
                f"📦 Cart Items : <code>{total_items}</code>{cart_warn}\n"
                f"💰 Price      : ₹{amt} / order\n"
                f"🔢 Total Value: ₹{tot_val:.2f}\n\n"
                f"👉 Click <b>🚀 Fire All (Payment Page)</b> to proceed.",
                reply_markup=get_main_keyboard(chat_id),
                parse_mode="HTML",
            )
        return True

    if message:
        await safe_edit_html(message, "❌ API Multi-Sync Failed!", reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")
    return False

# ==================== STEP 3: FIRE ALL TO PAYMENT PAGE (API) ====================
async def checkout_to_payment_stage(s_data):
    clean_token = s_data["token"].replace("Bearer ", "").strip()
    address_id = s_data["address_id"]
    cart_id = s_data["cart_id"]
    pincode = s_data["pincode"]
    store_id = s_data.get("store_id", "")
    cookies = s_data.get("cookies", {})
    # QC DELIVERY: overlay the saved QC-particle geolocation cookies (app_geo/
    # app_location_details with TE58_QC_/FR63_QC_ polygon ids) so the checkout
    # session is pinned to QUICK-Commerce. Server picks quick only when the cart
    # + geolocation + store all line up to a QC polygon.
    try:
        qc = get_quickcommerce_cookies(s_data.get("chat_id", ""))
        if qc:
            cookies = {**cookies, **qc}
    except Exception:
        pass
    s_data["cookies"] = cookies
    visitor_id = s_data["visitor_id"]
    live_price = s_data.get("cart_price", "0.00")

    # OD35-style header set: X-Geolocation carries the QC polygon_ids list -
    # this (together with the store_id) is what JioMart uses to route checkout
    # to the quick-commerce dark store instead of standard delivery.
    polygon_id = s_data.get("polygon_id", "")
    if not polygon_id:
        try:
            polygon_id = extract_qc_identity(str(s_data.get("chat_id", ""))).get("polygon_id", "")
        except Exception:
            polygon_id = ""

    headers = build_location_headers(
        pincode=pincode,
        token=clean_token,
        visitor_id=visitor_id,
        lat=s_data.get("latitude"),
        lon=s_data.get("longitude"),
        store_id=store_id,
        ch="MOB",
        polygon_id=polygon_id,
    )
    if polygon_id:
        # Mirror the real browser shape exactly (string lat/lon values).
        headers["X-Geolocation"] = json.dumps({
            "latitude": str(s_data.get("latitude") or ""),
            "longitude": str(s_data.get("longitude") or ""),
            "polygon_ids": [polygon_id],
        })
    # v2.0/checkout REQUIRES x-cart-id (missing => 401 "Invalid authorization token")
    if cart_id and str(cart_id).lower() not in ("", "none"):
        headers["x-cart-id"] = str(cart_id)
    logger.info(f"[QC] checkout pinned polygon={polygon_id or 'NONE'} store={store_id or 'auto'}")

    res_result = {"success": False, "order_id": "", "trx_id": "", "amount": live_price}

    try:
        async with aiohttp.ClientSession(cookies=cookies, timeout=aiohttp.ClientTimeout(total=45)) as session:
            sa_url = f"https://www.jiomart.com/ext/jmshipmentfee/cart/v1.0/select_address?cart_id={cart_id}"
            sa_payload = {
                "id": str(address_id),
                "billing_address_id": str(address_id),
                "cart_id": str(cart_id),
                "journey": "quickcommerce",
            }
            gs_url = f"https://www.jiomart.com/api/service/application/cart/v1.0/shipment?address_id={address_id}&area_code={pincode}&id={cart_id}"

            def _plain_headers(base_hdrs):
                """Legacy header set that already sealed orders successfully
                before QC pinning - auto fallback when the QC attempt fails."""
                drop = {"X-Geolocation", "X-Location-Detail", "glo_pincode",
                        "x-glo-pincode", "latitude", "longitude",
                        "x-latitude", "x-longitude"}
                out = {k: v for k, v in base_hdrs.items() if k not in drop}
                out["Content-Type"] = "application/json"
                out["Accept"] = "application/json, text/plain, */*"
                out["User-Agent"] = MOBILE_USER_AGENT
                out["Origin"] = "https://www.jiomart.com"
                out["Referer"] = "https://www.jiomart.com/cart/bag"
                if cart_id and str(cart_id).lower() not in ("", "none"):
                    out["x-cart-id"] = str(cart_id)
                return out

            # FIXED (2026-08-30): keep the PROVEN api/service checkout URL (sealed OK in
            # Run 6) but add the x-cart-id header + cart_id cookies that
            # apiready1 api_place_cod_order sends. Missing x-cart-id => 401
            # "Invalid authorization token".
            ck_url = "https://www.jiomart.com/api/service/application/cart/v2.0/checkout"
            if cart_id and str(cart_id).lower() not in ("", "none"):
                cookies = dict(cookies or {})
                cookies["cart_id"] = str(cart_id)
                cookies["jm_cart_id"] = str(cart_id)
            ck_payload = {
                "address_id": str(address_id),
                "aggregator": "JioOnePay",
                "merchant_code": "JIOPP",
                "payment_mode": "JIOPP",
                "callback_url": "https://www.jiomart.com/cart/order-status",
                "payment_methods": [
                    {
                        "mode": "JIOPP",
                        "name": "JioOnePay",
                        "payment": "required",
                        "payment_identifier": "JIOPP",
                        "payment_meta": {
                            "merchant_code": "JIOPP",
                            "payment_gateway": "JioOnePay",
                        },
                    }
                ],
            }

            async def _fire_attempt(hdrs):
                """select_address -> get_shipments -> v2.0/checkout.

                Returns (status, data, delivery_fee) - the delivery fee is read
                from the get_shipments breakup purely as informational data."""
                async with session.post(sa_url, json=sa_payload, headers=hdrs, timeout=15) as r1:
                    await r1.read()

                delivery_fee = 0.0
                async with session.get(gs_url, headers=hdrs, timeout=15) as r2:
                    if r2.status == 200:
                        gs_data = await r2.json(content_type=None)
                        cart_node = gs_data.get("cart", gs_data) if isinstance(gs_data, dict) else {}
                        pay_sum = cart_node.get("payment_summary", {}) if isinstance(cart_node, dict) else {}
                        breakup = cart_node.get("breakup_values", {}).get("raw", {}) if isinstance(cart_node, dict) else {}
                        try:
                            # JioMart hides the fee in total_charge (delivery_charge stays 0!)
                            delivery_fee = max(
                                float(breakup.get("delivery_charge") or 0),
                                float(breakup.get("total_charge") or 0),
                                float(breakup.get("convenience_fee") or 0))
                        except Exception:
                            delivery_fee = 0.0
                        if pay_sum.get("to_pay") is not None and float(pay_sum.get("to_pay") or 0) > 0:
                            res_result["amount"] = str(pay_sum["to_pay"])
                        else:
                            if breakup.get("total"):
                                res_result["amount"] = str(breakup["total"])
                        logger.info(f"[Checkout] subtotal={breakup.get('subtotal')} "
                                    f"delivery_charge={delivery_fee} to_pay={res_result.get('amount')}")

                async with session.post(ck_url, json=ck_payload, headers=hdrs, timeout=25) as resp:
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        data = {}
                    return resp.status, (data if isinstance(data, dict) else {}), delivery_fee

            # Attempt 1 = full QC headers (pinned quick-commerce).
            # Attempt 2 = legacy plain headers (proven sealing path) so orders
            # never die when JioMart rejects the pinned polygon for this cart.
            for mode in ("qc", "plain"):
                hdrs = headers if mode == "qc" else _plain_headers(headers)
                status, data, dfee_seen = await _fire_attempt(hdrs)
                logger.info(f"[Checkout Session #{s_data.get('index')} mode={mode}] "
                            f"Status={status} Resp={data}")
                data_node = data.get("data") if isinstance(data.get("data"), dict) else {}
                order_id = data.get("order_id") or data_node.get("order_id") or data_node.get("merchant_order_id")
                if not order_id:
                    logger.warning(f"[Checkout Session #{s_data.get('index')}] '{mode}' attempt got "
                                   f"no order id (HTTP {status})"
                                   + (" - falling back to plain headers..." if mode == "qc" else ""))
                    continue
                trx_id = data.get("merchant_transaction_id") or data_node.get("merchant_transaction_id")
                res_result["success"] = True
                res_result["order_id"] = str(order_id)
                res_result["trx_id"] = str(trx_id or f"TR{order_id}")
                res_result["payment_confirm_url"] = str(
                    data.get("payment_confirm_url")
                    or data_node.get("payment_confirm_url")
                    or "https://api.jiomartjcp.com/service/webhooks/payment/v1.0/confirm/"
                )
                res_result["callback_url"] = str(
                    data.get("callback_url") or data_node.get("callback_url")
                    or "https://www.jiomart.com/cart/order-status"
                )
                order_hex = str(order_id)[2:].lower() if str(order_id).upper().startswith("JM") else ""
                res_result["pgloader_id"] = find_pgloader_id(
                    data, exclude_ids=(address_id, cart_id), order_hex_prefix=order_hex[:8]
                )
                logger.info(f"[Checkout] pgloader_id={res_result['pgloader_id'] or 'NOT FOUND'} for {order_id}")
                break

    except Exception as e:
        logger.error(f"Checkout error for session #{s_data.get('index')}: {e}")

    return res_result

async def run_fire_to_payment_page(chat_id: str, message=None):
    sessions = USER_ACTIVE_SESSIONS.get(str(chat_id), [])
    if not sessions:
        if message:
            await safe_edit_html(message, "⚠️ No active sessions! Run <b>📥 Read All</b> first.", parse_mode="HTML")
        return

    if message:
        await safe_edit_html(message, f"🚀 <b>Advancing {len(sessions)} Sessions to Payment Page [{BOT_VERSION}]...</b>", parse_mode="HTML")

    tasks = [checkout_to_payment_stage(s) for s in sessions]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Pre-flight sanity check: use the PLATFORM cart API (/cart/v1.0/basic) which is
    # reliable - the jmshipmentfee/get_cart proxy spuriously returns
    # "location details not found" even on healthy sessions.
    ram_data = RAM_CACHE.get(str(chat_id), {})
    item_count = -1
    if ram_data.get("cookies") and ram_data.get("token"):
        any_success = any(isinstance(r, dict) and r.get("success") for r in results)
        try:
            async with aiohttp.ClientSession(cookies=ram_data.get("cookies", {})) as vs:
                vh = {"User-Agent": MOBILE_USER_AGENT, "Accept": "application/json",
                      "Authorization": f"Bearer {ram_data['token']}",
                      "x-channel": "MOB", "clientId": "mweb"}
                async with vs.get(
                    "https://www.jiomart.com/api/service/application/cart/v1.0/basic",
                    headers=vh, timeout=15) as vr:
                    vj = await vr.json(content_type=None)
                    item_count = int(vj.get("user_cart_items_count") or 0)
                    if item_count == 0 and not any_success:
                        await safe_edit_html(
                            message,
                            f"🛒 <b>Cart is EMPTY!</b> [{BOT_VERSION}]\n\n"
                            f"Checkout needs at least 1 item in the JioMart cart.\n\n"
                            f"➡️ 1. Open JioMart (browser) & add items to cart\n"
                            f"➡️ 2. Run <b>🔄 2-Step Sync</b>\n"
                            f"➡️ 3. Then click <b>🚀 Fire All</b>",
                            reply_markup=get_main_keyboard(chat_id),
                            parse_mode="HTML")
                        logger.warning(f"[Pre-flight] cart EMPTY (items=0) - fire aborted")
                        return
                    if isinstance(vj, dict) and not vj.get("success") and not any_success:
                        await safe_edit_html(
                            message,
                            f"⚠️ <b>Cart is empty / session stale!</b><br>"
                            f"➡️ Add items & run <b>🔄 2-Step Browser Sync</b>, then retry.",
                            parse_mode="HTML")
                        return
                    if any_success:
                        logger.info(f"[Pre-flight] cart items={item_count}; "
                                    f"{sum(1 for r in results if isinstance(r, dict) and r.get('success'))} "
                                    f"checkout(s) succeeded - continuing")
        except Exception as e:
            logger.warning(f"Pre-flight skipped: {e}")

    pending_map = {}
    for idx, (s, res) in enumerate(zip(sessions, results), 1):
        if isinstance(res, dict) and res.get("success"):
            try:
                amt_val = float(res["amount"])
            except Exception:
                amt_val = 0.0
            pending_map[idx] = {
                "index": idx,
                "order_id": res["order_id"],
                "trx_id": res["trx_id"],
                "amount": amt_val,
                "payment_confirm_url": res.get("payment_confirm_url", ""),
                "pgloader_id": res.get("pgloader_id", ""),
                "session_data": s,
            }

    PENDING_PAYMENTS[str(chat_id)] = pending_map
    SELECTED_PAYMENTS[str(chat_id)] = set(pending_map.keys())

    await show_payment_selection_screen(chat_id, message)

# ==================== PAYMENT SELECTION UI ====================
def build_payment_selection_keyboard(chat_id: str):
    pending = PENDING_PAYMENTS.get(str(chat_id), {})
    selected = SELECTED_PAYMENTS.get(str(chat_id), set())

    kb = []
    row = []
    for idx in sorted(pending.keys()):
        icon = "✅" if idx in selected else "⬜"
        amt = pending[idx]["amount"]
        btn_text = f"{icon} #{html.escape(str(idx))} (₹{amt:.2f})"
        row.append(InlineKeyboardButton(btn_text, callback_data=f"toggle_pay_{idx}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)

    total_selected = sum(pending[i]["amount"] for i in selected if i in pending)
    kb.append([
        InlineKeyboardButton("Select All", callback_data="pay_select_all"),
        InlineKeyboardButton("Clear All", callback_data="pay_clear_all"),
    ])
    kb.append([
        InlineKeyboardButton(f"🔥 Confirm & Fire ({len(selected)} Orders: ₹{total_selected:.2f})", callback_data="pay_confirm_fire_selected")
    ])
    kb.append([InlineKeyboardButton("🔙 Main Menu", callback_data="back_home")])
    return InlineKeyboardMarkup(kb)

async def show_payment_selection_screen(chat_id: str, message=None):
    pending = PENDING_PAYMENTS.get(str(chat_id), {})
    selected = SELECTED_PAYMENTS.get(str(chat_id), set())

    if not pending:
        if message:
            await safe_edit_html(message, "❌ No sessions currently at payment stage.",
                                 reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")
        return

    total_amt = sum(pending[i]["amount"] for i in selected if i in pending)
    lines = [
        f"💳 <b>Payment Page Sessions Ready (jm2.0.0) ({len(pending)} Total)</b>\n",
        f"• <b>Selected Count :</b> {len(selected)} / {len(pending)}",
        f"• <b>Final Payable :</b> <b>₹{total_amt:.2f}</b>\n",
        "<b>📋 Sessions at Payment Stage:</b>"
    ]
    for idx in sorted(pending.keys()):
        p = pending[idx]
        lines.append(f"• <b>#{idx}:</b> <code>{html.escape(str(p['order_id']))}</code> | <b>₹{p['amount']:.2f}</b>")
    lines.append("\n<i>Toggle any session button below, then press Confirm:</i>")
    msg_text = "\n".join(lines)

    if message:
        await safe_edit_html(message, msg_text, reply_markup=build_payment_selection_keyboard(chat_id), parse_mode="HTML")

# ==================== FINAL PAYMENT SEALING (FORM + ORDER-STATUS TRIGGER) ====================
async def execute_final_payment_seal(chat_id: str, message):
    pending = PENDING_PAYMENTS.get(str(chat_id), {})
    selected = SELECTED_PAYMENTS.get(str(chat_id), set())

    if not selected:
        await safe_edit_html(message, "⚠️ No sessions selected to fire!", reply_markup=build_payment_selection_keyboard(chat_id), parse_mode="HTML")
        return

    target_items = [pending[i] for i in sorted(selected) if i in pending]
    await safe_edit_html(message, f"⚡ <b>Sealing Payment & Confirming {len(target_items)} Orders [{BOT_VERSION}]...</b>", parse_mode="HTML")

    async def send_progress(text):
        try:
            await safe_edit_html(message, text, parse_mode="HTML")
        except Exception:
            pass

    async def seal_single(item):
        s = item["session_data"]
        token = s["token"].replace("Bearer ", "").strip()
        order_id = item["order_id"]
        trx_id = item["trx_id"] or f"TR{order_id}"
        amount = f"{item['amount']:.2f}"
        phone = s.get("phone") or "9040028319"
        visitor_id = s.get("visitor_id", "")
        pincode = str(s.get("pincode", ""))
        cookies = s.get("cookies", {})

        async with aiohttp.ClientSession(cookies=cookies) as session:
            # ============================================================
            # FULL PURE-API COD SEALING CHAIN (reverse-engineered live):
            #  A) POST payment_session            -> pgloader txn id
            #  B) GET  pgloader/{id}              -> auto-submit form (param1)
            #  C) POST /jop/v1/payment-options    -> JioOnePay session cookies
            #  D) POST /jop/v1/points-confirmpayment -> signed htmlForm
            #  E) POST jioResponseMsg -> paymentCallBackB2B -> SEALED
            # ============================================================
            api_h = {
                "User-Agent": MOBILE_USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Authorization": f"Bearer {token}",
                "x-channel": "MOB", "clientId": "mweb",
                "Referer": "https://www.jiomart.com/cart/bag",
                "Origin": "https://www.jiomart.com",
            }
            page_referer = "https://payments.jio.com/JioOnePay/"

            # -- A. payment_session: create gateway transaction --
            # VERIFIED live: form-urlencoded body; responds 302 whose Location
            # header points to /ext/jioonepay/api/v1/pgloader/{txn_id}
            pg_id = ""
            try:
                async with session.post(
                    "https://www.jiomart.com/api/service/webhooks/payment/v1.0/payment_session",
                    data={
                        "merchant_order_id": str(order_id),
                        "merchant_transaction_id": str(trx_id),
                    },
                    headers={
                        "User-Agent": MOBILE_USER_AGENT,
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Referer": "https://www.jiomart.com/cart/bag",
                    },
                    timeout=20, allow_redirects=False) as r_ps:
                    loc = r_ps.headers.get("Location", "") or ""
                    r_txt = ""
                    if not loc:
                        try:
                            r_txt = await r_ps.text()
                        except Exception:
                            pass
                    m_pg = (re.search(r"pgloader/([0-9a-f]{24})", loc)
                            or re.search(r"pgloader/([0-9a-f]{24})", r_txt))
                    if m_pg:
                        pg_id = m_pg.group(1)
                    logger.info(f"[A] payment_session {order_id} status={r_ps.status} "
                                f"loc={loc[:120]} pg_id={pg_id or 'NOT FOUND'}")
            except Exception as e:
                logger.warning(f"A warning {order_id}: {e}")

            # -- B+C. pgloader form -> payment-options (JOP session bootstrap) --
            if pg_id:
                try:
                    async with session.get(
                        f"https://www.jiomart.com/ext/jioonepay/api/v1/pgloader/{pg_id}",
                        headers={"User-Agent": MOBILE_USER_AGENT,
                                 "Referer": "https://www.jiomart.com/cart/order-status"},
                        timeout=20, allow_redirects=False) as r_pg:
                        pg_html = html.unescape(await r_pg.text())
                    m_form = re.search(r"<form[^>]*action=['\"]([^'\"]+)['\"][^>]*>(.*?)</form>",
                                       pg_html, re.S | re.I)
                    if m_form:
                        action = html.unescape(m_form.group(1))
                        fields = {m.group(1): html.unescape(m.group(2)) for m in re.finditer(
                            r"<input[^>]*name=['\"]([^'\"]+)['\"][^>]*value=['\"]([^'\"]*)['\"]",
                            m_form.group(2), re.I)}
                        if action.startswith("/"):
                            action = "https://payments.jio.com" + action
                        logger.info(f"[B] pgloader form -> {action} fields={list(fields.keys())}")
                        async with session.post(action, data=fields, headers={
                                "User-Agent": MOBILE_USER_AGENT,
                                "Content-Type": "application/x-www-form-urlencoded"},
                                timeout=25, allow_redirects=True) as r_po:
                            # The final redirected URL is the JopWebApp page with
                            # paramx1..paramx4 query params - required as Referer for [D]
                            page_referer = str(r_po.url)
                            logger.info(f"[C] payment-options status={r_po.status} "
                                        f"jop_webapp_url={page_referer[:130]}")
                    else:
                        logger.warning(f"[B] {order_id}: no form on pgloader page len={len(pg_html)}")
                except Exception as e:
                    logger.warning(f"B/C warning {order_id}: {e}")
            else:
                logger.warning(f"{order_id}: no pg_id - JOP calls may fail")

            # -- C2. JOP session warm-up (pure API, no browser) --
            # VERIFIED LIVE TRACE: the real browser loads JopWebApp and fires
            # these endpoints BEFORE points-confirmpayment returns the signed
            # htmlForm. Calling them here establishes the same server-side
            # JioOnePay session so we never need a payment browser.
            # XSRF: the JopWebApp Angular client mirrors the XSRF-TOKEN cookie
            # into an X-XSRF-TOKEN header on every POST. aiohttp does NOT do
            # this automatically - without it the gateway answers a bare
            # {"status": true} with NO signed htmlForm (verified in live runs).
            def _get_xsrf_token():
                tok = ""
                try:
                    for ck in session.cookie_jar:
                        if str(ck.key).lower() == "xsrf-token":
                            tok = str(ck.value)
                            break
                except Exception:
                    pass
                return tok

            xsrf_tok = _get_xsrf_token()
            logger.info(f"[C2] XSRF-TOKEN present={bool(xsrf_tok)}")
            if page_referer.startswith("https://payments.jio.com"):
                jop_warm_headers = {
                    "User-Agent": MOBILE_USER_AGENT,
                    "Accept": "application/json, text/plain, */*",
                    "Referer": page_referer,
                    "Origin": "https://payments.jio.com",
                }
                if xsrf_tok:
                    jop_warm_headers["X-XSRF-TOKEN"] = xsrf_tok

                # VERIFIED LIVE TRACE (#547): right after the JopWebApp page
                # the real client fires GET /jop/v1/payment-dashboard?paramx1..
                # with the SAME query as the landing URL. This establishes the
                # gateway-side session state - without it confirmpayment only
                # answers a bare {"status": true} with NO signed htmlForm.
                try:
                    query = page_referer.split("?", 1)[1]
                    _jop_query = "?" + query
                    dash_url = ("https://payments.jio.com/jop/v1/payment-dashboard"
                                + _jop_query)
                    async with session.get(dash_url, headers=jop_warm_headers,
                                           timeout=15) as _rd:
                        logger.info(f"[C2] payment-dashboard status={_rd.status}")
                except Exception as e:
                    logger.warning(f"[C2] payment-dashboard warning {order_id}: {e}")
                    _jop_query = ""
                # XSRF-TOKEN is typically set by this first authenticated GET
                fresh = _get_xsrf_token()
                if fresh:
                    xsrf_tok = fresh
                    jop_warm_headers["X-XSRF-TOKEN"] = fresh
                    logger.info(f"[C2] XSRF-TOKEN acquired after dashboard=True")

                try:
                    async with session.get(
                            "https://payments.jio.com/jop/v1/downtime",
                            headers=jop_warm_headers, timeout=12) as _r:
                        logger.info(f"[C2] downtime status={_r.status}")
                except Exception as e:
                    logger.warning(f"[C2] downtime warning {order_id}: {e}")
                for _wep in ("saved-wallets-ppl", "saved-wallets-ajm", "saved-cards",
                             "twid/fetch-offers"):
                    try:
                        async with session.post(
                                f"https://payments.jio.com/jop/v1/{_wep}",
                                json={}, headers=jop_warm_headers,
                                timeout=12) as _r:
                            logger.info(f"[C2] {_wep} status={_r.status}")
                    except Exception as e:
                        logger.warning(f"[C2] {_wep} warning {order_id}: {e}")

            # -- D. points-confirmpayment (COD selection + confirm) --
            cust_id = s.get("user_id", "")
            if not cust_id:
                try:
                    async with session.get(
                        "https://www.jiomart.com/api/service/application/cart/v1.0/address?checkout_mode=self",
                        headers=api_h, timeout=15) as r_adr:
                        adr = await r_adr.json(content_type=None)
                        alist = adr.get("address") if isinstance(adr, dict) else []
                        if alist and isinstance(alist[0], dict):
                            cust_id = str(alist[0].get("user_id") or "")
                except Exception:
                    pass

            jop_url = "https://payments.jio.com/jop/v1/points-confirmpayment"
            # VERIFIED LIVE CAPTURE (2026-08-26 23:06 manual COD success): the
            # real browser sends ONLY selection flags + the JopWebApp queryParams.
            # NO orderRefNum/amount/paymentMode/phoneNumber - the gateway takes
            # those from its dashboard-side session. Sending them made the
            # gateway answer a bare {"status": true} with no signed htmlForm.
            jop_payload = {
                "isCodSelected": "true",
                "isStoreCreditSelected": "false",
                "isRoneSelected": "false",
                "isEmpGVSelected": "false",
                "isGiftCardSelected": "false",
                "isJopWalletSelected": "false",
                "isVouchagramSelected": "false",
                "isSapCreditSelected": "false",
                "twidRewardPointsId": "",
                "twidVoucherId": "",
                "isNmsCashbackSelected": "false",
                "nmsCashbackAmount": "",
                "isNmsVoucherSelected": "false",
                "nmsVoucherCodes": "",
                "isVgGyfrSelected": "false",
                "isPromotionalCashSelected": "false",
                "queryParams": _jop_query,
            }
            # Amount note: the gateway always seals at its own server-side session
            # amount (payment_session.amountPayable), so no client-side override is
            # sent - the payload carries only selection flags + query params.
            jop_headers = {
                "Content-Type": "application/json",
                "User-Agent": MOBILE_USER_AGENT,
                "Origin": "https://payments.jio.com",
                "Referer": page_referer,
                "Accept": "application/json, text/plain, */*",
            }
            # refresh XSRF (payment-options chain may have rotated it)
            xsrf_tok = _get_xsrf_token() or xsrf_tok
            if xsrf_tok:
                jop_headers["X-XSRF-TOKEN"] = xsrf_tok

            b2b_action = ""
            jio_msg = ""
            # DIAGNOSTIC: dump which cookies we hold for payments.jio.com -
            # real browser carries the whole JOP session here.
            try:
                ck_keys = []
                for _ck in session.cookie_jar:
                    if "jio" in str(_ck.get("domain", "") or str(_ck)):
                        pass
                    ck_keys.append(str(_ck.key))
                logger.info(f"[D-pre] cookies={sorted(set(ck_keys))[:25]} "
                            f"cust_id={cust_id!r} phone={str(phone)[:4]}****")
            except Exception as _ce:
                logger.warning(f"[D-pre] cookie-dump err: {_ce}")
            # VERIFIED LIVE TRACE (manual browser 2026-08-26): a successful COD
            # confirm ALWAYS returns {'status': true, 'htmlForm': ...} with the
            # server-signed jioResponseMsg. If status=true arrives with NO
            # htmlForm, retry a couple of times before trusting it - this was
            # the likely cause of past "Seal Failed" results.
            # brief settle time - real browser spends ~15s on JopWebApp before
            # confirmpayment fires; hitting it instantly keeps yielding bare
            # {"status": true} in live runs.
            # brief settle time - the C2 warm-up above established the JOP
            # session; a short settle keeps the gateway from returning a bare
            # {"status": true} on the single confirm call below.
            await asyncio.sleep(2)
            try:
                async with session.post(jop_url, json=jop_payload, headers=jop_headers,
                                        timeout=20) as r_jop:
                    jop_status = r_jop.status
                    try:
                        jop_data = await r_jop.json(content_type=None)
                    except Exception:
                        jop_data = {}
                    new_xsrf = _get_xsrf_token()
                    if new_xsrf and new_xsrf != xsrf_tok:
                        xsrf_tok = new_xsrf
                        jop_headers["X-XSRF-TOKEN"] = xsrf_tok
                    _keys = list(jop_data.keys()) if isinstance(jop_data, dict) else None
                    _data_type = type(jop_data).__name__
                    logger.info(f"[D] {order_id} http={jop_status} keys={_keys if _keys else _data_type}")
                    html_form = html.unescape(str(jop_data.get("htmlForm", ""))) \
                        if isinstance(jop_data, dict) else ""
                    if isinstance(jop_data, dict) and jop_data.get("status") is True and html_form:
                        m_action = re.search(r"action=['\"]([^'\"]+)['\"]", html_form)
                        m_msg = re.search(
                            r'name=["\']jioResponseMsg["\']\s+value=["\']([^"\']+)["\']',
                            html_form)
                        if m_action:
                            b2b_action = m_action.group(1)
                        if m_msg:
                            jio_msg = html.unescape(m_msg.group(1))
                            # SEALED AMOUNT from signed msg (0|...|AMOUNT|NA|...)
                            try:
                                _parts = jio_msg.split("|")
                                _sealed_amt = _parts[3] if len(_parts) > 3 else "?"
                            except Exception:
                                _sealed_amt = "?"
                            logger.info(f"[D-TEST] SEALED_AMOUNT={_sealed_amt} jio_msg=<{jio_msg[:160]}>")
                        logger.info(f"[D] confirmpayment OK {order_id} "
                                    f"msg={bool(jio_msg)} action={bool(b2b_action)}")
                    else:
                        logger.warning(
                            f"[D] confirmpayment {order_id} Status={jop_status} "
                            f"FULL_RESP={str(jop_data)[:1500]}")
            except Exception as e:
                logger.warning(f"D warning {order_id}: {e}")

            # -- (E1 REMOVED) Direct {'status': True} fallback path was the
            # alternative when D returned no signed form. Removed: only the
            # server-signed B2B + payment_callback path (E/E2) seals now.


            # -- E. paymentCallBackB2B: submit server-signed jioResponseMsg (htmlForm path) --
            if not b2b_action:
                b2b_action = "https://payments.jio.com/jop/v1/b2b/paymentCallBackB2B"
            e2_sealed = False
            if jio_msg:
                b2b_headers = {
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://payments.jio.com",
                    # VERIFIED LIVE: browser's Referer here is the go-to-pg page
                    "Referer": "https://payments.jio.com/JopWebApp/go-to-pg/",
                    "User-Agent": MOBILE_USER_AGENT,
                }
                xsrf_tok = _get_xsrf_token() or xsrf_tok
                if xsrf_tok:
                    b2b_headers["X-XSRF-TOKEN"] = xsrf_tok
                try:
                    async with session.post(b2b_action, data={"jioResponseMsg": jio_msg},
                                            headers=b2b_headers, timeout=25,
                                            allow_redirects=True) as b_resp:
                        b_body = ""
                        try:
                            b_body = await b_resp.text()
                        except Exception:
                            pass
                        chain = " -> ".join(str(h.url) for h in b_resp.history)
                        logger.info(
                            f"[E] B2B callback {order_id} status={b_resp.status} "
                            f"final_url={str(b_resp.url)[:130]} chain={chain[:200]}")
                        # VERIFIED LIVE: the B2B response is ANOTHER auto-submit
                        # form: action=jioonepay.extensions.jiomartjcp.com/.../
                        # payment_callback with X-JIO-PAYLOAD-AUTH + responseData.
                        # The real browser submits it - we must too, or JioMart
                        # never receives the signed confirmation.
                        #
                        # responseData is a JSON string wrapped in MATCHING quotes
                        # (value='{"transactionType":...}'), so a plain [^'"]+ regex
                        # truncates at the first inner double-quote and the callback
                        # 500s ("Expected property name or '}'"). Use a quote-aware
                        # helper that consumes the two matching quote chars.
                        def _hidden_val(html_text, name, attr="value"):
                            m = re.search(
                                r"name=['\"]%s['\"][^>]*%s=([\"\'])" %
                                (re.escape(name), re.escape(attr)),
                                html_text)
                            if not m:
                                return ""
                            q = m.group(1)
                            tail = html_text[m.end():]
                            end = tail.find(q)
                            if end == -1:
                                return ""
                            return html.unescape(tail[:end])

                        cb_action = ""
                        m_cb = re.search(r"action=([\"'])(.*?)\1", b_body, re.S)
                        if m_cb:
                            cb_action = html.unescape(m_cb.group(2))
                        m_auth = _hidden_val(b_body, "X-JIO-PAYLOAD-AUTH")
                        m_rdata = _hidden_val(b_body, "responseData")
                        if cb_action and m_auth and m_rdata and "jioonepay" in cb_action:
                            cb_data = {
                                "X-JIO-PAYLOAD-AUTH": html.unescape(m_auth),
                                "responseData": html.unescape(m_rdata),
                            }
                            try:
                                async with session.post(cb_action, data=cb_data,
                                                        headers={
                                                            "Content-Type": "application/x-www-form-urlencoded",
                                                            "Origin": "https://payments.jio.com",
                                                            "Referer": "https://payments.jio.com/",
                                                            "User-Agent": MOBILE_USER_AGENT,
                                                        }, timeout=25,
                                                        allow_redirects=True) as cb_resp:
                                    # VERIFIED LIVE: payment_callback returns the
                                    # signed responseData (success:true) as its
                                    # BODY - not a redirect. Detect success by
                                    # re-parsing the returned body too.
                                    sealed_redirect = ("success=true" in str(cb_resp.url))
                                    cb_body = ""
                                    try:
                                        cb_body = await cb_resp.text()
                                    except Exception:
                                        pass
                                    # The callback chain may have posted the
                                    # responseData JSON with success:true
                                    e2_sealed = ("success" in cb_body.lower()
                                                 and "true" in cb_body.lower()) or sealed_redirect
                                    logger.info(
                                        f"[E2] payment_callback {order_id} "
                                        f"status={cb_resp.status} sealed={e2_sealed} "
                                        f"final_url={str(cb_resp.url)[:120]} "
                                        f"bdy={cb_body[:150]}")
                            except Exception as e:
                                logger.warning(f"E2 warning {order_id}: {e}")
                        else:
                            logger.warning(f"[E] {order_id}: no payment_callback form "
                                           f"in B2B response (len={len(b_body)})")
                except Exception as e:
                    logger.warning(f"E warning {order_id}: {e}")
            else:
                logger.warning(f"{order_id}: no signed jioResponseMsg - order NOT sealed")

            # 4. Step 4: Dynamic Retry Polling for Server Canonical Shipment ID (Captures #108/#117)
            # e2_sealed: the signed payment_callback (X-JIO-PAYLOAD-AUTH +
            # responseData with "success":true) was accepted -> the order IS
            # placed server-side even if polling below can't see it fast.
            shipment_id = ""
            sealed = e2_sealed
            api_headers = {
                "Accept": "application/json, text/plain, */*",
                "User-Agent": MOBILE_USER_AGENT,
                "Authorization": f"Bearer {token}",
                "x-visitor-id": visitor_id,
                "visitor-id": visitor_id,
                "pincode": pincode,
                "x-pincode": pincode,
                "x-channel": "MOB",
                "clientId": "mweb",
                "Referer": "https://www.jiomart.com/profile/orders",
                "Origin": "https://www.jiomart.com",
            }

            def _extract_shipment(node):
                """Find a canonical shipment id inside an order payload (dict or list)."""
                if isinstance(node, dict):
                    shipments = node.get("shipments") or node.get("bags") or []
                    if shipments and isinstance(shipments[0], dict):
                        return str(shipments[0].get("shipment_id") or shipments[0].get("shipmentId")
                                   or shipments[0].get("id") or "")
                    sid_val = node.get("shipment_id") or node.get("shipmentId") or ""
                    if isinstance(sid_val, str) and sid_val.strip():
                        return sid_val.strip()
                elif isinstance(node, list):
                    for sub in node:
                        found = _extract_shipment(sub)
                        if found:
                            return found
                return ""

            # -- Order already sealed by E2 (signed payment_callback). Removed
            # all alternative confirm paths (myorders deep-scan, cart-drained,
            # confirmed-history fallbacks). Only the canonical per-order
            # endpoint is polled briefly below to capture the shipment id.

            # Short poll for the canonical shipment ID (order already sealed by
            # E2's signed payment_callback).
            for attempt in range(2):
                await asyncio.sleep(1)
                try:
                    order_url = f"https://www.jiomart.com/api/service/application/order/v1.0/orders/{order_id}"
                    async with session.get(order_url, headers=api_headers, timeout=15) as o_resp:
                        if o_resp.status == 200:
                            o_data = await o_resp.json(content_type=None)
                            order_node = o_data.get("order", o_data) if isinstance(o_data, dict) else {}
                            sid = _extract_shipment(order_node)
                            if sid:
                                shipment_id = sid
                                break
                except Exception:
                    pass

            # -- G. (REMOVED) Browser fallback --
            # FULL API MODE ONLY: browser is used ONCE for session sync only.
            # Payment/sealing is 100% pure API (A-F chain above).

            if not sealed and (confirmed_history or cart_drained):
                # Order confirmed on server AND/OR cart drained -> truly placed.
                # fall back to a canonical-style id derived from the order id.
                if not shipment_id:
                    shipment_id = f"{order_id}-01"
                sealed = True

            if not shipment_id:
                shipment_id = "N/A"

            return sealed, order_id, shipment_id, item["amount"]

    results = []
    done_sealed = 0
    BATCH = 5
    for b_start in range(0, len(target_items), BATCH):
        batch = target_items[b_start : b_start + BATCH]
        partial = await asyncio.gather(*[seal_single(it) for it in batch],
                                       return_exceptions=True)
        for r in partial:
            if isinstance(r, tuple) and r[0]:
                done_sealed += 1
        results.extend(partial)
        await send_progress(
            f"⚡ <b>Sealing Payment: {done_sealed}/{len(target_items)} orders confirmed...</b>"
        )

    report = [f"🎉 <b>Final Order Execution Report [{BOT_VERSION}]:</b>\n"]
    success_count = 0
    total_val = 0.0

    for idx, res in enumerate(results, 1):
        if isinstance(res, tuple):
            ok, oid, sid, amt = res
            if ok:
                success_count += 1
                total_val += amt
                report.append(
                    f"✅ <b>Order #{idx}:</b> <code>{oid}</code>\n"
                    f"📦 <b>Shipment ID:</b> <code>{sid}</code> | 💰 ₹{amt:.2f} (Success)\n"
                )
            else:
                report.append(f"❌ <b>Order #{idx}:</b> <code>{oid}</code> (Seal Failed)")

    report.append(f"<b>Total Confirmed Orders: {success_count}/{len(target_items)}</b>")
    report.append(f"<b>Total Amount Paid: ₹{total_val:.2f}</b>")

    for item in target_items:
        pending.pop(item["index"], None)
    SELECTED_PAYMENTS[str(chat_id)].clear()

    await send_safe_html(message, "\n".join(report), reply_markup=get_main_keyboard(chat_id))

# ==================== OD35 PORT: OFFLINE DATABASE ====================
OFFLINE_DB = os.path.join(BASE_DIR, "offline_database.json")
DELIVERY_LOCATION_KEY = "delivery_location"
AWAITING_INPUT = {}        # {chat_id: step} - multi-step text/location input flows
OFFLINE_ADDR_TEMP = {}     # {chat_id: partial address dict}
ORDER_HISTORY_CACHE = {}   # {chat_id: [orders]}
ORDERS_PER_PAGE_OD = 30   # apiready1 style: 30 orders/page
ORDERS_MAX_SHOWN_OD = 100 # apiready1 style: up to 100 total orders

def _normalize_store_id(raw):
    if not raw:
        return ""
    s = str(raw).strip()
    return "" if s in ("259630", "None", "none") else s

def load_offline_db():
    if os.path.exists(OFFLINE_DB):
        try:
            with open(OFFLINE_DB, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_offline_db(data):
    try:
        with open(OFFLINE_DB, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"save_offline_db error: {e}")
        return False

def load_offline_addresses(chat_id):
    return load_offline_db().get(str(chat_id), {}).get("addresses", [])

def save_offline_addresses(chat_id, addr_list):
    db = load_offline_db()
    if str(chat_id) not in db:
        db[str(chat_id)] = {}
    db[str(chat_id)]["addresses"] = addr_list
    save_offline_db(db)

def slug_to_product_name(slug):
    if not slug:
        return "Product"
    parts = str(slug).rsplit("-", 2)
    return (parts[0] if len(parts) > 1 else parts[0]).replace("-", " ").title()

def load_offline_cart(chat_id):
    db = load_offline_db()
    user = db.get(str(chat_id), {})
    if not isinstance(user, dict):
        user = {}
    cart = user.get("cart")
    old_products = user.get("products")
    if not isinstance(cart, dict) and isinstance(old_products, dict):
        cart = {}
        for k, v in old_products.items():
            if isinstance(v, dict):
                cart[k] = {"url": v.get("url", ""), "name": v.get("name") or slug_to_product_name(k),
                           "price": float(v.get("price", 0) or 0), "quantity": int(v.get("quantity", 1) or 1)}
            else:
                cart[k] = {"url": v or "", "name": slug_to_product_name(k), "price": 0.0, "quantity": 1}
        user["cart"] = cart
        user.pop("products", None)
        db[str(chat_id)] = user
        save_offline_db(db)
    return cart if isinstance(cart, dict) else {}

def save_offline_cart(chat_id, cart):
    db = load_offline_db()
    if str(chat_id) not in db:
        db[str(chat_id)] = {}
    db[str(chat_id)]["cart"] = cart
    db[str(chat_id)].pop("products", None)
    save_offline_db(db)

def clear_offline_cart(chat_id):
    save_offline_cart(chat_id, {})

def cart_subtotal(cart):
    total = 0.0
    for item in (cart or {}).values():
        if isinstance(item, dict):
            try:
                total += float(item.get("price", 0) or 0) * int(item.get("quantity", 1) or 1)
            except (TypeError, ValueError):
                pass
    return round(total, 2)

def get_default_offline_address(chat_id):
    addrs = load_offline_addresses(chat_id)
    if not addrs:
        return None
    for a in addrs:
        if isinstance(a, dict) and a.get("is_default"):
            return a
    return addrs[0]

def set_default_offline_address(chat_id, idx):
    addrs = load_offline_addresses(chat_id)
    if not (0 <= idx < len(addrs)):
        return None
    for a in addrs:
        if isinstance(a, dict):
            a.pop("is_default", None)
    if isinstance(addrs[idx], dict):
        addrs[idx]["is_default"] = True
    save_offline_addresses(chat_id, addrs)
    return addrs[idx]

def get_saved_delivery(chat_id: str) -> dict:
    configs = load_configs()
    user_conf = configs.get(str(chat_id), {}) or {}
    loc = user_conf.get(DELIVERY_LOCATION_KEY) or {}
    return loc if isinstance(loc, dict) else {}

def get_resolved_location(chat_id: str) -> dict:
    def_addr = get_default_offline_address(chat_id) or {}
    saved_del = get_saved_delivery(chat_id) or {}
    pin = str(def_addr.get("pincode") or saved_del.get("pincode") or "400001")
    city = str(def_addr.get("city") or saved_del.get("city") or "Mumbai")
    state = str(def_addr.get("state") or saved_del.get("state") or "Maharashtra")
    lat = def_addr.get("latitude") or def_addr.get("lat") or saved_del.get("lat") or saved_del.get("latitude")
    lon = def_addr.get("longitude") or def_addr.get("lon") or saved_del.get("lon") or saved_del.get("longitude")
    try:
        lat_f = float(lat) if lat is not None else 19.0760
    except (TypeError, ValueError):
        lat_f = 19.0760
    try:
        lon_f = float(lon) if lon is not None else 72.8777
    except (TypeError, ValueError):
        lon_f = 72.8777
    return {"pincode": pin, "city": city, "state": state, "latitude": lat_f, "longitude": lon_f}

# ==================== OD35 PORT: LINK CONVERTER & PRODUCT META ====================
def extract_slug_from_url(url):
    if not url:
        return ""
    url = str(url).strip()
    m = re.search(r"(?:product|item|p)(?:/[^/?#]+)*/([a-zA-Z0-9\-]+-[0-9]{5,})", url)
    if m:
        return m.group(1).strip().rstrip("/")
    m2 = re.search(r"([a-zA-Z0-9\-]+-[0-9]{5,})", url)
    if m2:
        return m2.group(1).strip().rstrip("/")
    m3 = re.search(r"/(\d{6,15})(?:[/?#]|$)", url)
    if m3:
        return m3.group(1).strip()
    return ""

async def resolve_offline_short_url(short_url, timeout_ms=15000):
    if not short_url or "/l/" not in str(short_url):
        return short_url, extract_slug_from_url(short_url)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as sess:
            async with sess.get(short_url, headers={"User-Agent": MOBILE_USER_AGENT, "Accept": "text/html,*/*"},
                                allow_redirects=True) as resp:
                html_text = await resp.text()
        for var in ("custom", "alt", "ios_link", "location.href", "window.open"):
            for m in re.finditer(re.escape(var) + r"\s*=\s*['\"]([^'\"]+)['\"]", html_text):
                target = m.group(1)
                if "jiomart" in target and ("/product/" in target or "/p/" in target or "-" in target):
                    return target, extract_slug_from_url(target)
        m_fb = re.search(r"https?://[^'\"]*jiomart[^'\"]*(?:product|p|item)/[^'\"]*", html_text)
        if m_fb:
            return m_fb.group(0), extract_slug_from_url(m_fb.group(0))
    except Exception as e:
        logger.warning(f"Fast short link resolution failed: {e}")
    pw = browser = None
    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = await browser.new_context(viewport=FIXED_VIEWPORT, is_mobile=True, has_touch=True, user_agent=MOBILE_USER_AGENT)
        page = await ctx.new_page()
        await page.goto(short_url, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(3000)
        final_url = page.url
        await browser.close()
        await pw.stop()
        return final_url, extract_slug_from_url(final_url)
    except Exception as e:
        logger.warning(f"Browser fallback resolution error: {e}")
        if browser:
            try: await browser.close()
            except: pass
        if pw:
            try: await pw.stop()
            except: pass
        return short_url, extract_slug_from_url(short_url)

def parse_offline_link(text):
    if not text:
        return None
    raw = str(text).strip()
    quantity = 1
    m_qty = re.search(r"\s+(\d{1,4})\s*$", raw)
    if m_qty:
        try:
            quantity = int(m_qty.group(1))
        except Exception:
            quantity = 1
        raw = raw[:m_qty.start()].strip()
    m_short = re.search(r"/l/([A-Za-z0-9_-]+)", raw)
    short_code = m_short.group(1).strip() if m_short else ""
    slug = extract_slug_from_url(raw)
    target_url = raw
    if not raw.startswith("http"):
        if short_code:
            target_url = f"https://store1.jiomartjcp.com/l/{short_code}"
        elif slug:
            target_url = f"https://www.jiomart.com/product/{slug}"
        else:
            target_url = f"https://{raw}"
    if not slug and not short_code and "jiomart" not in target_url:
        return None
    return {"slug": slug, "url": target_url, "quantity": max(1, quantity), "short_code": short_code}

# Long-lived catalog API token found on disk (any session file). Stock qty is
# NOT user-specific, so any logged-in account's token works for sizes reads.
_FILE_TOKEN_CACHE = {"token": ""}


def find_session_file_token():
    """Scan session files (newest first) for a usable catalog API token.

    The token (base64 user:secret, NOT the eyJ... account JWT) lives in the
    cra_access_token cookie of saved browser sessions (inspect_stock capture,
    2026-08-28). Result is cached - stock reads only, never mixed with the
    active account's cart/checkout auth.
    """
    if _FILE_TOKEN_CACHE.get("token"):
        return _FILE_TOKEN_CACHE["token"]
    try:
        files = [os.path.join(SESSIONS_DIR, f) for f in get_all_session_keys()]
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for path in files:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    state = json.load(f)
            except Exception:
                continue
            for c in state.get("cookies") or []:
                if c.get("name") == "cra_access_token":
                    v = str(c.get("value") or "").strip()
                    if v and not v.startswith("eyJ"):
                        _FILE_TOKEN_CACHE["token"] = v
                        return v
    except Exception as e:
        logger.warning(f"find_session_file_token error: {e}")
    return ""


def get_pure_api_auth(chat_id: str):
    """RAM token/cookies first, then active session file (od35 get_pure_api_auth)."""
    ram = RAM_CACHE.get(str(chat_id)) or {}
    token = ram.get("token") or ""
    cookies = ram.get("cookies") or {}
    if not token or not cookies:
        try:
            _, path = get_active_account(chat_id)
            if path and os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    state = json.load(f)
                if not cookies:
                    cookies = get_cookies_from_state(state)
                if not token and isinstance(state.get("origins"), list):
                    for origin in state["origins"]:
                        if "jiomart.com" in (origin.get("origin") or ""):
                            for item in origin.get("localStorage", []) or []:
                                if item.get("name") in ("token", "access_token", "jwt"):
                                    token = str(item.get("value", "")).replace("Bearer ", "").replace('"', '').strip()
                                    break
                if not token:
                    # Session-file token fallback (inspect_stock capture, 2026-08-28):
                    # the long-lived catalog API token (base64 user:secret) is stored
                    # in the cra_access_token cookie. Skip eyJ... JWT values - those
                    # are account.relianceretail.com tokens and return 401 on catalog.
                    for c in state.get("cookies") or []:
                        if c.get("name") == "cra_access_token":
                            v = str(c.get("value") or "").strip()
                            if v and not v.startswith("eyJ"):
                                token = v
                            break
        except Exception:
            pass
    return token, cookies

async def extract_product_meta(slug: str, token: str, cookies: dict):
    """Resolve catalog item_id + REAL size tokens via the product-detail endpoint."""
    item_id = None
    size_val = "OS"
    name_val = slug.replace("-", " ").title()
    vertical_val = "GROCERIES"
    seller_val = 1
    detail_sizes = []
    m_id = re.search(r"-([0-9]{5,})(?:[/?#]|$)", slug)
    if m_id:
        item_id = int(m_id.group(1))
    elif str(slug).isdigit():
        item_id = int(slug)
    clean_token = token.replace("Bearer ", "").strip() if token else ""
    headers = {
        "User-Agent": MOBILE_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.jiomart.com",
        "Referer": f"https://www.jiomart.com/product/{slug}",
    }
    if clean_token:
        headers["Authorization"] = f"Bearer {clean_token}"
    async with aiohttp.ClientSession(cookies=cookies) as session:
        try:
            detail_url = f"https://www.jiomart.com/api/service/application/catalog/v1.0/products/{slug}"
            async with session.get(detail_url, headers=headers, timeout=8) as resp:
                if resp.status == 200:
                    pd = await resp.json(content_type=None)
                    if isinstance(pd, dict):
                        if not item_id and pd.get("uid") and str(pd.get("uid")).isdigit():
                            item_id = int(pd["uid"])
                        attrs = pd.get("attributes") or {}
                        if isinstance(attrs, dict):
                            for key in ("size", "product-size"):
                                sval = attrs.get(key)
                                if sval:
                                    detail_sizes.append(str(sval))
                            sizes_list = attrs.get("sizes")
                            if isinstance(sizes_list, list):
                                for s in sizes_list:
                                    if s:
                                        detail_sizes.append(str(s))
                            if attrs.get("vertical-code"):
                                vertical_val = str(attrs["vertical-code"])
                            if attrs.get("title") and name_val.lower().startswith("product"):
                                name_val = str(attrs["title"])
                        if pd.get("name") and name_val.lower().startswith("product"):
                            name_val = str(pd["name"])
        except Exception:
            pass
    base = list(dict.fromkeys([s for s in detail_sizes if s]))
    primary = base[0] if base else size_val
    variants = list(dict.fromkeys(base + ["OS"])) if base else ["OS"]
    return {"item_id": item_id, "item_size": primary, "name": name_val, "vertical": vertical_val,
            "seller_id": seller_val, "size_variants": variants, "price": 0.0}

async def fetch_offline_item_details(slug, url, quantity=1, token="", cookies=None, pincode="400001", lat=None, lon=None):
    slug = (str(slug) or "").strip()
    if not slug:
        slug = extract_slug_from_url(url)
    name = slug_to_product_name(slug)
    try:
        qty = int(quantity)
    except (TypeError, ValueError):
        qty = 1
    return {"key": slug, "name": name, "url": url, "price": 0.0, "quantity": max(1, qty)}

# ==================== OD35 PORT: TELEGRAM UI KEYBOARDS ====================
def get_main_keyboard(chat_id: str):
    count = get_user_count(chat_id)
    ram = RAM_CACHE.get(str(chat_id), {})
    has_sync = bool(ram.get("token") and ram.get("address_id"))

    kb = [
        [
            InlineKeyboardButton("👤 View Profile", callback_data="action_view_profile"),
            InlineKeyboardButton(f"🔄 2-Step Sync{' ✅' if has_sync else ''}", callback_data="action_2step_sync"),
        ],
        [
            InlineKeyboardButton("➖ 1", callback_data="count_dec"),
            InlineKeyboardButton(f"🔢 Count: {count}", callback_data="open_count_dropdown"),
            InlineKeyboardButton("➕ 1", callback_data="count_inc"),
        ],
        [
            InlineKeyboardButton(f"📥 Read All ({count}x API)", callback_data="action_read_all_api"),
            InlineKeyboardButton("🚀 Fire All (Payment Page)", callback_data="action_fire_to_payment"),
        ],
        [
            InlineKeyboardButton("🔑 Switch Session", callback_data="open_session_manager"),
            InlineKeyboardButton("👤 My Keys", callback_data="my_keys"),
            InlineKeyboardButton("🧹 Clear RAM", callback_data="action_clear_ram"),
        ],
        [
            InlineKeyboardButton("💾 Offline Database", callback_data="off_db_menu"),
            InlineKeyboardButton("📦 Order History", callback_data="action_order_history"),
        ],
        [
            InlineKeyboardButton("🛒 Cart", callback_data="cart_view"),
            InlineKeyboardButton("🔍 Search Products", callback_data="action_search_prod"),
        ],
        [
            InlineKeyboardButton("🛑 Force Stop All", callback_data="force_stop_all"),
        ],
    ]
    if is_admin(chat_id):
        kb.append([InlineKeyboardButton("🛡️ Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(kb)

def get_dropdown_keyboard():
    kb = []
    row = []
    for c in PRESET_COUNTS:
        row.append(InlineKeyboardButton(f"{c}", callback_data=f"set_count_{c}"))
        if len(row) == 3:
            kb.append(row)
            row = []
    if row: kb.append(row)
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="back_home")])
    return InlineKeyboardMarkup(kb)

# ==================== BOT UPTIME + AUTO-RESTART COUNTDOWN (IST) ====================
IST_TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30))   # Indian Standard Time
BOT_START_UTC = datetime.datetime.now(datetime.timezone.utc)          # bot process start (UTC)
RUN_TIMEOUT_MIN = 355        # same as workflow timeout-minutes (GitHub hard cap = 6h)
CRON_FALLBACK_UTC_HOUR = 2   # daily cron FALLBACK at 02:00 UTC (07:30 IST) - chain mode does the rest
CHAIN_AFTER_MIN = 330        # 5h30m -> bot renews itself cleanly (chain mode, ~2 min gap)
APP_REF = None               # current Application (live timer always uses the active one)
LIVE_TASK_STARTED = False
HOME_LIVE_MSGS = {}          # {chat_id: [message_id, last_refresh_ts]} - live countdown targets

def _next_restart_utc():
    """Earliest restart moment: daily cron fallback (02:00 UTC) OR chain (+5h30m) OR timeout."""
    now = datetime.datetime.now(datetime.timezone.utc)
    cron_next = now.replace(hour=CRON_FALLBACK_UTC_HOUR, minute=0, second=0, microsecond=0)
    if cron_next <= now:
        cron_next += datetime.timedelta(days=1)
    chain_next = BOT_START_UTC + datetime.timedelta(minutes=CHAIN_AFTER_MIN)
    timeout_next = BOT_START_UTC + datetime.timedelta(minutes=RUN_TIMEOUT_MIN)
    return min(cron_next, chain_next, timeout_next), cron_next

def _fmt_hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"

def build_uptime_block() -> str:
    """Home page block: bot start date/time (IST) + live auto-restart countdown."""
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    restart_at, cron_next = _next_restart_utc()
    left = (restart_at - now_utc).total_seconds()
    started_ist = BOT_START_UTC.astimezone(IST_TZ).strftime("%d-%m-%Y %H:%M:%S")
    restart_ist = restart_at.astimezone(IST_TZ).strftime("%H:%M:%S")
    note = " <i>(GitHub cron may delay +1-2h)</i>" if restart_at == cron_next else ""
    return (
        f"⏰ <b>Bot Started :</b> <code>{started_ist} IST</code>\n"
        f"⏳ <b>Restart In  :</b> <b>{_fmt_hms(left)}</b> (at {restart_ist} IST){note}\n"
        f"💡 <i>Finish orders before the auto-restart (data auto-saves).</i>\n"
    )

async def home_live_timer():
    """LIVE countdown: refresh every open home page every 30s so the remaining-time
    timer ticks in real time (auto-stops after 30 min of no refresh)."""
    global APP_REF
    while True:
        await asyncio.sleep(30)
        app = APP_REF
        if app is None:
            continue
        now_ts = time.time()
        for chat_id, entry in list(HOME_LIVE_MSGS.items()):
            if now_ts - entry[1] > 1800:   # home page idle > 30 min -> stop editing
                HOME_LIVE_MSGS.pop(chat_id, None)
                continue
            try:
                await app.bot.edit_message_text(
                    build_dashboard(chat_id), chat_id=chat_id, message_id=entry[0],
                    reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")
            except Exception:
                pass   # "message is not modified" / deleted -> ignore

async def dispatch_new_run():
    """Dispatch a fresh GitHub Actions run (used by admin Restart button AND chain mode)."""
    tok = (os.environ.get("GITHUB_TOKEN") or "").strip()
    repo = (os.environ.get("GH_REPO") or "").strip()
    if not tok or not repo:
        return False, "GITHUB_TOKEN / GH_REPO missing — running on PC? Restart manually."
    ok, body = False, ""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"https://api.github.com/repos/{repo}/actions/workflows/bot_public.yml/dispatches",
                headers={"Authorization": f"Bearer {tok}",
                         "Accept": "application/vnd.github+json",
                         "X-GitHub-Api-Version": "2022-11-28"},
                json={"ref": "main"},
                timeout=aiohttp.ClientTimeout(total=20)) as resp:
                ok = (resp.status == 204)
                body = await resp.text()
    except Exception as e:
        body = str(e)
    return ok, body

CHAIN_FAIL_COUNT = 0         # consecutive chain-dispatch failures

async def auto_chain_timer():
    """CHAIN MODE: at 5h30m uptime the bot renews itself cleanly (fresh run + backup),
    keeping the bot online ~24h/day with only ~2-min maintenance gaps."""
    global CHAIN_FAIL_COUNT
    while True:
        await asyncio.sleep(60)
        try:
            uptime_min = (datetime.datetime.now(datetime.timezone.utc) - BOT_START_UTC).total_seconds() / 60
            if uptime_min < CHAIN_AFTER_MIN:
                continue
            logger.info(f"[CHAIN] {CHAIN_AFTER_MIN}m reached - dispatching fresh run (chain restart)")
            ok, body = await dispatch_new_run()
            if ok:
                await asyncio.sleep(2)
                os._exit(0)   # clean exit -> backup steps still run; new run already starting
            CHAIN_FAIL_COUNT += 1
            logger.warning(f"[CHAIN] dispatch failed ({CHAIN_FAIL_COUNT}x) - retry in 60s: {str(body)[:150]}")
            if CHAIN_FAIL_COUNT == 3 and APP_REF is not None:
                deadline = (BOT_START_UTC + datetime.timedelta(minutes=RUN_TIMEOUT_MIN)).astimezone(IST_TZ).strftime("%d-%m-%Y %H:%M IST")
                for adm in get_admin_ids():
                    try:
                        await APP_REF.bot.send_message(
                            chat_id=adm,
                            text=("⚠️ <b>Chain restart FAILED (3x)!</b>\n"
                                  f"Bot will be force-stopped at <code>{deadline}</code>.\n"
                                  "Please start a new run from GitHub Actions or press ♻️ Restart Bot again."),
                            parse_mode="HTML")
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"[CHAIN] error: {e}")

async def bot_post_init(app):
    """PTB post_init: keep APP_REF current + start live-timer & chain tasks once."""
    global APP_REF, LIVE_TASK_STARTED
    APP_REF = app
    if not LIVE_TASK_STARTED:
        LIVE_TASK_STARTED = True
        loop = asyncio.get_running_loop()
        loop.create_task(home_live_timer())
        loop.create_task(auto_chain_timer())
        logger.info("[LIVE] countdown + chain-mode tasks started")

def build_dashboard(chat_id: str) -> str:
    ram = RAM_CACHE.get(str(chat_id), {})
    active_key, _ = get_active_account(chat_id)
    count = get_user_count(chat_id)
    token_str = (ram.get("token")[:16] + "...") if ram.get("token") else "NOT SYNCED"

    # ---- Home billing block: always show offline cart + total billing amount ----
    try:
        off_cart = load_offline_cart(chat_id) or {}
        off_qty = int(sum(int((x.get('quantity') or 1)) for x in off_cart.values() if isinstance(x, dict)) or 0)
        off_sub = cart_subtotal(off_cart)
        billing_block = (
            f"\n🛍️ <b>Offline Cart Billing:</b>\n"
            f"📦 <b>Items      :</b> {len(off_cart)} item(s) | Qty {off_qty}\n"
            f"💰 <b>Subtotal    :</b> <b>₹{off_sub:.2f}</b>\n"
            f"⚡ <b>Live Price  :</b> ₹{ram.get('cart_price', '0.00')}"
        )
    except Exception:
        billing_block = ""

    return (
        f"🤖 <b>JioMart Multi-Session Test Engine [{BOT_VERSION}]</b>\n\n"
        f"📁 <b>Active Session :</b> <code>{active_key or 'None'}</code>\n"
        f"🔢 <b>Batch Count    :</b> <b>{count}</b>\n"
        f"🎫 <b>Token          :</b> <code>{token_str}</code>\n"
        f"📍 <b>Address ID     :</b> <code>{ram.get('address_id', 'N/A')}</code>\n"
        f"🛒 <b>Cart ID        :</b> <code>{ram.get('cart_id', 'N/A')}</code>\n"
        f"💰 <b>Live Price     :</b> <code>₹{ram.get('cart_price', '0.00')}</code>\n\n"
        f"{billing_block}\n\n"
        f"{build_uptime_block()}"
        f"<i>Select an action below:</i>"
    )

# ==================== HANDLERS & ROUTER ====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    msg_text = build_dashboard(chat_id)
    sent = None
    try:
        if update.message:
            sent = await update.message.reply_text(msg_text, reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")
        elif update.callback_query:
            sent = await update.callback_query.edit_message_text(msg_text, reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")
    except Exception:
        pass
    if sent is not None and getattr(sent, "message_id", None):
        HOME_LIVE_MSGS[chat_id] = [sent.message_id, time.time()]   # register live countdown target

# ==================== SEARCH & ADD TO CART (CATALOG API) ====================
PRODUCT_SEARCH_CACHE = {}  # {chat_id: {uid: {uid, slug, name, sp, mrp, size, brand}}} - last catalog search

def build_quick_add_callback(uid: str, slug: str) -> str:
    """quick_add_{uid}_{slug} callback - capped to Telegram's 64-byte callback_data limit."""
    base = f"quick_add_{uid}"
    room = 64 - len(base) - 1
    return f"{base}_{(slug or '')[:max(0, room)]}"

# ---- Search-session tracking & offline-cart controls (search flow) ----
SEARCH_RESULT_MSGS = {}   # {chat_id: [message_id, ...]} - product cards for the active search
SEARCH_SUMMARY_MSG = {}   # {chat_id: message_id} - summary card holding Complete/Cancel
SEARCH_CARD_UIDS = {}     # {chat_id: {message_id: uid}} - which product each search card shows
SEARCH_KEYWORD = {}       # {chat_id: last search keyword} - for the search summary card


async def refresh_search_cards(context, chat_id, skip_mid=None):
    """Re-render every open search-result card with FRESH index-based qty controls
    after any offline-cart change (indices shift when items are added/removed).
    This is what keeps the search page's [- qty + 🗑] buttons working correctly."""
    chat = str(chat_id)
    umap = SEARCH_CARD_UIDS.get(chat) or {}
    if not umap:
        return
    for mid, uid in list(umap.items()):
        if skip_mid is not None and mid == skip_mid:
            continue
        prod = (PRODUCT_SEARCH_CACHE.get(chat) or {}).get(uid) or {}
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=chat, message_id=mid,
                reply_markup=InlineKeyboardMarkup(product_cart_markup(chat_id, uid, prod)))
        except Exception:
            pass

# ---- Search history (persisted in offline DB, deletable) ----
def get_search_history(chat_id):
    return load_offline_db().get(str(chat_id), {}).get("search_history", [])

def save_search_history(chat_id, hist):
    db = load_offline_db()
    if str(chat_id) not in db:
        db[str(chat_id)] = {}
    db[str(chat_id)]["search_history"] = (hist or [])[:20]
    save_offline_db(db)

def add_search_history(chat_id, keyword):
    kw = (keyword or "").strip()[:60]
    if not kw:
        return
    hist = get_search_history(chat_id)
    hist = [k for k in hist if k != kw]  # dedupe, most-recent-first
    hist.insert(0, kw)
    save_search_history(chat_id, hist)

def _entry_from_product(uid, prod, quantity=1):
    """Build an offline-cart entry dict from a catalog search result."""
    return {
        "uid": str(uid),
        "name": str(prod.get("name") or "Product"),
        "slug": str(prod.get("slug") or ""),
        "price": float(prod.get("sp") or 0),
        "mrp": float(prod.get("mrp") or 0),
        "quantity": max(1, int(quantity or 1)),
        "size": str(prod.get("size") or "OS"),
        "pack": str(prod.get("pack") or ""),
        "seller_id": int(prod.get("seller_id") or 1),
        "vertical": str(prod.get("vertical") or "GROCERIES"),
        "size_variants": prod.get("size_variants"),
        "max_qty": int(prod.get("max_qty") or 0),
        "sellable": bool(prod.get("sellable")),
    }

def _cap_qty(entry, new_qty):
    """Cap quantity by the item's store max-qty-in-order (default cap 10)."""
    try:
        cap = int((entry or {}).get("max_qty") or 0)
    except (TypeError, ValueError):
        cap = 0
    new_qty = max(1, int(new_qty or 1))
    return min(new_qty, cap) if cap > 0 else min(10, new_qty)

def product_cart_markup(chat_id, uid, prod=None):
    """Search-result card controls - SAME method as the Cart page: index-based
    [➖ qty ➕ 🗑] (ocdec_/ocinc_/ocrem_) when the item is in the offline cart,
    otherwise the Add-to-Cart button (uid+slug callback).
    Includes a 🛒 View Cart button to jump to the full cart page."""
    uid = str(uid)
    prod = prod or {}
    cart = load_offline_cart(chat_id)
    it = cart.get(uid)
    if not it or not isinstance(it, dict):
        sp = float(prod.get("sp") or 0)
        return [[InlineKeyboardButton(f"🛒 Add to Cart (₹{sp:g})",
                                      callback_data=build_quick_add_callback(uid, str(prod.get("slug") or "")))],
                [InlineKeyboardButton("🛒 View Cart", callback_data="cart_view")]]
    qty = int(it.get("quantity", 1) or 1)
    j = list(cart.keys()).index(uid)
    return [[
        InlineKeyboardButton("➖", callback_data=f"ocdec_{j}"),
        InlineKeyboardButton(f"{qty}", callback_data="prod_noop"),
        InlineKeyboardButton("➕", callback_data=f"ocinc_{j}"),
        InlineKeyboardButton("🗑", callback_data=f"ocrem_{j}"),
    ], [InlineKeyboardButton("🛒 View Cart", callback_data="cart_view")]]

def cart_control_rows(chat_id):
    """Per-item [➖ qty ➕ 🗑] rows - index-based oc* callbacks (apiready1 formula).
    Shared by the Cart page AND the search-product page summary."""
    rows = []
    cart = load_offline_cart(chat_id)
    for j, (uid, it) in enumerate(cart.items()):
        if not isinstance(it, dict):
            continue
        qty = int(it.get("quantity", 1) or 1)
        rows.append([
            InlineKeyboardButton("➖", callback_data=f"ocdec_{j}"),
            InlineKeyboardButton(f"{qty}", callback_data="prod_noop"),
            InlineKeyboardButton("➕", callback_data=f"ocinc_{j}"),
            InlineKeyboardButton("🗑", callback_data=f"ocrem_{j}"),
        ])
    return rows

def offcart_markup(chat_id, page=0):
    """Offline-cart view: FULL apiready1-style UI with pagination, mode toggle,
    coupon, address, sync buttons, and per-item qty controls."""
    mode = get_cart_mode(chat_id)
    cart = load_offline_cart(chat_id)
    cart_names = list(cart.keys())
    # Pagination: 15 items per page
    per_page = 15
    total_pages = max(1, (len(cart_names) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = start + per_page
    visible_keys = cart_names[start:end]

    mode_text = "⚡ Mode: ONLINE (live sync)" if mode == "ONLINE" else "📁 Mode: OFFLINE"
    kb = []
    # Mode toggle + Add Product
    kb.append([InlineKeyboardButton(mode_text, callback_data="cart_toggle_mode"),
               InlineKeyboardButton("➕ Add Product", callback_data="off_add_prod")])
    # Per-item qty controls (index-based, visible page only)
    for j, uid in enumerate(visible_keys, start=start):
        it = cart.get(uid)
        if not isinstance(it, dict):
            continue
        qty = int(it.get("quantity", 1) or 1)
        nm = str(it.get("name") or uid)[:20]
        kb.append([
            InlineKeyboardButton("➖", callback_data=f"ocdec_{j}"),
            InlineKeyboardButton(f"{nm}", callback_data="prod_noop"),
            InlineKeyboardButton(f"{qty}", callback_data="prod_noop"),
            InlineKeyboardButton("➕", callback_data=f"ocinc_{j}"),
            InlineKeyboardButton("🗑", callback_data=f"ocrem_{j}"),
        ])
    # Pagination navigation
    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"cartpage_{page-1}"))
        nav_row.append(InlineKeyboardButton(f"📄 {page+1}/{total_pages}", callback_data="prod_noop"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"cartpage_{page+1}"))
        kb.append(nav_row)
    # Coupon + Address (PRESET COUPON: set once -> auto-applied every time)
    pc_code = get_preset_coupon(chat_id)
    pc_label = f"🎟️ Apply Coupon ({pc_code})" if pc_code else "🎟️ Apply Coupon"
    kb.append([InlineKeyboardButton(pc_label, callback_data="cart_apply_coupon"),
               InlineKeyboardButton("✏️ Set Preset", callback_data="cart_set_preset_coupon")])
    kb.append([InlineKeyboardButton("❌ Remove Coupon", callback_data="cart_remove_coupon")])
    kb.append([InlineKeyboardButton("📥 Read Online Cart", callback_data="cart_read_online"),
               InlineKeyboardButton("🏠 Address", callback_data="addr_view")])
    # Inject + Search
    kb.append([InlineKeyboardButton("🧩 Inject Cart (API)", callback_data="action_inject_cart"),
               InlineKeyboardButton("🔍 Search Products", callback_data="action_search_prod")])
    # Sync buttons
    kb.append([InlineKeyboardButton("🔄 Offline → Online Sync", callback_data="cart_sync_off_to_on"),
               InlineKeyboardButton("📥 Online → Offline Sync", callback_data="cart_sync_on_to_off")])
    # Empty/Clear
    kb.append([InlineKeyboardButton("🧹 Empty Cart", callback_data="cart_empty_cart"),
               InlineKeyboardButton("🗑 Clear Cart", callback_data="off_clear_cart")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="off_db_menu")])
    return kb


def build_online_cart_view(chat_id):
    """apiready1-style LIVE online cart view: item name + qty controls per row."""
    ram = RAM_CACHE.get(str(chat_id)) or {}
    view = ONLINE_CART_VIEW.get(str(chat_id)) or {}
    items = view.get("items") or []
    if not items:
        text = ("🌐 <b>Online Cart (LIVE)</b>\n\n"
                "🛒 Online cart is <b>EMPTY</b>.\n"
                f"💰 RAM Cart Price: ₹{ram.get('cart_price') or '0.00'}")
        kb = [[InlineKeyboardButton("🔄 Refresh", callback_data="cart_read_online")],
              [InlineKeyboardButton("🔙 Back to Cart", callback_data="cart_view")]]
        return text, InlineKeyboardMarkup(kb)
    lines = [f"🌐 <b>Online Cart (LIVE)</b> — {len(items)} item(s) | Cart ID: <code>{view.get('cart_id') or 'N/A'}</code>", ""]
    kb = []
    for i, itm in enumerate(items):
        nm = str(itm.get("name") or f"Item {itm.get('item_id')}")[:34]
        q = int(itm.get("quantity", 1) or 1)
        lines.append(f"{i + 1}. {html.escape(nm)} — <b>{q} x</b>")
        kb.append([InlineKeyboardButton("➖", callback_data=f"ondec_{i}"),
                   InlineKeyboardButton(f"{q}", callback_data="onl_noop"),
                   InlineKeyboardButton("➕", callback_data=f"oninc_{i}"),
                                       InlineKeyboardButton("🗑", callback_data=f"ondel_{i}")])
        lines.append("")
    # 🚚 Delivery charge display
    # PROVEN (live test 2026-08-30): JioMart hides the fee in total_charge
    # (delivery_charge stays 0) and charges ₹30 below ₹399 EVEN in manual
    # browser QC orders at pin 751010 (browser trace: ₹181 + ₹30 = ₹211).
    dfee = view.get("dfee") or {}
    dc = float(dfee.get("delivery_charge") or 0)
    sub = float(dfee.get("subtotal") or 0)
    lines.append("🚚 Journey: <b>QuickCommerce (QC)</b>"
                 if (ram.get("polygon_id") or "").lower().startswith(("te", "fr")) else "🚚 Journey: <b>Standard</b>")
    if dc > 0:
        lines.append(f"🚚 Delivery/Handling Fee: <b>₹{dc:g}</b>")
        if sub < FREE_DELIVERY_THRESHOLD:
            need = FREE_DELIVERY_THRESHOLD - sub
            lines.append(f"⚠️ Server-mandatory below ₹{FREE_DELIVERY_THRESHOLD} "
                         f"(manual browser ମଧ୍ୟ pay କରେ)।")
            lines.append(f"ℹ️ ₹{need:g} more add କଲେ (₹{FREE_DELIVERY_THRESHOLD}+) fee = ₹0!")
        if dfee.get("info"):
            lines.append(f"ℹ️ {html.escape(str(dfee['info'])[:80])}")
    elif dfee:
        lines.append("🎁 Delivery Fee: <b>FREE</b>")
    lines.append(f"💰 Live Cart Price: <b>₹{ram.get('cart_price') or '0.00'}</b>")
    kb.append([InlineKeyboardButton("🔄 Refresh", callback_data="cart_read_online"),
               InlineKeyboardButton("🧹 Delete All", callback_data="cart_empty_cart")])
    kb.append([InlineKeyboardButton("🔙 Back to Cart", callback_data="cart_view")])
    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def rerender_online_cart(query, chat_id):
    """Re-render the live online-cart view after a qty change/delete."""
    items, cart_id = await api_get_live_cart_items(chat_id)
    dfee = await api_read_delivery_fee(chat_id)
    ONLINE_CART_VIEW[str(chat_id)] = {"items": items, "cart_id": cart_id,
                                      "dfee": dfee,
                                      "mid": query.message.message_id}
    text, kb = build_online_cart_view(chat_id)
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass


def build_address_view(chat_id):
    """Cart-page 🏠 Address menu: offline address list (click = inject via API)."""
    addrs = load_offline_addresses(chat_id)
    def_idx = get_default_offline_address(chat_id)
    d_idx = None
    if def_idx:
        try:
            d_idx = load_offline_addresses(chat_id).index(def_idx)
        except ValueError:
            d_idx = None
    lines = [f"🏠 <b>Address Manager</b> — {len(addrs)} offline address(es)", ""]
    kb = []
    for i, a in enumerate(addrs):
        nm = str(a.get("name") or "Address")[:24]
        pin = str(a.get("pincode") or "")
        star = " ⭐" if i == d_idx else ""
        lines.append(f"{i + 1}. {html.escape(nm)} ({pin}){star}")
        kb.append([InlineKeyboardButton(f"📍 Inject to Account: {nm} ({pin}){star}",
                                        callback_data=f"injaddr_{i}")])
    if not addrs:
        lines.append("ℹ️ No offline addresses saved yet.")
    kb.append([InlineKeyboardButton("➕ Add Address", callback_data="addr_add_mode"),
               InlineKeyboardButton("🗑 Delete Address", callback_data="addr_del_view")])
    kb.append([InlineKeyboardButton("🏠 Inject Default Address", callback_data="action_inject_address")])
    kb.append([InlineKeyboardButton("🔙 Back to Cart", callback_data="cart_view")])
    return "\n".join(lines), InlineKeyboardMarkup(kb)

def build_search_summary_card(chat_id, keyword):
    """Search-product page summary card: heading + FULL billing text + live cart
    control buttons + [✅ Complete Search] [❌ Cancel Search] [🕘 History]."""
    chat = str(chat_id)
    cart = load_offline_cart(chat_id)
    sub = cart_subtotal(cart)
    head = (f"🔍 <b>Search:</b> <code>{html.escape(str(keyword) or '-')}</code>\n"
            f"🛍️ <b>Offline Cart:</b> {len(cart)} item(s) • <b>₹{sub:.2f}</b>\n"
            f"👆 Tap <b>🛒 Add to Cart</b> on any result → controls appear below.")
    ram = RAM_CACHE.get(chat) or {}
    if not ram.get("token"):
        head += "\n⚠️ No synced session in RAM — run <b>🔄 2-Step Sync</b> before Inject."
    text = head + "\n\n" + build_billing_summary(chat_id)
    kb = []
    kb.extend(cart_control_rows(chat_id))
    kb.append([InlineKeyboardButton("✅ Complete Search", callback_data="done_search"),
               InlineKeyboardButton("❌ Cancel Search", callback_data="cancel_search")])
    kb.append([InlineKeyboardButton("🕘 Search History", callback_data="search_hist")])
    return text, InlineKeyboardMarkup(kb)

async def run_product_search(chat_id, keyword, source_msg, edit_source=True):
    """Search JioMart, post one card per result, then a summary card with
    [🛒 View Cart] [✅ Complete Search] [❌ Cancel] [🕘 History]. Records result
    card ids in SEARCH_RESULT_MSGS so Complete Search closes them all."""
    chat = str(chat_id)
    # Re-search: delete any previous result cards + summary for this chat first.
    try:
        bot = source_msg.get_bot()
        for mid in SEARCH_RESULT_MSGS.pop(chat, []):
            try:
                await bot.delete_message(chat, mid)
            except Exception:
                pass
        ssum = SEARCH_SUMMARY_MSG.pop(chat, None)
        if ssum:
            try:
                await bot.delete_message(chat, ssum)
            except Exception:
                pass
        umap = SEARCH_CARD_UIDS.get(chat) or {}
        for mid in list(umap.keys()):
            umap.pop(mid, None)   # stale mappings for deleted cards
    except Exception:
        pass
    ram = RAM_CACHE.get(chat) or {}
    token0 = ram.get("token") or ""
    cookies0 = ram.get("cookies") or {}
    pin0 = str(ram.get("pincode") or (get_resolved_location(chat_id) or {}).get("pincode") or "754004")
    if not token0:
        # NO-SYNC stock fix (2026-08-28): RAM empty after restart, but the saved
        # session file carries the long-lived catalog token (cra_access_token
        # cookie) -> pull token + cookies from disk so search can show REAL
        # store stock qty WITHOUT a 2-Step Sync.
        try:
            ftok, fck = get_pure_api_auth(chat_id)
            if ftok:
                token0 = ftok
                if fck and not cookies0:
                    cookies0 = fck
                logger.info("[Search] no-sync token fallback from session file -> real stock qty enabled")
        except Exception as _fe:
            logger.warning(f"[Search] token fallback failed: {_fe}")
        if not token0:
            # Last resort: any saved session's catalog token (stock qty is NOT
            # user-specific) - used ONLY for the sizes stock reads below.
            try:
                atok = find_session_file_token()
                if atok:
                    token0 = atok
                    logger.info("[Search] no-sync ANY-session token fallback -> real stock qty enabled")
            except Exception as _ae:
                logger.warning(f"[Search] any-token fallback failed: {_ae}")
    if not edit_source:
        try:
            await source_msg.reply_text(f"🔎 <b>Searching JioMart for:</b> <code>{html.escape(keyword)}</code> ...",
                                        parse_mode="HTML")
        except Exception:
            pass
    results = await search_jiomart_catalog(keyword, pincode=pin0, token=token0,
                                           visitor_id=ram.get("visitor_id") or "")
    if not results:
        kb = [[InlineKeyboardButton("🔙 Main Menu", callback_data="back_home")],
              [InlineKeyboardButton("🕘 Search History", callback_data="search_hist")]]
        await source_msg.reply_text("❌ <b>No products found!</b> Try another keyword.",
                                    reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
        return None
    add_search_history(chat_id, keyword)
    cache0 = PRODUCT_SEARCH_CACHE.setdefault(chat, {})
    # ---- STORE STOCK read (vertex deliverable API, no token needed) ----
    try:
        codes = [str(r.get("sku_code")) for r in results if r.get("sku_code")]
        stock_map = await api_check_store_stock(chat_id, codes, pin0)
        for r in results:
            st = stock_map.get(str(r.get("sku_code") or ""))
            if st:
                r["stock"] = st
    except Exception as e:
        logger.warning(f"store stock check skipped: {e}")
    # ---- REAL stock quantity (catalog v2 sizes, needs Bearer token) ----
    if token0:
        try:
            for r in results:
                qty_info = await api_get_store_stock_qty(chat_id, str(r.get("slug") or ""),
                                                         token=token0,
                                                         cookies=cookies0 or (RAM_CACHE.get(str(chat_id)) or {}).get("cookies") or {},
                                                         pincode=pin0)
                if qty_info:
                    r.setdefault("stock", {})["quantity"] = qty_info["quantity"]
                    r["stock"]["sellable"] = qty_info["sellable"]
                    r["stock"]["size_items"] = qty_info.get("sizes") or []
                    if qty_info["quantity"] <= 0:
                        r["stock"]["sellable"] = False
        except Exception as e:
            logger.warning(f"stock qty merge skipped: {e}")
    mids = []
    sent = 0
    for r in results:
        uid = str(r.get("uid") or "")
        if not uid:
            continue
        cache0[uid] = r
        try:
            sp_v = float(r.get("sp") or 0)
        except (TypeError, ValueError):
            sp_v = 0.0
        try:
            mrp_v = float(r.get("mrp") or 0)
        except (TypeError, ValueError):
            mrp_v = 0.0
        off_txt = (f"  <s>₹{mrp_v:g}</s> ({int(round((mrp_v - sp_v) / mrp_v * 100))}% off)"
                   if mrp_v > sp_v > 0 else "")
        lines = [f"🛒 <b>{html.escape(str(r.get('name') or 'Product'))}</b>"]
        if r.get("brand"):
            lines.append(f"🏷 Brand: {html.escape(str(r['brand']))}")
        lines.append(f"📦 Pack: {html.escape(str(r.get('pack') or r.get('size') or 'OS'))}")
        lines.append(f"💰 Selling Price: ₹{sp_v:g}{off_txt}  |  MRP: ₹{mrp_v:g}")
        lines.append(stock_status_line(r))
        lines.append(f"🔖 UID: <code>{uid}</code>")
        if r.get("slug"):
            lines.append(f"🔗 <code>{html.escape(str(r['slug']))}</code>")
        try:
            card = await source_msg.reply_text(
                "\n".join(lines),
                reply_markup=InlineKeyboardMarkup(product_cart_markup(chat_id, uid, r)),
                parse_mode="HTML")
            mids.append(card.message_id)
            SEARCH_CARD_UIDS.setdefault(chat, {})[card.message_id] = uid
            sent += 1
            await asyncio.sleep(0.35)
        except Exception as e:
            logger.error(f"product result send error: {e}")
    if not sent:
        kb = [[InlineKeyboardButton("❌ Cancel Search", callback_data="cancel_search")]]
        if hasattr(source_msg, "edit_text"):
            try:
                await source_msg.edit_text("❌ <b>No products found!</b> Try another keyword.",
                                           reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
                return
            except Exception:
                pass
        await source_msg.reply_text("❌ <b>No products found!</b> Try another keyword.",
                                    reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
        return
    SEARCH_RESULT_MSGS.setdefault(chat, []).extend(mids)
    SEARCH_KEYWORD[chat] = keyword
    sum_text, sum_kb = build_search_summary_card(chat_id, keyword)
    summ = None
    if edit_source and hasattr(source_msg, "edit_text"):
        try:
            summ = await source_msg.edit_text(sum_text, reply_markup=sum_kb, parse_mode="HTML")
        except Exception:
            summ = await source_msg.reply_text(sum_text, reply_markup=sum_kb, parse_mode="HTML")
    else:
        summ = await source_msg.reply_text(sum_text, reply_markup=sum_kb, parse_mode="HTML")
    if summ is not None:
        SEARCH_SUMMARY_MSG[chat] = summ.message_id
    return summ

async def search_jiomart_catalog(query: str, pincode: str = "754004", token: str = "",
                                 visitor_id: str = ""):
    """Fetch search results from JioMart catalog API.

    LIVE-VERIFIED (2026-08-28, browser capture): the API reads the pincode from
    the x-location-detail JSON header (NOT pincode/x-pincode) and needs a
    visitor-id; Authorization is optional. Real item shape:
      price.effective/marked = {"min": x, "max": y}; brand = {"name": ...};
      sizes = ["OS"] (real item_size for add_items); net-quantity-value/unit =
      pack size; seller_id top-level; attributes.vertical-code = GROCERIES.
    """
    url = f"https://www.jiomart.com/api/service/application/catalog/v1.0/products?q={query}&page_size=5&page_no=1"
    loc_detail = json.dumps({"country": "INDIA", "country_iso_code": "IN", "pincode": str(pincode)})
    headers = {
        "User-Agent": MOBILE_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.jiomart.com",
        "Referer": "https://www.jiomart.com/",
        "pincode": str(pincode),
        "x-pincode": str(pincode),
        "x-location-detail": loc_detail,
        "visitor-id": visitor_id or str(uuid.uuid4()),
    }
    if token:
        headers["Authorization"] = f"Bearer {token.replace('Bearer ', '').strip()}"

    def _num(val):
        """price.effective/marked can be {'min': x, 'max': y} or a plain number."""
        if isinstance(val, dict):
            val = val.get("min", val.get("max", 0))
        try:
            return float(val or 0)
        except (TypeError, ValueError):
            return 0.0

    def _brand(b):
        if isinstance(b, dict):
            return str(b.get("name") or "").strip().title()
        return str(b or "").strip().title()

    def _pack(itm):
        val = itm.get("net-quantity-value")
        unit = itm.get("net-quantity-unit") or ""
        if not val:
            return ""
        s = str(val).strip()
        if unit and unit.lower() not in s.lower():
            s = f"{s} {unit}".strip()
        return s

    results = []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    d_body = data.get("data") or {}
                    items = data.get("items") or d_body.get("items") or []
                    for itm in items[:5]:
                        if not isinstance(itm, dict):
                            continue
                        uid = itm.get("uid") or itm.get("id")
                        slug = itm.get("slug") or ""
                        name = itm.get("name") or "Product"
                        price_obj = itm.get("price") or {}
                        sp = _num(price_obj.get("effective"))
                        mrp = _num(price_obj.get("marked"))
                        attrs = itm.get("attributes") or {}
                        sizes = [str(x) for x in (itm.get("sizes") or []) if x]
                        size = sizes[0] if sizes else (attrs.get("size") or "OS")
                        iv = itm.get("instock_variants") or {}
                        in_stock_sizes = [str(x) for x in (iv.get("sizes") or []) if x]
                        try:
                            max_q = int((attrs.get("max-qty-in-order") or 0))
                        except (TypeError, ValueError):
                            max_q = 0
                        results.append({
                            "uid": uid,
                            "slug": slug,
                            "name": name,
                            "sp": sp,
                            "mrp": mrp,
                            "size": size,
                            "pack": _pack(itm),
                            "brand": _brand(itm.get("brand")),
                            "seller_id": int(itm.get("seller_id") or 1),
                            "vertical": str(attrs.get("vertical-code") or "GROCERIES"),
                            "size_variants": sizes or None,
                            "sku_code": str(itm.get("sku_code") or itm.get("item_code") or ""),
                            "sellable": bool(itm.get("sellable")),
                            "in_stock_sizes": in_stock_sizes,
                            "max_qty": max_q,
                            "moq": int((itm.get("moq") or {}).get("minimum", 1) or 1),
                        })
    except Exception as e:
        logger.error(f"Catalog search error: {e}")
    return results

# ==================== DELIVERY FEE DISPLAY ENGINE ====================
FREE_DELIVERY_THRESHOLD = 399.0   # JioMart free-delivery cutoff (bill >= 399)

async def api_read_delivery_fee(chat_id: str) -> dict:
    """Read live delivery charge from get_cart breakup:
    {delivery_charge, subtotal, total, info, dc_config}."""
    ram = RAM_CACHE.get(str(chat_id)) or {}
    token = ram.get("token") or ""
    if not token:
        return {}
    cookies = ram.get("cookies") or {}
    loc = get_resolved_location(chat_id) or {}
    pin = str(ram.get("pincode") or loc.get("pincode") or "754004")
    headers = build_location_headers(pin, token=token, visitor_id=ram.get("visitor_id", ""),
                                     lat=loc.get("latitude"), lon=loc.get("longitude"),
                                     store_id=str(_normalize_store_id(ram.get("store_id"))),
                                     ch="MOB", polygon_id=ram.get("polygon_id") or "")
    url = f"https://www.jiomart.com/api/service/application/cart/v1.0/detail?area_code={pin}&b=true&i=true"
    out = {}
    try:
        async with aiohttp.ClientSession(cookies=cookies) as session:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    cart_obj = data.get("cart", data) or {}
                    breakup = (cart_obj.get("breakup_values", {}) or {})
                    raw = breakup.get("raw", {}) or {}
                    out = {
                        "delivery_charge": max(
                            float(raw.get("delivery_charge") or 0),
                            float(raw.get("total_charge") or 0),
                            float(raw.get("convenience_fee") or 0)),
                        "delivery_charge_raw": float(raw.get("delivery_charge") or 0),
                        "total_charge": float(raw.get("total_charge") or 0),
                        "subtotal": float(raw.get("subtotal") or raw.get("sub_total") or 0),
                        "total": float(raw.get("total") or 0),
                        "cod_charge": float(raw.get("cod_charge") or 0),
                        "info": str(data.get("delivery_charge_info") or cart_obj.get("delivery_charge_info") or ""),
                        "dc_config": ((cart_obj.get("common_config") or data.get("common_config") or {})
                                      .get("delivery_charges_config") or {}),
                    }
    except Exception as e:
        logger.warning(f"api_read_delivery_fee error: {e}")
    return out

async def refresh_live_cart_price(chat_id: str) -> str:
    """Re-fetch the live cart via get_cart and refresh RAM cart_price/cart_id."""
    ram = RAM_CACHE.get(str(chat_id)) or {}
    token = ram.get("token") or ""
    if not token:
        return str(ram.get("cart_price", "0.00"))
    cookies = ram.get("cookies") or {}
    loc = get_resolved_location(chat_id) or {}
    pin = str(ram.get("pincode") or loc.get("pincode") or "754004")
    headers = build_location_headers(pin, token=token, visitor_id=ram.get("visitor_id", ""),
                                     lat=loc.get("latitude"), lon=loc.get("longitude"),
                                     store_id=str(_normalize_store_id(ram.get("store_id"))),
                                     ch="MOB", polygon_id=ram.get("polygon_id") or "")
    url = f"https://www.jiomart.com/api/service/application/cart/v1.0/detail?area_code={pin}&b=true&i=true"
    try:
        async with aiohttp.ClientSession(cookies=cookies) as session:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    cid = data.get("cart_id") or ""
                    if cid and str(cid).lower() != "none":
                        ram["cart_id"] = str(cid)
                    cart_obj = data if isinstance(data, dict) else {}
                    pay_sum = cart_obj.get("payment_summary", {}) or {}
                    price = None
                    if pay_sum.get("to_pay") is not None and float(pay_sum.get("to_pay") or 0) > 0:
                        price = str(pay_sum["to_pay"])
                    else:
                        breakup = cart_obj.get("breakup_values", {}) or {}
                        raw = breakup.get("raw", {}) if isinstance(breakup, dict) else {}
                        if raw.get("total") or breakup.get("total"):
                            price = str(raw.get("total") or breakup.get("total"))
                    if price:
                        ram["cart_price"] = price
                        return price
    except Exception as e:
        logger.error(f"refresh_live_cart_price error: {e}")
    return str(ram.get("cart_price", "0.00"))

async def inject_product_to_cart_robust(token: str, visitor_id: str, cookies: dict, item_id: int, size: str,
                                        vertical: str = "GROCERIES", pincode: str = "400001", store_id: str = "",
                                        seller_id: int = 1, size_variants: list = None, lat: float = None,
                                        lon: float = None, quantity: int = 1, seller_ids: list = None,
                                        chat_id: str = ""):
    """od35 add_items engine: tries every size x seller x journey combo until success.

    seller_ids (optional): REAL seller ids from the sizes API seller_identifiers -
    tried FIRST; without them many items fail as \"Out of Stock\" because the real
    fulfilment seller is not 1/16369."""
    sellers = [seller_id, 1, 16369] if seller_id not in [1, 16369] else [seller_id, 1]
    if seller_ids:
        real = [int(s) for s in seller_ids if str(s).strip().isdigit()]
        if real:
            sellers = list(dict.fromkeys(real + sellers))[:6]
    sellers = list(dict.fromkeys(sellers))
    sizes_to_try = size_variants if size_variants else [size, "OS"]
    last_err = None
    # JioMart moved add-to-cart to the application cart API (2026-09):
    #   POST https://www.jiomart.com/api/service/application/cart/v2.0/detail
    # No journey query param / payload key any more (old /ext/jmshipmentfee/... 404s).
    url = "https://www.jiomart.com/api/service/application/cart/v2.0/detail"
    for sz in list(dict.fromkeys(sizes_to_try)):
        for seller in sellers:
            payload = {
                "item_id": int(item_id),
                "item_size": str(sz),
                "quantity": max(1, int(quantity or 1)),
                "seller_id": int(seller),
                "meta": {"vertical_code": str(vertical), "compute_delivery_fee": True},
                "extra_meta": {"is_tradein_opted": False},
            }
            headers = build_location_headers(pincode, token=token, visitor_id=visitor_id,
                                             lat=lat, lon=lon, store_id=str(store_id), ch="MOB")
            async with aiohttp.ClientSession(cookies=cookies) as session:
                try:
                    async with session.post(url, json=payload, headers=headers, timeout=8) as resp:
                        res_data = await resp.json(content_type=None)
                        logger.info(f"Add-to-cart Status={resp.status} Size={sz} Seller={seller} => {res_data}")
                        if resp.status == 200 and isinstance(res_data, dict) and res_data.get("success") is True \
                                and bool(res_data.get("identifiers")):
                            cid = res_data.get("cart_id")
                            if cid and str(cid).lower() != "none":
                                return True, str(cid), ""
                            return True, "", ""
                        msg = (res_data or {}).get("message") or json.dumps(res_data)[:120]
                        if msg:
                            last_err = msg
                        if "out of stock" in str(msg).lower():
                            break
                except Exception as e:
                    last_err = str(e)
    return False, None, last_err or "Item not added / Out of Stock"

def get_session_pagination_keyboard(chat_id: str, page: int = 0):
    """Session switcher with PAGINATION - user sees ONLY his own keys (admin sees ALL).

    Buttons: ⏮ Prev / 📄 page-of-total / Next ▶️, then Back to Main Menu."""
    if is_admin(chat_id):
        all_keys = get_all_session_keys()
    else:
        # Non-admin: only this user's saved keys (from configs saved_keys)
        all_keys = sorted(get_user_saved_keys(chat_id).keys())
    active_key, _ = get_active_account(chat_id)
    page_size = 50
    total_pages = max(1, (len(all_keys) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))

    start_idx = page * page_size
    end_idx = min(start_idx + page_size, len(all_keys))
    page_keys = all_keys[start_idx:end_idx]

    kb = []
    for k in page_keys:
        star = " ⭐" if k == active_key else ""
        kb.append([InlineKeyboardButton(f"📁 {k}{star}", callback_data=f"select_sess_{k}")])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"sess_page_{page - 1}"))
    nav_row.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages} ({len(all_keys)})", callback_data="sess_noop"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Next ▶️", callback_data=f"sess_page_{page + 1}"))
    if nav_row:
        kb.append(nav_row)

    kb.append([InlineKeyboardButton("🔍 Search Session", callback_data="sess_search")])
    kb.append([InlineKeyboardButton("👤 My Keys", callback_data="my_keys")])
    kb.append([InlineKeyboardButton("➕ New Login", callback_data="sess_new_login"),
               InlineKeyboardButton("🔙 Main Menu", callback_data="back_home")])
    return InlineKeyboardMarkup(kb)


# ==================== OD35 PORT: API ADDRESS ENGINE ====================
async def get_user_saved_addresses_direct(cookies: dict, token: str, pincode: str, lat=None, lon=None):
    headers = build_location_headers(pincode, token=token, lat=lat, lon=lon, store_id="", ch="MOB")
    address_url = "https://www.jiomart.com/api/service/application/cart/v1.0/address?checkout_mode=self"
    try:
        async with aiohttp.ClientSession(cookies=cookies) as session:
            async with session.get(address_url, headers=headers, timeout=10) as response:
                if response.status == 200:
                    data = await response.json(content_type=None)
                    if isinstance(data, dict) and isinstance(data.get("address"), list):
                        return data.get("address", [])
    except Exception as e:
        logger.error(f"Error fetching address: {e}")
    return []

async def api_verify_logistics_pincode(cookies: dict, token: str, pincode: str, lat=None, lon=None):
    headers = build_location_headers(pincode, token=token, lat=lat, lon=lon, store_id="", ch="MOB")
    url = f"https://www.jiomart.com/api/service/application/logistics/v1.0/pincode/{pincode}"
    try:
        async with aiohttp.ClientSession(cookies=cookies) as session:
            async with session.get(url, headers=headers, timeout=8) as resp:
                return resp.status == 200
    except Exception:
        pass
    return False

async def api_select_jiomart_address(cookies: dict, token: str, address_hex_id: str, pincode: str, lat=None, lon=None, cart_id: str = ""):
    headers = build_location_headers(pincode, token=token, lat=lat, lon=lon, store_id="", ch="MOB")
    headers["Content-Type"] = "application/json"
    select_url = "https://www.jiomart.com/ext/jmshipmentfee/cart/v1.0/select_address"
    if cart_id and str(cart_id).lower() != "ready":
        select_url += f"?cart_id={cart_id}"
    payload = {"id": str(address_hex_id), "billing_address_id": str(address_hex_id),
               "cart_id": str(cart_id) if cart_id and str(cart_id).lower() != "ready" else "",
               "journey": "quickcommerce"}
    try:
        async with aiohttp.ClientSession(cookies=cookies) as session:
            async with session.post(select_url, json=payload, headers=headers, timeout=12) as response:
                if response.status == 200:
                    data = await response.json(content_type=None)
                    if data.get("is_valid", True) is False:
                        return {"success": False, "error": f"JioMart Note: {data.get('message', '')}"}
                    return {"success": True, "data": data}
                return {"success": False, "error": f"HTTP {response.status}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

async def api_delete_jiomart_address_direct(cookies: dict, token: str, address_hex_id: str, pincode: str, lat=None, lon=None):
    headers = build_location_headers(pincode, token=token, lat=lat, lon=lon, store_id="", ch="MOB")
    del_url = f"https://www.jiomart.com/api/service/application/cart/v1.0/address/{address_hex_id}"
    try:
        async with aiohttp.ClientSession(cookies=cookies) as session:
            async with session.delete(del_url, headers=headers, timeout=10) as response:
                return {"success": response.status in [200, 204]}
    except Exception as e:
        return {"success": False, "error": str(e)}

async def api_delete_all_addresses_direct(cookies: dict, token: str, pincode: str, lat=None, lon=None):
    addrs = await get_user_saved_addresses_direct(cookies, token, pincode, lat, lon)
    if not addrs:
        return {"success": True, "deleted_count": 0}
    deleted_count = 0
    for addr in addrs:
        a_id = str(addr.get("id", ""))
        try:
            res = await api_delete_jiomart_address_direct(cookies, token, a_id, pincode, lat, lon)
            if res.get("success"):
                deleted_count += 1
        except Exception:
            pass
        await asyncio.sleep(0.3)
    return {"success": True, "deleted_count": deleted_count}

async def session_search_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receives the search keyword typed after pressing 🔍 Search Session.
    Matches session file names / mobile numbers (case-insensitive substring)."""
    chat_id = str(update.effective_chat.id)

    # ---- COUPON CODE input (apiready1 prompt_apply / preset coupon) ----
    coupon_mode = COUPON_STATE.get(chat_id)
    if coupon_mode:
        COUPON_STATE.pop(chat_id, None)
        code = (update.message.text or "").strip().upper()
        if not code:
            return await update.message.reply_text("❌ Empty coupon code!", parse_mode="HTML")
        if coupon_mode == "preset":
            set_preset_coupon(chat_id, code)
            return await update.message.reply_text(
                f"✅ <b>Preset Coupon Saved:</b> <code>{code}</code>\n"
                f"🎟️ ଏହା ଏବେ <b>ପ୍ରତ୍ୟେକ ଥରେ AUTO-APPLY</b> ହେବ — Cart Inject ପରେ ଓ Cart ଖୋଲିଲା ବେଳକୁ "
                f"(ପୁଣି ପଚରାଯିବ ନାହିଁ)। Cart page ର <b>✏️ Set Preset</b> ଦବାଇ ବଦଳାଇ ପାରିବେ।",
                reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")
        # coupon_mode == "apply"
        msg = await update.message.reply_text(f"🎟️ <b>Applying Coupon <code>{code}</code>...</b>", parse_mode="HTML")
        res = await api_apply_jiomart_coupon(chat_id, code)
        if res.get("success"):
            await msg.edit_text(f"🎉 <b>Coupon Applied!</b> Savings: <b>₹{res.get('value')}</b> off\n{res.get('message', '')}",
                                reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")
        else:
            await msg.edit_text(f"❌ <b>Coupon Failed:</b> {res.get('message')}",
                                reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")
        return

    # ---- OD35 PORT: multi-step offline address / product input flows ----
    text0 = (update.message.text or "").strip()
    if AWAITING_INPUT.get(chat_id) == "off_addr_pin":
        AWAITING_INPUT.pop(chat_id, None)
        pin = re.sub(r"\D", "", text0)
        if len(pin) != 6:
            AWAITING_INPUT[chat_id] = "off_addr_pin"
            return await update.message.reply_text("❌ Invalid 6-digit PIN!", parse_mode="HTML")
        OFFLINE_ADDR_TEMP.setdefault(chat_id, {})["pincode"] = pin
        AWAITING_INPUT[chat_id] = "off_addr_gps"
        return await update.message.reply_text(f"✅ PIN: <code>{pin}</code>\n\nSend GPS (Location attach or <code>lat, lon</code>):", parse_mode="HTML")

    if AWAITING_INPUT.get(chat_id) == "off_addr_gps":
        AWAITING_INPUT.pop(chat_id, None)
        m_gps = re.match(r"\s*([+-]?\d+\.\d+)\s*,\s*([+-]?\d+\.\d+)\s*$", text0)
        if not m_gps:
            AWAITING_INPUT[chat_id] = "off_addr_gps"
            return await update.message.reply_text("❌ Format: <code>lat, lon</code> (e.g. 20.46, 86.00)", parse_mode="HTML")
        OFFLINE_ADDR_TEMP.setdefault(chat_id, {})["latitude"] = float(m_gps.group(1))
        OFFLINE_ADDR_TEMP[chat_id]["longitude"] = float(m_gps.group(2))
        AWAITING_INPUT[chat_id] = "off_addr_phone"
        return await update.message.reply_text("📞 <b>Step 3/5:</b> Send Mobile Number (e.g. 9933701234):", parse_mode="HTML")

    if AWAITING_INPUT.get(chat_id) == "off_addr_phone":
        AWAITING_INPUT.pop(chat_id, None)
        phone = re.sub(r"\D", "", text0)
        if len(phone) != 10:
            AWAITING_INPUT[chat_id] = "off_addr_phone"
            return await update.message.reply_text("❌ Invalid Mobile Number!", parse_mode="HTML")
        OFFLINE_ADDR_TEMP[chat_id]["phone"] = phone
        AWAITING_INPUT[chat_id] = "off_addr_name"
        return await update.message.reply_text("👤 <b>Step 4/5:</b> Send Receiver Name:", parse_mode="HTML")

    if AWAITING_INPUT.get(chat_id) == "off_addr_name":
        AWAITING_INPUT.pop(chat_id, None)
        OFFLINE_ADDR_TEMP[chat_id]["name"] = text0
        AWAITING_INPUT[chat_id] = "off_addr_house"
        return await update.message.reply_text("🏠 <b>Step 5/5:</b> Send House / Flat / Street:", parse_mode="HTML")

    if AWAITING_INPUT.get(chat_id) == "off_addr_house":
        AWAITING_INPUT.pop(chat_id, None)
        OFFLINE_ADDR_TEMP[chat_id]["address1"] = text0
        OFFLINE_ADDR_TEMP[chat_id]["address2"] = text0
        OFFLINE_ADDR_TEMP[chat_id].setdefault("city", "City")
        OFFLINE_ADDR_TEMP[chat_id].setdefault("state", "State")
        OFFLINE_ADDR_TEMP[chat_id].setdefault("landmark", "Near Tower")
        new_addr = dict(OFFLINE_ADDR_TEMP.pop(chat_id, {}))
        off_addrs = load_offline_addresses(chat_id)
        off_addrs.append(new_addr)
        save_offline_addresses(chat_id, off_addrs)
        return await update.message.reply_text("🎉 <b>Address Saved!</b>", reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")

    # ---- ONLINE ADDRESS INPUT FLOW (collect fields then inject via API directly) ----
    if AWAITING_INPUT.get(chat_id) == "addr_online_pin":
        AWAITING_INPUT.pop(chat_id, None)
        pin = re.sub(r"\D", "", text0)
        if len(pin) != 6:
            AWAITING_INPUT[chat_id] = "addr_online_pin"
            return await update.message.reply_text("❌ Invalid 6-digit PIN!", parse_mode="HTML")
        OFFLINE_ADDR_TEMP.setdefault(chat_id, {})["pincode"] = pin
        AWAITING_INPUT[chat_id] = "addr_online_gps"
        return await update.message.reply_text(
            f"✅ PIN: <code>{pin}</code>\n\nStep 2/5: Send GPS (Location attach or <code>lat, lon</code>):",
            parse_mode="HTML")

    if AWAITING_INPUT.get(chat_id) == "addr_online_gps":
        AWAITING_INPUT.pop(chat_id, None)
        m_gps = re.match(r"\s*([+-]?\d+\.\d+)\s*,\s*([+-]?\d+\.\d+)\s*$", text0)
        if not m_gps:
            AWAITING_INPUT[chat_id] = "addr_online_gps"
            return await update.message.reply_text("❌ Format: <code>lat, lon</code> (e.g. 20.46, 86.00)", parse_mode="HTML")
        OFFLINE_ADDR_TEMP.setdefault(chat_id, {})["latitude"] = float(m_gps.group(1))
        OFFLINE_ADDR_TEMP[chat_id]["longitude"] = float(m_gps.group(2))
        AWAITING_INPUT[chat_id] = "addr_online_phone"
        return await update.message.reply_text("📞 <b>Step 3/5:</b> Send Mobile Number (e.g. 9933701234):", parse_mode="HTML")

    if AWAITING_INPUT.get(chat_id) == "addr_online_phone":
        AWAITING_INPUT.pop(chat_id, None)
        phone = re.sub(r"\D", "", text0)
        if len(phone) != 10:
            AWAITING_INPUT[chat_id] = "addr_online_phone"
            return await update.message.reply_text("❌ Invalid Mobile Number!", parse_mode="HTML")
        OFFLINE_ADDR_TEMP[chat_id]["phone"] = phone
        AWAITING_INPUT[chat_id] = "addr_online_name"
        return await update.message.reply_text("👤 <b>Step 4/5:</b> Send Customer Name:", parse_mode="HTML")

    if AWAITING_INPUT.get(chat_id) == "addr_online_name":
        AWAITING_INPUT.pop(chat_id, None)
        OFFLINE_ADDR_TEMP[chat_id]["name"] = text0
        AWAITING_INPUT[chat_id] = "addr_online_house"
        return await update.message.reply_text("🏠 <b>Step 5/5:</b> Send House / Flat / Street:", parse_mode="HTML")

    if AWAITING_INPUT.get(chat_id) == "addr_online_house":
        AWAITING_INPUT.pop(chat_id, None)
        addr_dict = dict(OFFLINE_ADDR_TEMP.pop(chat_id, {}))
        addr_dict["address1"] = text0
        addr_dict["address2"] = text0
        addr_dict.setdefault("city", "City")
        addr_dict.setdefault("state", "State")
        addr_dict.setdefault("landmark", "Near Tower")
        # Inject directly via API (no offline save)
        token, cookies = get_pure_api_auth(chat_id)
        if not token:
            return await update.message.reply_text(
                "❌ No valid auth token! Run <b>🔄 2-Step Sync</b> first.",
                reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")
        ram = RAM_CACHE.get(chat_id) or {}
        cart_id = ram.get("cart_id", "")
        lat = addr_dict.get("latitude")
        lon = addr_dict.get("longitude")
        msg = await update.message.reply_text("🌐 <b>Adding address to your JioMart account via API...</b>", parse_mode="HTML")
        res = await api_ensure_or_add_default_address_direct(cookies, token, addr_dict, lat, lon, cart_id=cart_id)
        if res.get("success"):
            new_hex_id = res.get("hex_id")
            ram["address_id"] = new_hex_id
            ram["pincode"] = str(addr_dict.get("pincode", ""))
            try:
                configs = load_configs()
                cfg = configs.setdefault(chat_id, {})
                cfg["address_id"] = new_hex_id
                cfg["pincode"] = str(addr_dict.get("pincode", ""))
                save_configs(configs)
            except Exception:
                pass
            await msg.edit_text(
                f"✅ <b>Address Added to Account via API!</b>\n"
                f"📍 {addr_dict.get('name', 'Address')} ({addr_dict.get('pincode', '')})\n"
                f"🆔 ID: <code>{new_hex_id}</code>",
                reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")
        else:
            await msg.edit_text(
                f"❌ <b>API Error:</b> {res.get('error', 'Unknown error')}",
                reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")

    if AWAITING_INPUT.get(chat_id) == "adm_add_id":
        AWAITING_INPUT.pop(chat_id, None)
        if not is_admin(chat_id):
            return await update.message.reply_text("🚫 Admin access only!", parse_mode="HTML")
        target = text0.strip()
        if add_admin_user(target):
            return await update.message.reply_text(
                f"✅ <b>Admin Added!</b> 👑 {target} is now an admin.",
                reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")
        return await update.message.reply_text(
            "❌ Failed: invalid ID or already an admin.",
            reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")

    if AWAITING_INPUT.get(chat_id) == "login_phone":
        # apiready1 login (NEW SSO flow 2026): jiomart.com/profile redirects to
        # account.relianceretail.com -> input#phoneNumber -> "Sign In" -> OTP boxes.
        phone = re.sub(r"\D", "", text0)
        if len(phone) != 10:
            return await update.message.reply_text("❌ Please enter a valid 10-digit mobile number!", parse_mode="HTML")
        load_msg = await update.message.reply_text(f"⏳ Requesting OTP for <b>{phone}</b>...", parse_mode="HTML")
        pw = browser = None
        try:
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            ctx = await browser.new_context(viewport=FIXED_VIEWPORT, is_mobile=True, has_touch=True,
                                            user_agent=MOBILE_USER_AGENT)
            page = await ctx.new_page()
            await page.goto("https://www.jiomart.com/profile", wait_until="domcontentloaded", timeout=60000)
            # SSO redirect chain takes ~12s -> poll for the mobile input (max 60s)
            mob_input = None
            for _ in range(20):
                try:
                    loc_ = page.locator("#phoneNumber")
                    if await loc_.count() > 0 and await loc_.first.is_visible():
                        mob_input = loc_.first
                        break
                except Exception:
                    pass
                await asyncio.sleep(3)
            if mob_input is None:
                raise RuntimeError("Login page did not load (no mobile input found)")
            await mob_input.fill(phone)
            await asyncio.sleep(1)
            clicked = False
            try:
                await page.locator("button:has-text('Sign In')").first.click(timeout=8000)
                clicked = True
            except Exception:
                await page.evaluate("""() => {
                    const b = Array.from(document.querySelectorAll('button'))
                        .find(x => (x.innerText || '').includes('Sign In'));
                    if (b) b.click();
                }""")
            # wait for OTP code boxes to appear (max 30s)
            otp_ready = False
            for _ in range(10):
                try:
                    if await page.locator("input[class*='InputCodeItem']").count() >= 4:
                        otp_ready = True
                        break
                    if await page.locator("#otp, input[id*='otp'], input[name*='otp']").count() > 0:
                        otp_ready = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(3)
            if not otp_ready:
                raise RuntimeError("OTP screen did not appear")
            LOGIN_TEMP[chat_id] = {"pw": pw, "browser": browser, "ctx": ctx, "page": page, "phone": phone}
            AWAITING_INPUT[chat_id] = "login_otp"
            await load_msg.edit_text(f"📩 OTP sent to <b>{phone}</b>!\n\nEnter the OTP received:",
                                     reply_markup=InlineKeyboardMarkup([[
                                         InlineKeyboardButton("🔄 Resend OTP", callback_data="login_resend"),
                                         InlineKeyboardButton("❌ Cancel Login", callback_data="login_cancel")]]),
                                     parse_mode="HTML")
        except Exception as e:
            logger.error(f"Login Init Error: {e}")
            try: await browser.close()
            except Exception: pass
            try:
                if pw: await pw.stop()
            except Exception: pass
            AWAITING_INPUT.pop(chat_id, None)
            await load_msg.edit_text(f"❌ Failed to request OTP: {e}", parse_mode="HTML")
        return

    if AWAITING_INPUT.get(chat_id) == "login_otp":
        # apiready1 login: fill OTP, verify, save session (apiready1 format).
        otp = re.sub(r"\D", "", text0)
        if len(otp) < 4:
            return await update.message.reply_text("❌ Please enter a valid OTP!", parse_mode="HTML")
        session_obj = LOGIN_TEMP.get(chat_id)
        if not session_obj:
            AWAITING_INPUT.pop(chat_id, None)
            return await update.message.reply_text("❌ Session expired. Start again: 🔑 Switch Session → ➕ New Login.",
                                                   parse_mode="HTML")
        load_msg = await update.message.reply_text("⏳ Verifying OTP & saving session...", parse_mode="HTML")
        try:
            page = session_obj["page"]
            ctx = session_obj["ctx"]
            browser = session_obj["browser"]
            pw = session_obj["pw"]
            phone = session_obj["phone"]
            # apiready1: purge old inputs before typing the OTP
            try:
                await purge_login_otp_inputs(page)
            except Exception:
                pass
            # Reliance JDS: 6 single-digit code boxes (input[class*='InputCodeItem'])
            code_loc = page.locator("input[class*='InputCodeItem']")
            boxes = 0
            try:
                boxes = await code_loc.count()
            except Exception:
                boxes = 0
            if boxes >= min(len(otp), 4):
                for i, digit in enumerate(otp[:boxes]):
                    try:
                        await code_loc.nth(i).click()
                        await page.keyboard.type(digit, delay=100)
                    except Exception:
                        try:
                            await code_loc.nth(i).fill(digit)
                        except Exception:
                            pass
                    await asyncio.sleep(0.15)
            else:
                single = page.locator("#otp, input[id*='otp'], input[name*='otp'], input[type='tel']").first
                try:
                    await single.click()
                    await page.keyboard.type(otp, delay=100)
                except Exception:
                    await single.fill(otp)
            await asyncio.sleep(1)
            v_clicked = False
            try:
                await page.locator("button:has-text('Verify OTP'), button:has-text('Verify'), button[type='submit']").first.click(timeout=8000)
                v_clicked = True
            except Exception:
                pass
            if not v_clicked:
                await page.evaluate("""() => {
                    const b = Array.from(document.querySelectorAll('button'))
                        .find(x => /verify/i.test(x.innerText || '')) ||
                        Array.from(document.querySelectorAll('button')).find(x => x.type === 'submit');
                    if (b) b.click();
                }""")
            await asyncio.sleep(6)
            # apiready1: WRONG OTP detection -> re-prompt (stay in login_otp state)
            is_wrong_otp = await page.evaluate("""() => {
                const toast = document.querySelector('div[class*="toast"], div[class*="error"], div[class*="alert"]');
                if (toast && toast.innerText && toast.innerText.toLowerCase().includes('wrong otp')) return true;
                const textNodes = Array.from(document.querySelectorAll('div, p, span'));
                return !!textNodes.find(el => el.innerText && el.innerText.includes('Wrong OTP'));
            }""")
            if is_wrong_otp:
                await load_msg.edit_text(
                    "❌ <b>Wrong OTP detected!</b> Please send the correct OTP:",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔄 Resend OTP", callback_data="login_resend"),
                        InlineKeyboardButton("❌ Cancel Login", callback_data="login_cancel")]]),
                    parse_mode="HTML")
                try:
                    await purge_login_otp_inputs(page)
                except Exception:
                    pass
                return
            # apiready1: NEW ACCOUNT name-setup screen detection
            is_new_account = await page.evaluate("""() => {
                const textNodes = Array.from(document.querySelectorAll('div, h2, h1, p, span'));
                return !!textNodes.find(el => el.innerText && (el.innerText.includes('Instant account setup') || el.innerText.includes('All we need is your name')));
            }""")
            if is_new_account:
                preset_name = _login_preset_name(chat_id)
                await load_msg.edit_text(
                    f"🆕 <b>Instant account setup detected!</b> (New user)\n📝 Auto-typing name <code>{html.escape(preset_name)}</code>...",
                    parse_mode="HTML")
                try:
                    await page.evaluate("""() => {
                        const setupContainer = Array.from(document.querySelectorAll('div'))
                            .find(el => el.innerText && el.innerText.includes('Instant account setup'));
                        let inputField = setupContainer ? setupContainer.querySelector('input') : null;
                        if (!inputField) {
                            inputField = document.querySelector('input[placeholder*="name"], input[name*="firstName"], input[id*="name"]');
                        }
                        if (inputField) {
                            inputField.focus(); inputField.value = '';
                            inputField.dispatchEvent(new Event('input', { bubbles: true }));
                        }
                    }""")
                    await asyncio.sleep(1)
                    await page.keyboard.type(preset_name, delay=100)
                    await asyncio.sleep(1.5)
                    try:
                        await page.locator("button:has-text('Get Started'), button:has-text('Continue'), button[type='submit']").first.click(timeout=6000, force=True)
                    except Exception:
                        await page.evaluate("""() => {
                            const b = Array.from(document.querySelectorAll('button'))
                                .find(x => /get started|continue/i.test(x.innerText || '')) ||
                                Array.from(document.querySelectorAll('button')).find(x => x.type === 'submit');
                            if (b) b.click();
                        }""")
                    await asyncio.sleep(4)
                    await load_msg.edit_text("⏳ Account created! Saving session...", parse_mode="HTML")
                    await _login_finalize(chat_id, session_obj, load_msg)
                except Exception as ne:
                    # auto-fill failed -> ask user for the name (apiready1 NEW_NAME_STATE)
                    AWAITING_INPUT[chat_id] = "login_name"
                    await load_msg.edit_text(
                        f"⚠️ Auto-fill failed: {html.escape(str(ne)[:80])}\n\n"
                        "✍️ Please type and send the customer's <b>Full Name</b>:",
                        reply_markup=InlineKeyboardMarkup(
                            [[InlineKeyboardButton("❌ Cancel Login", callback_data="login_cancel")]]),
                        parse_mode="HTML")
                return
            await _login_finalize(chat_id, session_obj, load_msg)
        except Exception as e:
            logger.error(f"Login Verify Error: {e}")
            await load_msg.edit_text(f"❌ Failed to verify OTP: {e}", parse_mode="HTML")
        return

    if AWAITING_INPUT.get(chat_id) == "login_name":
        # apiready1 NEW_NAME_STATE: new user typed their name on the setup screen
        customer_name = text0.strip()
        session_obj = LOGIN_TEMP.get(chat_id)
        if not session_obj:
            AWAITING_INPUT.pop(chat_id, None)
            return await update.message.reply_text("❌ Session expired. Start again: 🔑 Switch Session → ➕ New Login.",
                                                   parse_mode="HTML")
        load_msg = await update.message.reply_text(f"✍️ Typing name <b>{html.escape(customer_name)}</b>...", parse_mode="HTML")
        try:
            page = session_obj["page"]
            await page.evaluate("""() => {
                const inputField = document.querySelector('input[placeholder*="name"], input[name*="firstName"], input[id*="name"]');
                if (inputField) {
                    inputField.focus(); inputField.value = '';
                    inputField.dispatchEvent(new Event('input', { bubbles: true }));
                }
            }""")
            await asyncio.sleep(1)
            await page.keyboard.type(customer_name, delay=100)
            await asyncio.sleep(1.5)
            try:
                await page.locator("button:has-text('Get Started'), button:has-text('Continue'), button[type='submit']").first.click(timeout=6000, force=True)
            except Exception:
                await page.evaluate("""() => {
                    const b = Array.from(document.querySelectorAll('button'))
                        .find(x => /get started|continue/i.test(x.innerText || '')) ||
                        Array.from(document.querySelectorAll('button')).find(x => x.type === 'submit');
                    if (b) b.click();
                }""")
            await asyncio.sleep(4)
            await load_msg.edit_text("⏳ Account created! Saving session...", parse_mode="HTML")
            await _login_finalize(chat_id, session_obj, load_msg)
        except Exception as e:
            logger.error(f"Login Name Error: {e}")
            await load_msg.edit_text(f"❌ Name Entry Fail: {html.escape(str(e)[:80])}", parse_mode="HTML")
        return

    if AWAITING_INPUT.get(chat_id) == "off_prod":
        AWAITING_INPUT.pop(chat_id, None)
        parsed = parse_offline_link(text0)
        if not parsed:
            return await update.message.reply_text("❌ Invalid product link format!", parse_mode="HTML")
        loading_msg = await update.message.reply_text("⏳ <b>Resolving & fetching item details...</b>", parse_mode="HTML")
        target_url = parsed["url"]
        slug = parsed.get("slug") or ""
        if parsed.get("short_code") and not slug:
            try:
                resolved_url, slug = await resolve_offline_short_url(parsed["url"])
                if resolved_url: target_url = resolved_url
            except Exception:
                pass
        canonical_slug = extract_slug_from_url(target_url) or slug
        if canonical_slug:
            target_url = f"https://www.jiomart.com/product/{canonical_slug}"
            slug = canonical_slug
        item = await fetch_offline_item_details(slug, target_url, parsed["quantity"])
        cart = load_offline_cart(chat_id)
        cart[item["key"]] = item
        save_offline_cart(chat_id, cart)
        return await loading_msg.edit_text(
            build_billing_summary(chat_id),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛒 View Cart", callback_data="off_view_cart")]]),
            parse_mode="HTML")

    # ---- SEARCH & ADD TO CART: catalog product search keyword ----
    if USER_SEARCH_STATE.get(chat_id) == "prod_search":
        # Keep USER_SEARCH_STATE="prod_search" active -> sending another name
        # while results are showing re-searches (old results deleted) automatically.
        keyword0 = text0.strip()
        if not keyword0:
            return await update.message.reply_text(
                "❌ Empty search! Type a product name (e.g. <code>Fortune Oil</code>):",
                parse_mode="HTML")
        wait = await update.message.reply_text(f"🔎 <b>Searching JioMart for:</b> <code>{html.escape(keyword0)}</code> ...",
                                               parse_mode="HTML")
        await run_product_search(chat_id, keyword0, wait)
        return

    if not USER_SEARCH_STATE.pop(chat_id, False):
        return  # not in search mode - ignore the text silently

    keyword = (update.message.text or "").strip()
    if not keyword:
        return

    active_key, _ = get_active_account(chat_id)
    if is_admin(chat_id):
        all_keys = get_all_session_keys()
    else:
        # Non-admin: search only within this user's own keys
        all_keys = sorted(get_user_saved_keys(chat_id).keys())
    matches = [k for k in all_keys if keyword.lower() in k.lower()]

    kb = []
    for k in matches[:50]:
        star = " ⭐" if k == active_key else ""
        kb.append([InlineKeyboardButton(f"📁 {k}{star}", callback_data=f"select_sess_{k}")])

    if len(matches) > 50:
        kb.append([InlineKeyboardButton(f"⚠️ {len(matches) - 50} more - type a longer keyword",
                                        callback_data="sess_noop")])
    kb.append([InlineKeyboardButton("🔙 Session Switcher", callback_data="open_session_manager")])

    if matches:
        shown = min(len(matches), 50)
        text = (f"🔍 <b>Search:</b> <code>{html.escape(keyword)}</code>\n"
                f"✅ <b>{len(matches)} session(s) found</b> (showing {shown})")
    else:
        text = (f"🔍 <b>Search:</b> <code>{html.escape(keyword)}</code>\n"
                f"❌ <b>No session matched!</b> Try a mobile number or another keyword.")

    await update.message.reply_text(
        text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

async def handle_location_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """OD35 PORT: capture attached GPS location during the Add-Offline-Address flow."""
    chat_id = str(update.effective_chat.id)
    if AWAITING_INPUT.get(chat_id) == "off_addr_gps":
        loc = update.message.location
        if not loc:
            return await update.message.reply_text("⚠️ Location not found! Type <code>lat, lon</code>:", parse_mode="HTML")
        AWAITING_INPUT.pop(chat_id, None)
        OFFLINE_ADDR_TEMP.setdefault(chat_id, {})["latitude"] = loc.latitude
        OFFLINE_ADDR_TEMP[chat_id]["longitude"] = loc.longitude
        AWAITING_INPUT[chat_id] = "off_addr_phone"
        return await update.message.reply_text(
            f"🗺️ <b>GPS Captured:</b> {loc.latitude:.6f}, {loc.longitude:.6f}\n\n📞 Send Mobile Number:",
            parse_mode="HTML")


# ==================== OD35 PORT: INJECT ADDRESS ====================
async def api_add_jiomart_address_direct(cookies: dict, token: str, addr_dict: dict, lat=None, lon=None, cart_id: str = ""):
    pin = str(addr_dict.get("pincode", "400001"))
    await api_verify_logistics_pincode(cookies, token, pin, lat, lon)
    await asyncio.sleep(1.5)
    headers = build_location_headers(pin, token=token, lat=lat, lon=lon, store_id="", ch="MOB")
    add_url = "https://www.jiomart.com/api/service/application/cart/v1.0/address"
    phone_num = str(addr_dict.get("phone", "9933701234"))
    name_val = str(addr_dict.get("name", "Customer Name"))
    addr1 = str(addr_dict.get("address1", "Street Address"))
    addr2 = str(addr_dict.get("address2", "Locality"))
    city_val = str(addr_dict.get("city", "City")).upper()
    state_val = str(addr_dict.get("state", "State")).upper()
    landmark_val = str(addr_dict.get("landmark", "Near Tower"))
    try:
        lat_val = float(addr_dict.get("latitude", lat if lat else 19.0760))
    except Exception:
        lat_val = 19.0760
    try:
        long_val = float(addr_dict.get("longitude", lon if lon else 72.8777))
    except Exception:
        long_val = 72.8777
    payload = {
        "is_default_address": True, "name": name_val, "contact_person": name_val,
        "phone": phone_num, "email": f"{phone_num}@nomail.jiomart.com",
        "address1": addr1, "address2": addr2, "landmark": landmark_val,
        "area": addr1, "city": city_val, "state": state_val, "country": "India",
        "country_phone_code": "91", "pincode": pin, "area_code": pin,
        "address_type": "Home",
        "address": f"{addr1}, {addr2}, {city_val}, {state_val} {pin}, India",
        "display_address": f"{addr2}, {addr1}, {landmark_val}",
        "_custom_json": {"flat_or_house_no": addr2, "floor_no": "", "tower_no": "",
                         "address_line": "", "input_mode": "MAP_POLY"},
        "geo_location": {"latitude": lat_val, "longitude": long_val},
    }
    try:
        async with aiohttp.ClientSession(cookies=cookies) as session:
            async with session.post(add_url, json=payload, headers=headers, timeout=12) as response:
                if response.status in [200, 201]:
                    res_json = await response.json(content_type=None)
                    new_hex_id = str(res_json.get("id") or res_json.get("address_id") or "")
                    if new_hex_id:
                        await api_select_jiomart_address(cookies, token, new_hex_id, pin, lat=lat_val, lon=long_val, cart_id=cart_id)
                    return {"success": True, "hex_id": new_hex_id, "data": res_json}
                txt = await response.text()
                return {"success": False, "error": f"HTTP {response.status}: {html.escape(txt[:120])}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

async def api_ensure_or_add_default_address_direct(cookies: dict, token: str, addr_dict: dict, lat=None, lon=None, cart_id: str = ""):
    pin = str(addr_dict.get("pincode", "400001"))
    try:
        addrs = await get_user_saved_addresses_direct(cookies, token, pin, lat, lon)
        old_found = bool(addrs)
    except Exception:
        addrs, old_found = [], False
    deleted_count = 0
    if old_found:
        del_res = await api_delete_all_addresses_direct(cookies, token, pin, lat, lon)
        deleted_count = del_res.get("deleted_count", 0)
    add_res = await api_add_jiomart_address_direct(cookies, token, addr_dict, lat, lon, cart_id=cart_id)
    if not add_res.get("success"):
        return {"success": False, "error": add_res.get("error", "Add failed")}
    new_hex_id = add_res.get("hex_id", "")
    if not new_hex_id:
        return {"success": False, "error": "API returned no hex_id"}
    return {"success": True, "hex_id": new_hex_id, "reused": False, "old_found": old_found, "deleted_count": deleted_count}

# ==================== OD35 PORT: INJECT CART (PURE API) ====================
async def api_get_live_cart_items(chat_id: str):
    """Fetch current online cart items + cart_id.
    Tries BOTH quickcommerce and standard journeys - the cart may be on either
    depending on how items were previously added, and we need to see/clear ALL
    items regardless of journey type."""
    ram = RAM_CACHE.get(str(chat_id)) or {}
    token = ram.get("token") or ""
    if not token:
        return [], ""
    cookies = ram.get("cookies") or {}
    loc = get_resolved_location(chat_id)
    pin = ram.get("pincode") or str(loc["pincode"])
    store_now = _normalize_store_id(ram.get("store_id"))
    polygon = ram.get("polygon_id") or ""
    headers = build_location_headers(pin, token=token, visitor_id=ram.get("visitor_id", ""),
                                     lat=loc["latitude"], lon=loc["longitude"],
                                     store_id=str(store_now), ch="MOB", polygon_id=polygon)
    items = []
    cart_id = ram.get("cart_id", "")
    seen_ids = set()
    # JioMart moved cart-read to the application cart API (2026-09):
    #   GET /api/service/application/cart/v1.0/detail?area_code=...&b=true&i=true
    # (old /ext/jmshipmentfee/cart/v1.0/get_cart proxy now returns 404)
    url = f"https://www.jiomart.com/api/service/application/cart/v1.0/detail?area_code={pin}&b=true&i=true"
    try:
        async with aiohttp.ClientSession(cookies=cookies) as session:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if str(data.get("cart_id") or "").lower() != "none":
                        cart_id = str(data.get("cart_id") or "")
                    for idx, itm in enumerate(data.get("items") or []):
                        prod = itm.get("product", {}) or {}
                        art = itm.get("article", {}) or {}
                        iid = prod.get("uid") or prod.get("item_code") or itm.get("item_id")
                        if not iid:
                            continue
                        art_id = str(art.get("uid") or art.get("seller_identifier") or "")
                        key = f"{iid}_{art_id}"
                        if key in seen_ids:
                            continue
                        seen_ids.add(key)
                        item_sz = itm.get("item_size") or (art.get("size") if isinstance(art, dict) else None) or "OS"
                        item_idx = itm.get("res_item_index")
                        if item_idx is None:
                            item_idx = idx
                        seller = 1
                        if isinstance(art, dict) and isinstance(art.get("seller"), dict):
                            seller = (art.get("seller") or {}).get("uid") or seller
                        items.append({
                            "item_id": str(iid),
                            "article_id": art_id,
                            "seller_id": itm.get("seller_id") or seller,
                            "item_size": item_sz,
                            "identifier": (itm.get("identifiers") or {}).get("identifier") or "",
                            "item_index": item_idx,
                            "quantity": int(itm.get("quantity", 1) or 1),
                            "name": prod.get("name", ""),
                        })
    except Exception as e:
        logger.error(f"get_live_cart_items error: {e}")
    return items, cart_id

async def api_clear_online_cart(chat_id: str, message=None):
    """DELETE ALL live cart items (update_cart remove_item) BEFORE injecting.

    Robust (user request: inject = first delete old items, then inject):
    removes ONE item per round and RE-FETCHES the live cart afterwards -
    item_index shifts after every removal, so a pre-built list fails from
    the 2nd item on. Loops until the cart is empty (max 15 rounds).
    Tries BOTH journeys (quickcommerce + standard) so items added via either
    journey type get cleared."""
    ram = RAM_CACHE.get(str(chat_id)) or {}
    token = ram.get("token") or ""
    if not token:
        if message:
            await safe_edit_html(message, "❌ No valid auth token! Run <b>🔄 2-Step Sync</b> first.", parse_mode="HTML")
        return False, "No token"
    cookies = ram.get("cookies") or {}
    loc = get_resolved_location(chat_id)
    pin = str(ram.get("pincode") or loc["pincode"])
    store_now = _normalize_store_id(ram.get("store_id"))
    polygon = ram.get("polygon_id") or ""
    total_removed = 0
    last_err = ""
    remaining = 0
    for round_no in range(15):
        items, cart_id = await api_get_live_cart_items(chat_id)
        remaining = len(items)
        if not items:
            break
        itm = items[0]   # oldest entry; index is always fresh from this fetch
        # JioMart moved cart-update to the application cart API (2026-09):
        #   PUT /api/service/application/cart/v2.0/detail?id=<cart_id>
        ok = False
        update_url = "https://www.jiomart.com/api/service/application/cart/v2.0/detail"
        if cart_id and str(cart_id).lower() != "none":
            update_url += f"?id={cart_id}"
        headers = build_location_headers(pin, token=token, visitor_id=ram.get("visitor_id", ""),
                                         lat=loc["latitude"], lon=loc["longitude"],
                                         store_id=str(store_now), ch="MOB", polygon_id=polygon)
        item_payload = {
            "article_id": str(itm["article_id"]),
            "item_id": int(itm["item_id"]) if str(itm["item_id"]).isdigit() else itm["item_id"],
            "identifiers": {"identifier": str(itm["identifier"])},
            "item_size": str(itm["item_size"]),
            "quantity": 0,
            "parent_item_identifiers": {"identifier": None, "parent_item_size": None, "parent_item_id": None},
            "meta": {"vertical_code": "GROCERIES", "compute_delivery_fee": True},
            "item_index": int(itm["item_index"]),
            "extra_meta": {"is_tradein_opted": False},
        }
        payload = {"operation": "remove_item", "item": item_payload}
        try:
            async with aiohttp.ClientSession(cookies=cookies) as session:
                async with session.put(update_url, json=payload, headers=headers, timeout=10) as resp:
                    if resp.status == 200:
                        try:
                            rd = await resp.json(content_type=None)
                        except Exception:
                            rd = {}
                        if isinstance(rd, dict) and rd.get("success") is False:
                            last_err = str((rd or {}).get("message") or "")[:120]
                        else:
                            ok = True
                    else:
                        last_err = f"HTTP {resp.status}"
        except Exception as e:
            last_err = str(e)
        if ok:
            total_removed += 1
            if message and total_removed % 3 == 0:
                try:
                    await safe_edit_html(message, f"🧹 <b>Deleting old cart items... {total_removed} deleted</b>", parse_mode="HTML")
                except Exception:
                    pass
        elif round_no >= 10:
            break   # same item refusing to go - don't loop forever
        await asyncio.sleep(0.3)
    ram = RAM_CACHE.setdefault(str(chat_id), {})
    ram["cart_id"] = ""
    if message and total_removed:
        try:
            await safe_edit_html(message, f"🧹 <b>Deleted {total_removed} old cart item(s). Now injecting...</b>", parse_mode="HTML")
        except Exception:
            pass
    if last_err and not total_removed and remaining:
        logger.warning(f"clear cart failed: {last_err}")
    return total_removed > 0 or not remaining, f"Deleted {total_removed} old items"

# ==================== PORT: apiready1 CART PAGE QTY SET LOGIC ====================
def is_valid_cart_id_format(cid):
    """apiready1 exact copy: validate saved/live cart id."""
    if not cid or str(cid).strip() in ["None", "", "null", "undefined"]:
        return False
    return True

async def api_update_jiomart_cart_item(chat_id, target_item_id, new_qty, target_article_id="",
                                       cart_id_param="", current_qty=1, captured_items=None,
                                       live_cart_id=None):
    """apiready1 PORT (cart page quantity set): update_item / remove_item on
    /api/service/application/cart/v2.0/detail?id=<cart_id> via PUT (2026-09).
    Live cart items give article_id/seller_id/item_size/identifier/item_index;
    falls back to the offline-cart entry / search cache (get_cached_item_details)."""
    chat_id_str = str(chat_id)
    if captured_items is None:
        captured_items, live_cart_id = await api_get_live_cart_items(chat_id_str)
    ram = RAM_CACHE.get(chat_id_str) or {}
    cart_id = live_cart_id or cart_id_param or ram.get("cart_id") or ""

    item_id_str = str(target_item_id)
    article_id = target_article_id
    seller_id = 1
    item_size = "OS"
    identifier_val = ""
    item_index = 0
    for idx, itm in enumerate(captured_items or []):
        if str(itm.get("item_id")) == item_id_str:
            article_id = itm.get("article_id") or article_id
            seller_id = itm.get("seller_id") or 1
            item_size = itm.get("item_size") or "OS"
            identifier_val = itm.get("identifier") or ""
            item_index = itm.get("item_index", idx)
            break
    if not article_id or not identifier_val:
        # get_cached_item_details equivalent: offline cart entry -> search cache
        cached_info = (load_offline_cart(chat_id_str) or {}).get(item_id_str) \
            or (PRODUCT_SEARCH_CACHE.get(chat_id_str) or {}).get(item_id_str) or {}
        article_id = cached_info.get("article_id", article_id)
        seller_id = cached_info.get("seller_id", seller_id)
        item_size = cached_info.get("size") or cached_info.get("item_size") or item_size
        identifier_val = cached_info.get("identifier", identifier_val)
        item_index = cached_info.get("item_index", item_index)

    token, cookies = get_pure_api_auth(chat_id_str)
    if not token:
        return {"success": False, "debug": "No token (run 2-Step Sync)"}
    loc = get_resolved_location(chat_id_str) or {}
    pincode = str(ram.get("pincode") or loc.get("pincode") or "754004")
    headers = build_location_headers(pincode, token=token, visitor_id=ram.get("visitor_id", ""),
                                     lat=loc.get("latitude"), lon=loc.get("longitude"),
                                     store_id=str(_normalize_store_id(ram.get("store_id"))),
                                     ch="MOB", polygon_id=ram.get("polygon_id") or "")

    update_url = "https://www.jiomart.com/api/service/application/cart/v2.0/detail"
    if is_valid_cart_id_format(cart_id):
        update_url += f"?id={cart_id}"

    item_id_val = int(item_id_str) if item_id_str.isdigit() else target_item_id
    op_type = "remove_item" if new_qty <= 0 else "update_item"

    item_payload = {
        "article_id": str(article_id),
        "item_id": item_id_val,
        "identifiers": {"identifier": str(identifier_val)},
        "item_size": str(item_size),
        "quantity": 0 if new_qty <= 0 else int(new_qty),
        "parent_item_identifiers": {
            "identifier": None,
            "parent_item_size": None,
            "parent_item_id": None,
        },
        "meta": {"vertical_code": "GROCERIES", "compute_delivery_fee": True},
        "item_index": int(item_index),
        "extra_meta": {"is_tradein_opted": False},
    }

    payload = {"operation": op_type, "item": item_payload}

    try:
        async with aiohttp.ClientSession(cookies=cookies) as session:
            async with session.put(update_url, json=payload, headers=headers, timeout=10) as response:
                resp_text = await response.text()
                if response.status == 200:
                    try:
                        res_json = json.loads(resp_text)
                        if res_json.get("success") is True or res_json.get("code") == 200:
                            return {"success": True, "debug": "OK 200"}
                    except Exception:
                        return {"success": True, "debug": "OK 200"}
                return {"success": False, "debug": f"HTTP {response.status}: {resp_text[:50]}"}
    except Exception as e:
        return {"success": False, "debug": f"Err: {str(e)}"}

async def sync_online_cart_qty(chat_id, uid, new_qty, live_items=None, live_cid=None):
    """apiready1 cart_adjust formula: keep an offline item's qty in sync with the
    LIVE online cart. Returns (handled_online: bool, ok: bool, debug: str).
    handled_online=False -> item is not in the online cart (offline only)."""
    ram = RAM_CACHE.get(str(chat_id)) or {}
    if not ram.get("token"):
        return False, True, "offline-only (no token)"
    if live_items is None:
        live_items, live_cid = await api_get_live_cart_items(chat_id)
    in_live = any(str(x.get("item_id")) == str(uid) for x in (live_items or []))
    if not in_live:
        return False, True, "item not in online cart (offline only)"
    res = await api_update_jiomart_cart_item(chat_id, target_item_id=uid, new_qty=new_qty,
                                             captured_items=live_items, cart_id_param=live_cid or "")
    if res.get("success"):
        try:
            ram["cart_price"] = await refresh_live_cart_price(chat_id)
        except Exception:
            pass
    return True, bool(res.get("success")), res.get("debug", "")

# ==================== PORT: apiready1 CART MODE / COUPON / EMPTY / READ ONLINE ====================
COUPON_STATE = {}   # {chat_id: "apply"|"preset"} - waiting for a coupon code text

def get_cart_mode(chat_id):
    """OFFLINE (approx, no online touch) vs ONLINE (sync with live cart) - per chat."""
    return str(load_offline_db().get(str(chat_id), {}).get("cart_mode") or "OFFLINE")

def set_cart_mode(chat_id, mode):
    db = load_offline_db()
    if str(chat_id) not in db:
        db[str(chat_id)] = {}
    db[str(chat_id)]["cart_mode"] = "ONLINE" if str(mode).upper() == "ONLINE" else "OFFLINE"
    save_offline_db(db)

def get_preset_coupon(chat_id):
    return str(load_offline_db().get(str(chat_id), {}).get("preset_coupon") or "")

def set_preset_coupon(chat_id, code):
    db = load_offline_db()
    if str(chat_id) not in db:
        db[str(chat_id)] = {}
    db[str(chat_id)]["preset_coupon"] = str(code or "").strip().upper()
    save_offline_db(db)

async def api_apply_jiomart_coupon(chat_id, coupon_code):
    """apiready1 apply_jiomart_coupon PORT: POST /cart/v1.0/coupon?id=<cart_id>."""
    chat_id_str = str(chat_id)
    ram = RAM_CACHE.get(chat_id_str) or {}
    token = ram.get("token") or ""
    if not token:
        return {"success": False, "message": "No token (run 2-Step Sync)"}
    cookies = ram.get("cookies") or {}
    loc = get_resolved_location(chat_id) or {}
    pin = str(ram.get("pincode") or loc.get("pincode") or "754004")
    headers = build_location_headers(pin, token=token, visitor_id=ram.get("visitor_id", ""),
                                     lat=loc.get("latitude"), lon=loc.get("longitude"),
                                     store_id=str(_normalize_store_id(ram.get("store_id"))),
                                     ch="MOB", polygon_id=ram.get("polygon_id") or "")
    headers["Content-Type"] = "application/json"
    cart_id = str(ram.get("cart_id") or "")
    coupon_url = "https://www.jiomart.com/api/service/application/cart/v1.0/coupon"
    if is_valid_cart_id_format(cart_id):
        coupon_url += f"?id={cart_id}"
    payload = {"coupon_code": str(coupon_code).strip().upper()}
    try:
        async with aiohttp.ClientSession(cookies=cookies) as session:
            async with session.post(coupon_url, json=payload, headers=headers, timeout=12) as response:
                if response.status in (401, 403):
                    return {"success": False, "message": "HTTP 401: Session Expired. Run 🔄 2-Step Sync."}
                if response.status == 200:
                    res_data = await response.json(content_type=None)
                    breakup = (res_data.get("breakup_values") or {}).get("coupon") or {}
                    if breakup.get("is_applied") is True:
                        try:
                            ram["cart_price"] = await refresh_live_cart_price(chat_id)
                        except Exception:
                            pass
                        return {"success": True, "value": breakup.get("value", 0),
                                "title": breakup.get("title", ""), "message": breakup.get("message", "Coupon applied")}
                    return {"success": False, "message": res_data.get("message") or breakup.get("message") or "Coupon Apply Failed"}
                resp_text = await response.text()
                return {"success": False, "message": f"HTTP {response.status}: {resp_text[:60]}"}
    except Exception as e:
        return {"success": False, "message": f"Connection error: {str(e)}"}

async def api_remove_jiomart_coupon(chat_id):
    """apiready1 remove_jiomart_coupon PORT: DELETE /cart/v1.0/coupon?id=<cart_id>."""
    chat_id_str = str(chat_id)
    ram = RAM_CACHE.get(chat_id_str) or {}
    token = ram.get("token") or ""
    if not token:
        return {"success": False, "message": "No token (run 2-Step Sync)"}
    cookies = ram.get("cookies") or {}
    loc = get_resolved_location(chat_id) or {}
    pin = str(ram.get("pincode") or loc.get("pincode") or "754004")
    headers = build_location_headers(pin, token=token, visitor_id=ram.get("visitor_id", ""),
                                     lat=loc.get("latitude"), lon=loc.get("longitude"),
                                     store_id=str(_normalize_store_id(ram.get("store_id"))),
                                     ch="MOB", polygon_id=ram.get("polygon_id") or "")
    cart_id = str(ram.get("cart_id") or "")
    coupon_url = "https://www.jiomart.com/api/service/application/cart/v1.0/coupon"
    if is_valid_cart_id_format(cart_id):
        coupon_url += f"?id={cart_id}"
    try:
        async with aiohttp.ClientSession(cookies=cookies) as session:
            async with session.delete(coupon_url, headers=headers, timeout=10) as response:
                if response.status == 200:
                    try:
                        await refresh_live_cart_price(chat_id)
                    except Exception:
                        pass
                    return {"success": True, "message": "Coupon Removed!"}
                return {"success": False, "message": f"HTTP {response.status}"}
    except Exception as e:
        return {"success": False, "message": str(e)}

async def api_read_online_cart_lines(chat_id):
    """apiready1 get_online_cart_details -> human-readable list (name/qty/size)."""
    ram = RAM_CACHE.get(str(chat_id)) or {}
    if not ram.get("token"):
        return None, "❌ No synced session! Run <b>🔄 2-Step Sync</b> first."
    live_items, cart_id = await api_get_live_cart_items(chat_id)
    if not live_items:
        return [], "🛒 <b>Online Cart is empty.</b>"
    lines = [f"🛒 <b>Online Cart (ID:</b> <code>{cart_id or 'session active'}</code><b>):</b>\n"]
    total_qty = 0
    for i, it in enumerate(live_items, 1):
        total_qty += int(it.get("quantity", 1) or 1)
        lines.append(f"{i}. <b>{html.escape(str(it.get('name') or 'Product'))}</b>"
                     f" — qty <b>{it.get('quantity')}</b> | size {html.escape(str(it.get('item_size') or 'OS'))}"
                     f" | id <code>{it.get('item_id')}</code>")
    lines.append(f"\n📦 Total Items: <b>{total_qty}</b>")
    return lines, "\n".join(lines)

async def api_check_store_stock(chat_id, sku_codes, pincode=None):
    """Store-stock read (browser-captured vertex API):
    POST /ext/vertex/application/api/v1.0/deliverable/products
    with {"item_codes": ["<sku>", ...]} (STRINGS!) -> per-sku stock info.
    Returns {sku_code: {"sellable": bool, "sizes": [...], "max_qty": int, "moq": int}}."""
    ram = RAM_CACHE.get(str(chat_id)) or {}
    loc = get_resolved_location(chat_id) or {}
    pin = str(pincode or ram.get("pincode") or loc.get("pincode") or "754004")
    headers = {"User-Agent": MOBILE_USER_AGENT,
               "Accept": "application/json, text/plain, */*",
               "Content-Type": "application/json",
               "x-location-detail": json.dumps({"country": "INDIA", "country_iso_code": "IN", "pincode": pin}),
               "visitor-id": ram.get("visitor_id") or str(uuid.uuid4())}
    codes = [str(c) for c in (sku_codes or []) if str(c).strip()]
    if not codes:
        return {}
    url = "https://www.jiomart.com/ext/vertex/application/api/v1.0/deliverable/products"
    stock = {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={"item_codes": codes}, headers=headers, timeout=12) as resp:
                if resp.status != 200:
                    logger.warning(f"vertex stock HTTP {resp.status}")
                    return stock
                data = await resp.json(content_type=None)
                for itm in (data.get("items") or []):
                    if not isinstance(itm, dict):
                        continue
                    code = str(itm.get("sku_code") or itm.get("item_code") or "")
                    iv = itm.get("instock_variants") or {}
                    sizes = [str(s) for s in (iv.get("sizes") or []) if s]
                    attrs = itm.get("attributes") or {}
                    try:
                        max_q = int(attrs.get("max-qty-in-order") or 0)
                    except (TypeError, ValueError):
                        max_q = 0
                    stock[code] = {
                        "sellable": bool(itm.get("sellable")),
                        "sizes": sizes,
                        "max_qty": max_q,
                        "moq": int((itm.get("moq") or {}).get("minimum", 1) or 1),
                    }
    except Exception as e:
        logger.error(f"vertex stock check error: {e}")
    return stock

async def api_get_store_stock_qty(chat_id, slug, token="", cookies=None, pincode=None, store_ids=None):
    """REAL store stock count via (auth needed):
    GET /api/service/application/catalog/v2.0/products/{slug}/sizes
    -> {"sizes":[{"display","value","quantity","is_available","seller_identifiers"}]}
    Returns {"quantity": int, "sizes":[...]} or None if unavailable.
    LIVE-VERIFIED: with a valid Bearer token it returns quantity (e.g. 359 pcs),
    no store_ids needed (200)."""
    if not slug or not token:
        return None
    ram = RAM_CACHE.get(str(chat_id)) or {}
    loc = get_resolved_location(chat_id) or {}
    pin = str(pincode or ram.get("pincode") or loc.get("pincode") or "754004")
    headers = {"User-Agent": MOBILE_USER_AGENT,
               "Accept": "application/json, text/plain, */*",
               "Content-Type": "application/json",
               "Authorization": f"Bearer {str(token).replace('Bearer ', '').strip()}",
               "x-location-detail": json.dumps({"country": "INDIA", "country_iso_code": "IN", "pincode": pin}),
               "visitor-id": ram.get("visitor_id") or str(uuid.uuid4())}
    url = f"https://www.jiomart.com/api/service/application/catalog/v2.0/products/{slug}/sizes"
    if store_ids:
        ids = ",".join(str(s) for s in store_ids if str(s).strip())
        if ids:
            url += f"?store_ids={ids}"
    try:
        async with aiohttp.ClientSession(cookies=cookies or {}) as session:
            async with session.get(url, headers=headers, timeout=12) as resp:
                if resp.status != 200:
                    logger.warning(f"stock qty sizes HTTP {resp.status}")
                    return None
                data = await resp.json(content_type=None)
                sellers = []
                sizes = []
                for s in (data.get("sizes") or []):
                    if not isinstance(s, dict):
                        continue
                    sizes.append((s.get("display") or s.get("value") or "OS",
                                  int(s.get("quantity") or 0),
                                  bool(s.get("is_available"))))
                    # REAL seller ids for this store (used by add_items) -
                    # missing these makes inject fail as "Out of Stock".
                    for sid in (s.get("seller_identifiers") or []):
                        sv = str(sid).strip()
                        if sv.isdigit() and sv not in sellers:
                            sellers.append(sv)
                total = sum(q for _, q, _ in sizes)
                return {"quantity": total, "sizes": sizes, "sellable": bool(data.get("sellable")),
                        "sellers": sellers}
    except Exception as e:
        logger.warning(f"stock qty fetch error ({slug}): {e}")
        return None

def stock_status_line(r):
    """Build the stock line for a search/product card from search + vertex + qty data."""
    st = r.get("stock") or {}
    sellable = bool(r.get("sellable", st.get("sellable", True)))
    if sellable is False:
        return "📦 Stock: 🔴 Out of Stock"
    qty = st.get("quantity") if isinstance(st.get("quantity"), int) else None
    # vertex/sellability + max-qty
    st_sizes = r.get("in_stock_sizes") or st.get("sizes") or []
    max_q = int(r.get("max_qty") or st.get("max_qty") or 0)
    if qty is not None and qty > 0:
        line = f"📦 Stock: 🟢 In Stock — {qty} pc"
        if st_sizes:
            line += f" ({', '.join(st_sizes)})"
        if max_q:
            line += f" | Max {max_q}/order"
        return line
    if sellable and (st_sizes or (not st and "sellable" not in r)):
        line = "📦 Stock: 🟢 In Stock" + (f" ({', '.join(st_sizes)})" if st_sizes else "")
        if max_q:
            line += f" | Max {max_q}/order"
        return line
    return "📦 Stock: 🔴 Out of Stock"

async def run_step_inject_offline_cart(chat_id: str, message=None):
    """OD35 Step: inject every offline-cart item into the live JioMart cart via API."""
    ram = RAM_CACHE.get(str(chat_id)) or {}
    token = ram.get("token") or ""
    if not token:
        if message:
            await safe_edit_html(message, "❌ RAM Data missing! Run <b>🔄 2-Step Sync</b> first.", parse_mode="HTML")
        return False, "RAM Data missing. Run 2-Step Sync first."
    cart = load_offline_cart(chat_id)
    if not cart:
        if message:
            await safe_edit_html(message, "❌ Offline Cart is empty! Add products in 💾 Offline Database first.", parse_mode="HTML")
        return False, "Offline Cart is empty."
    ok_clear, clear_msg = await api_clear_online_cart(chat_id, message)
    logger.info(f"[Inject] old cart clear: ok={ok_clear} ({clear_msg})")
    loc = get_resolved_location(chat_id)
    pin_now = str(loc["pincode"])
    lat_now, lon_now = loc["latitude"], loc["longitude"]
    store_now = _normalize_store_id(ram.get("store_id"))
    visitor_id = ram.get("visitor_id") or str(uuid.uuid4())
    cookies = ram.get("cookies") or {}
    if message:
        await safe_edit_html(message, f"🛒 <b>Injecting {len(cart)} offline item(s) via API (PIN: {pin_now})...</b>", parse_mode="HTML")
    last_cart_id = None
    injected_count = 0
    failed_items = []
    for slug, pdata in cart.items():
        if is_force_stopped(chat_id):
            failed_items.append(("🛑 FORCE STOP", "Injection aborted by user"))
            break
        if not re.search(r"[0-9]{5,}", str(slug)) and pdata.get("url") and "jiomartjcp.com/l/" in str(pdata.get("url")):
            try:
                _, slug = await resolve_offline_short_url(pdata["url"])
            except Exception:
                pass
        p_info = await extract_product_meta(slug, token, cookies)
        qty = int(pdata.get("quantity", 1) or 1)
        if not p_info.get("item_id"):
            logger.warning(f"Skipping cart item {slug}: could not resolve product id.")
            failed_items.append((pdata.get("name") or slug, "Unresolved product"))
            continue
        # REAL seller + available sizes via sizes API (Out-of-Stock inject fix):
        # meta always returns seller_id=1 -> many items fail add_items. The
        # sizes endpoint gives seller_identifiers (true fulfilment sellers) and
        # which sizes actually have quantity in this pincode.
        # IMPORTANT: the API needs the FULL SLUG - numeric uid keys (search
        # adds key=uid) return empty sizes, so use the stored entry slug.
        cart_slug = str(slug)
        if cart_slug.isdigit() and pdata.get("slug"):
            cart_slug = str(pdata["slug"])
        inject_sellers = None
        try:
            inject_store_ids = [str(store_now)] if store_now else None
            qty_info = await api_get_store_stock_qty(chat_id, cart_slug, token=token,
                                                     cookies=cookies, pincode=str(pin_now),
                                                     store_ids=inject_store_ids)
        except Exception:
            qty_info = None
        size_variants_final = p_info.get("size_variants", [p_info["item_size"]]) or []
        if qty_info:
            if qty_info.get("sellers"):
                inject_sellers = qty_info["sellers"]
            avail = [s[0] for s in (qty_info.get("sizes") or []) if len(s) > 2 and s[2]]
            if avail:
                size_variants_final = list(dict.fromkeys(avail + list(size_variants_final)))
            logger.info(f"[Inject] {cart_slug[:60]}: sellers={inject_sellers} avail_sizes={avail} qty={qty_info.get('quantity')}")
        success, new_cid, err_msg = await inject_product_to_cart_robust(
            token, visitor_id, cookies, p_info["item_id"], p_info["item_size"],
            p_info["vertical"], pincode=str(pin_now), store_id=str(store_now),
            seller_id=p_info.get("seller_id", 1),
            size_variants=size_variants_final,
            lat=lat_now, lon=lon_now, quantity=qty,
            seller_ids=inject_sellers,
            chat_id=chat_id)
        if success:
            injected_count += 1
            if new_cid:
                last_cart_id = new_cid
        else:
            failed_items.append((pdata.get("name") or slug, err_msg or "Out of stock"))
    if injected_count > 0:
        ram = RAM_CACHE.setdefault(str(chat_id), {})
        if last_cart_id:
            ram["cart_id"] = last_cart_id
        ram["pincode"] = pin_now
        try:
            ram["cart_price"] = await refresh_live_cart_price(chat_id)
        except Exception as e:
            logger.warning(f"inject price refresh error: {e}")
        # 🎟️ PRESET COUPON AUTO-APPLY: coupon set once -> applied on EVERY
        # successful inject (no prompt). Applied BEFORE the success report.
        cp_note = ""
        pc = get_preset_coupon(chat_id)
        if pc:
            cp_res = await api_apply_jiomart_coupon(chat_id, pc)
            if cp_res.get("success"):
                cp_note = f"\n🎟️ <b>Preset Coupon <code>{pc}</code> applied:</b> ₹{cp_res.get('value')} off"
                try:
                    ram["cart_price"] = await refresh_live_cart_price(chat_id)
                except Exception:
                    pass
            else:
                cp_note = f"\n⚠️ Preset Coupon <code>{pc}</code> failed: {cp_res.get('message')}"
        if message:
            msg = f"✅ <b>Cart Injected:</b> {injected_count} item(s) added via API (PIN: {pin_now})!\n🧹 <b>Old items deleted:</b> {clear_msg}{cp_note}"
            if failed_items:
                msg += "\n\n⚠️ <b>Not added:</b>\n" + "\n".join(f"• {n} — {m}" for n, m in failed_items[:10])
            await safe_edit_html(message, msg, parse_mode="HTML")
        return True, f"Injected {injected_count} items."
    if message:
        await safe_edit_html(message, f"❌ Cart Inject Failed: Items Out of Stock in PIN {pin_now}", parse_mode="HTML")
    return False, "Failed to inject cart."

# ==================== OD35 PORT: ORDER HISTORY (API, UP TO 50 + CANCEL) ====================
def _orders_auth(chat_id: str):
    """Active session file token/cookies first, then RAM (od35 _orders_auth)."""
    token, cookies = get_pure_api_auth(chat_id)
    return token, cookies or {}

def _orders_headers(token: str) -> dict:
    clean_tok = token.replace("Bearer ", "").strip() if token else ""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": MOBILE_USER_AGENT,
        "Origin": "https://www.jiomart.com",
        "Referer": "https://www.jiomart.com/profile/orders",
    }
    if clean_tok:
        headers["authorization"] = f"Bearer {clean_tok}"
    return headers

def _extract_order_list_order(raw_data) -> list:
    orders = []
    if not isinstance(raw_data, dict):
        return orders
    orders = raw_data.get("orders") or []
    d = raw_data.get("data")
    if not orders and isinstance(d, dict):
        orders = d.get("orders") or []
    if not orders:
        for v in raw_data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict) and any(
                    k in v[0] for k in ("order_id", "id", "shipments", "bags")):
                orders = v
                break
    return orders if isinstance(orders, list) else []

async def api_get_my_orders_od(chat_id: str, limit: int = 50):
    """Fetch order history via JioMart API (hellcat getmainorders), capped at limit."""
    token, cookies = _orders_auth(chat_id)
    if not token:
        return {"success": False, "error": "No token"}
    headers = _orders_headers(token)
    url = "https://www.jiomart.com/ext/hellcat/myorders/getmainorders/"
    orders = []
    page = 0
    try:
        async with aiohttp.ClientSession(cookies=cookies, timeout=aiohttp.ClientTimeout(total=10)) as session:
            while page < 100:
                query = {"page": str(page), "pageSize": "10"}
                raw = None
                try:
                    async with session.get(url, headers=headers, params=query) as resp:
                        if resp.status == 200:
                            try:
                                raw = await resp.json(content_type=None)
                            except Exception:
                                raw = None
                except Exception:
                    raw = None
                page_orders = _extract_order_list_order(raw) if raw else []
                if not page_orders:
                    break
                orders.extend(page_orders)
                if len(orders) >= limit:
                    break
                if raw and (raw.get("hasMore") is False or raw.get("isLastPage") is True):
                    break
                page += 1
            if not orders:
                try:
                    async with aiohttp.ClientSession(cookies=cookies, timeout=aiohttp.ClientTimeout(total=10)) as session:
                        async with session.get(url, headers=headers) as resp:
                            if resp.status == 200:
                                raw = await resp.json(content_type=None)
                                orders = _extract_order_list_order(raw) or []
                except Exception:
                    pass
        return {"success": True, "data": orders[:limit]}
    except Exception as e:
        return {"success": False, "error": str(e)}

async def api_get_shipment_details_od(shipment_id, chat_id):
    if not shipment_id or str(shipment_id).lower() in ("", "n/a", "none", "null"):
        return {"price": 0.0, "status": "N/A", "date": None}
    token, cookies = _orders_auth(chat_id)
    headers = _orders_headers(token)
    url = f"https://www.jiomart.com/ext/hellcat/details/{shipment_id}/"
    try:
        async with aiohttp.ClientSession(cookies=cookies, timeout=aiohttp.ClientTimeout(total=6)) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    shipment = data.get("shipment", {}) or data.get("data", {}).get("shipment", {})
                    breakup = shipment.get("breakup_values", [])
                    price_val = 0.0
                    if isinstance(breakup, list):
                        for item in breakup:
                            if isinstance(item, dict) and item.get("name") == "total":
                                try:
                                    price_val = float(item.get("value", 0))
                                except (TypeError, ValueError):
                                    price_val = 0.0
                                break
                    raw_status = shipment.get("shipment_status") or shipment.get("status") or "N/A"
                    if isinstance(raw_status, dict):
                        status = str(raw_status.get("name") or raw_status.get("value") or raw_status.get("status") or "N/A")
                    else:
                        status = str(raw_status)
                    created = shipment.get("created_at") or shipment.get("order_created_at") or shipment.get("date")
                    return {"price": price_val, "status": status, "date": _format_date_value(created)}
    except Exception as e:
        logger.error(f"shipment details error {shipment_id}: {e}")
    return {"price": 0.0, "status": "N/A", "date": None}

async def api_verify_cancel_od(order_id, shipment_id, chat_id):
    token, cookies = _orders_auth(chat_id)
    headers = _orders_headers(token)
    url = "https://www.jiomart.com/ext/hellcat/myorders/verify-cancel"
    payload = {"order_id": str(order_id), "shipment_id": str(shipment_id),
               "skuids": [], "qty": 1, "source": "JCP"}
    try:
        async with aiohttp.ClientSession(cookies=cookies, timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if data.get("status") == "success" or data.get("errCode") == "00":
                        return {"success": True, "message": data.get("reason", "Allowed to cancel")}
                    return {"success": False, "error": data.get("reason", "Not allowed to cancel")}
                return {"success": False, "error": f"HTTP {resp.status}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

async def api_execute_full_cancel_od(order_id, shipment_id, chat_id, reason_id=1150, reason_text="No longer needed"):
    token, cookies = _orders_auth(chat_id)
    headers = _orders_headers(token)
    url = "https://www.jiomart.com/ext/hellcat/order/fullcancel"
    payload = {"shipment_id": str(shipment_id), "reason_id": int(reason_id),
               "reason": str(reason_text), "order_id": str(order_id), "source": "JCP"}
    try:
        async with aiohttp.ClientSession(cookies=cookies, timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if data.get("status") == "success" or data.get("errCode") == "00":
                        return {"success": True, "message": data.get("message", "Order cancelled successfully")}
                    return {"success": False, "error": data.get("message", "Cancellation failed")}
                return {"success": False, "error": f"HTTP {resp.status}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

async def show_order_history(chat_id: str, message, page: int = 0):
    """Fetch (up to 100) orders and display a page with a View + Cancel button
    per order (apiready1 style - 30 orders/page)."""
    if chat_id not in ORDER_HISTORY_CACHE:
        if message:
            await safe_edit_html(message, "📦 <b>Fetching order history (up to 100)...</b>", parse_mode="HTML")
        res = await api_get_my_orders_od(chat_id, limit=ORDERS_MAX_SHOWN_OD)
        if not res.get("success"):
            if message:
                await safe_edit_html(message, f"❌ <b>Order History Error:</b> {html.escape(str(res.get('error', '')))}", parse_mode="HTML")
            return
        ORDER_HISTORY_CACHE[chat_id] = res.get("data") or []
    orders = ORDER_HISTORY_CACHE.get(chat_id) or []
    if not orders:
        if message:
            await safe_edit_html(message, "📭 <b>No orders found.</b>",
                                 reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Main Menu", callback_data="back_home")]]))
        return
    total_pages = max(1, (len(orders) + ORDERS_PER_PAGE_OD - 1) // ORDERS_PER_PAGE_OD)
    page = max(0, min(page, total_pages - 1))
    start = page * ORDERS_PER_PAGE_OD
    page_orders = orders[start:start + ORDERS_PER_PAGE_OD]
    pending = []
    for order in page_orders:
        shipments = order.get("shipments") or order.get("bags") or []
        first_ship = shipments[0] if isinstance(shipments, list) and shipments else {}
        sid = (first_ship.get("shipment_id") or first_ship.get("id")) or order.get("shipment_id") or ""
        pending.append(sid)
    ship_results = await asyncio.gather(*[api_get_shipment_details_od(sid, chat_id) for sid in pending])
    lines = [f"📦 <b>Order History (Showing {min(len(page_orders), ORDERS_PER_PAGE_OD)} of {len(orders)})</b>\n"]
    keyboard = []
    for local_idx, order in enumerate(page_orders, 1):
        global_idx = start + local_idx
        shipments = order.get("shipments") or order.get("bags") or []
        first_ship = shipments[0] if isinstance(shipments, list) and shipments else {}
        order_id = order.get("order_id") or order.get("id") or "N/A"
        shipment_id = (first_ship.get("shipment_id") or first_ship.get("id")) or order.get("shipment_id") or ""
        ship_details = ship_results[local_idx - 1]
        price = ship_details.get("price", 0)
        ship_status = ship_details.get("status", "N/A")
        date_str = ship_details.get("date") or date_str_from_order(order) or "N/A"
        raw_status = order.get("status") or order.get("order_status") or "Placed"
        if isinstance(raw_status, dict):
            raw_status = raw_status.get("name") or raw_status.get("value") or raw_status.get("status") or "Placed"
        combined = f"{raw_status} {ship_status}".lower()
        icon = "🔴 Cancelled" if "cancel" in combined else f"🟢 {raw_status}"
        total_items = 0
        if shipments and isinstance(shipments[0], dict):
            total_items = sum(int(s.get("quantity", 0) or 0) for s in shipments[0].get("items", []) or [])
        lines.append(f"<b>Order #{global_idx}</b> | ₹{price} | {total_items} items | {date_str}\nStatus: {icon}")
        # apiready1 style: [View Order] [Cancel] on the same row
        if shipment_id:
            keyboard.append([InlineKeyboardButton(f"#{global_idx} 📄 View", callback_data=f"view_order_{global_idx}"),
                             InlineKeyboardButton(f"❌ Cancel", callback_data=f"order_cancel_{global_idx}")])
        else:
            keyboard.append([InlineKeyboardButton(f"#{global_idx} 📄 View", callback_data=f"view_order_{global_idx}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"orders_page_{page - 1}"))
    nav.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data="none"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"orders_page_{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔙 Main Menu", callback_data="back_home")])
    await send_safe_html(message, "\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))

def _strftime_dt(dt):
    try:
        return dt.strftime("%d-%b-%Y | %I:%M %p")
    except Exception:
        return None

def _format_date_value(raw_date):
    """apiready1 deep date parser: epoch/ISO -> '%d-%b-%Y | %I:%M %p'."""
    if raw_date is None:
        return None
    try:
        if isinstance(raw_date, (int, float)):
            if raw_date > 10000000000:
                raw_date = raw_date / 1000.0
            return _strftime_dt(datetime.datetime.fromtimestamp(raw_date))
        s = str(raw_date).strip()
        if s.lower() in ("none", "null", "n/a", ""):
            return None
        clean = s.replace("Z", "").split(".")[0]
        if clean.isdigit():
            return _strftime_dt(datetime.datetime.fromtimestamp(float(clean)))
        return _strftime_dt(datetime.datetime.fromisoformat(clean))
    except Exception:
        return None

def date_str_from_order(order: dict) -> str:
    """apiready1 extract_date_time_from_order: deep date parser with all fallbacks."""
    if not isinstance(order, dict):
        return ""
    ships = order.get("shipments") or order.get("bags") or []
    if not isinstance(ships, list):
        ships = []
    first_ship = ships[0] if (len(ships) and isinstance(ships[0], dict)) else {}
    possible_dates = [
        order.get("order_date"),
        order.get("created_at"),
        order.get("order_created_at"),
        order.get("order_time"),
        order.get("created_date"),
        first_ship.get("created_at"),
        first_ship.get("shipment_created_at"),
        first_ship.get("order_created_at"),
        first_ship.get("date"),
    ]
    if isinstance(order.get("meta"), dict):
        mv = order["meta"].get("created_at")
        if isinstance(mv, dict):
            mv = mv.get("value") or mv.get("label") or mv.get("name")
        possible_dates.append(mv)
    for raw in possible_dates:
        if raw is None:
            continue
        if isinstance(raw, dict):
            raw = raw.get("value") or raw.get("label") or raw.get("name")
        if raw is None:
            continue
        val = _format_date_value(raw)
        if val:
            return val
    return ""

async def view_order_details_od(chat_id: str, message, idx: int):
    """apiready1 view_order_handler: detailed order view (Order ID, Shipment ID,
    Price, Status, Date, Items list) with Cancel button."""
    orders = ORDER_HISTORY_CACHE.get(chat_id) or []
    if not (1 <= idx <= len(orders)):
        if message:
            await safe_edit_html(message, "❌ <b>Order not found.</b> Try refreshing the list.",
                                 reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Orders", callback_data="action_order_history")]]), parse_mode="HTML")
        return
    order = orders[idx - 1]
    order_id = order.get("order_id") or order.get("id") or "N/A"
    shipments = order.get("shipments") or order.get("bags") or []
    first_ship = shipments[0] if isinstance(shipments, list) and shipments else {}
    shipment_id = (first_ship.get("shipment_id") or first_ship.get("id")) or order.get("shipment_id") or "N/A"
    ship_details = await api_get_shipment_details_od(shipment_id, chat_id)
    raw_status = order.get("status") or order.get("order_status") or "Placed"
    if isinstance(raw_status, dict):
        raw_status = raw_status.get("name") or raw_status.get("value") or raw_status.get("status") or "Placed"
    combined = f"{raw_status} {ship_details.get('status', 'N/A')}".lower()
    is_cancelled = "cancel" in combined
    msg = [f"📦 <b>Order Details #{idx}</b>\n",
           f"🆔 <b>Order ID:</b> <code>{html.escape(str(order_id))}</code>",
           f"📦 <b>Shipment ID:</b> <code>{html.escape(str(shipment_id))}</code>",
           f"💰 <b>Price:</b> ₹{ship_details.get('price', 0)}",
           f"📋 <b>Status:</b> {'🔴 Cancelled' if is_cancelled else f'🟢 {raw_status}'}",
           f"📅 <b>Date:</b> {ship_details.get('date') or date_str_from_order(order) or 'N/A'}"]
    items = first_ship.get("items", []) or []
    if items:
        msg.append(f"\n📊 <b>Items ({len(items)}):</b>")
        for item in items[:8]:
            product = item.get("product", {}) if isinstance(item.get("product"), dict) else {}
            name = product.get("name") or item.get("name") or "Unknown"
            qty = item.get("quantity", 0)
            img_link = ""
            msg.append(f"• {html.escape(str(name))} x{qty}")
        if len(items) > 8:
            msg.append(f"... and {len(items) - 8} more items")
    keyboard = []
    if shipment_id and str(shipment_id) not in ("", "N/A", "None", "none", "null"):
        keyboard.append([InlineKeyboardButton("❌ Cancel Order", callback_data=f"order_cancel_{idx}")])
    keyboard.append([InlineKeyboardButton("🔙 Back to Orders", callback_data="action_order_history"),
                     InlineKeyboardButton("🏠 Main Menu", callback_data="back_home")])
    if message:
        await safe_edit_html(message, "\n".join(msg),
                             reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def cancel_order_od(chat_id: str, message, idx: int):
    """One-click cancel an order and refresh the history view."""
    orders = ORDER_HISTORY_CACHE.get(chat_id) or []
    if not (1 <= idx <= len(orders)):
        if message:
            await safe_edit_html(message, "❌ <b>Order not found.</b>", parse_mode="HTML")
        return
    order = orders[idx - 1]
    order_id = order.get("order_id") or order.get("id") or "N/A"
    shipments = order.get("shipments") or order.get("bags") or []
    first_ship = shipments[0] if isinstance(shipments, list) and shipments else {}
    shipment_id = (first_ship.get("shipment_id") or first_ship.get("id")) or order.get("shipment_id") or ""
    if not shipment_id or str(shipment_id).lower() in ("", "n/a", "none", "null"):
        if message:
            await safe_edit_html(message, "❌ <b>This order has no cancellable shipment.</b>", parse_mode="HTML")
        return
    if message:
        await safe_edit_html(message, f"❌ Cancelling order <b>#{idx}</b>...", parse_mode="HTML")
    verify = await api_verify_cancel_od(order_id, shipment_id, chat_id)
    if not verify.get("success"):
        if message:
            await safe_edit_html(message, f"❌ <b>Cannot Cancel:</b> {html.escape(str(verify.get('error')))}", parse_mode="HTML")
        return
    cancel = await api_execute_full_cancel_od(order_id, shipment_id, chat_id)
    if cancel.get("success"):
        msg = f"✅ <b>Order Cancelled Successfully!</b>\n\n🆔 Order ID: <code>{html.escape(str(order_id))}</code>"
        ORDER_HISTORY_CACHE.pop(chat_id, None)
    else:
        msg = f"❌ <b>Cancellation Failed:</b> {html.escape(str(cancel.get('error')))}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🧾 View Updated Orders", callback_data="action_order_history")],
        [InlineKeyboardButton("🔙 Main Menu", callback_data="back_home")],
    ])
    try:
        await message.edit_text(msg, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await message.reply_text(msg, reply_markup=kb, parse_mode="HTML")

def build_billing_summary(chat_id: str) -> str:
    """OD35 dashboard: offline cart table + delivery info + live IDs (jm RAM)."""
    cart = load_offline_cart(chat_id)
    addr = get_default_offline_address(chat_id) or {}
    ram = RAM_CACHE.get(str(chat_id), {})
    active_key, _ = get_active_account(chat_id)
    loc = get_resolved_location(chat_id)
    pincode = loc["pincode"]
    name = str(addr.get("name") or "")
    phone = str(addr.get("phone") or "")
    address = ", ".join(x for x in [str(addr.get("address1") or ""), str(addr.get("address2") or "")] if x)
    token_str = (ram.get("token")[:16] + "...") if ram.get("token") else "NOT SYNCED"
    store_raw = _normalize_store_id(ram.get("store_id"))
    store_id_str = store_raw if store_raw else "Auto (from cart)"
    if ram.get("store_code"):
        store_id_str += f" [{ram.get('store_code')}]"
    polygon_raw = str(ram.get("polygon_id") or "")
    if not polygon_raw:
        try:
            polygon_raw = str(extract_qc_identity(str(chat_id)).get("polygon_id") or "")
        except Exception:
            polygon_raw = ""
    final_amount = ""
    for _ls in (USER_ACTIVE_SESSIONS.get(chat_id) or []):
        if isinstance(_ls, dict) and _ls.get("cart_price") and str(_ls.get("cart_price")) not in ("", "0.00", "0", "Error", "None"):
            final_amount = str(_ls["cart_price"])
            break
    if not final_amount:
        final_amount = str(ram.get("cart_price") or "0.00")

    def to_int(v, d=1):
        try: return int(v or d)
        except (TypeError, ValueError): return d
    def to_float(v, d=0.0):
        try: return float(v or d)
        except (TypeError, ValueError): return d

    name_w, qp_w, tot_w = 16, 10, 12
    dash = "-" * (name_w + 1 + qp_w + 1 + 3 + 1 + tot_w)
    hdr = f"{'Item Name':<{name_w}} {'Qty x Price':>{qp_w}} = {'Total':>{tot_w}}"
    rows = []
    total_count = 0
    for slug, it in cart.items():
        if not isinstance(it, dict): continue
        nm = str(it.get("name") or slug_to_product_name(slug))[:name_w]
        qty = to_int(it.get("quantity", 1))
        pr = to_float(it.get("price", 0))
        lt = pr * qty
        price_s = f"{pr:.2f}" if pr > 0 else "NA"
        total_s = f"{lt:.2f}" if pr > 0 else "NA"
        rows.append(f"{nm:<{name_w}} {f'{qty} x {price_s}':>{qp_w}} = {total_s:>{tot_w}}")
        total_count += qty
    item_rows = "\n".join(rows) if rows else "Cart is empty."
    total_val = sum(to_float(it.get("price", 0)) * to_int(it.get("quantity", 1))
                    for it in cart.values() if isinstance(it, dict))
    body_lines = [
        hdr, dash, item_rows, dash,
        f"{'Total Items:':<20} {total_count:>16}",
        f"{'🛍️ Total Amount:':<20} ₹{total_val:>13.2f}",
        "",
        "🚚 Delivery Info:",
        f"👤 {name:<18} 📱 {phone}",
        f"🏠 {address}",
        f"📍 Pin: {pincode} ({loc['city']}, {loc['state']})",
        "",
        "🔑 Active Session & Database IDs:",
        f"📁 Active Session: {active_key or 'None'}",
        f"🏪 Store: {store_id_str} | 📍 Polygon: {polygon_raw or 'N/A'}",
        f"💰 Final Amount: ₹{final_amount}",
        f"🎫 Bearer Token: {token_str}",
        f"📍 Address ID: {ram.get('address_id') or 'N/A'} | 🛒 Cart ID: {ram.get('cart_id') or 'READY'}",
    ]
    body = "\n".join(body_lines)
    header = f"📊 Your Dashboard [{BOT_VERSION}]"
    return f"{header}\n\n<pre><code class=\"language-text\">{html.escape(body)}</code></pre>"

async def run_step_inject_address_api(chat_id: str, message=None):
    """OD35: inject the default offline address into the account via API."""
    def_addr = get_default_offline_address(chat_id)
    if not def_addr:
        if message:
            await safe_edit_html(message, "❌ No default offline address set! Add one in 💾 Offline Database.", parse_mode="HTML")
        return False
    loc = get_resolved_location(chat_id)
    pin_now = str(loc["pincode"])
    lat_now, lon_now = loc["latitude"], loc["longitude"]
    if message:
        await safe_edit_html(message, f"🏠 <b>Inject Address (PIN: {pin_now})...</b>", parse_mode="HTML")
    token, cookies = get_pure_api_auth(chat_id)
    if not token:
        if message:
            await safe_edit_html(message, "❌ No valid auth token! Run <b>🔄 2-Step Sync</b> first.", parse_mode="HTML")
        return False
    ram = RAM_CACHE.get(str(chat_id)) or {}
    cart_id = ram.get("cart_id", "")
    add_res = await api_ensure_or_add_default_address_direct(cookies, token, def_addr, lat_now, lon_now, cart_id=cart_id)
    if add_res.get("success"):
        new_hex_id = add_res.get("hex_id")
        ram = RAM_CACHE.setdefault(str(chat_id), {})
        ram["address_id"] = new_hex_id
        ram["pincode"] = pin_now
        ram["latitude"] = float(lat_now)
        ram["longitude"] = float(lon_now)
        try:
            configs = load_configs()
            cfg = configs.setdefault(str(chat_id), {})
            cfg["address_id"] = new_hex_id
            cfg["latitude"] = str(float(lat_now))
            cfg["longitude"] = str(float(lon_now))
            cfg["pincode"] = str(pin_now)
            cfg[DELIVERY_LOCATION_KEY] = {"pincode": str(pin_now), "latitude": float(lat_now),
                                          "longitude": float(lon_now), "city": str(loc.get("city", "")),
                                          "state": str(loc.get("state", ""))}
            save_configs(configs)
        except Exception:
            pass
        if message:
            if add_res.get("old_found"):
                await safe_edit_html(message, f"✅ <b>Address Injected:</b> Deleted {add_res.get('deleted_count', 0)} old address(es) & added new via API (ID: <code>{new_hex_id}</code>)!", parse_mode="HTML")
            else:
                await safe_edit_html(message, f"✅ <b>Address Injected via API</b> (ID: <code>{new_hex_id}</code>)!", parse_mode="HTML")
        return True
    if message:
        await safe_edit_html(message, f"❌ Address Injection Failed: {add_res.get('error')}", parse_mode="HTML")
    return False

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = str(update.effective_chat.id)
    data = query.data
    logger.info(f"[CB] {chat_id} -> {data}")
    if data != "back_home":        # user navigated away from home -> stop live countdown
        HOME_LIVE_MSGS.pop(chat_id, None)

    if data == "force_stop_all":
        # 🛑 GLOBAL FORCE STOP (apiready1 pattern): cancel every running task,
        # close open browsers, clear waiting-input states for this chat.
        try:
            await query.answer("🛑 Emergency Force Stop Executed!", show_alert=True)
        except Exception:
            pass
        FORCE_STOP_FLAG[chat_id] = True
        cancelled = 0
        for t in list(RUNNING_TASKS.get(chat_id, set())):
            if not t.done():
                t.cancel()
                cancelled += 1
        RUNNING_TASKS.pop(chat_id, None)
        # Also abort any in-progress browser LOGIN flow (apiready1 LOGIN_TEMP).
        login_obj = LOGIN_TEMP.pop(chat_id, None)
        if login_obj:
            try: await login_obj["browser"].close()
            except Exception: pass
            try:
                if login_obj.get("pw"): await login_obj["pw"].stop()
            except Exception: pass
        bd = USER_BROWSERS.pop(chat_id, None)
        browser_note = "No browser open"
        if bd:
            try: await bd["ctx"].close()
            except Exception: pass
            try: await bd["browser"].close()
            except Exception: pass
            try:
                if bd.get("pw"):
                    await bd["pw"].stop()
            except Exception: pass
            browser_note = "Browser CLOSED"
        AWAITING_INPUT.pop(chat_id, None)
        SELECTED_PAYMENTS.pop(chat_id, None)
        logger.info(f"[FORCE STOP] {chat_id}: tasks cancelled={cancelled}, {browser_note}")
        try:
            await query.edit_message_text(
                f"🛑 <b>FORCE STOP EXECUTED! [{BOT_VERSION}]</b>\n\n"
                f"✅ <b>Tasks cancelled:</b> {cancelled}\n"
                f"🌐 <b>{browser_note}</b>\n\n"
                f"All running operations stopped immediately. Start fresh from Main Menu.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Main Menu", callback_data="back_home")]]),
                parse_mode="HTML")
        except Exception:
            try:
                await query.message.reply_text(
                    "🛑 <b>FORCE STOP EXECUTED!</b>",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Main Menu", callback_data="back_home")]]),
                    parse_mode="HTML")
            except Exception:
                pass
        return

    if data == "action_view_profile":
        msg = await query.message.reply_text("👤 <b>Opening Profile...</b>", parse_mode="HTML")
        register_task(chat_id, asyncio.create_task(open_view_profile(chat_id, msg)))

    elif data == "action_save_close_browser":
        msg = await query.message.reply_text("💾 <b>Saving Session & Closing...</b>", parse_mode="HTML")
        await handle_save_close_browser(chat_id, msg)

    elif data == "action_close_browser":
        msg = await query.message.reply_text("❌ <b>Closing Browser...</b>", parse_mode="HTML")
        await handle_close_browser(chat_id, msg)

    elif data == "action_2step_sync":
        msg = await query.message.reply_text(f"🔄 <b>Starting 2-Step Browser Sync [{BOT_VERSION}] (10s Address + 5s Cart)...</b>", parse_mode="HTML")
        register_task(chat_id, asyncio.create_task(run_2step_browser_sync(chat_id, msg)))

    elif data == "count_dec":
        set_user_count(chat_id, get_user_count(chat_id) - 1)
        await query.edit_message_reply_markup(reply_markup=get_main_keyboard(chat_id))

    elif data == "count_inc":
        set_user_count(chat_id, get_user_count(chat_id) + 1)
        await query.edit_message_reply_markup(reply_markup=get_main_keyboard(chat_id))

    elif data == "open_count_dropdown":
        await query.edit_message_text("🔢 <b>Select Batch Count:</b>", reply_markup=get_dropdown_keyboard(), parse_mode="HTML")

    elif data.startswith("set_count_"):
        set_user_count(chat_id, int(data.replace("set_count_", "")))
        await start_command(update, context)

    elif data == "action_read_all_api":
        count = get_user_count(chat_id)
        msg = await query.message.reply_text(f"⚡ <b>Syncing {count}x Sessions via API [{BOT_VERSION}]...</b>", parse_mode="HTML")
        register_task(chat_id, asyncio.create_task(run_multi_session_api_sync(chat_id, count, msg)))

    elif data == "action_fire_to_payment":
        msg = await query.message.reply_text(f"🚀 <b>Executing Checkout to Payment Page [{BOT_VERSION}]...</b>", parse_mode="HTML")
        register_task(chat_id, asyncio.create_task(run_fire_to_payment_page(chat_id, msg)))

    elif data.startswith("toggle_pay_"):
        idx = int(data.replace("toggle_pay_", ""))
        selected = SELECTED_PAYMENTS.setdefault(str(chat_id), set())
        if idx in selected:
            selected.remove(idx)
        else:
            selected.add(idx)
        await show_payment_selection_screen(chat_id, query.message)

    elif data == "pay_select_all":
        pending = PENDING_PAYMENTS.get(str(chat_id), {})
        SELECTED_PAYMENTS[str(chat_id)] = set(pending.keys())
        await show_payment_selection_screen(chat_id, query.message)

    elif data == "pay_clear_all":
        SELECTED_PAYMENTS[str(chat_id)] = set()
        await show_payment_selection_screen(chat_id, query.message)

    elif data == "pay_confirm_fire_selected":
        msg = await query.message.reply_text(f"⚡ <b>Confirming Orders [{BOT_VERSION}]...</b>", parse_mode="HTML")
        register_task(chat_id, asyncio.create_task(execute_final_payment_seal(chat_id, msg)))

    elif data == "action_clear_ram":
        RAM_CACHE.pop(str(chat_id), None)
        USER_ACTIVE_SESSIONS.pop(str(chat_id), None)
        PENDING_PAYMENTS.pop(str(chat_id), None)
        SELECTED_PAYMENTS.pop(str(chat_id), None)
        await query.edit_message_text(f"🧹 <b>RAM Cleared [{BOT_VERSION}]!</b>", reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")

    elif data == "open_session_manager":
        if is_admin(chat_id):
            all_keys = get_all_session_keys()
            label = "Total Sessions (ALL users)"
        else:
            all_keys = sorted(get_user_saved_keys(chat_id).keys())
            label = "My Sessions"
        await query.edit_message_text(
            f"🔑 <b>Select Active Session:</b>\n"
            f"📊 {label}: <b>{len(all_keys)}</b> (50 per page)",
            reply_markup=get_session_pagination_keyboard(chat_id, 0),
            parse_mode="HTML",
        )

    elif data.startswith("sess_page_"):
        try:
            page = int(data.replace("sess_page_", ""))
        except ValueError:
            page = 0
        await query.edit_message_reply_markup(reply_markup=get_session_pagination_keyboard(chat_id, page))

    elif data == "off_db_menu":
        off_addrs = load_offline_addresses(chat_id)
        cart = load_offline_cart(chat_id)
        def_addr = get_default_offline_address(chat_id)
        loc = get_resolved_location(chat_id)
        kb = [
            [InlineKeyboardButton("➕ Add Offline Address", callback_data="off_add_addr")],
            [InlineKeyboardButton("🏠 Inject Address (API)", callback_data="action_inject_address")],
            [InlineKeyboardButton("📦 Add To Offline Cart", callback_data="off_add_prod")],
            [InlineKeyboardButton("🛒 Inject Cart (API)", callback_data="action_inject_cart")],
            [InlineKeyboardButton(f"🛒 View Cart ({len(cart)} item(s))", callback_data="off_view_cart")],
            [InlineKeyboardButton("⭐ Default Address", callback_data="off_set_default")],
            [InlineKeyboardButton(f"🎟️ Set Preset Coupon: {get_preset_coupon(chat_id) or 'None'}",
                                  callback_data="cart_set_preset_coupon"),
             InlineKeyboardButton("✅ Apply Now", callback_data="cart_apply_preset_coupon")],
            [InlineKeyboardButton("📥 Read Online Cart", callback_data="cart_read_online"),
             InlineKeyboardButton("🧹 Empty Cart", callback_data="cart_empty_cart")],
            [InlineKeyboardButton("🗑️ Delete Offline Data", callback_data="off_del_list")],
            [InlineKeyboardButton("🔙 Main Menu", callback_data="back_home")],
        ]
        sub = cart_subtotal(cart)
        addr_str = f"{html.escape(str(def_addr.get('name', '')))} | {loc['pincode']}" if def_addr else "Not set"
        await query.edit_message_text(
            f"💾 <b>Offline Database</b>\n\n"
            f"📍 Addresses: <b>{len(off_addrs)}</b>\n"
            f"🛒 Items: <b>{len(cart)}</b> | Subtotal: <b>₹{sub:.2f}</b>\n"
            f"⭐ Default Address: <b>{addr_str}</b>",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

    elif data == "off_add_addr":
        OFFLINE_ADDR_TEMP.pop(chat_id, None)
        AWAITING_INPUT[chat_id] = "off_addr_pin"
        await query.edit_message_text(
            "➕ <b>Add Offline Address</b>\n\nSend 6-digit Pincode (e.g. 754004):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="off_db_menu")]]), parse_mode="HTML")

    elif data == "off_add_prod":
        AWAITING_INPUT[chat_id] = "off_prod"
        await query.edit_message_text(
            "📦 <b>Add To Offline Cart</b>\n\nSend JioMart link (e.g. <code>store1.jiomartjcp.com/l/uYGl 5</code>):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="off_db_menu")]]), parse_mode="HTML")

    elif data == "off_view_cart":
        cart = load_offline_cart(chat_id)
        if not cart:
            kb = [[InlineKeyboardButton("➕ Add To Cart", callback_data="off_add_prod")],
                  [InlineKeyboardButton("🔍 Search Products", callback_data="action_search_prod")],
                  [InlineKeyboardButton("🔙 Back", callback_data="off_db_menu")]]
            await query.edit_message_text("🛒 <b>Cart is empty.</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
            return
        await query.edit_message_text(build_billing_summary(chat_id),
                                      reply_markup=InlineKeyboardMarkup(offcart_markup(chat_id)),
                                      parse_mode="HTML")

    elif data == "off_clear_cart":
        clear_offline_cart(chat_id)
        await query.edit_message_text("🧹 <b>Offline Cart Cleared!</b>", reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")

    elif data.startswith("off_del_item_"):
        try: j = int(data.replace("off_del_item_", ""))
        except ValueError: j = -1
        cart = load_offline_cart(chat_id)
        keys = list(cart.keys())
        if 0 <= j < len(keys):
            cart.pop(keys[j], None)
            save_offline_cart(chat_id, cart)
            await query.answer("🗑️ Item Deleted!", show_alert=False)
            await start_command(update, context)

    elif data == "off_set_default":
        off_addrs = load_offline_addresses(chat_id)
        if not off_addrs:
            return await query.edit_message_text("❌ No offline address found.", parse_mode="HTML")
        kb = []
        for i, a in enumerate(off_addrs):
            star = " ⭐" if (isinstance(a, dict) and a.get("is_default")) else ""
            kb.append([InlineKeyboardButton(f"📌 {a.get('name', 'Address')} | {a.get('pincode', '')}{star}", callback_data=f"off_set_default_{i}")])
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="off_db_menu")])
        await query.edit_message_text("⭐ <b>Select Default Offline Address:</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

    elif data.startswith("off_set_default_"):
        try: idx = int(data.replace("off_set_default_", ""))
        except ValueError: idx = -1
        set_default_offline_address(chat_id, idx)
        await query.answer("⭐ Default Address Set!", show_alert=False)
        await start_command(update, context)

    elif data == "off_del_list":
        off_addrs = load_offline_addresses(chat_id)
        kb = []
        for i, a in enumerate(off_addrs):
            kb.append([InlineKeyboardButton(f"🗑️ {a.get('name', 'Address')} ({a.get('pincode', '')})", callback_data=f"off_del_addr_{i}")])
        kb.append([InlineKeyboardButton("🧹 Delete All Addresses", callback_data="off_del_all_addr")])
        kb.append([InlineKeyboardButton("🧹 Clear Cart", callback_data="off_clear_cart")])
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="off_db_menu")])
        await query.edit_message_text("🗑️ <b>Delete Offline Data:</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

    elif data.startswith("off_del_addr_"):
        try: idx = int(data.replace("off_del_addr_", ""))
        except ValueError: idx = -1
        off_addrs = load_offline_addresses(chat_id)
        if 0 <= idx < len(off_addrs):
            off_addrs.pop(idx)
            save_offline_addresses(chat_id, off_addrs)
            await query.answer("✅ Address Deleted!", show_alert=False)
            await start_command(update, context)

    elif data == "off_del_all_addr":
        save_offline_addresses(chat_id, [])
        await query.edit_message_text("🧹 <b>All Offline Addresses Deleted!</b>", reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")

    elif data == "action_inject_address":
        msg = await query.message.reply_text("🏠 <b>Injecting offline address via API...</b>", parse_mode="HTML")
        register_task(chat_id, asyncio.create_task(run_step_inject_address_api(chat_id, msg)))

    elif data == "action_inject_cart":
        msg = await query.message.reply_text("🛒 <b>Injecting Offline Cart items via API...</b>", parse_mode="HTML")
        register_task(chat_id, asyncio.create_task(run_step_inject_offline_cart(chat_id, msg)))

    elif data == "action_order_history":
        msg = await query.message.reply_text("📦 <b>Fetching order history (up to 100)...</b>", parse_mode="HTML")
        ORDER_HISTORY_CACHE.pop(chat_id, None)
        asyncio.create_task(show_order_history(chat_id, msg, page=0))

    elif data == "orders_back":
        msg = await query.message.reply_text("📦 <b>Fetching order history...</b>", parse_mode="HTML")
        asyncio.create_task(show_order_history(chat_id, msg, page=0))

    elif data.startswith("orders_page_"):
        try:
            p = int(data.replace("orders_page_", ""))
        except ValueError:
            p = 0
        asyncio.create_task(show_order_history(chat_id, query.message, page=p))

    elif data.startswith("view_order_"):
        try:
            v_idx = int(data.replace("view_order_", ""))
        except ValueError:
            v_idx = 0
        if not ORDER_HISTORY_CACHE.get(chat_id):
            msg = await query.message.reply_text("📦 <b>Fetching order details...</b>", parse_mode="HTML")
            res = await api_get_my_orders_od(chat_id, limit=ORDERS_MAX_SHOWN_OD)
            if res.get("success"):
                ORDER_HISTORY_CACHE[chat_id] = res.get("data") or []
            await msg.delete()
        await view_order_details_od(chat_id, query.message, v_idx)

    elif data.startswith("order_cancel_"):
        try:
            od_idx = int(data.replace("order_cancel_", ""))
        except ValueError:
            od_idx = 0
        await cancel_order_od(chat_id, query.message, od_idx)

    elif data.startswith("quick_add_"):
        rest = data[len("quick_add_"):]
        uid1, _, slug1 = rest.partition("_")
        uid1 = uid1.strip()
        if not uid1 or not uid1.isdigit():
            await query.edit_message_text("❌ Invalid product! Search again (🔍 Search Products).",
                                          reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")
            return
        prod1 = (PRODUCT_SEARCH_CACHE.get(str(chat_id)) or {}).get(uid1) or {}
        name1 = str(prod1.get("name") or slug1 or "Product")
        cart = load_offline_cart(chat_id)
        existing = cart.get(uid1)
        if existing and isinstance(existing, dict):
            existing["quantity"] = _cap_qty(existing, int(existing.get("quantity", 1) or 1) + 1)
            cart[uid1] = existing
        else:
            if not prod1:
                prod1 = {"uid": uid1, "slug": slug1, "name": name1, "sp": 0, "size": "OS",
                         "vertical": "GROCERIES", "seller_id": 1}
            cart[uid1] = _entry_from_product(uid1, prod1, quantity=1)
        save_offline_cart(chat_id, cart)
        try:
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup(product_cart_markup(chat_id, uid1, prod1)))
        except Exception as e:
            logger.warning(f"quick_add markup swap error: {e}")
        await refresh_search_cards(context, chat_id, skip_mid=query.message.message_id)
        n_qty = int(cart[uid1]["quantity"])
        await query.answer(f"✅ {html.unescape(name1)[:40]} added to OFFLINE cart (qty {n_qty})", show_alert=False)

    elif data.startswith("qty_"):
        # LEGACY uid-based search-card controls (old screens) - same offline-only logic
        action, _, uid2 = data[len("qty_"):].rpartition("_")
        cart = load_offline_cart(chat_id)
        it = cart.get(uid2)
        if not it or not isinstance(it, dict):
            return
        if action == "min":
            q = int(it.get("quantity", 1) or 1) - 1
            if q <= 0:
                cart.pop(uid2, None)
            else:
                it["quantity"] = q
                cart[uid2] = it
        elif action == "plus":
            it["quantity"] = _cap_qty(it, int(it.get("quantity", 1) or 1) + 1)
            cart[uid2] = it
        elif action == "del":
            cart.pop(uid2, None)
        save_offline_cart(chat_id, cart)
        prod2 = (PRODUCT_SEARCH_CACHE.get(str(chat_id)) or {}).get(uid2) or {}
        try:
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup(product_cart_markup(chat_id, uid2, prod2)))
        except Exception as e:
            logger.warning(f"qty markup refresh error: {e}")
        await refresh_search_cards(context, chat_id, skip_mid=query.message.message_id)
        await query.answer("🛒 Offline cart updated")

    elif data.startswith("ocinc_") or data.startswith("ocdec_") or data.startswith("ocrem_"):
        # Cart page qty controls - INDEX-based (apiready1 cinc/cdec/crem formula).
        # apiready1 live-sync (api_update_jiomart_cart_item / sync_online_cart_qty)
        # stays in the file for later, but is NOT called here for now (offline only).
        action = "plus" if data.startswith("ocinc_") else ("min" if data.startswith("ocdec_") else "del")
        try:
            j = int(data.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            return
        cart = load_offline_cart(chat_id)
        keys = list(cart.keys())
        if not (0 <= j < len(keys)):
            return
        uid2 = keys[j]
        it = cart.get(uid2)
        if not it or not isinstance(it, dict):
            return
        if action == "min":
            if int(it.get("quantity", 1) or 1) > 1:
                it["quantity"] = int(it.get("quantity", 1) or 1) - 1
                cart[uid2] = it
            else:
                return await query.answer("❌ Minimum Qty 1। Delete କରିବା ପାଇଁ 🗑️ ବ୍ୟବହାର କରନ୍ତୁ।", show_alert=True)
        elif action == "plus":
            it["quantity"] = _cap_qty(it, int(it.get("quantity", 1) or 1) + 1)
            cart[uid2] = it
        elif action == "del":
            cart.pop(uid2, None)
        save_offline_cart(chat_id, cart)
        # Was this button on a SEARCH card, the SEARCH summary card, or a Cart view?
        is_search_card = query.message.message_id in (SEARCH_CARD_UIDS.get(str(chat_id)) or {})
        is_summary_card = query.message.message_id == SEARCH_SUMMARY_MSG.get(str(chat_id))
        prod2 = (PRODUCT_SEARCH_CACHE.get(str(chat_id)) or {}).get(uid2) or {}
        if is_search_card:
            # Search page: swap just this card's controls (Add button if removed)
            try:
                await query.edit_message_reply_markup(
                    reply_markup=InlineKeyboardMarkup(product_cart_markup(chat_id, uid2, prod2)))
            except Exception:
                pass
        elif is_summary_card:
            # Search-page summary card: re-render billing + controls + Complete/Cancel
            sum_text, sum_kb = build_search_summary_card(chat_id, SEARCH_KEYWORD.get(str(chat_id), ""))
            try:
                await query.edit_message_text(sum_text, reply_markup=sum_kb, parse_mode="HTML")
            except Exception:
                pass  # "message is not modified"
        else:
            # Cart / billing view: full re-render
            cart_after = load_offline_cart(chat_id)
            if not cart_after:
                kb = [[InlineKeyboardButton("➕ Add Product", callback_data="off_add_prod")],
                      [InlineKeyboardButton("🏠 Add Address", callback_data="off_add_addr")],
                      [InlineKeyboardButton("🔍 Search Products", callback_data="action_search_prod")],
                      [InlineKeyboardButton("🔙 Back", callback_data="off_db_menu")]]
                await query.edit_message_text("🛒 <b>Cart is empty.</b>",
                                              reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
            else:
                try:
                    await query.edit_message_text(build_billing_summary(chat_id),
                                                  reply_markup=InlineKeyboardMarkup(offcart_markup(chat_id)),
                                                  parse_mode="HTML")
                except Exception:
                    pass  # "message is not modified"
        # Keep every other open search card in sync (indices shift on add/remove)
        await refresh_search_cards(context, chat_id, skip_mid=query.message.message_id)
        await query.answer("🛒 Offline cart updated")

    elif data == "cart_view":
        # "Cart" home button: offline cart with [- qty + 🗑] controls + billing.
        cart = load_offline_cart(chat_id)
        if not cart:
            kb = [[InlineKeyboardButton("➕ Add Product", callback_data="off_add_prod")],
                  [InlineKeyboardButton("🏠 Add Address", callback_data="off_add_addr")],
                  [InlineKeyboardButton("🔍 Search Products", callback_data="action_search_prod")],
                  [InlineKeyboardButton("🔙 Back", callback_data="back_home")]]
            await query.edit_message_text("🛒 <b>Cart is empty.</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
            return
        await query.edit_message_text(build_billing_summary(chat_id),
                                      reply_markup=InlineKeyboardMarkup(offcart_markup(chat_id)),
                                      parse_mode="HTML")
        # 🎟️ PRESET COUPON AUTO-APPLY on cart open (silent background task;
        # needs live token from RAM - set once, applied every time).
        if get_preset_coupon(chat_id) and (RAM_CACHE.get(chat_id) or {}).get("token"):
            async def _auto_preset_coupon():
                _pc = get_preset_coupon(chat_id)
                _res = await api_apply_jiomart_coupon(chat_id, _pc)
                if _res.get("success"):
                    logger.info(f"[PresetCoupon] chat={chat_id} auto-applied {_pc} "
                                f"(₹{_res.get('value')} off)")
                else:
                    logger.info(f"[PresetCoupon] chat={chat_id} auto-apply failed: {_res.get('message')}")
            register_task(chat_id, asyncio.create_task(_auto_preset_coupon()))

    elif data == "cart_toggle_mode":
        # apiready1 toggle_sync_mode: OFFLINE <-> ONLINE cart mode (per chat)
        new_mode = "ONLINE" if get_cart_mode(chat_id) == "OFFLINE" else "OFFLINE"
        set_cart_mode(chat_id, new_mode)
        await query.answer(f"Mode: {new_mode}", show_alert=True)
        if not load_offline_cart(chat_id):
            await start_command(update, context)
        else:
            await query.edit_message_text(build_billing_summary(chat_id),
                                          reply_markup=InlineKeyboardMarkup(offcart_markup(chat_id)),
                                          parse_mode="HTML")

    elif data.startswith("cartpage_"):
        # Cart pagination: navigate between pages of cart items
        try:
            page = int(data.replace("cartpage_", ""))
        except ValueError:
            page = 0
        await query.edit_message_text(build_billing_summary(chat_id),
                                      reply_markup=InlineKeyboardMarkup(offcart_markup(chat_id, page=page)),
                                      parse_mode="HTML")

    elif data == "cart_sync_off_to_on":
        await query.answer("🔄 Sync Offline → Online: coming soon!", show_alert=True)

    elif data == "cart_sync_on_to_off":
        await query.answer("📥 Sync Online → Offline: coming soon!", show_alert=True)

    elif data == "cart_apply_coupon":
        pc = get_preset_coupon(chat_id)
        if pc:
            # PRESET MODE: coupon set once -> EVERY press applies the SAME coupon
            # directly (no code prompt every time).
            msg = await query.message.reply_text(
                f"⏳ <b>Applying Preset Coupon <code>{pc}</code>...</b>", parse_mode="HTML")
            res = await api_apply_jiomart_coupon(chat_id, pc)
            if res.get("success"):
                await msg.edit_text(
                    f"🎉 <b>Preset Coupon Applied ({pc})!</b> Savings: <b>₹{res.get('value')}</b> off\n{res.get('message', '')}",
                    reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")
            else:
                await msg.edit_text(
                    f"❌ <b>Preset Coupon Failed ({pc}):</b> {res.get('message')}",
                    reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")
            return
        # No preset yet -> ask ONCE to save it (after this it never asks again)
        COUPON_STATE[str(chat_id)] = "preset"
        await query.edit_message_text(
            "🎟️ <b>No Preset Coupon saved yet.</b>\n\nSend the coupon code ONCE to save it "
            "(e.g. <code>R2A5V1E4HOT</code>) — after that it AUTO-APPLIES every time:",
            parse_mode="HTML")

    elif data == "cart_set_preset_coupon":
        COUPON_STATE[str(chat_id)] = "preset"
        await query.edit_message_text("🎟️ <b>Set Preset Coupon</b>\n\nSend the coupon code to save (e.g. <code>R2A5V1E4HOT</code>):",
                                      parse_mode="HTML")

    elif data == "cart_apply_preset_coupon":
        pc = get_preset_coupon(chat_id)
        if not pc:
            COUPON_STATE[str(chat_id)] = "preset"
            await query.edit_message_text("⚠️ <b>No Preset Coupon saved.</b>\n\nSend a coupon code to save as preset:",
                                          parse_mode="HTML")
            return
        msg = await query.message.reply_text(f"⏳ <b>Applying Preset Coupon <code>{pc}</code>...</b>", parse_mode="HTML")
        res = await api_apply_jiomart_coupon(chat_id, pc)
        if res.get("success"):
            await msg.edit_text(f"🎉 <b>Preset Coupon Applied!</b> Savings: <b>₹{res.get('value')}</b> off\n{res.get('message', '')}",
                                reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")
        else:
            await msg.edit_text(f"❌ <b>Preset Coupon Failed:</b> {res.get('message')}",
                                reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")

    elif data == "cart_remove_coupon":
        msg = await query.message.reply_text("⏳ <b>Removing coupon...</b>", parse_mode="HTML")
        res = await api_remove_jiomart_coupon(chat_id)
        await msg.edit_text(("✅ <b>Coupon Removed!</b>" if res.get("success")
                             else f"⚠️ {res.get('message')}"),
                            reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")

    elif data == "cart_read_online":
        # 📥 Read Online Cart (apiready1 style): LIVE items + per-item qty controls
        ram = RAM_CACHE.get(chat_id) or {}
        if not ram.get("token"):
            await query.answer("❌ Run 🔄 2-Step Sync first (live token needed)!", show_alert=True)
            return
        try:
            await query.edit_message_text("⏳ <b>Reading live online cart via API...</b>", parse_mode="HTML")
        except Exception:
            pass
        items, cart_id = await api_get_live_cart_items(chat_id)
        dfee = await api_read_delivery_fee(chat_id)
        ONLINE_CART_VIEW[chat_id] = {"items": items, "cart_id": cart_id,
                                     "dfee": dfee,
                                     "mid": query.message.message_id}
        text, kb = build_online_cart_view(chat_id)
        try:
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await query.message.reply_text(text, reply_markup=kb, parse_mode="HTML")

    elif data == "onl_noop":
        await query.answer()

    elif data.startswith(("oninc_", "ondec_", "ondel_")):
        # 🌐 LIVE online-cart qty controls (apiready1 cart_adjust formula):
        # update_item / remove_item on the REAL JioMart cart via API.
        view = ONLINE_CART_VIEW.get(chat_id) or {}
        items = view.get("items") or []
        action = data.split("_", 1)[0]
        try:
            i = int(data.rsplit("_", 1)[1])
        except (ValueError, IndexError):
            return
        if not (0 <= i < len(items)):
            await query.answer("⚠️ View is stale - refreshing...")
            await rerender_online_cart(query, chat_id)
            return
        itm = items[i]
        q = int(itm.get("quantity", 1) or 1)
        if action == "oninc":
            new_q = q + 1
        elif action == "ondec":
            if q <= 1:
                return await query.answer("❌ Minimum Qty 1। Delete ପାଇଁ 🗑️ ବ୍ୟବହାର କରନ୍ତୁ।", show_alert=True)
            new_q = q - 1
        else:
            new_q = 0
        res = await api_update_jiomart_cart_item(
            chat_id, itm.get("item_id"), new_q,
            target_article_id=str(itm.get("article_id") or ""),
            cart_id_param=str(view.get("cart_id") or ""),
            current_qty=q, captured_items=items,
            live_cart_id=str(view.get("cart_id") or ""))
        if res.get("success"):
            try:
                await refresh_live_cart_price(chat_id)
            except Exception:
                pass
            await rerender_online_cart(query, chat_id)
            await query.answer(f"✅ Online cart: {'deleted' if new_q == 0 else f'qty {new_q}'}")
        else:
            await query.answer(f"❌ {str(res.get('debug') or 'Update failed')[:180]}", show_alert=True)

    elif data == "cart_empty_cart":
        # apiready1 empty cart: clear ONLINE (if ONLINE mode / token) and/or OFFLINE
        mode = get_cart_mode(chat_id)
        msg = await query.message.reply_text("⏳ <b>Emptying cart...</b>", parse_mode="HTML")
        rep = []
        off_count = len(load_offline_cart(chat_id))
        if off_count:
            clear_offline_cart(chat_id)
            rep.append(f"✅ <b>Offline Cart Cleared:</b> {off_count} items")
        else:
            rep.append("ℹ️ <b>Offline Cart:</b> already empty")
        ram = RAM_CACHE.get(str(chat_id)) or {}
        if mode == "ONLINE" and ram.get("token"):
            ok, detail = await api_clear_online_cart(chat_id)
            rep.append(("✅ <b>Online Cart Cleared via API</b>" if ok else f"⚠️ Online: {detail}"))
        elif mode == "ONLINE":
            rep.append("⚠️ <b>ONLINE mode</b> but no token — run 🔄 2-Step Sync to clear online")
        await msg.edit_text("🗑️ <b>Cart Emptied!</b>\n\n" + "\n".join(rep),
                            reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")

    elif data == "addr_view":
        # 🏠 Address Manager: offline address list (click = inject via API)
        text, kb = build_address_view(chat_id)
        try:
            await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await query.message.reply_text(text, reply_markup=kb, parse_mode="HTML")

    elif data == "addr_add_mode":
        # ➕ Add Address: choose ONLINE (inject via API) or OFFLINE (save to DB)
        kb = [
            [InlineKeyboardButton("🌐 Online — Inject to Account via API", callback_data="addr_add_online")],
            [InlineKeyboardButton("💾 Offline — Save to Offline DB", callback_data="addr_add_offline")],
            [InlineKeyboardButton("🔙 Back", callback_data="addr_view")],
        ]
        await query.edit_message_text(
            "➕ <b>Add Address</b>\n\nChoose mode:\n\n"
            "🌐 <b>Online</b> — Directly add to your JioMart account via API\n"
            "💾 <b>Offline</b> — Save to local DB (inject later manually)",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

    elif data == "addr_add_online":
        # 🌐 Online add: collect address fields then inject via API directly
        OFFLINE_ADDR_TEMP.pop(chat_id, None)
        AWAITING_INPUT[chat_id] = "addr_online_pin"
        await query.edit_message_text(
            "🌐 <b>Add Online Address (API)</b>\n\n"
            "Step 1/5: Send 6-digit Pincode (e.g. 754004):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Cancel", callback_data="addr_add_mode")]]),
            parse_mode="HTML")

    elif data == "addr_add_offline":
        # 💾 Offline add: same as existing off_add_addr flow
        OFFLINE_ADDR_TEMP.pop(chat_id, None)
        AWAITING_INPUT[chat_id] = "off_addr_pin"
        await query.edit_message_text(
            "➕ <b>Add Offline Address</b>\n\nSend 6-digit Pincode (e.g. 754004):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="addr_add_mode")]]),
            parse_mode="HTML")

    elif data.startswith("injaddr_"):
        # 📍 Inject a specific offline address from the list
        try:
            idx = int(data.replace("injaddr_", ""))
        except ValueError:
            idx = -1
        addrs = load_offline_addresses(chat_id)
        if not (0 <= idx < len(addrs)):
            return await query.answer("⚠️ Address not found!", show_alert=True)
        addr = addrs[idx]
        # Set as default then inject
        set_default_offline_address(chat_id, idx)
        msg = await query.message.reply_text(
            f"🏠 <b>Injecting address: {addr.get('name', 'Address')} ({addr.get('pincode', '')})...</b>",
            parse_mode="HTML")
        register_task(chat_id, asyncio.create_task(run_step_inject_address_api(chat_id, msg)))

    elif data == "addr_del_view":
        # 🗑 Delete address view
        addrs = load_offline_addresses(chat_id)
        if not addrs:
            return await query.answer("ℹ️ No offline addresses to delete!", show_alert=True)
        kb = []
        for i, a in enumerate(addrs):
            star = " ⭐" if a.get("is_default") else ""
            kb.append([InlineKeyboardButton(
                f"🗑 {a.get('name', 'Address')} ({a.get('pincode', '')}){star}",
                callback_data=f"addr_del_{i}")])
        kb.append([InlineKeyboardButton("🧹 Delete All", callback_data="addr_del_all")])
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="addr_view")])
        await query.edit_message_text(
            "🗑️ <b>Delete Address</b>\n\nTap an address to delete:",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

    elif data.startswith("addr_del_"):
        try:
            idx = int(data.replace("addr_del_", ""))
        except ValueError:
            idx = -1
        addrs = load_offline_addresses(chat_id)
        if 0 <= idx < len(addrs):
            deleted_name = addrs[idx].get("name", "Address")
            addrs.pop(idx)
            save_offline_addresses(chat_id, addrs)
            await query.answer(f"✅ Deleted: {deleted_name}", show_alert=False)
            # Refresh delete view
            text, kb = build_address_view(chat_id)
            try:
                await query.edit_message_text(text, reply_markup=kb, parse_mode="HTML")
            except Exception:
                pass
        else:
            await query.answer("⚠️ Invalid selection!", show_alert=True)

    elif data == "addr_del_all":
        save_offline_addresses(chat_id, [])
        await query.edit_message_text(
            "🧹 <b>All offline addresses deleted!</b>",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="addr_view")]]),
            parse_mode="HTML")

    elif data == "my_keys":
        text, mk = build_my_keys_view(chat_id)
        await query.edit_message_text(text, reply_markup=mk, parse_mode="HTML")

    elif data.startswith("mykey_"):
        # Switch to the user's OWN saved key
        chosen = data[len("mykey_"):]
        my = get_user_saved_keys(chat_id)
        if chosen not in my:
            return await query.answer("❌ Key not found in your keys!", show_alert=True)
        set_active_session_key(chat_id, chosen)
        await query.answer(f"⭐ Switched to {chosen[:30]}", show_alert=True)
        text, mk = build_my_keys_view(chat_id)
        try:
            await query.edit_message_text(text, reply_markup=mk, parse_mode="HTML")
        except Exception:
            pass

    elif data == "admin_panel" or data == "admks_0":
        if not get_admin_ids():
            # BOOTSTRAP: no admin exists yet -> first user to open panel becomes admin
            configs = load_configs()
            configs["_admins"] = [str(chat_id)]
            save_configs(configs)
            logger.info(f"[ADMIN] bootstrapped first admin: {chat_id}")
        if not is_admin(chat_id):
            return await query.answer("🚫 Admin access only!", show_alert=True)
        text, mk = build_admin_panel(chat_id, page=0)
        await query.edit_message_text(text, reply_markup=mk, parse_mode="HTML")

    elif data == "adm_restart":
        # ADMIN: restart the bot by dispatching a fresh GitHub Actions run.
        if not is_admin(chat_id):
            return await query.answer("🚫 Admin access only!", show_alert=True)
        await query.answer("♻️ Restarting bot...")
        ok, body = await dispatch_new_run()
        if ok:
            logger.info(f"[ADMIN] restart requested by admin {chat_id} -> workflow dispatched")
            await query.edit_message_text(
                "✅ <b>Restart triggered!</b>\n\n"
                "🆕 A new run is starting — this run will close automatically.\n"
                "💾 Database + sessions are already saved (auto-save).",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Main Menu", callback_data="back_home")]]),
                parse_mode="HTML")
            await asyncio.sleep(2)
            os._exit(0)   # end this run cleanly -> backup steps still execute
        else:
            await query.edit_message_text(
                f"❌ <b>GitHub API error:</b>\n<code>{html.escape(str(body)[:300])}</code>",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_panel")]]),
                parse_mode="HTML")

    elif data.startswith("admks_"):
        if not is_admin(chat_id):
            return await query.answer("🚫 Admin access only!", show_alert=True)
        try:
            pg = int(data.replace("admks_", ""))
        except ValueError:
            pg = 0
        text, mk = build_admin_panel(chat_id, page=pg)
        await query.edit_message_text(text, reply_markup=mk, parse_mode="HTML")

    elif data.startswith("admuse_"):
        # ADMIN: adopt (use) any user's session key
        if not is_admin(chat_id):
            return await query.answer("🚫 Admin access only!", show_alert=True)
        try:
            idx = int(data.replace("admuse_", ""))
        except ValueError:
            return
        flat = ADMIN_KEY_CACHE.get(str(chat_id)) or []
        if not (0 <= idx < len(flat)):
            return await query.answer("❌ Key list expired — refresh.", show_alert=True)
        owner, key, _ = flat[idx]
        # Set as ADMIN's active key + copy into admin's saved_keys for persistence
        configs = load_configs()
        cfg = configs.setdefault(str(chat_id), {})
        sk = cfg.setdefault("saved_keys", {})
        if key not in sk:
            sk[key] = os.path.join(SESSIONS_DIR, key) if not os.path.isabs(key) else key
            if not os.path.exists(sk[key]):
                sk[key] = os.path.join(SESSIONS_DIR, key if key.endswith(".json") else key + ".json")
        cfg["active_key"] = key
        save_configs(configs)
        await query.answer(f"✅ Now using key of user ...{owner[-4:]}: {key[:26]}", show_alert=True)
        text, mk = build_admin_panel(chat_id, page=idx // 20)
        try:
            await query.edit_message_text(text, reply_markup=mk, parse_mode="HTML")
        except Exception:
            pass

    elif data == "adm_add":
        if not is_admin(chat_id):
            return await query.answer("🚫 Admin access only!", show_alert=True)
        AWAITING_INPUT[chat_id] = "adm_add_id"
        await query.edit_message_text(
            "➕ <b>Add Admin</b>\n\nSend the user's <b>Chat ID</b> (e.g. <code>7351664603</code>):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="admin_panel")]]),
            parse_mode="HTML")

    elif data == "adm_del":
        if not is_admin(chat_id):
            return await query.answer("🚫 Admin access only!", show_alert=True)
        admins = get_admin_ids()
        kb = []
        for a in admins:
            kb.append([InlineKeyboardButton(f"🗑 {a}", callback_data=f"admdel_{a}")])
        kb.append([InlineKeyboardButton("🔙 Back", callback_data="admin_panel")])
        await query.edit_message_text(
            "🗑 <b>Delete Admin</b> — tap an admin to remove:",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

    elif data.startswith("admdel_"):
        if not is_admin(chat_id):
            return await query.answer("🚫 Admin access only!", show_alert=True)
        target = data.replace("admdel_", "")
        env_admin = str(env_config.get("ADMIN_ID") or "").strip()
        if target == env_admin:
            return await query.answer("🚫 .env root admin cannot be deleted!", show_alert=True)
        if remove_admin_user(target):
            await query.answer(f"✅ Admin {target} deleted!", show_alert=True)
        else:
            await query.answer("❌ Not found in admin list.", show_alert=True)
        text, mk = build_admin_panel(chat_id, page=0)
        try:
            await query.edit_message_text(text, reply_markup=mk, parse_mode="HTML")
        except Exception:
            pass

    elif data == "sess_new_login":
        # apiready1-style NEW LOGIN: browser OTP login flow via text messages.
        AWAITING_INPUT[chat_id] = "login_phone"
        LOGIN_TEMP.pop(chat_id, None)
        await query.edit_message_text(
            "📱 <b>New JioMart Login</b>\n\nEnter 10-digit mobile number:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="login_cancel")]]),
            parse_mode="HTML")

    elif data == "login_resend":
        # apiready1 login_resend_otp_action: click 'Resend OTP' on the login page.
        session_obj = LOGIN_TEMP.get(chat_id)
        if not session_obj:
            return await query.answer("❌ No active login. Start again.", show_alert=True)
        page = session_obj["page"]
        resent = False
        try:
            resend_lnk = page.locator("text=Resend").first
            if await resend_lnk.is_visible(timeout=3000):
                await resend_lnk.click(force=True)
                resent = True
        except Exception:
            pass
        if not resent:
            try:
                await page.evaluate("""() => {
                    const el = Array.from(document.querySelectorAll('button, a, span, div'))
                        .find(x => /resend/i.test(x.innerText || ''));
                    if (el) el.click();
                }""")
                resent = True
            except Exception:
                pass
        await query.answer("🔄 Resend OTP clicked!" if resent else "⚠️ Resend not found yet — wait a few seconds.", show_alert=False)

    elif data == "login_cancel":
        # Cancel an in-progress login flow & close its browser.
        session_obj = LOGIN_TEMP.pop(chat_id, None)
        AWAITING_INPUT.pop(chat_id, None)
        if session_obj:
            try: await session_obj["browser"].close()
            except Exception: pass
            try: await session_obj["pw"].stop()
            except Exception: pass
        await query.edit_message_text("ℹ️ Login cancelled.",
                                      reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")

    elif data.startswith("hc_"):
        # (legacy home controls removed) - no-op fallback
        await query.answer("🛒 Use the Cart button", show_alert=False)

    elif data == "sess_noop":
        pass

    elif data == "sess_search":
        USER_SEARCH_STATE[str(chat_id)] = True
        await query.edit_message_text(
            "🔍 <b>Search Session</b>\n\n"
            "Send session ID / keyword / mobile number\n"
            "(e.g. <code>6370419362</code>):",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Cancel", callback_data="open_session_manager")]]
            ),
            parse_mode="HTML",
        )

    elif data == "action_search_prod":
        USER_SEARCH_STATE[str(chat_id)] = "prod_search"
        await query.edit_message_text(
            "🔍 <b>Search JioMart Products:</b>\n\n"
            "Type the product name (e.g. <code>Fortune Oil</code> or <code>Sugar 1kg</code>):",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🕘 Search History", callback_data="search_hist")],
                 [InlineKeyboardButton("🔙 Cancel", callback_data="back_home")]]
            ),
            parse_mode="HTML",
        )

    elif data == "done_search":
        # Close all search-result cards + clear search history + cached results.
        mids = SEARCH_RESULT_MSGS.pop(str(chat_id), [])
        for mid in mids:
            try:
                await context.bot.delete_message(chat_id, mid)
            except Exception:
                pass
        SEARCH_RESULT_MSGS.pop(str(chat_id), None)
        SEARCH_SUMMARY_MSG.pop(str(chat_id), None)
        PRODUCT_SEARCH_CACHE.pop(str(chat_id), None)   # clean cached search results
        SEARCH_CARD_UIDS.pop(str(chat_id), None)       # clean card -> uid map
        save_search_history(chat_id, [])              # clean search history too
        USER_SEARCH_STATE.pop(str(chat_id), None)      # end prod_search mode
        await query.message.edit_text("✅ <b>Search completed.</b> History & results cleaned.",
                                      reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")

    elif data == "cancel_search":
        mids = SEARCH_RESULT_MSGS.pop(str(chat_id), [])
        for mid in mids:
            try:
                await context.bot.delete_message(chat_id, mid)
            except Exception:
                pass
        SEARCH_RESULT_MSGS.pop(str(chat_id), None)
        SEARCH_SUMMARY_MSG.pop(str(chat_id), None)
        SEARCH_CARD_UIDS.pop(str(chat_id), None)       # clean card -> uid map
        USER_SEARCH_STATE.pop(str(chat_id), None)      # end prod_search mode
        await query.message.edit_text("❌ <b>Search cancelled.</b>",
                                      reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")

    elif data == "search_hist":
        hist = get_search_history(chat_id)
        kb = []
        if hist:
            for i, k in enumerate(hist):
                kb.append([InlineKeyboardButton(f"🔁 {html.escape(k)[:38]}", callback_data=f"hist_resel_{i}"),
                           InlineKeyboardButton("🗑", callback_data=f"hist_del_{i}")])
            kb.append([InlineKeyboardButton("🧹 Clear All", callback_data="hist_clear")])
        else:
            kb = [[InlineKeyboardButton("🔍 Search Now", callback_data="back_home")]]
        kb += [[InlineKeyboardButton("🔙 Cancel", callback_data="back_home")]]
        await query.edit_message_text(
            f"🕘 <b>Search History</b> ({len(hist)}) — tap to re-search / 🗑 to delete:",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

    elif data.startswith("hist_del_"):
        try:
            i = int(data.replace("hist_del_", ""))
        except ValueError:
            i = -1
        hist = get_search_history(chat_id)
        if 0 <= i < len(hist):
            hist.pop(i)
            save_search_history(chat_id, hist)
        await start_command(update, context)

    elif data == "hist_clear":
        save_search_history(chat_id, [])
        await query.edit_message_text("🧹 <b>Search History Cleared!</b>",
                                      reply_markup=get_main_keyboard(chat_id), parse_mode="HTML")

    elif data.startswith("hist_resel_"):
        try:
            i = int(data.replace("hist_resel_", ""))
        except ValueError:
            i = -1
        hist = get_search_history(chat_id)
        if 0 <= i < len(hist):
            await run_product_search(chat_id, hist[i], query.message, edit_source=False)
            await query.answer("🔍 Re-searching...", show_alert=False)
        else:
            await query.answer("⚠️ History entry not found", show_alert=False)

    elif data == "prod_noop":
        await query.answer("Use ➖ / ➕ / 🗑 to adjust", show_alert=False)

    elif data.startswith("select_sess_"):
        chosen = data.replace("select_sess_", "")
        set_active_session_key(chat_id, chosen)
        RAM_CACHE.pop(str(chat_id), None)
        await start_command(update, context)

    elif data == "back_home":
        await start_command(update, context)

    else:
        # Catch-all: log unmatched callbacks so "button not working" is debuggable.
        logger.warning(f"[CB] Unhandled callback: {data}")
        try:
            await query.answer("⚠️ Unknown action — reopen the menu.", show_alert=False)
        except Exception:
            pass

# ==================== MAIN RUNNER ====================
def main():
    if not TOKEN:
        print("❌ Error: BOT_TOKEN not found in .env!")
        return

    print(f"🚀 JioMart Multi-Session Bot [{BOT_VERSION}] Running...")

    while True:
        app = Application.builder().token(TOKEN).post_init(bot_post_init).build()
        app.add_handler(CommandHandler("start", start_command))
        app.add_handler(CallbackQueryHandler(callback_router))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, session_search_text_handler))
        app.add_handler(MessageHandler(filters.LOCATION, handle_location_messages))
        try:
            # bootstrap_retries=-1 -> NEVER abort while internet/VPN is down;
            # keep waiting until api.telegram.org is reachable again.
            app.run_polling(bootstrap_retries=-1)
            break
        except telegram.error.TimedOut:
            logger.warning("Network down (TimedOut). Retrying bot startup in 15s...")
            time.sleep(15)
        except KeyboardInterrupt:
            print("\n👋 Bot stopped by user.")
            break

if __name__ == "__main__":
    main()