"""Validation for admin_handler.py (fake Telegram objects, no network).

Run with:  python tests/test_admin_handler.py
"""

print("HELLO_FROM_TEST")

import asyncio
import os
import sys
import time

from telegram import InlineKeyboardMarkup
from telegram.constants import ParseMode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import admin_handler  # noqa: E402
import ai_agent  # noqa: E402
import db  # noqa: E402

failures = 0


def check(name: str, condition: bool) -> None:
    global failures
    print(("PASS" if condition else "FAIL"), "-", name)
    if not condition:
        failures += 1


class FakeMessage:
    def __init__(self, text="", chat_id=777, message_id=1, user=None, photo=None, caption=None):
        self.text = text
        self.chat_id = chat_id
        self.message_id = message_id
        self.from_user = user
        self.photo = photo          # truthy for media (photo) messages
        self.caption = caption      # media caption (None for plain text)
        self.replies = []  # (text, kwargs)
        self.edits = []  # (text, kwargs)
        self.caption_edits = []  # (caption, kwargs)

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, dict(kwargs)))
        return FakeMessage(text=text, chat_id=self.chat_id, message_id=self.message_id + 1000)

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, dict(kwargs)))
        return self

    async def edit_caption(self, caption=None, **kwargs):
        self.caption = caption
        self.caption_edits.append((caption, dict(kwargs)))
        return self

    async def delete(self):
        self.deleted = True


class FakeUser:
    def __init__(self, user_id):
        self.id = user_id


class FakeUpdate:
    def __init__(self, message=None, user=None, callback_query=None):
        self.effective_message = message
        self.message = message
        self.effective_user = user
        self.callback_query = callback_query


class FakeQuery:
    def __init__(self, data, message, user):
        self.data = data
        self.message = message
        self.from_user = user
        self.answers = []  # (text, show_alert)
        self.caption_edits = []  # (caption, kwargs)
        self.text_edits = []      # (text, kwargs)
        self.markup_edits = []    # reply_markup objects

    async def answer(self, text=None, show_alert=False, **kwargs):
        self.answers.append((text, show_alert))

    async def edit_message_caption(self, caption=None, **kwargs):
        self.caption_edits.append((caption, dict(kwargs)))
        return self.message

    async def edit_message_text(self, text=None, **kwargs):
        self.text_edits.append((text, dict(kwargs)))
        return self.message

    async def edit_message_reply_markup(self, reply_markup=None, **kwargs):
        self.markup_edits.append(reply_markup)
        return self.message


class FakeBot:
    def __init__(self):
        self.sent = []          # kwargs of every send_message / send_photo
        self.deleted = []       # message_ids passed to delete_message
        self._next_id = 1000

    def _stamp(self, kind, kwargs):
        self._next_id += 1
        self.sent.append(dict(kwargs, __kind=kind))
        return _Sent(self._next_id)

    async def send_message(self, **kwargs):
        return self._stamp("message", kwargs)

    async def send_photo(self, **kwargs):
        return self._stamp("photo", kwargs)

    async def delete_message(self, **kwargs):
        self.deleted.append(kwargs.get("message_id"))

    async def forward_message(self, **kwargs):
        """Channel-forward stand-in: returns a message with the hosted photo."""
        self._next_id += 1
        self.sent.append(dict(kwargs, __kind="forward"))
        return _Forwarded(self._next_id, file_id="chan-file-1")


class _PhotoFile:
    """Minimal stand-in for a Telegram PhotoSize (only file_id matters)."""

    def __init__(self, file_id):
        self.file_id = file_id


class _Sent:
    """Minimal stand-in for a sent Telegram Message."""

    def __init__(self, message_id):
        self.message_id = message_id


class _Forwarded:
    """Stand-in for a channel-forwarded photo message."""

    def __init__(self, message_id, file_id="chan-file-1"):
        self.message_id = message_id
        self.photo = [_PhotoFile(file_id)]


class FakeContext:
    def __init__(self, bot=None):
        self.bot = bot or FakeBot()
        self.user_data = {}


def set_admin(value):
    if value is None:
        os.environ.pop("ADMIN_ID", None)
    else:
        os.environ["ADMIN_ID"] = value


def patch_db(attrs):
    originals = {name: getattr(db, name) for name in attrs}
    for name, value in attrs.items():
        setattr(db, name, value)

    def restore():
        for name, value in originals.items():
            setattr(db, name, value)

    return restore


def patch_fetch(fn):
    original = admin_handler.fetch_images
    admin_handler.fetch_images = fn

    def restore():
        admin_handler.fetch_images = original

    return restore


def expect_value_error(name, text):
    try:
        admin_handler.parse_stock_entry(text)
    except ValueError:
        check(name, True)
    else:
        check(name, False)


def test_sync():
    original_admin_id = os.environ.get("ADMIN_ID")
    admin_handler._pending.clear()
    try:
        # --- entry parsing -------------------------------------------------
        entry = admin_handler.parse_stock_entry(
            "Fortune Mustard Oil 1L, 210, 170, 15, 8901234567890"
        )
        check(
            "parse: example entry",
            entry
            == {"name": "Fortune Mustard Oil 1L", "mrp": 210, "price": 170,
                "qty": 15, "ean": "8901234567890"},
        )
        entry = admin_handler.parse_stock_entry("Salt, Iodised, 28, 25, 5, 8901030595486")
        check("parse: comma inside name", entry["name"] == "Salt, Iodised")
        entry = admin_handler.parse_stock_entry("Ghee 1L, ₹1299, Rs. 1 249.5, 10, 1234567890123")
        check("parse: rupee/Rs./spaces stripped", entry["mrp"] == 1299 and entry["price"] == 1249.5)
        entry = admin_handler.parse_stock_entry("Tea, Rs. 99.5, 95, 12, 222")
        check("parse: Rs. prefix stripped", entry["mrp"] == 99.5)
        expect_value_error("parse: too few fields", "Oil, 210, 170, 8901234567890")
        expect_value_error("parse: bad MRP", "Oil, abc, 170, 5, 123")
        expect_value_error("parse: fractional qty", "Oil, 210, 170, 2.5, 123")
        expect_value_error("parse: negative qty", "Oil, 210, 170, -1, 123")
        expect_value_error("parse: empty name", ", 210, 170, 5, 123")
        expect_value_error("parse: empty EAN", "Oil, 210, 170, 5, ")

        # --- authorisation -------------------------------------------------
        set_admin(None)
        check("dev mode: empty ADMIN_ID -> allowed", admin_handler.is_admin(123))
        set_admin("0")
        check("dev mode: ADMIN_ID=0 -> allowed", admin_handler.is_admin(123))
        set_admin("7351664603")
        check("ADMIN_ID match", admin_handler.is_admin(7351664603))
        check("ADMIN_ID mismatch", not admin_handler.is_admin(1))
        set_admin("111, 222")
        check("ADMIN_ID list match", admin_handler.is_admin(222))
        set_admin("not-a-number")
        check("garbage ADMIN_ID -> denied", not admin_handler.is_admin(1))

        # --- rendering / keyboard ------------------------------------------
        session = {
            "name": "Oil *Special* 500ml",
            "mrp": 210,
            "price": 170.5,
            "qty": 15,
            "ean": "8901234567890",
            "current_start_index": 1,
            "image_urls": [f"https://img.example/{i}.jpg" for i in range(1, 6)],
        }
        md_text, plain_text = admin_handler._render_picker(session)
        check("markdown image links", "1. [Image 1](https://img.example/1.jpg)" in md_text)
        check("markdown escapes asterisk", "Oil \\*Special\\* 500ml" in md_text)
        check("plain text unescaped", "Oil *Special* 500ml" in plain_text)
        keyboard = admin_handler._picker_keyboard(session)
        rows = keyboard.inline_keyboard
        check(
            "number buttons sel_1..sel_5",
            [b.callback_data for b in rows[0]] == [f"sel_{i}" for i in range(1, 6)],
        )
        check(
            "next button exact spec label",
            any(b.text == "Next 5 ➡️" and b.callback_data == "next_5" for b in rows[1]),
        )
        check("cancel button", any(b.callback_data == "img_cancel" for b in rows[2]))
        session["current_start_index"] = 96
        keyboard = admin_handler._picker_keyboard(session)
        check(
            "no next button at window edge",
            all(b.callback_data != "next_5" for row in keyboard.inline_keyboard for b in row),
        )
        check("next allowed at 91", admin_handler._next_allowed(91))
        check("next blocked at 96", not admin_handler._next_allowed(96))
    finally:
        set_admin(original_admin_id)
        admin_handler._pending.clear()


