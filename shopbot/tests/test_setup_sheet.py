"""Validation for setup_sheet.py (no network, no credentials needed).

Covers: credential resolution, email resolution, .env upsert, arg parsing,
header logic and sharing logic - all with fakes.

Run with:  python tests/test_setup_sheet.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Pre-import the slow dependency first (this machine is slow at imports).
import gspread.exceptions  # noqa: E402, F401

import setup_sheet  # noqa: E402
from setup_sheet import SetupError  # noqa: E402

# Never block on the interactive prompt during tests.
import builtins as _builtins


def _no_input(prompt=""):
    raise EOFError("no interactive input in tests")


_builtins.input = _no_input


def _no_input(prompt=""):
    raise EOFError("no interactive input in tests")


_builtins.input = _no_input

failures = 0


def check(name: str, condition: bool) -> None:
    global failures
    print(("PASS" if condition else "FAIL"), "-", name)
    if not condition:
        failures += 1


class FakeWorksheet:
    def __init__(self, first_row=None):
        self._first_row = list(first_row or [])
        self.updated = []
        self.inserted = []
        self.formatted = []

    def row_values(self, row):
        assert row == 1
        return list(self._first_row)

    def update(self, values=None, range_name=None, **kwargs):
        self.updated.append((values, range_name))
        return {}

    def insert_row(self, values, index=1, **kwargs):
        self.inserted.append((list(values), index))
        return {}

    def format(self, range_name, fmt):
        self.formatted.append((range_name, fmt))
        return {}


class FakeSpreadsheet:
    def __init__(self, sid="abc123", first_row=None):
        self.id = sid
        self.sheet1 = FakeWorksheet(first_row)
        self.shared = []

    def share(self, email, perm_type="user", role="writer", **kwargs):
        self.shared.append((email, perm_type, role))
        return {"ok": True}


class FakeExistingClient:
    def __init__(self, sheet):
        self._sheet = sheet
        self.created = []

    def open(self, title):
        return self._sheet

    def create(self, title):
        raise AssertionError("create() should not be called when open() works")


class FakeFreshClient:
    def __init__(self, sheet):
        self._sheet = sheet
        self.created = []

    def open(self, title):
        from gspread.exceptions import SpreadsheetNotFound

        raise SpreadsheetNotFound()

    def create(self, title):
        self.created.append(title)
        return self._sheet


def with_env(**values):
    """Temporarily set env vars; returns a restore callable."""
    saved = {k: os.environ.get(k) for k in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    def restore():
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    return restore


def main() -> int:
    # --- credential resolution ---------------------------------------------
    # Point DEFAULT_CREDS at a missing temp path so the real credentials.json
    # (if present in the workspace) does not mask the "not found" case.
    orig_default = setup_sheet.DEFAULT_CREDS
    with tempfile.TemporaryDirectory() as tmp:
        setup_sheet.DEFAULT_CREDS = Path(tmp) / "credentials.json"
        missing = Path(tmp) / "nope.json"
        restore = with_env(GOOGLE_SHEETS_CREDENTIALS_FILE=None)
        try:
            got = setup_sheet.resolve_credentials(str(missing))
            check("explicit missing creds path -> SetupError", False)
        except SetupError as exc:
            check("explicit missing creds path -> SetupError", "credentials.json" in str(exc))
        finally:
            restore()

        real = Path(tmp) / "real.json"
        real.write_text("{}", encoding="utf-8")
        check("explicit creds path resolves", setup_sheet.resolve_credentials(str(real)) == real)
    setup_sheet.DEFAULT_CREDS = orig_default

    # --- email resolution ---------------------------------------------------
    restore = with_env(USER_EMAIL=None)
    try:
        check("bad --email rejected", _bad_email("--bad@@x") is True)
        check(
            "--email accepted",
            setup_sheet.resolve_email("owner@gmail.com") == "owner@gmail.com",
        )
        check(
            "USER_EMAIL fallback",
            _with_user_email("shop.owner@yahoo.in") == "shop.owner@yahoo.in",
        )
    finally:
        restore()
    check("garbage email rejected", _bad_email("not-an-email") is True)
    return main_continued()


def _bad_email(value: str) -> bool:
    """True when resolve_email rejects ``value`` with SetupError."""
    try:
        setup_sheet.resolve_email(value)
    except SetupError:
        return True
    except (EOFError, OSError):
        return True  # fell through to the interactive prompt
    return False


def _with_user_email(value: str) -> str:
    restore = with_env(USER_EMAIL=value)
    try:
        return setup_sheet.resolve_email(None)
    finally:
        restore()


def main_continued() -> int:
    global failures
    # --- .env upsert --------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / ".env"
        env_file.write_text(
            "# comment\nBOT_TOKEN=old-token\nADMIN_ID=1\n", encoding="utf-8"
        )
        original = setup_sheet.ENV_PATH
        setup_sheet.ENV_PATH = env_file
        try:
            setup_sheet.upsert_env({"BOT_TOKEN": "new-token", "GOOGLE_SHEET_ID": "abc"})
            text = env_file.read_text(encoding="utf-8")
            check("upsert updates existing key", "BOT_TOKEN=new-token" in text.splitlines())
            check("upsert appends new key", "GOOGLE_SHEET_ID=abc" in text.splitlines())
            check("upsert keeps comments", "# comment" in text.splitlines())
            check("upsert keeps other keys", "ADMIN_ID=1" in text.splitlines())
            check("upsert ends with newline", text.endswith("\n") and not text.endswith("\n\n"))
        finally:
            setup_sheet.ENV_PATH = original

    # --- spreadsheet create / re-use ----------------------------------------
    existing = FakeSpreadsheet(sid="keep-me")
    sheet, created = setup_sheet.ensure_spreadsheet(FakeExistingClient(existing), "T")
    check("re-uses existing sheet", sheet is existing and created is False)

    fresh = FakeSpreadsheet(sid="brand-new")
    sheet, created = setup_sheet.ensure_spreadsheet(FakeFreshClient(fresh), "T")
    check("creates missing sheet", sheet is fresh and created is True)

    # --- headers --------------------------------------------------------------
    blank = FakeSpreadsheet(first_row=[])
    setup_sheet.ensure_headers(blank)
    check(
        "writes headers to blank sheet",
        blank.sheet1.updated == [([setup_sheet.HEADERS], "A1:F1")],
    )
    check("header formatted bold", blank.sheet1.formatted == [("A1:F1", {"textFormat": {"bold": True}})])

    already = FakeSpreadsheet(first_row=list(setup_sheet.HEADERS))
    setup_sheet.ensure_headers(already)
    check("skips matching headers", already.sheet1.updated == [] and already.sheet1.inserted == [])

    occupied = FakeSpreadsheet(first_row=["old", "data"])
    setup_sheet.ensure_headers(occupied)
    check(
        "inserts above existing row 1",
        occupied.sheet1.inserted == [(setup_sheet.HEADERS, 1)],
    )

    # --- sharing ----------------------------------------------------------------
    shareable = FakeSpreadsheet()
    setup_sheet.share_with_user(shareable, "owner@gmail.com")
    check(
        "shares as user/writer",
        shareable.shared == [("owner@gmail.com", "user", "writer")],
    )

    # --- arg parsing --------------------------------------------------------------
    args = setup_sheet.parse_args(["--email", "a@b.com"])
    check("parses --email", args.email == "a@b.com")
    args = setup_sheet.parse_args([])
    check("defaults email/title", args.email is None and "MUNNA" in args.title)

    print()
    if failures:
        print(f"{failures} check(s) FAILED")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

