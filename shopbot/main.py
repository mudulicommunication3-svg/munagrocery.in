"""MUNNA MOBILE & Grocery - Telegram bot entry point.

Wires everything together:
  * admin_handler  -> auto-stock entry flow + Admin Panel (registered FIRST)
  * storefront     -> customer shop: Browse Store cart + Mini App (group -1)
  * ai_agent       -> customer assistant + conversational ordering (LAST)
  * /start, /help  -> quick info for everyone
  * /product       -> JioMart live lookup (pincode fallback, image + price)

Run with:  python main.py
"""

from __future__ import annotations

import asyncio
import html
import logging
import os

from dotenv import load_dotenv

load_dotenv()

from telegram.ext import Application, CommandHandler  # noqa: E402

import admin_handler  # noqa: E402

import jiomart_search  # noqa: E402  - JioMart catalog fetcher (replaces Google Custom Search)

import ai_agent  # noqa: E402
import storefront  # noqa: E402  - customer storefront (chat cart + Mini App)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s: %(message)s",
)
logger = logging.getLogger("shopbot")

WELCOME = (
    "🙏 ନମସ୍କାର! Welcome to *MUNNA MOBILE & Grocery*.\n\n"
    "📦 Browse Store - shop by category with a chat cart\n"
    "🔍 Search - tap Search, then type a product name\n"
    "🛒 Cart - view and manage your cart\n"
    "💬 Or just type what you need, e.g. \"I need 2 Fortune oils\"\n"
    "🤖 AI Chat mode - talk to the assistant, add/remove/cart in chat\n"
    "🏬 Store mode - store buttons + direct cart only (no AI replies)\n\n"
    "Commands: /help /language /myorders /clearhistory"
)

HELP_TEXT = (
    "*MUNNA MOBILE & Grocery*\n\n"
    "📦 *Browse Store* - categories, photo cards, Add to Cart,\n"
    "View Cart, then send your address + phone to order.\n\n"
    "🔍 *Search* - tap Search, type a product name, and get\n"
    "matching photo cards with an Add to Cart button.\n\n"
    "🛍️ *Open Shop* - Mini App store; checkout comes back here.\n\n"
    "💬 Just type your need, e.g.:\n"
    "• \"I need 2 Fortune oils and 1 Tata salt\"\n"
    "• \"ମୋତେ ୨ ତେଲ ଦରକାର\"\n"
    "Reply *Confirm* to place the order.\n\n"
    "🔎 Check a product live on JioMart (stock checked by pincode):\n"
    "`/product tata iodised salt 1 kg`\n\n"
    "Admins can send stock entries as:\n"
    "`Name, MRP, Selling Price, Qty, EAN`\n"
    "e.g. `Fortune Mustard Oil 1L, 210, 170, 15, 8901234567890`"
)