URLS = [f"https://img.example/{i}.jpg" for i in range(1, 6)]


def test_existing_item_flow():
    """Duplicate EAN -> update + exact success reply."""
    set_admin("42")
    restore_db = patch_db(
        {
            "find_by_ean": lambda ean: (2, ["Oil", "200", "160", "12", ean, ""]),
            "update_stock": lambda found, qty, mrp, price: {
                "row": 2, "old_stock": 12, "new_stock": 27,
                "mrp": mrp, "selling_price": price,
            },
        }
    )
    try:
        admin_handler._pending.clear()
        message = FakeMessage(text="Oil, 210, 170, 15, 123", message_id=10)
        update = FakeUpdate(message=message, user=FakeUser(42))
        asyncio.run(admin_handler.handle_stock_entry(update, FakeContext()))
        replies = message.replies
        check("exactly one reply", len(replies) == 1)
        text = replies[0][0]
        check(
            "exact spec reply first line",
            text.splitlines()[0] == "✅ Item exists. Stock and Price updated.",
        )
        check("stock summary included", "12 → 27" in text)
        check("no session stored for duplicates", 42 not in admin_handler._pending)
    finally:
        restore_db()


def test_new_item_and_select_flow():
    """New EAN -> picker stored; sel_2 -> add_product(url2) + success."""
    set_admin("42")
    restore_db = patch_db({"find_by_ean": lambda ean: None, "add_product": lambda *a: {"ok": True}})
    restore_fetch = patch_fetch(lambda query, start=1: list(URLS))
    saved = {}

    def fake_add(name, mrp, price, qty, ean, url):
        saved.update(name=name, mrp=mrp, price=price, qty=qty, ean=ean, url=url)
        return {"ok": True}

    restore_db2 = patch_db({"add_product": fake_add})
    try:
        admin_handler._pending.clear()
        message = FakeMessage(text="Oil, 210, 170, 15, 123", message_id=10)
        update = FakeUpdate(message=message, user=FakeUser(42))
        asyncio.run(admin_handler.handle_stock_entry(update, FakeContext()))

        session = admin_handler._pending.get(42)
        check("session stored for admin", session is not None)
        check(
            "session spec fields",
            session["name"] == "Oil" and session["current_start_index"] == 1
            and session["image_urls"] == URLS and session["ean"] == "123",
        )
        picker = message.replies[0]
        check("picker uses Markdown", picker[1].get("parse_mode") == ParseMode.MARKDOWN)
        markup = picker[1].get("reply_markup")
        check("picker has selection keyboard", isinstance(markup, InlineKeyboardMarkup))

        picker_message = FakeMessage(message_id=session["message_id"])
        query = FakeQuery(data="sel_2", message=picker_message, user=FakeUser(42))
        context = FakeContext()
        asyncio.run(admin_handler.handle_image_callback(FakeUpdate(callback_query=query), context))
        check(
            "add_product called with chosen URL",
            saved.get("url") == URLS[1] and saved.get("ean") == "123",
        )
        check("session cleared after save", 42 not in admin_handler._pending)
        check(
            "exact spec success reply",
            any(r[0] == "✅ New product added to the database." for r in picker_message.replies),
        )
        check("picker edited after save", len(picker_message.edits) == 1)
    finally:
        restore_db()
        restore_db2()
        restore_fetch()


def test_pagination_flow():
    """next_5 -> fetch(start=6), session updated, message edited."""
    set_admin("42")
    calls = []

    def fake_fetch(query, start=1):
        calls.append(start)
        return [f"https://img.example/p2-{i}.jpg" for i in range(1, 6)]

    restore_fetch = patch_fetch(fake_fetch)
    try:
        admin_handler._pending.clear()
        session = {
            "name": "Oil", "mrp": 210, "price": 170, "qty": 15, "ean": "123",
            "current_start_index": 1, "image_urls": list(URLS),
            "created": time.time(), "chat_id": 777, "message_id": 20,
        }
        admin_handler._pending[42] = session
        picker_message = FakeMessage(message_id=20)
        query = FakeQuery(data="next_5", message=picker_message, user=FakeUser(42))
        asyncio.run(admin_handler.handle_image_callback(FakeUpdate(callback_query=query), FakeContext()))
        check("fetch called with start=6", calls == [6])
        check("session start updated", admin_handler._pending[42]["current_start_index"] == 6)
        check("picker message edited", len(picker_message.edits) == 1)
        check(
            "edit uses Markdown",
            picker_message.edits[0][1].get("parse_mode") == ParseMode.MARKDOWN,
        )
    finally:
        restore_fetch()


def test_defence_and_filter():
    """Non-admins are ignored; the filter skips non-entry text."""
    set_admin("999")
    restore_db = patch_db({"find_by_ean": lambda ean: (_ for _ in ()).throw(Exception("no-call"))})
    try:
        admin_handler._pending.clear()
        message = FakeMessage(text="Oil, 210, 170, 15, 123")
        update = FakeUpdate(message=message, user=FakeUser(555))
        asyncio.run(admin_handler.handle_stock_entry(update, FakeContext()))
        check("non-admin gets no reply", message.replies == [])

        admin_message = FakeMessage(text="Oil, 210, 170, 15, 123", user=FakeUser(555))
        check(
            "filter: entry but wrong admin -> False",
            admin_handler.ENTRY_FILTER.filter(admin_message) is False,
        )
        check(
            "filter: message from admin but no comma -> False",
            admin_handler.ENTRY_FILTER.filter(FakeMessage(text="hello", user=FakeUser(999)))
            is False,
        )
        admin_ok = FakeMessage(text="Oil, 210, 170, 15, 123", user=FakeUser(999))
        check(
            "filter: admin + comma -> True",
            admin_handler.ENTRY_FILTER.filter(admin_ok) is True,
        )
    finally:
        restore_db()


def test_expired_and_stale_picker():
    """Expired or stale pickers are rejected without touching live sessions."""
    set_admin("42")
    try:
        admin_handler._pending.clear()
        # live session on message 30; button clicked on stale message 7
        admin_handler._pending[42] = {
            "name": "Oil", "mrp": 210, "price": 170, "qty": 15, "ean": "123",
            "current_start_index": 1, "image_urls": list(URLS),
            "created": time.time(), "chat_id": 777, "message_id": 30,
        }
        stale_message = FakeMessage(message_id=7)
        query = FakeQuery(data="sel_1", message=stale_message, user=FakeUser(42))
        asyncio.run(admin_handler.handle_image_callback(FakeUpdate(callback_query=query), FakeContext()))
        check("stale picker -> alert", any("expired" in (a[0] or "") for a in query.answers))
        check("live session kept", admin_handler._pending.get(42, {}).get("message_id") == 30)

        # truly missing session
        admin_handler._pending.clear()
        query = FakeQuery(data="sel_1", message=stale_message, user=FakeUser(42))
        asyncio.run(admin_handler.handle_image_callback(FakeUpdate(callback_query=query), FakeContext()))
        check("missing session -> alert", any("expired" in (a[0] or "") for a in query.answers))
    finally:
        admin_handler._pending.clear()


def test_save_failure_keeps_picker():
    """DatabaseError on save alerts and keeps the picker alive."""
    set_admin("42")

    def boom(*args):
        raise db.DatabaseError("sheet exploded")

    restore_db = patch_db({"add_product": boom})
    try:
        admin_handler._pending.clear()
        session = {
            "name": "Oil", "mrp": 210, "price": 170, "qty": 15, "ean": "123",
            "current_start_index": 1, "image_urls": list(URLS),
            "created": time.time(), "chat_id": 777, "message_id": 40,
        }
        admin_handler._pending[42] = session
        picker_message = FakeMessage(message_id=40)
        query = FakeQuery(data="sel_1", message=picker_message, user=FakeUser(42))
        asyncio.run(admin_handler.handle_image_callback(FakeUpdate(callback_query=query), FakeContext()))
        check(
            "save failure -> alert",
            any("Could not save" in (a[0] or "") for a in query.answers),
        )
        check("session kept for retry", 42 in admin_handler._pending)
        check("picker not edited", picker_message.edits == [])
    finally:
        restore_db()
        admin_handler._pending.clear()


