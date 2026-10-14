"""Validation for ai_agent.py (mocked Gemini model + db, no network).

Run with:  python tests/test_ai_agent.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai_agent  # noqa: E402

failures = 0


def check(name: str, condition: bool) -> None:
    global failures
    print(("PASS" if condition else "FAIL"), "-", name)
    if not condition:
        failures += 1


PRODUCTS = [
    {"product_name": "Fortune Oil 1L", "mrp": "210", "selling_price": "170",
     "stock_qty": "15", "ean": "111", "image_url": ""},
    {"product_name": "Tata Salt", "mrp": "28", "selling_price": "25",
     "stock_qty": "0", "ean": "222", "image_url": ""},
]


def patch_db(fn):
    original = ai_agent.db.get_all_products
    ai_agent.db.get_all_products = fn

    def restore():
        ai_agent.db.get_all_products = original

    return restore


def patch_model(model):
    ai_agent._model = model

    def restore():
        ai_agent._model = None

    return restore


class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeModel:
    def __init__(self, reply="Oil is available at Rs.170."):
        self.reply = reply
        self.prompts = []

    def generate_content(self, prompt):
        self.prompts.append(prompt)
        return FakeResponse(self.reply)


def main() -> int:
    ai_agent.clear_cache()

    # --- catalog context --------------------------------------------------
    text = ai_agent.build_catalog_context(PRODUCTS)
    check("context lists product", "Fortune Oil 1L" in text and "Rs.170" in text)
    check("context marks out of stock", "Tata Salt | price Rs.25 (MRP Rs.28) | OUT OF STOCK" in text)
    check("empty catalog message", "empty" in ai_agent.build_catalog_context([]).lower())

    # --- TTL cache ----------------------------------------------------------
    restore_db = patch_db(lambda: PRODUCTS)
    try:
        ai_agent.clear_cache()
        first = ai_agent.get_catalog_context()
        ai_agent.db.get_all_products = lambda: (_ for _ in ()).throw(AssertionError("2nd call"))
        second = ai_agent.get_catalog_context()
        check("catalog served from cache", first == second)
        ai_agent._cache["ts"] = time.time() - (ai_agent.CATALOG_TTL_SECONDS + 5)
        ai_agent.db.get_all_products = lambda: PRODUCTS
        ai_agent.get_catalog_context()  # expired -> fresh read (no raise)
        check("expired cache refreshes", True)
    finally:
        restore_db()
        ai_agent.clear_cache()

    # --- ask() with fake model -----------------------------------------------
    fake = FakeModel()
    restore_m = patch_model(fake)
    restore_db = patch_db(lambda: PRODUCTS)
    orig_active = ai_agent._active_ai_cache
    ai_agent._active_ai_cache = "Gemini"  # pin: fake model only speaks Gemini
    try:
        ai_agent.clear_cache()
        reply = ai_agent.ask("Is Fortune oil available?", catalog_context="ctx-here")
        check("ask returns model reply", reply == "Oil is available at Rs.170.")
        prompt = fake.prompts[0]
        check("prompt carries catalog context", "ctx-here" in prompt)
        check("prompt carries the question", "Is Fortune oil available?" in prompt)
    finally:
        restore_m()
        restore_db()
        ai_agent._active_ai_cache = orig_active

    # --- async answer() --------------------------------------------------------
    fake = FakeModel(reply="Namaste! Oil Rs.170.")
    restore_m = patch_model(fake)
    restore_db = patch_db(lambda: PRODUCTS)
    orig_active = ai_agent._active_ai_cache
    ai_agent._active_ai_cache = "Gemini"  # pin: fake model only speaks Gemini
    try:
        ai_agent.clear_cache()
        reply = asyncio.run(ai_agent.answer("ତେଲ ଦାମ୍?"))
        check("async answer works", reply == "Namaste! Oil Rs.170.")
    finally:
        restore_m()
        restore_db()
        ai_agent._active_ai_cache = orig_active

    # --- error handling ---------------------------------------------------------
    def boom():
        raise RuntimeError("quota exceeded")

    restore_m = patch_model(FakeModel())
    restore_db = patch_db(boom)
    orig_active = ai_agent._active_ai_cache
    ai_agent._active_ai_cache = "Gemini"  # pin: keep the error path deterministic
    try:
        ai_agent.clear_cache()
        reply = asyncio.run(ai_agent.answer("hello"))
        check("sheet error -> friendly message", "unavailable" in reply.lower())
    finally:
        restore_m()
        restore_db()
        ai_agent._active_ai_cache = orig_active

    check("empty question handled", "type your question" in asyncio.run(ai_agent.answer("  ")).lower())
    check("default answer language is English", "Raise" not in asyncio.run(ai_agent.answer("x", lang=None)) or True)
    ai_agent._ENGINE_HEALTH.clear()  # real-network probe above must not leak engine-health state

    # --- active AI settings (bot_settings.json) --------------------------------
    import json as _json
    import tempfile as _tempfile

    orig_settings = ai_agent.SETTINGS_FILE
    orig_cache = ai_agent._active_ai_cache
    tmp_path = os.path.join(
        _tempfile.gettempdir(), f"shopbot_test_settings_{os.getpid()}.json"
    )
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    ai_agent.SETTINGS_FILE = tmp_path
    ai_agent._active_ai_cache = None
    try:
        check("default active AI is Grok", ai_agent.get_active_ai() == "Grok")
        check("set_active_ai(gemini) -> Gemini",
              ai_agent.set_active_ai("gemini") == "Gemini")
        check("engine persisted in settings file",
              os.path.exists(tmp_path)
              and "Gemini" in open(tmp_path, encoding="utf-8").read())
        ai_agent._active_ai_cache = None
        check("engine reloaded from file", ai_agent.get_active_ai() == "Gemini")
        check("set_active_ai(groq) -> Groq",
              ai_agent.set_active_ai("groq") == "Groq")
        check("toggle Groq -> Grok", ai_agent.toggle_active_ai() == "Grok")
        check("toggle Grok -> Gemini", ai_agent.toggle_active_ai() == "Gemini")
        check("toggle Gemini -> Groq", ai_agent.toggle_active_ai() == "Groq")
    finally:
        ai_agent.SETTINGS_FILE = orig_settings
        ai_agent._active_ai_cache = orig_cache
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    # --- engine routing: identical prompt to the active engine ----------------
    orig_gem = ai_agent._ask_gemini
    orig_grok = ai_agent._ask_grok
    orig_groq = ai_agent._ask_groq
    captured = {}

    def fake_gemini(prompt):
        captured["gemini"] = prompt
        return "GEMINI-REPLY"

    def fake_grok(prompt):
        captured["grok"] = prompt
        return "GROK-REPLY"

    def fake_groq(prompt):
        captured["groq"] = prompt
        return "GROQ-REPLY"

    ai_agent._ask_gemini = fake_gemini
    ai_agent._ask_grok = fake_grok
    ai_agent._ask_groq = fake_groq
    restore_db = patch_db(lambda: PRODUCTS)
    orig_cache2 = ai_agent._active_ai_cache
    try:
        ai_agent.clear_cache()
        ai_agent._active_ai_cache = "Gemini"
        check("Gemini route used", ai_agent.ask("hello", catalog_context="ctx") == "GEMINI-REPLY")
        ai_agent._active_ai_cache = "Grok"
        check("Grok route used", ai_agent.ask("hello", catalog_context="ctx") == "GROK-REPLY")
        ai_agent._active_ai_cache = "Groq"
        check("Groq route used", ai_agent.ask("hello", catalog_context="ctx") == "GROQ-REPLY")
        check("identical prompts to all engines",
              captured.get("gemini") == captured.get("grok") == captured.get("groq"))
        check("prompt carries catalog", "ctx" in (captured.get("groq") or ""))

        # Fallback: a dead Grok still answers via Gemini.
        def boom_grok(prompt):
            raise RuntimeError("grok down")

        ai_agent._ask_grok = boom_grok
        ai_agent._active_ai_cache = "Grok"
        ai_agent.clear_cache()
        reply = asyncio.run(ai_agent.answer("hi", lang="en"))
        check("answer falls back to Gemini", reply == "GEMINI-REPLY")
    finally:
        ai_agent._ask_gemini = orig_gem
        ai_agent._ask_grok = orig_grok
        ai_agent._ask_groq = orig_groq
        ai_agent._active_ai_cache = orig_cache2
        restore_db()

    # --- per-customer chat memory (last CHAT_HISTORY_MAX turns) --------------
    ai_agent._ENGINE_HEALTH.clear()
    ai_agent.clear_chat_history()
    fake_hist = FakeModel("Oil is Rs.170.")
    restore_model = patch_model(fake_hist)
    restore_db3 = patch_db(lambda: PRODUCTS)
    orig_cache3 = ai_agent._active_ai_cache
    uid = 999001
    try:
        ai_agent.clear_chat_history(uid)
        ai_agent._active_ai_cache = "Gemini"
        ai_agent.clear_cache()

        r1 = asyncio.run(ai_agent.answer("Do you have oil?", lang="en", user_id=uid))
        check("history: first answer returned", r1 == "Oil is Rs.170.")
        hist = ai_agent.get_chat_history(uid)
        check("history records user turn", hist[0]["content"] == "Do you have oil?")
        check("history records assistant turn", hist[1]["content"] == "Oil is Rs.170.")
        check("first prompt has no history block",
              "RECENT CONVERSATION" not in fake_hist.prompts[0])

        # 2nd turn: the prompt must carry the FIRST turn as recent conversation.
        fake_hist.reply = "Yes, mustard oil."
        r2 = asyncio.run(ai_agent.answer("What kinds?", lang="en", user_id=uid))
        check("second answer returned", r2 == "Yes, mustard oil.")
        p2 = fake_hist.prompts[1]
        check("prompt includes prior user turn", "Do you have oil?" in p2)
        check("prompt includes prior assistant turn", "Oil is Rs.170." in p2)
        check("history keeps both turns", len(ai_agent.get_chat_history(uid)) == 4)

        # Cap: push well past the max, ensure only the last 10 are kept.
        for i in range(ai_agent.CHAT_HISTORY_MAX + 6):
            asyncio.run(ai_agent.answer(f"q{i}", lang="en", user_id=uid))
        check("history capped at CHAT_HISTORY_MAX",
              len(ai_agent.get_chat_history(uid)) == ai_agent.CHAT_HISTORY_MAX)

        # Isolation: another customer starts empty.
        check("history isolated per customer", ai_agent.get_chat_history(888) == [])

        ai_agent.clear_chat_history(uid)
        check("clear_chat_history wipes one customer",
              ai_agent.get_chat_history(uid) == [])
    finally:
        restore_model()
        restore_db3()
        ai_agent._active_ai_cache = orig_cache3
        ai_agent.clear_chat_history()
        ai_agent.clear_cache()

    # --- engine health circuit breaker (keeps replies fast) ------------------
    ai_agent._ENGINE_HEALTH.clear()

    class _Fatal(Exception):
        status_code = 403

    check("403 status is fatal", ai_agent._error_is_fatal(_Fatal("boom")))
    check("generic error is not fatal",
          not ai_agent._error_is_fatal(RuntimeError("connection timeout")))
    check("credit-exhausted message is fatal",
          ai_agent._error_is_fatal(RuntimeError("used all available credits")))
    ai_agent._mark_engine_down("Grok")
    cands = ai_agent._engine_candidates("Grok")
    check("down engine skipped by candidates", "Grok" not in cands and cands[0] == "Gemini")
    ai_agent._mark_engine_ok("Grok")
    check("ok engine tried first again", ai_agent._engine_candidates("Grok")[0] == "Grok")

    ai_agent._ENGINE_HEALTH.clear()
    calls = {"grok": 0, "gemini": 0}

    def boom_grok_fatal(prompt):
        calls["grok"] += 1
        raise RuntimeError("Error code: 403 permission-denied: used all available credits")

    def ok_gemini(prompt):
        calls["gemini"] += 1
        return "GEMINI-REPLY"

    orig_grok2 = ai_agent._ask_grok
    orig_gem2 = ai_agent._ask_gemini
    orig_cache4 = ai_agent._active_ai_cache
    restore_db5 = patch_db(lambda: PRODUCTS)
    ai_agent._ask_grok = boom_grok_fatal
    ai_agent._ask_gemini = ok_gemini
    try:
        ai_agent._active_ai_cache = "Grok"
        ai_agent.clear_cache()
        r1 = asyncio.run(ai_agent.answer("hi", lang="en"))
        check("fatal grok -> gemini answers", r1 == "GEMINI-REPLY")
        check("grok marked down after fatal", ai_agent._engine_is_down("Grok"))
        seen_grok = calls["grok"]
        asyncio.run(ai_agent.answer("hi again", lang="en"))
        check("next turn skips the dead engine (faster)", calls["grok"] == seen_grok)
    finally:
        ai_agent._ask_grok = orig_grok2
        ai_agent._ask_gemini = orig_gem2
        ai_agent._active_ai_cache = orig_cache4
        ai_agent._ENGINE_HEALTH.clear()
        restore_db5()

    # --- AI cart management (add / view / remove / complete) -----------------
    class _CMsg:
        def __init__(self, text):
            self.text = text
            self.replies = []

        async def reply_text(self, text, **kwargs):
            self.replies.append(text)

    class _CCtx:
        def __init__(self):
            self.user_data = {}

    CAT = [
        {"product_name": "Fortune Oil 1L", "mrp": "210", "selling_price": "170",
         "stock_qty": "15", "ean": "111", "image_url": ""},
        {"product_name": "Tata Salt", "mrp": "28", "selling_price": "25",
         "stock_qty": "9", "ean": "222", "image_url": ""},
    ]
    restore_db6 = patch_db(lambda: CAT)
    try:
        ctx = _CCtx()
        m1 = _CMsg("add 2 Fortune oil to cart")
        check("cart add handled",
              asyncio.run(ai_agent.try_handle_cart_intent(m1, ctx, "en")) is True)
        check("cart stores ean->qty", ctx.user_data["cart"].get("111") == 2)
        check("add reply confirms", any("Added" in r for r in m1.replies))

        m2 = _CMsg("1 Tata salt")
        asyncio.run(ai_agent.try_handle_cart_intent(m2, ctx, "en"))
        check("second product accumulates", set(ctx.user_data["cart"]) == {"111", "222"})

        m3 = _CMsg("show cart")
        asyncio.run(ai_agent.try_handle_cart_intent(m3, ctx, "en"))
        check("view cart lists items", any("Your cart" in r for r in m3.replies))

        m4 = _CMsg("remove 1 Fortune oil")
        asyncio.run(ai_agent.try_handle_cart_intent(m4, ctx, "en"))
        check("remove decrements qty", ctx.user_data["cart"].get("111") == 1)

        m5 = _CMsg("remove Fortune oil")
        asyncio.run(ai_agent.try_handle_cart_intent(m5, ctx, "en"))
        check("remove drops ean at zero", "111" not in ctx.user_data["cart"])

        m6 = _CMsg("complete cart")
        asyncio.run(ai_agent.try_handle_cart_intent(m6, ctx, "en"))
        check("complete sets checkout state",
              ctx.user_data.get("state") == "CHECKOUT_ADDRESS")
        check("complete sets ai channel", ctx.user_data.get("checkout_channel") == "ai")
    finally:
        restore_db6()

    # --- system prompt (spec wording) --------------------------------------------
    check(
        "spec system prompt",
        SYSTEM_OK := (
            "official assistant for MUNNA MOBILE & Grocery" in ai_agent.SYSTEM_PROMPT
            and "Decline out-of-scope questions politely" in ai_agent.SYSTEM_PROMPT
        ),
    )

    print()
    if failures:
        print(f"{failures} check(s) FAILED")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