async def cmd_start(update, context) -> None:
    """Greet users with the store menu + Admin Panel button for admins."""
    if not update.effective_message:
        return
    user = update.effective_user
    is_admin = admin_handler.is_admin(user.id if user else None)
    keyboard = storefront.start_keyboard(
        is_admin=is_admin,
        admin_panel_markup=admin_handler.panel_keyboard() if is_admin else None,
        chat_mode=storefront.get_chat_mode(context),
        user_id=user.id if user else None,
    )
    try:
        await update.effective_message.reply_text(
            WELCOME,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return
    except Exception as exc:  # noqa: BLE001 - e.g. Button_url_invalid (bad WEB_APP_URL)
        logger.warning(
            "/start with full menu failed (%s); retrying without Open Shop", exc
        )
    # A bad Mini App URL must never silence /start - drop the Open Shop button.
    try:
        fallback = storefront.start_keyboard(
            is_admin=is_admin,
            admin_panel_markup=admin_handler.panel_keyboard() if is_admin else None,
            chat_mode=storefront.get_chat_mode(context),
            include_webapp=False,
        )
        await update.effective_message.reply_text(
            WELCOME,
            parse_mode="Markdown",
            reply_markup=fallback,
        )
    except Exception as exc:  # noqa: BLE001 - last resort: plain text, no buttons
        logger.error("/start fallback failed: %s", exc)
        try:
            await update.effective_message.reply_text(WELCOME)
        except Exception:  # noqa: BLE001
            pass


async def cmd_help(update, context) -> None:
    """Show usage help."""
    if update.effective_message:
        await update.effective_message.reply_text(HELP_TEXT)


async def cmd_product(update, context) -> None:
    """Look up a product on JioMart, trying fallback pincodes until in stock.

    Usage: /product <product name>
    e.g.   /product tata iodised salt 1 kg
    """
    message = update.effective_message
    if message is None:
        return
    query = " ".join(context.args or []).strip()
    if not query:
        await message.reply_text(
            "Usage: `/product <product name>`\n"
            "e.g. `/product tata iodised salt 1 kg`",
            parse_mode="Markdown",
        )
        return

    status = await message.reply_text(f"🔎 Searching JioMart for: {query} ...")
    product = await asyncio.to_thread(jiomart_search.search_product, query)

    if not product:
        await status.edit_text(
            "❌ No product found on JioMart for that search."
        )
        return

    lines = [
        f"🛒 <b>{html.escape(str(product.get('name') or 'Product'))}</b>",
        f"💰 Price: ₹{product.get('price', 0):g}"
        + (f" (MRP ₹{product.get('mrp', 0):g})"
           if product.get("mrp") and product.get("mrp") != product.get("price")
           else ""),
        f"📍 PIN <code>{html.escape(str(product.get('pincode') or ''))}</code>"
        f" — {'🟢 In Stock' if product.get('in_stock') else '🔴 Out of Stock'}",
    ]
    if product.get("pack"):
        lines.append(f"📦 Pack: {html.escape(str(product['pack']))}")
    if product.get("link"):
        lines.append(f"🔗 {product['link']}")

    image_url = str(product.get("image") or "").strip()
    caption = "\n".join(lines)
    if image_url:
        try:
            await status.delete()
        except Exception:
            pass
        await message.reply_photo(
            photo=image_url, caption=caption, parse_mode="HTML"
        )
    else:
        await status.edit_text(caption, parse_mode="HTML")


def main() -> None:
    token = (
        os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN") or ""
    ).strip()
    if not token:
        raise SystemExit("TELEGRAM_TOKEN (or BOT_TOKEN) is missing from .env")

    async def _post_init(a):
        # Start the periodic auto-restart countdown (5.5h by default).
        # Must be async (PTB awaits post_init) and never raise - a broken
        # countdown should never stop the bot from starting.
        try:
            import selfrestart

            asyncio.create_task(selfrestart.auto_restart_watcher(a))
        except Exception:  # noqa: BLE001 - countdown must never break startup
            logger.error("Could not start auto-restart watcher", exc_info=True)

        # Webapp <-> bot order sync (Google Sheet is the shared database:
        # web orders -> Telegram admin alert + customer live status message,
        # status changes -> live message edits). See web_sync.py.
        try:
            import web_sync

            asyncio.create_task(web_sync.sync_loop(a))
        except Exception:  # noqa: BLE001 - sync must never break startup
            logger.error("Could not start web order sync loop", exc_info=True)

        # Sheet search bus: the webapp admin panel's JioMart search runs
        # through THIS PC (jiomart_search.fetch_items) because phones cannot
        # reach JioMart directly (browser CORS / dead relays / localhost).
        try:
            import search_bus

            asyncio.create_task(search_bus.search_loop(a))
        except Exception:  # noqa: BLE001 - search bus must never break startup
            logger.error("Could not start search bus loop", exc_info=True)

        # Real web login: deliver phone-OTP codes from the LoginOTPs tab to
        # admins over Telegram (server-verified, single-use). See otp_sync.py.
        try:
            import otp_sync

            asyncio.create_task(otp_sync.otp_loop(a))
        except Exception:  # noqa: BLE001 - OTP relay must never break startup
            logger.error("Could not start login OTP loop", exc_info=True)

    app = Application.builder().token(token).post_init(_post_init).build()

    # Order matters: admin flow first, then the storefront (group -1, so its
    # checkout text wins over the AI fallback), then the AI assistant.
    admin_handler.register(app)
    storefront.register(app)
    ai_agent.register(app)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("product", cmd_product))
    app.add_handler(CommandHandler("myorders", storefront.cmd_myorders))

    logger.info("MUNNA MOBILE & Grocery bot is starting (polling)...")
    app.run_polling(allowed_updates=[])  # empty list = auto-detect update types


if __name__ == "__main__":
    main()