def test_cancel_flow():
    """Cancel clears the session and edits the message."""
    set_admin("42")
    try:
        admin_handler._pending.clear()
        session = {
            "name": "Oil", "mrp": 210, "price": 170, "qty": 15, "ean": "123",
            "current_start_index": 1, "image_urls": list(URLS),
            "created": time.time(), "chat_id": 777, "message_id": 50,
        }
        admin_handler._pending[42] = session
        picker_message = FakeMessage(message_id=50)
        query = FakeQuery(data="img_cancel", message=picker_message, user=FakeUser(42))
        asyncio.run(admin_handler.handle_image_callback(FakeUpdate(callback_query=query), FakeContext()))
        check("cancel clears session", 42 not in admin_handler._pending)
        check(
            "cancel edits message",
            any("cancelled" in (e[0] or "").lower() for e in picker_message.edits),
        )
    finally:
        admin_handler._pending.clear()


def test_language_preferences():
    """Language codes: normalization, defaults, per-user storage."""
    ai_agent._prefs.clear()
    check("default language is English", ai_agent.get_user_language(999) == "en")
    check("user None default", ai_agent.get_user_language(None) == "en")
    check("set Odia", ai_agent.set_user_language(999, "or") == "or")
    check("stored Odia", ai_agent.get_user_language(999) == "or")
    check("alias Odia", ai_agent.set_user_language(999, "Odia") == "or")
    check("alias Hindi", ai_agent.set_user_language(999, "हिन्दी") == "hi")
    check("alias English", ai_agent.set_user_language(999, "english") == "en")
    check("bad code rejected", ai_agent.set_user_language(999, "xx") is None)
    check("language kept after bad code", ai_agent.get_user_language(999) == "en")
    check("per-user isolation", ai_agent.get_user_language(1000) == "en")
    ai_agent._prefs.clear()


def test_language_menu_ui():
    """Menu text, exactly 3 buttons, correct callback data."""
    check(
        "menu buttons",
        ai_agent.LANG_BUTTONS == [("en", "English"), ("or", "ଓଡ଼ିଆ"), ("hi", "हिन्दी")],
    )
    keyboard = ai_agent._language_keyboard()
    buttons = keyboard.inline_keyboard[0]
    check("three buttons", len(buttons) == 3)
    check(
        "callback data",
        [b.callback_data for b in buttons] == ["lang_en", "lang_or", "lang_hi"],
    )
    check("Odia confirm", "ଓଡ଼ିଆ" in ai_agent.LANG_CONFIRM["or"])
    check("Hindi confirm", "हिन्दी" in ai_agent.LANG_CONFIRM["hi"])
    check(
        "each instruction mentions ONLY",
        all("ONLY" in v for v in ai_agent.LANG_INSTRUCTION.values()),
    )


def test_ask_uses_set_language():
    """ask() embeds the SET-language instruction for any input language."""
    import ai_agent as _ag

    class PromptModel:
        def __init__(self):
            self.prompts = []

        def generate_content(self, prompt):
            self.prompts.append(prompt)
            return FakeAgentResponse("ତେଲ ଅଛି।")

    fake = PromptModel()
    _ag._model = fake
    orig_active = _ag._active_ai_cache
    _ag._active_ai_cache = "Gemini"  # pin: fake model only speaks Gemini
    try:
        ai_agent.clear_cache()
        _ag.ask("oil price?", lang="or", catalog_context="ctx")
        prompt = fake.prompts[0]
        check("Odia instruction embedded", "ONLY in Odia" in prompt)
        check("question preserved", "oil price?" in prompt)
        _ag.ask("ତେଲ ଦାମ୍?", lang="hi", catalog_context="ctx")
        check("Hindi instruction embedded", "ONLY in Hindi" in fake.prompts[1])
        _ag.ask("oil price?", lang="en", catalog_context="ctx")
        check("English instruction embedded", "ONLY in English" in fake.prompts[2])
        _ag.ask("oil price?", lang="xx-bad", catalog_context="ctx")
        check("bad language falls back to English", "ONLY in English" in fake.prompts[3])
    finally:
        _ag._model = None
        _ag._active_ai_cache = orig_active


class FakeAgentResponse:
    def __init__(self, text):
        self.text = text


def test_handle_customer_uses_lang():
    """Customer handler passes the user's language into answer()."""
    import ai_agent as _ag

    seen = {}

    async def fake_answer(text, lang="en", **kwargs):
        seen["text"] = text
        seen["lang"] = lang
        return "ଉତ୍ତର"

    ai_agent.set_user_language(4242, "or")
    message = FakeMessage(text="oil price?", user=FakeUser(4242))
    holder = {}
    real_reply = FakeMessage.reply_text
    real_edit = FakeMessage.edit_text

    async def fake_reply_text(self, text, **kwargs):
        holder["status"] = FakeMessage(text=text)
        return holder["status"]

    async def fake_edit_text(self, text, **kwargs):
        holder["status"] = FakeMessage(text=text)
        return holder["status"]

    FakeMessage.reply_text = fake_reply_text
    FakeMessage.edit_text = fake_edit_text
    original = _ag.answer
    _ag.answer = fake_answer
    try:
        asyncio.run(
            _ag.handle_customer_message(
                FakeUpdate(message=message, user=FakeUser(4242)), FakeContext()
            )
        )
        check("lang passed to answer()", seen.get("lang") == "or")
        check(
            "reply edited in",
            holder.get("status") is not None and holder["status"].text == "ଉତ୍ତର",
        )
    finally:
        _ag.answer = original
        FakeMessage.reply_text = real_reply
        FakeMessage.edit_text = real_edit
        ai_agent._prefs.clear()


def test_language_callback_flow():
    """lang_or saves Odia and edits the message with the confirm text."""
    import ai_agent as _ag

    ai_agent._prefs.clear()
    picker = FakeMessage(message_id=60)

    class LangQuery:
        def __init__(self):
            self.data = "lang_or"
            self.message = picker
            self.from_user = FakeUser(4242)
            self.answers = []

        async def answer(self, text=None, show_alert=False, **kwargs):
            self.answers.append((text, show_alert))

    query = LangQuery()
    asyncio.run(
        _ag.handle_language_callback(FakeUpdate(callback_query=query), FakeContext())
    )
    check("callback saves Odia", ai_agent.get_user_language(4242) == "or")
    check(
        "message confirms in Odia",
        any("ଓଡ଼ିଆ" in (e[0] or "") for e in picker.edits),
    )
    ai_agent._prefs.clear()


def test_language_edit_failure_tolerated():
    """Edit failures after saving must not lose the saved preference."""
    import ai_agent as _ag

    picker = FakeMessage(message_id=60)
    real_edit = FakeMessage.edit_text

    async def boom_edit(self, text, **kwargs):
        raise RuntimeError("dead chat")

    class LangQuery2:
        def __init__(self):
            self.data = "lang_hi"
            self.message = picker
            self.from_user = FakeUser(4242)

        async def answer(self, text=None, show_alert=False, **kwargs):
            return None

    FakeMessage.edit_text = boom_edit
    try:
        asyncio.run(
            _ag.handle_language_callback(
                FakeUpdate(callback_query=LangQuery2()), FakeContext()
            )
        )
        check("pref saved despite edit failure", ai_agent.get_user_language(4242) == "hi")
    except Exception:
        check("pref saved despite edit failure", False)
    finally:
        FakeMessage.edit_text = real_edit
        ai_agent._prefs.clear()


def test_language_non_ascii_answer_failure_safe():
    """Non-matching callback data is ignored safely."""
    import ai_agent as _ag

    ai_agent._prefs.clear()

    class WeirdQuery:
        def __init__(self):
            self.data = "lang_???"
            self.message = FakeMessage(message_id=61)
            self.from_user = FakeUser(5556)
            self.answers = []

        async def answer(self, text=None, show_alert=False, **kwargs):
            self.answers.append((text, show_alert))

    try:
        asyncio.run(
            _ag.handle_language_callback(
                FakeUpdate(callback_query=WeirdQuery()), FakeContext()
            )
        )
        check("weird callback ignored", ai_agent.get_user_language(5556) == "en")
    except Exception:
        check("weird callback ignored", False)
    ai_agent._prefs.clear()


def test_language_callback_deny_non_lang():
    """Callbacks whose data is not lang_* are ignored."""

    class OtherQuery:
        def __init__(self):
            self.data = "sel_1"
            self.message = FakeMessage(message_id=61)
            self.from_user = FakeUser(5555)
            self.answers = []

        async def answer(self, text=None, show_alert=False, **kwargs):
            self.answers.append((text, show_alert))

    import ai_agent as _ag

    ai_agent._prefs.clear()
    query = OtherQuery()
    before = dict(ai_agent._prefs)
    try:
        asyncio.run(
            _ag.handle_language_callback(FakeUpdate(callback_query=query), FakeContext())
        )
    except Exception:
        pass
    check("non-lang callback leaves prefs untouched", dict(ai_agent._prefs) == before)


def test_language_callback_expired_picker():
    """Answer failures never crash the flow; edits stay safe."""
    import ai_agent as _ag

    ai_agent._prefs.clear()

    class DeadQuery:
        def __init__(self):
            self.data = "lang_hi"
            self.message = None
            self.from_user = FakeUser(4242)

        async def answer(self, text=None, show_alert=False, **kwargs):
            raise RuntimeError("gone")

    try:
        asyncio.run(
            _ag.handle_language_callback(FakeUpdate(callback_query=DeadQuery()), FakeContext())
        )
        check("answer failure tolerated", True)
    except Exception:
        check("answer failure tolerated", False)
    ai_agent._prefs.clear()


def test_panel_pincode_and_pagination():
    """Manual pincode (valid/invalid) + pagination + search-stays-alive + done."""
    import admin_handler as ah

    set_admin("12345")
    admin = FakeUser(12345)

    # --- fake products (7 items => 3 pages of 3) ---------------------------
    def make(i):
        return {
            "uid": str(1000 + i),
            "name": f"Product {i}",
            "price": 100 + i,
            "mrp": 120 + i,
            "image": f"https://img/{i}.jpg",
            "in_stock": True,
            "pincode": "753003",
        }

    products = [make(i) for i in range(7)]

    orig_search = ah._search_products_sync
    seen_pins = []

    def fake_search(keyword, pincode=None):
        seen_pins.append(pincode)
        return list(products)

    ah._search_products_sync = fake_search
    try:
        # ---- manual pincode: valid ------------------------------------
        ctx = FakeContext()
        ctx.user_data["state"] = ah.STATE_SET_PINCODE
        msg = FakeMessage(text="751001", user=admin, chat_id=777, message_id=1)
        asyncio.run(
            ah.handle_admin_panel_text(FakeUpdate(message=msg, user=admin), ctx)
        )
        check("valid pincode stored", ctx.user_data.get("panel_pincode") == "751001")
        check(
            "valid pincode enters search state",
            ctx.user_data.get("state") == ah.STATE_SEARCH_PRODUCT,
        )

        # ---- manual pincode: invalid (stays in set-pin state) --------
        ctx2 = FakeContext()
        ctx2.user_data["state"] = ah.STATE_SET_PINCODE
        msg2 = FakeMessage(text="12", user=admin, chat_id=777, message_id=2)
        asyncio.run(
            ah.handle_admin_panel_text(FakeUpdate(message=msg2, user=admin), ctx2)
        )
        check("invalid pincode rejected", "panel_pincode" not in ctx2.user_data)
        check(
            "invalid pincode keeps state",
            ctx2.user_data.get("state") == ah.STATE_SET_PINCODE,
        )

        # ---- search uses the manual pin first ------------------------
        seen_pins.clear()
        ctx3 = FakeContext()
        ctx3.user_data["state"] = ah.STATE_SEARCH_PRODUCT
        ctx3.user_data["panel_pincode"] = "751001"
        msg3 = FakeMessage(text="oil", user=admin, chat_id=777, message_id=3)
        asyncio.run(
            ah.handle_admin_panel_text(FakeUpdate(message=msg3, user=admin), ctx3)
        )
        check("search passes manual pin", seen_pins and seen_pins[0] == "751001")
    finally:
        ah._search_products_sync = orig_search
        set_admin(None)


def test_panel_pagination_and_done():
    """Pagination bar (Next/Prev) + Search Done clears the session."""
    import admin_handler as ah

    set_admin("12345")
    admin = FakeUser(12345)

    def make(i):
        return {
            "uid": str(2000 + i), "name": f"P{i}", "price": 10 + i,
            "mrp": 12 + i, "image": f"https://img/{i}.jpg",
            "in_stock": True, "pincode": "753003",
        }

    products = [make(i) for i in range(7)]
    orig_search = ah._search_products_sync
    ah._search_products_sync = lambda kw, pincode=None: list(products)
    try:
        ctx = FakeContext()
        ctx.user_data["state"] = ah.STATE_SEARCH_PRODUCT
        msg = FakeMessage(text="oil", user=admin, chat_id=777, message_id=3)
        asyncio.run(ah.handle_admin_panel_text(FakeUpdate(message=msg, user=admin), ctx))
        bot = ctx.bot
        photos = [s for s in bot.sent if s.get("__kind") == "photo"]
        check("page 1 shows 3 cards", len(photos) == 3)
        check("7 results cached", len(ctx.user_data.get("panel_results") or []) == 7)

        def nav_buttons(snapshot):
            out = []
            for s in snapshot:
                rm = s.get("reply_markup")
                if not rm:
                    continue
                for row in rm.inline_keyboard:
                    for b in row:
                        out.append(b.callback_data)
            return out

        navs = [s for s in bot.sent if s.get("__kind") == "message" and s.get("reply_markup")]
        cbs = nav_buttons(navs)
        check("next button on page 1", "admin_page_2" in cbs)
        check("done button present", "admin_search_done" in cbs)

        before = len(bot.deleted)
        q2 = FakeQuery("admin_page_2", FakeMessage(message_id=50), admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=q2), ctx))
        check("paging deletes old cards", len(bot.deleted) > before)
        check("advanced to page 2", ctx.user_data.get("panel_page") == 2)

        q3 = FakeQuery("admin_page_3", FakeMessage(message_id=52), admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=q3), ctx))
        navs = [s for s in bot.sent if s.get("__kind") == "message" and s.get("reply_markup")]
        last = nav_buttons(navs[-1:])
        check("last page has no Next", "admin_page_4" not in last)
        check("last page has Prev", "admin_page_2" in last)

        ctx.user_data["panel_card_ids"] = [111, 112]
        ctx.user_data["panel_nav_id"] = 113
        qd = FakeQuery("admin_search_done", FakeMessage(message_id=60), admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qd), ctx))
        check("done clears state", ctx.user_data.get("state") is None)
        check("done clears results", "panel_results" not in ctx.user_data)
        check("done deletes page", 111 in bot.deleted and 113 in bot.deleted)
    finally:
        ah._search_products_sync = orig_search
        set_admin(None)


def test_browse_category_cards_and_stock():
    """Category tap sends one product card per item; +/- steppers update DB."""
    import admin_handler as ah

    set_admin("12345")
    admin = FakeUser(12345)

    rows = [
        {
            "product_name": "Fortune Oil 1L", "ean": "111", "mrp": 210,
            "selling_price": 170, "stock_qty": "5",
            "image_url": "https://img/oil.jpg", "category": "General",
        },
        {
            "product_name": "Aashirvaad Atta 5kg", "ean": "222", "mrp": 300,
            "selling_price": 235, "stock_qty": "0",
            "image_url": "", "category": "General",
        },
    ]

    restore = patch_db({
        "get_categories": lambda: ["General"],
        "get_all_products": lambda: [dict(r) for r in rows],
    })
    adjusted = {}

    def fake_adjust(ean, delta):
        adjusted[ean] = delta
        for r in rows:
            if r["ean"] == ean:
                old = int(r["stock_qty"])
                new = max(0, old + delta)
                r["stock_qty"] = str(new)
                return {"ean": ean, "old_stock": old, "new_stock": new}
        raise ValueError("unknown ean")

    restore2 = patch_db({"adjust_stock_by_ean": fake_adjust})
    try:
        ctx = FakeContext()
        ctx.user_data["panel_chat_id"] = 777
        bot = ctx.bot

        qb = FakeQuery("admin_browse", FakeMessage(message_id=1), admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qb), ctx))
        check("browse loads categories", len(qb.message.edits) >= 1)

        before = len(bot.sent)
        qc = FakeQuery("admin_cat_0", FakeMessage(message_id=2), admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qc), ctx))
        new_sends = bot.sent[before:]
        photos = [s for s in new_sends if s.get("__kind") == "photo"]
        texts = [s for s in new_sends if s.get("__kind") == "message"]
        check("category tap sends 2 cards (1 photo + 1 text)",
              len(photos) == 1 and len(texts) == 1)
        caps = [s.get("caption") or s.get("text") or "" for s in new_sends]
        check("card 1 shows name", any("Fortune Oil 1L" in c for c in caps))
        check("card 2 shows name", any("Aashirvaad Atta 5kg" in c for c in caps))
        check("card shows MRP + selling", any("MRP" in c and "170" in c for c in caps))
        check("card shows stock qty", any("Stock Qty" in c and ">5<" in c for c in caps))
        check("qty 0 marked out of stock", any("Out of stock" in c for c in caps))

        def cbs(send):
            rm = send.get("reply_markup")
            return [b.callback_data for row in rm.inline_keyboard for b in row] if rm else []

        card_cbs = []
        for s in new_sends:
            card_cbs.extend(cbs(s))
        check("card has +/- steppers",
              "admin_stock_dec_111" in card_cbs and "admin_stock_inc_111" in card_cbs)
        check("card has Edit Details", "admin_editmenu_111" in card_cbs)
        check("card has Delete Item", "admin_del_111" in card_cbs)

        # Tap ➕ -> DB adjust +1 and caption re-rendered with new qty.
        qi = FakeQuery("admin_stock_inc_111", FakeMessage(chat_id=777, message_id=11), admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qi), ctx))
        check("plus calls adjust_stock_by_ean(+1)", adjusted.get("111") == 1)
        check("card re-rendered with qty 6",
              any("Stock Qty" in (c or "") and ">6<" in (c or "") for c, _ in qi.caption_edits))
        check("cache qty updated to 6",
              (ah._product_cache.get("111") or {}).get("qty") == 6)

        # Tap ➖ repeatedly -> clamps at 0, never goes negative or deleted.
        for _ in range(10):
            qd = FakeQuery("admin_stock_dec_111", FakeMessage(chat_id=777, message_id=11), admin)
            asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qd), ctx))
        final_qty = (ah._product_cache.get("111") or {}).get("qty")
        check("minus clamps at 0 (never negative)", final_qty == 0)
        check("qty 0 shows out-of-stock toast",
              any("Out of stock" in (t or "") for t, _ in qd.answers))

        # Category snapshot stays in sync.
        snap = ah._category_cache.get(777) or {}
        gen = snap.get("General") or []
        syncd = [p for p in gen if str(p.get("ean")) == "111"]
        check("category snapshot qty synced to 0",
              bool(syncd) and int(syncd[0].get("stock_qty")) == 0)

        # Edit Details swaps to the full editor; Back returns to +/−.
        ah._product_cache["111"]["qty"] = 3
        qe = FakeQuery("admin_editmenu_111", FakeMessage(chat_id=777, message_id=11), admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qe), ctx))
        ok_editor = bool(qe.markup_edits) and qe.markup_edits[0] is not None
        check("edit menu swaps keyboard", ok_editor)
        if ok_editor:
            ecb = [b.callback_data for row in qe.markup_edits[0].inline_keyboard for b in row]
            check("editor has edit name/price",
                  "edit_name_111" in ecb and "edit_price_111" in ecb)
        qback = FakeQuery("admin_backcard_111", FakeMessage(chat_id=777, message_id=11), admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qback), ctx))
        # admin_backcard_ restores caption + buttons through _edit_to_html
        # (message.edit_text), not edit_message_reply_markup.
        ok_back = bool(qback.message.edits)
        check("back returns to +/- controls", ok_back)
        if ok_back:
            back_text, back_kwargs = qback.message.edits[-1]
            back_kb = back_kwargs.get("reply_markup")
            bcb = [b.callback_data for row in back_kb.inline_keyboard for b in row] if back_kb else []
            check("back keyboard has steppers", "admin_stock_inc_111" in bcb)
            check("back restores browse caption", "Fortune Oil 1L" in back_text)

        # Callback pattern accepts the new callbacks.
        import re
        pat = re.compile(ah.PANEL_CALLBACK_PATTERN)
        check("pattern accepts stock_inc", bool(pat.match("admin_stock_inc_111")))
        check("pattern accepts stock_dec", bool(pat.match("admin_stock_dec_111")))
        check("pattern accepts editmenu", bool(pat.match("admin_editmenu_111")))
        check("pattern accepts backcard", bool(pat.match("admin_backcard_111")))
    finally:
        restore()
        restore2()
        set_admin(None)
        ah._product_cache.clear()
        ah._category_cache.clear()


def test_delete_item_flow():
    """🗑 Delete Item: confirm dialog + real DB delete on photo & text cards.

    Regression: browse cards are photos, so the confirm/done messages must be
    written with edit_caption (edit_text raises on media and is swallowed).
    """
    import admin_handler as ah

    set_admin("12345")
    admin = FakeUser(12345)

    rows = [
        {
            "product_name": "Fortune Oil 1L", "ean": "111", "mrp": 210,
            "selling_price": 170, "stock_qty": "5",
            "image_url": "https://img/oil.jpg", "category": "General",
        },
        {
            "product_name": "Aashirvaad Atta 5kg", "ean": "222", "mrp": 300,
            "selling_price": 235, "stock_qty": "3",
            "image_url": "", "category": "General",
        },
    ]

    restore = patch_db({
        "get_categories": lambda: ["General"],
        "get_all_products": lambda: [dict(r) for r in rows],
    })
    deleted = []

    def fake_delete(ean):
        deleted.append(ean)
        rows[:] = [r for r in rows if str(r["ean"]) != str(ean)]
        return {"ean": ean}

    restore2 = patch_db({"delete_product_by_ean": fake_delete})
    try:
        ctx = FakeContext()
        ctx.user_data["panel_chat_id"] = 777

        # Open the category so both cards land in the product cache.
        qb = FakeQuery("admin_browse", FakeMessage(message_id=1), admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qb), ctx))
        qc = FakeQuery("admin_cat_0", FakeMessage(chat_id=777, message_id=2), admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qc), ctx))
        check("cache seeded for both cards",
              "111" in ah._product_cache and "222" in ah._product_cache)

        # --- PHOTO card: Delete -> confirm must edit the CAPTION ----------
        photo_msg = FakeMessage(chat_id=777, message_id=11,
                                photo=["https://img/oil.jpg"], caption="card")
        qd = FakeQuery("admin_del_111", photo_msg, admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qd), ctx))
        check("photo confirm edits caption (not text)",
              len(photo_msg.caption_edits) == 1 and len(photo_msg.edits) == 0)
        if photo_msg.caption_edits:
            confirm_kb = photo_msg.caption_edits[-1][1].get("reply_markup")
            ccb = [b.callback_data for row in confirm_kb.inline_keyboard for b in row]
            check("confirm has Yes + Cancel",
                  "admin_delconfirm_111" in ccb and "admin_backcard_111" in ccb)

        # --- Cancel returns to the browse card ----------------------------
        qcancel = FakeQuery("admin_backcard_111", photo_msg, admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qcancel), ctx))
        check("cancel restores photo caption", len(photo_msg.caption_edits) == 2)
        if len(photo_msg.caption_edits) >= 2:
            back_kb = photo_msg.caption_edits[-1][1].get("reply_markup")
            bcb = [b.callback_data for row in back_kb.inline_keyboard for b in row]
            check("cancel restores +/- steppers", "admin_stock_inc_111" in bcb)

        # --- Yes deletes from the DB and shows the done message ------------
        qyes = FakeQuery("admin_delconfirm_111", photo_msg, admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qyes), ctx))
        check("photo delete reaches database", deleted == ["111"])
        check("deleted uid removed from cache", "111" not in ah._product_cache)
        check("photo done message edits caption",
              len(photo_msg.caption_edits) == 3 and len(photo_msg.edits) == 0)

        # --- TEXT card: same flow but through edit_text --------------------
        text_msg = FakeMessage(chat_id=777, message_id=12, text="card")
        qd2 = FakeQuery("admin_del_222", text_msg, admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qd2), ctx))
        check("text confirm edits text",
              len(text_msg.edits) == 1 and len(text_msg.caption_edits) == 0)
        qyes2 = FakeQuery("admin_delconfirm_222", text_msg, admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qyes2), ctx))
        check("text delete reaches database", deleted == ["111", "222"])
        check("text done message edits text", len(text_msg.edits) == 2)
        check("all products deleted", len(rows) == 0)

        # Category snapshot no longer lists deleted items.
        snap = ah._category_cache.get(777) or {}
        gen = snap.get("General") or []
        check("category snapshot cleared of deleted eans",
              not [p for p in gen if str(p.get("ean")) in ("111", "222")])
    finally:
        restore()
        restore2()
        set_admin(None)
        ah._product_cache.clear()
        ah._category_cache.clear()


def test_browse_search_cards():
    """Browse-by-Category 🔍 Search queries OUR DB and sends stock cards."""
    import admin_handler as ah

    set_admin("12345")
    admin = FakeUser(12345)

    rows = [
        {"product_name": "Fortune Oil 1L", "ean": "111", "mrp": 210,
         "selling_price": 170, "stock_qty": "5",
         "image_url": "https://img/oil.jpg", "category": "General"},
        {"product_name": "Tata Salt 1kg", "ean": "222", "mrp": 28,
         "selling_price": 25, "stock_qty": "9",
         "image_url": "", "category": "Grocery"},
    ]

    seen = {}

    def fake_search(keyword, limit=25):
        seen["keyword"] = keyword
        # emulate db.search_products: only rows whose name matches
        needle = str(keyword).lower()
        return [dict(r) for r in rows if needle in r["product_name"].lower()]

    restore = patch_db({
        "get_categories": lambda: ["General", "Grocery"],
        "get_all_products": lambda: [dict(r) for r in rows],
        "search_products": fake_search,
    })
    try:
        ctx = FakeContext()
        ctx.user_data["panel_chat_id"] = 777
        bot = ctx.bot

        # 1. Tapping 🔍 Search opens the prompt and enters the browse-search state.
        qs = FakeQuery("admin_browse_search", FakeMessage(message_id=1), admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qs), ctx))
        check("browse search enters state",
              ctx.user_data.get("state") == ah.STATE_BROWSE_SEARCH)
        check("browse search shows prompt",
              any("ସର୍ଚ୍ଚ" in (t or "") for t, _ in qs.message.edits))

        # 2. Typing a keyword searches OUR database (not JioMart).
        msg = FakeMessage(text="fortune", user=admin, chat_id=777, message_id=5)
        asyncio.run(ah.handle_admin_panel_text(FakeUpdate(message=msg, user=admin), ctx))
        check("browse search queries our DB", seen.get("keyword") == "fortune")
        check("browse search keeps state alive",
              ctx.user_data.get("state") == ah.STATE_BROWSE_SEARCH)
        photos = [s for s in bot.sent if s.get("__kind") == "photo"]
        texts = [s for s in bot.sent if s.get("__kind") == "message"]
        check("browse search sends 1 matching card",
              len(photos) == 1 and "fortune" in (photos[0].get("caption") or "").lower())
        check("browse search card has stock controls",
              any("admin_stock_inc_111" in
                  [b.callback_data for row in s["reply_markup"].inline_keyboard for b in row]
                  for s in photos if s.get("reply_markup")))
        check("browse search card has Delete (stock manage menu)",
              any("admin_del_111" in
                  [b.callback_data for row in s["reply_markup"].inline_keyboard for b in row]
                  for s in photos if s.get("reply_markup")))
        # only 1 card because Tata Salt did not match "fortune"
        check("browse search excludes non-matches",
              not any("Tata Salt" in (s.get("caption") or s.get("text") or "")
                      for s in bot.sent))

        # 3. A no-match keyword shows the no-results toast (stays alive).
        bot.sent.clear()
        msg2 = FakeMessage(text="zzzz", user=admin, chat_id=777, message_id=6)
        asyncio.run(ah.handle_admin_panel_text(FakeUpdate(message=msg2, user=admin), ctx))
        check("browse search no-results reply",
              any("ମିଳିଲା ନାହିଁ" in (r[0] or "") for r in msg2.replies))

        # 4. Callback pattern accepts the browse-search button.
        import re
        pat = re.compile(ah.PANEL_CALLBACK_PATTERN)
        check("pattern accepts admin_browse_search",
              bool(pat.match("admin_browse_search")))

        # 5. Search button is present on the category list keyboard.
        kb = ah._browse_categories_keyboard(["General"])
        cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
        check("category keyboard has Search button",
              "admin_browse_search" in cbs)
    finally:
        restore()
        set_admin(None)
        ah._product_cache.clear()
        ah._category_cache.clear()


def test_imglink_generator():
    """🖼 Image Link Generator: keyword -> JioMart photo cards with the URL."""
    import re

    import admin_handler as ah

    set_admin("12345")
    admin = FakeUser(12345)

    urls = [
        "https://cdn.example/oil-1.jpg",
        "https://cdn.example/oil-2.jpg",
        "https://cdn.example/oil-3.jpg",
    ]
    seen = {}

    def fake_fetch(keyword, start_index=1):
        seen["keyword"] = keyword
        seen["start"] = start_index
        return list(urls)

    restore = patch_fetch(fake_fetch)
    try:
        # 1. Panel home has the button; the pattern accepts the callbacks.
        home = ah._panel_home_keyboard()
        home_cbs = [b.callback_data for row in home.inline_keyboard for b in row]
        check("panel home has Image Link Generator button", "admin_imglink" in home_cbs)
        pat = re.compile(ah.PANEL_CALLBACK_PATTERN)
        check("pattern accepts admin_imglink", bool(pat.match("admin_imglink")))
        check("pattern accepts admin_imglink_more_4",
              bool(pat.match("admin_imglink_more_4")))
        check("pattern accepts admin_imglink_urls", bool(pat.match("admin_imglink_urls")))

        # 2. Tapping the button arms the prompt + state.
        ctx = FakeContext()
        qs = FakeQuery("admin_imglink", FakeMessage(message_id=1), admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qs), ctx))
        check("imglink enters state", ctx.user_data.get("state") == ah.STATE_IMAGE_LINK)
        check("imglink shows prompt", bool(qs.message.edits))

        # 3. Typing a keyword searches JioMart and posts photo cards.
        msg = FakeMessage(text="fortune oil", user=admin, chat_id=777, message_id=5)
        asyncio.run(ah.handle_admin_panel_text(FakeUpdate(message=msg, user=admin), ctx))
        check("imglink queries JioMart", seen.get("keyword") == "fortune oil")
        check("imglink keeps state alive",
              ctx.user_data.get("state") == ah.STATE_IMAGE_LINK)
        photos = [x for x in ctx.bot.sent if x.get("__kind") == "photo"]
        check("imglink sends 3 photo cards", len(photos) == 3)
        caption = (photos[0].get("caption") or "") if photos else ""
        check("imglink caption has the raw URL in a code block",
              "<code>" + urls[0] + "</code>" in caption)
        check("imglink caption links the URL", "href='" + urls[0] + "'" in caption)
        markup = photos[0].get("reply_markup") if photos else None
        cbs = [b.callback_data for row in markup.inline_keyboard for b in row] if markup else []
        check("imglink card has More + All URLs", "admin_imglink_more_4" in cbs
              and "admin_imglink_urls" in cbs)

        # 4. "All URLs" posts one message with tappable code blocks.
        qs_msg = FakeMessage(message_id=2)
        qs2 = FakeQuery("admin_imglink_urls", qs_msg, admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qs2), ctx))
        joined = "\n".join(t or "" for t, _ in qs_msg.replies)
        check("imglink All URLs lists every URL", all(u in joined for u in urls))

        # 5. No results -> Odia hint, state stays so another keyword works.
        restore()
        restore = patch_fetch(lambda keyword, start_index=1: [])
        msg2 = FakeMessage(text="zzzz", user=admin, chat_id=777, message_id=6)
        asyncio.run(ah.handle_admin_panel_text(FakeUpdate(message=msg2, user=admin), ctx))
        check("imglink no results hint", any("ମିଳିଲା ନାହିଁ" in (t or "") for t, _ in msg2.replies))
        check("imglink keeps state after no results",
              ctx.user_data.get("state") == ah.STATE_IMAGE_LINK)
    finally:
        restore()


def test_add_admin_flow():
    """👥 Add Admin: grant + revoke stock-manage access from the panel."""
    import re
    import tempfile

    import admin_handler as ah

    set_admin("12345")
    admin = FakeUser(12345)

    # Redirect the persisted admin list to a throwaway file.
    orig_file = ah._ADMINS_FILE
    tmp_path = os.path.join(
        tempfile.gettempdir(), f"shopbot_test_admins_{os.getpid()}.json"
    )
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    ah._ADMINS_FILE = tmp_path
    ah._extra_admins = None
    try:
        # 1. Panel home shows the button; pattern accepts the callbacks.
        home = ah._panel_home_keyboard()
        home_cbs = [b.callback_data for row in home.inline_keyboard for b in row]
        check("panel home has Add Admin button", "admin_addadmin" in home_cbs)
        pat = re.compile(ah.PANEL_CALLBACK_PATTERN)
        check("pattern accepts admin_addadmin", bool(pat.match("admin_addadmin")))
        check("pattern accepts admin_rmadmin_<uid>",
              bool(pat.match("admin_rmadmin_999888777")))

        # 2. Tapping Add Admin arms the prompt.
        ctx = FakeContext()
        qs = FakeQuery("admin_addadmin", FakeMessage(message_id=1), admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qs), ctx))
        check("add-admin enters state",
              ctx.user_data.get("state") == ah.STATE_ADD_ADMIN)
        prompt_edits = [t for t, _ in qs.message.edits]
        check("add-admin prompt shown",
              any("User ID" in (t or "") for t in prompt_edits))
        prompt_kb = qs.message.edits[0][1].get("reply_markup")
        pcbs = [b.callback_data for row in prompt_kb.inline_keyboard for b in row]
        check("add-admin prompt has Back", "admin_panel" in pcbs)

        # 3. Bad id -> error, state kept for retry.
        bad = FakeMessage(text="not-a-number", user=admin, chat_id=777, message_id=2)
        asyncio.run(ah.handle_admin_panel_text(FakeUpdate(message=bad, user=admin), ctx))
        check("add-admin rejects bad id",
              any("ଭୁଲ User ID" in (r[0] or "") for r in bad.replies))
        check("add-admin keeps state after bad id",
              ctx.user_data.get("state") == ah.STATE_ADD_ADMIN)

        # 4. Valid id -> persisted, is_admin flips, confirmation sent.
        good = FakeMessage(text="999888777", user=admin, chat_id=777, message_id=3)
        asyncio.run(ah.handle_admin_panel_text(FakeUpdate(message=good, user=admin), ctx))
        check("add-admin persisted",
              "999888777" in ah._load_extra_admins())
        check("add-admin file written",
              os.path.exists(tmp_path)
              and "999888777" in open(tmp_path, encoding="utf-8").read())
        check("new id is now an admin", ah.is_admin(999888777) is True)
        check("unknown id still denied", ah.is_admin(111222333) is False)
        check("add-admin confirmation sent",
              any("999888777" in (r[0] or "") for r in good.replies))
        check("add-admin clears state",
              "state" not in ctx.user_data)

        # 5. The new admin can open the panel and manage stock.
        new_admin = FakeUser(999888777)
        ctx2 = FakeContext()
        qs2 = FakeQuery("admin_panel", FakeMessage(message_id=5), new_admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qs2), ctx2))
        denied = [a for a in qs2.answers if a[0] and "Not authorised" in a[0]]
        check("new admin passes authorisation", not denied)
        check("new admin is admin via is_admin", ah.is_admin(999888777) is True)

        # 6. Duplicate id -> warned, not re-added.
        ctx.user_data["state"] = ah.STATE_ADD_ADMIN
        dup = FakeMessage(text="999888777", user=admin, chat_id=777, message_id=4)
        asyncio.run(ah.handle_admin_panel_text(FakeUpdate(message=dup, user=admin), ctx))
        check("duplicate add warned",
              any("ପୂର୍ବରୁ" in (r[0] or "") for r in dup.replies))

        # 7. Remove button revokes access.
        qs3 = FakeQuery("admin_rmadmin_999888777", FakeMessage(message_id=6), admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qs3), ctx))
        check("remove revokes access", ah.is_admin(999888777) is False)
        check("remove clears the list", "999888777" not in ah._load_extra_admins())

        # 8. Self-removal is blocked (no lock-out).
        ah.add_extra_admin("555666777")
        thief = FakeUser(555666777)
        qs4 = FakeQuery("admin_rmadmin_555666777", FakeMessage(message_id=7), thief)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qs4), ctx))
        check("self-removal blocked", ah.is_admin(555666777) is True)
        blocked = [a for a in qs4.answers if a[1]]  # show_alert answers
        check("self-removal answered with alert", bool(blocked))

        # 9. all_admin_ids lists env + extras.
        ids = ah.all_admin_ids()
        check("all_admin_ids has env admin", "12345" in ids)
        check("all_admin_ids has extra admin", "555666777" in ids)
    finally:
        ah._ADMINS_FILE = orig_file
        ah._extra_admins = None
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        set_admin(None)


def test_ai_toggle():
    """🤖 Active AI toggle: Grok -> Gemini -> Groq cycle, persisted in bot_settings.json."""
    import re
    import tempfile

    import admin_handler as ah

    import ai_agent

    orig_settings = ai_agent.SETTINGS_FILE
    tmp_path = os.path.join(
        tempfile.gettempdir(), f"shopbot_test_settings_{os.getpid()}.json"
    )
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    ai_agent.SETTINGS_FILE = tmp_path
    ai_agent._active_ai_cache = None
    try:
        # Default engine is Grok; the home button reflects it.
        check("default active AI is Grok", ai_agent.get_active_ai() == "Grok")
        label = ah._active_ai_button_label()
        check("home button shows Grok", "Grok" in label)
        pat = re.compile(ah.PANEL_CALLBACK_PATTERN)
        check("pattern accepts admin_ai_toggle", bool(pat.match("admin_ai_toggle")))

        # Tap the toggle: Grok -> Gemini (persisted, button re-rendered).
        set_admin("12345")
        admin = FakeUser(12345)
        ctx = FakeContext()
        qs = FakeQuery("admin_ai_toggle", FakeMessage(message_id=1), admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qs), ctx))
        check("toggle switches to Gemini", ai_agent.get_active_ai() == "Gemini")
        check("toggle persisted to settings",
              os.path.exists(tmp_path)
              and "Gemini" in open(tmp_path, encoding="utf-8").read())
        check("toggle answers the query",
              any(a[0] and "Gemini" in a[0] for a in qs.answers))
        toggle_kb = qs.message.edits[0][1].get("reply_markup")
        check("toggle edits home with Gemini button",
              "admin_ai_toggle" in
              [b.callback_data for row in toggle_kb.inline_keyboard for b in row]
              and any("Gemini" in b.text
                      for row in toggle_kb.inline_keyboard for b in row))

        # Tap again: Gemini -> Groq.
        qs2 = FakeQuery("admin_ai_toggle", FakeMessage(message_id=2), admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qs2), ctx))
        check("toggle switches to Groq", ai_agent.get_active_ai() == "Groq")
        check("home button shows Groq", "Groq" in ah._active_ai_button_label())

        # Tap once more: Groq -> Grok (full 3-way cycle).
        qs3 = FakeQuery("admin_ai_toggle", FakeMessage(message_id=3), admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qs3), ctx))
        check("toggle cycles back to Grok", ai_agent.get_active_ai() == "Grok")
        check("home button shows Grok again",
              "Grok" in ah._active_ai_button_label())
    finally:
        ai_agent.SETTINGS_FILE = orig_settings
        ai_agent._active_ai_cache = None
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        set_admin(None)


def test_manual_add_flow():
    """➕ Add Product Manually (part 1): Smart Category -> name -> photo."""
    import admin_handler as ah

    set_admin("12345")
    admin = FakeUser(12345)

    restore = patch_db({"get_categories": lambda: ["General", "Grocery"]})
    try:
        ctx = FakeContext()
        bot = ctx.bot

        # 1. Entry point button + pattern.
        home = ah._panel_home_keyboard()
        check("panel home has manual-add button",
              "admin_manual" in
              [b.callback_data for row in home.inline_keyboard for b in row])
        import re
        check("pattern accepts admin_manual",
              bool(re.compile(ah.PANEL_CALLBACK_PATTERN).match("admin_manual")))
        check("pattern accepts admin_manual_cat_<i>",
              bool(re.compile(ah.PANEL_CALLBACK_PATTERN).match("admin_manual_cat_3")))
        check("pattern accepts admin_manual_skip_ean",
              bool(re.compile(ah.PANEL_CALLBACK_PATTERN).match("admin_manual_skip_ean")))
        check("pattern accepts admin_manual_cancel",
              bool(re.compile(ah.PANEL_CALLBACK_PATTERN).match("admin_manual_cancel")))

        # 2. Tap the button: Step-1 Smart Category picker from the sheet.
        qs = FakeQuery("admin_manual", FakeMessage(message_id=1), admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qs), ctx))
        check("manual enters category state",
              ctx.user_data.get("state") == ah.STATE_MANUAL_CATEGORY)
        kb = qs.message.edits[0][1].get("reply_markup")
        cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
        check("category picker lists sheet categories",
              "admin_manual_cat_0" in cbs and "admin_manual_cat_1" in cbs)
        check("picker has Type New + Cancel",
              "admin_manual_newcat" in cbs and "admin_manual_cancel" in cbs)

        # 3. Pick the first category -> Step 2 (name).
        qs2 = FakeQuery("admin_manual_cat_0", FakeMessage(message_id=2), admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qs2), ctx))
        check("picked category stored",
              ctx.user_data.get("manual", {}).get("category") == "General")
        check("manual enters name state",
              ctx.user_data.get("state") == ah.STATE_MANUAL_NAME)

        # 4. Type New Category path.
        qs3 = FakeQuery("admin_manual_newcat", FakeMessage(message_id=3), admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qs3), ctx))
        check("new-category enters typing state",
              ctx.user_data.get("state") == ah.STATE_MANUAL_NEW_CATEGORY)
        mnew = FakeMessage(text="Snacks", user=admin, chat_id=777, message_id=10)
        asyncio.run(ah.handle_admin_panel_text(FakeUpdate(message=mnew, user=admin), ctx))
        check("typed category stored",
              ctx.user_data.get("manual", {}).get("category") == "Snacks")

        # 5. Type the name -> Step 3 (photo).
        mname = FakeMessage(text="Test Soap 100g", user=admin, chat_id=777, message_id=11)
        asyncio.run(ah.handle_admin_panel_text(FakeUpdate(message=mname, user=admin), ctx))
        check("manual enters photo state",
              ctx.user_data.get("state") == ah.STATE_MANUAL_PHOTO)

        # 6. Upload the photo -> forwarded to the DB channel.
        mphoto = FakeMessage(text=None, user=admin, chat_id=777, message_id=12,
                             photo=[_PhotoFile("user-file-1")])
        asyncio.run(ah.handle_admin_manual_photo(FakeUpdate(message=mphoto, user=admin), ctx))
        forwards = [s for s in bot.sent if s.get("__kind") == "forward"]
        check("photo forwarded to the channel", len(forwards) == 1)
        check("sheet uses the channel file_id",
              ctx.user_data.get("manual", {}).get("image") == "chan-file-1")
        check("manual enters prices state",
              ctx.user_data.get("state") == ah.STATE_MANUAL_PRICES)
    finally:
        restore()
        set_admin(None)
        ah._product_cache.clear()


def test_manual_add_save():
    """➕ Add Product Manually (part 2): prices -> qty -> ean -> sheet save."""
    import admin_handler as ah

    set_admin("12345")
    admin = FakeUser(12345)

    seen = {}

    def fake_add(name, mrp, price, qty, ean, image, category=""):
        seen.update({"name": name, "mrp": mrp, "price": price,
                     "qty": qty, "ean": ean, "image": image, "category": category})
        return {"product_name": name, "mrp": mrp, "selling_price": price,
                "stock_qty": qty, "ean": ean, "image_url": image, "category": category}

    restore = patch_db({
        "add_product": fake_add,
        "find_by_ean": lambda ean: None,
    })
    try:
        ctx = FakeContext()
        bot = ctx.bot

        # Seed the flow mid-way and type prices "150, 130".
        ctx.user_data["manual"] = {"category": "Snacks", "name": "Test Soap 100g",
                                   "image": "chan-file-1"}
        ctx.user_data["state"] = ah.STATE_MANUAL_PRICES
        mprices = FakeMessage(text="150, 130", user=admin, chat_id=777, message_id=13)
        asyncio.run(ah.handle_admin_panel_text(FakeUpdate(message=mprices, user=admin), ctx))
        check("manual enters qty state",
              ctx.user_data.get("state") == ah.STATE_MANUAL_QTY)
        check("prices parsed",
              ctx.user_data.get("manual", {}).get("mrp") == 150
              and ctx.user_data.get("manual", {}).get("price") == 130)

        # Type the qty -> EAN step with Skip.
        mqty = FakeMessage(text="25", user=admin, chat_id=777, message_id=14)
        asyncio.run(ah.handle_admin_panel_text(FakeUpdate(message=mqty, user=admin), ctx))
        check("manual enters ean state",
              ctx.user_data.get("state") == ah.STATE_MANUAL_EAN)
        ean_kb = mqty.replies[0][1].get("reply_markup")
        check("ean prompt has Skip + Cancel",
              "admin_manual_skip_ean" in
              [b.callback_data for row in ean_kb.inline_keyboard for b in row]
              and "admin_manual_cancel" in
              [b.callback_data for row in ean_kb.inline_keyboard for b in row])

        # Skip -> auto-EAN, sheet append, confirmation + preview card.
        bot.sent.clear()
        qskip = FakeQuery("admin_manual_skip_ean", FakeMessage(message_id=4), admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qskip), ctx))
        check("sheet append called with manual row",
              seen.get("name") == "Test Soap 100g"
              and seen.get("category") == "Snacks"
              and seen.get("image") == "chan-file-1")
        ean = str(seen.get("ean") or "")
        check("ean auto-generated (13 digits)", ean.isdigit() and len(ean) == 13)
        check("manual state cleared after save", "state" not in ctx.user_data)
        previews = [s for s in bot.sent if s.get("__kind") == "photo"]
        check("preview card sent",
              len(previews) == 1
              and "Product successfully added" in (previews[0].get("caption") or ""))
        pkb = previews[0].get("reply_markup")
        check("preview has Admin Panel button",
              "admin_panel" in
              [b.callback_data for row in pkb.inline_keyboard for b in row])

        # Duplicate typed EAN is rejected (stays in EAN state).
        restore2 = patch_db({"find_by_ean": lambda ean: (1, ["row"])})
        try:
            ctx.user_data["manual"] = {"name": "Dup", "category": "General",
                                       "mrp": 10, "price": 9, "qty": 1, "image": ""}
            ctx.user_data["state"] = ah.STATE_MANUAL_EAN
            mdup = FakeMessage(text="1111", user=admin, chat_id=777, message_id=15)
            asyncio.run(ah.handle_admin_panel_text(FakeUpdate(message=mdup, user=admin), ctx))
            check("duplicate ean warned",
                  any("ପୂର୍ବରୁ" in (r[0] or "") for r in mdup.replies))
            check("duplicate ean keeps state",
                  ctx.user_data.get("state") == ah.STATE_MANUAL_EAN)
        finally:
            restore2()

        # Cancel clears everything.
        ctx.user_data["manual"] = {"name": "X"}
        ctx.user_data["state"] = ah.STATE_MANUAL_QTY
        qc = FakeQuery("admin_manual_cancel", FakeMessage(message_id=5), admin)
        asyncio.run(ah.handle_admin_panel_callback(FakeUpdate(callback_query=qc), ctx))
        check("cancel clears manual state",
              "state" not in ctx.user_data and "manual" not in ctx.user_data)
    finally:
        restore()
        set_admin(None)
        ah._product_cache.clear()


def main() -> int:
    original_admin_id = os.environ.get("ADMIN_ID")
    try:
        test_sync()
        test_existing_item_flow()
        test_new_item_and_select_flow()
        test_pagination_flow()
        test_defence_and_filter()
        test_expired_and_stale_picker()
        test_save_failure_keeps_picker()
        test_cancel_flow()
        test_language_preferences()
        test_language_menu_ui()
        test_language_callback_deny_non_lang()
        test_ask_uses_set_language()
        test_language_callback_expired_picker()
        test_handle_customer_uses_lang()
        test_language_callback_flow()
        test_language_edit_failure_tolerated()
        test_language_non_ascii_answer_failure_safe()
        test_panel_pincode_and_pagination()
        test_panel_pagination_and_done()
        test_browse_category_cards_and_stock()
        test_delete_item_flow()
        test_browse_search_cards()
        test_add_admin_flow()
        test_imglink_generator()
        test_ai_toggle()
        test_manual_add_flow()
        test_manual_add_save()
    finally:
        set_admin(original_admin_id)
        admin_handler._pending.clear()

    print()
    if failures:
        print(f"{failures} check(s) FAILED")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



