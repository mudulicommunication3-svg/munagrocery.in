"""One-time setup: create the "MUNNA MOBILE & Grocery Database" Google Sheet.

What this script does:
  1. Authenticates with ``credentials.json`` (Google service account).
  2. Re-uses (or creates) a spreadsheet titled
     "MUNNA MOBILE & Grocery Database".
  3. Ensures the header row in the first worksheet:
     ["Product Name", "MRP", "Selling Price", "Stock Qty",
      "EAN Barcode", "Image URL"]
  4. Shares the sheet with your personal Gmail as writer/editor.
  5. Saves the IDs/URL into ``.env`` (GOOGLE_SHEET_ID, SPREADSHEET_URL, ...).

Usage:
    python setup_sheet.py --email you@gmail.com
    python setup_sheet.py            # uses USER_EMAIL from .env, else prompts

Prerequisites:
  * ``credentials.json`` in the project root (service-account key).
  * Google Sheets API + Google Drive API enabled in the same project
    (creation & sharing need the Drive scope).
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("setup_sheet")

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
DEFAULT_CREDS = BASE_DIR / "credentials.json"

SPREADSHEET_TITLE = "MUNNA MOBILE & Grocery Database"
HEADERS: List[str] = [
    "Product Name",
    "MRP",
    "Selling Price",
    "Stock Qty",
    "EAN Barcode",
    "Image URL",
    "Category",
]

ORDERS_TAB_NAME = "Orders"
ORDERS_HEADERS: List[str] = [
    "Timestamp",
    "Order ID",
    "Customer Name",
    "Phone",
    "Address",
    "Items",
    "Total",
    "Channel",
    "Telegram User ID",
    "Status",
    "Status Msg ID",
    "Location",
    "Payment",
]

# Drive scope is required to CREATE spreadsheets and SHARE them.
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SetupError(Exception):
    """Fatal setup problem with a user-friendly message."""


def resolve_credentials(explicit: Optional[str] = None) -> Path:
    """Locate the service-account JSON key file."""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    env_path = (os.getenv("GOOGLE_SHEETS_CREDENTIALS_FILE") or "").strip()
    if env_path:
        p = Path(env_path)
        candidates.append(p if p.is_absolute() else BASE_DIR / p)
    candidates.append(DEFAULT_CREDS)

    for path in candidates:
        if path.is_file():
            return path

    tried = ", ".join(str(p) for p in candidates)
    raise SetupError(
        "credentials.json not found.\n"
        f"Looked at: {tried}\n\n"
        "To fix:\n"
        "  1. Google Cloud Console -> IAM & Admin -> Service Accounts\n"
        "  2. Create account -> KEYS tab -> ADD KEY -> JSON (download)\n"
        "  3. Save it here as: "
        f"{DEFAULT_CREDS}\n"
        "  4. Enable 'Google Sheets API' AND 'Google Drive API' in the project."
    )


def get_client(creds_path: Path):
    """Authenticate with the service account (Sheets + Drive scopes)."""
    import gspread

    try:
        return gspread.service_account(filename=str(creds_path), scopes=SCOPES)
    except Exception as exc:  # noqa: BLE001 - show friendly guidance
        raise SetupError(
            f"Google authentication failed using {creds_path.name}: {exc}\n"
            "Check that the JSON key is valid and both Sheets + Drive "
            "APIs are enabled."
        ) from exc


def find_existing(client, title: str):
    """Return an already-created spreadsheet with this title, if any."""
    try:
        return client.open(title)
    except Exception:  # noqa: BLE001 - SpreadsheetNotFound (or Drive 404)
        return None


def ensure_spreadsheet(client, title: str):
    """Re-use today's database sheet if present, else create it.

    Tries the Drive API first; when that fails because service accounts may
    not own files (Google storage-quota policy), falls back to the Sheets
    API ``spreadsheets.create`` endpoint, which is not quota-checked.
    """
    existing = find_existing(client, title)
    if existing is not None:
        logger.info("Found existing spreadsheet '%s' (re-using it).", title)
        return existing, False

    drive_error: Optional[Exception] = None
    try:
        spreadsheet = client.create(title)
        logger.info("Created new spreadsheet '%s' (Drive API).", title)
        return spreadsheet, True
    except Exception as exc:  # noqa: BLE001 - typically storageQuotaExceeded
        drive_error = exc
        logger.warning("Drive API create failed (%s); trying the Sheets API...", exc)

    try:
        from gspread.urls import SPREADSHEETS_API_V4_BASE_URL

        response = client.http_client.request(
            "post",
            SPREADSHEETS_API_V4_BASE_URL,
            json={"properties": {"title": title}},
        )
        payload = response.json() if hasattr(response, "json") else response
        spreadsheet_id = (payload or {}).get("spreadsheetId")
        if not spreadsheet_id:
            raise RuntimeError(f"no spreadsheetId in response: {payload}")
    except Exception as exc:  # noqa: BLE001
        raise SetupError(
            "Could not create the spreadsheet automatically.\n"
            f"  Drive API said: {drive_error}\n"
            f"  Sheets API said: {exc}\n\n"
            "MANUAL ALTERNATIVE (30 seconds):\n"
            "  1. Open https://sheets.new in your browser\n"
            f"  2. Rename it to: {title}\n"
            "  3. Share it (Editor) with the service-account email shown as\n"
            "     'client_email' inside credentials.json\n"
            "  4. Re-run:  python setup_sheet.py --url <PASTE SHEET URL>"
        ) from exc
    logger.info("Created new spreadsheet '%s' (Sheets API).", title)
    return client.open_by_key(spreadsheet_id), True


def already_has_access(client, spreadsheet_id: str, email: str) -> bool:
    """True when the email already has any permission on the spreadsheet."""
    try:
        response = client.http_client.request(
            "get",
            f"https://www.googleapis.com/drive/v3/files/{spreadsheet_id}/permissions",
            params={
                "fields": "permissions(emailAddress,type,role)",
                "supportsAllDrives": "true",
            },
        )
        payload = response.json() if hasattr(response, "json") else response
        for permission in (payload or {}).get("permissions", []):
            if str(permission.get("emailAddress") or "").lower() == email.lower():
                return True
    except Exception as exc:  # noqa: BLE001 - unknown -> share anyway
        logger.debug("Permission check failed (%s); will try sharing.", exc)
    return False


def ensure_headers(spreadsheet) -> None:
    """Write the header row into the first worksheet if it is missing."""
    worksheet = spreadsheet.sheet1
    try:
        first_row = worksheet.row_values(1)
    except Exception as exc:  # noqa: BLE001
        raise SetupError(f"Could not read the first worksheet: {exc}") from exc
    cleaned = [str(cell).strip() for cell in first_row]
    if cleaned == HEADERS:
        logger.info("Header row already present - nothing to change.")
    elif any(cleaned):
        logger.warning(
            "Row 1 already has data (%r); writing headers above it.", first_row
        )
        worksheet.insert_row(HEADERS, 1)
    else:
        try:
            worksheet.update(values=[HEADERS], range_name="A1:G1")
        except Exception as exc:  # noqa: BLE001
            raise SetupError(f"Could not write the header row: {exc}") from exc
        try:
            worksheet.format("A1:G1", {"textFormat": {"bold": True}})
        except Exception as exc:  # non-fatal cosmetic step
            logger.debug("Could not bold the header row: %s", exc)
        logger.info("Header row written: %s", HEADERS)
    ensure_orders_tab(spreadsheet)


def ensure_orders_tab(spreadsheet) -> None:
    """Create the Orders tab (one row per placed order) when missing."""
    try:
        worksheet = spreadsheet.worksheet(ORDERS_TAB_NAME)
    except Exception:  # noqa: BLE001 - WorksheetNotFound (or API 404)
        worksheet = None
    if worksheet is not None:
        logger.info("Orders tab already present - nothing to change.")
        return
    try:
        worksheet = spreadsheet.add_worksheet(
            title=ORDERS_TAB_NAME, rows=1000, cols=len(ORDERS_HEADERS)
        )
    except Exception as exc:  # noqa: BLE001
        raise SetupError(f"Could not create the Orders tab: {exc}") from exc
    try:
        worksheet.update(values=[ORDERS_HEADERS], range_name="A1:I1")
    except Exception as exc:  # noqa: BLE001
        raise SetupError(f"Could not write the Orders header row: {exc}") from exc
    try:
        worksheet.format("A1:I1", {"textFormat": {"bold": True}})
    except Exception as exc:  # non-fatal cosmetic step
        logger.debug("Could not bold the Orders header row: %s", exc)
    logger.info("Orders tab created: %s", ORDERS_HEADERS)


def share_with_user(spreadsheet, email: str) -> None:
    """Share the sheet with the owner's Gmail as writer (editor)."""
    try:
        spreadsheet.share(
            email,
            perm_type="user",
            role="writer",
            notify=True,
            email_message="MUNNA MOBILE & Grocery database - shared by setup_sheet.py",
        )
    except Exception as exc:  # noqa: BLE001
        raise SetupError(
            f"Could not share the sheet with {email}: {exc}\n"
            "You can share it manually: open the sheet URL -> Share -> "
            f"add {email} as Editor."
        ) from exc
    logger.info("Shared with %s as writer.", email)


def resolve_email(explicit: Optional[str]) -> str:
    """Owner Gmail from --email, USER_EMAIL, or an interactive prompt."""
    explicit = (explicit or "").strip()
    if explicit:
        if not EMAIL_RE.match(explicit):
            raise SetupError(f"'{explicit}' is not a valid email address.")
        return explicit
    from_env = (os.getenv("USER_EMAIL") or "").strip()
    if from_env:
        if not EMAIL_RE.match(from_env):
            raise SetupError(f"'{from_env}' is not a valid email address.")
        return from_env
    if not sys.stdin.isatty():
        raise SetupError(
            "Your Gmail address is required.\n"
            "Re-run with:  python setup_sheet.py --email you@gmail.com\n"
            "or set USER_EMAIL=you@gmail.com in .env"
        )
    typed = input("Enter your personal Gmail address (to share the sheet): ").strip()
    if not EMAIL_RE.match(typed):
        raise SetupError(f"'{typed}' is not a valid email address.")
    return typed


def upsert_env(updates: Dict[str, str]) -> None:
    """Write key=value pairs into .env, preserving comments and order."""
    lines: List[str] = []
    if ENV_PATH.is_file():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    seen = set()
    out: List[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(line)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    out_text = "\n".join(out).rstrip("\n") + "\n"
    ENV_PATH.write_text(out_text, encoding="utf-8")
    logger.info("Updated %s with: %s", ENV_PATH.name, ", ".join(updates))


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create the MUNNA MOBILE & Grocery Google Sheet."
    )
    parser.add_argument(
        "--email",
        default=None,
        help="Your personal Gmail (else USER_EMAIL from .env, else prompt).",
    )
    parser.add_argument(
        "--credentials",
        default=None,
        help="Path to the service-account JSON (default: ./credentials.json).",
    )
    parser.add_argument(
        "--title",
        default=SPREADSHEET_TITLE,
        help=f"Spreadsheet title (default: {SPREADSHEET_TITLE!r}).",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="Use an existing sheet (URL or ID) instead of creating one.",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args(argv)
    try:
        creds_path = resolve_credentials(args.credentials)
        logger.info("Using credentials: %s", creds_path.name)
        client = get_client(creds_path)

        source = (args.url or "").strip()
        if source:
            try:
                if source.startswith("http://") or source.startswith("https://"):
                    spreadsheet = client.open_by_url(source)
                else:
                    spreadsheet = client.open_by_key(source)
            except Exception as exc:  # noqa: BLE001
                raise SetupError(
                    f"Could not open the spreadsheet: {exc}\n"
                    "Check the URL/ID and make sure the sheet is shared with\n"
                    "the service-account email (client_email in credentials.json)"
                ) from exc
            created = False
            logger.info("Re-using the sheet given via --url.")
        else:
            spreadsheet, created = ensure_spreadsheet(client, args.title)
        ensure_headers(spreadsheet)

        email = resolve_email(args.email)
        spreadsheet_id = str(spreadsheet.id)
        if already_has_access(client, spreadsheet_id, email):
            logger.info("%s already has access - sharing skipped.", email)
        else:
            share_with_user(spreadsheet, email)

        spreadsheet_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}"
        updates = {
            "GOOGLE_SHEETS_CREDENTIALS_FILE": creds_path.name
            if creds_path.parent == BASE_DIR
            else str(creds_path),
            "GOOGLE_SHEET_ID": spreadsheet_id,
            "GOOGLE_SHEET_NAME": args.title,
            "GOOGLE_SHEET_WORKSHEET": spreadsheet.sheet1.title,
            "SPREADSHEET_URL": spreadsheet_url,
            "USER_EMAIL": email,
        }
        upsert_env(updates)

        print()
        print("Setup complete!")
        print(f"  {'Created' if created else 'Re-used'} : {args.title}")
        print(f"  Spreadsheet ID  : {spreadsheet_id}")
        print(f"  URL             : {spreadsheet_url}")
        print(f"  Shared with     : {email} (writer)")
        print(f"  Headers         : {', '.join(HEADERS)}")
        print(f"  Saved to        : {ENV_PATH.name}")
        return 0
    except SetupError as exc:
        print(f"\nSETUP FAILED\n{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())


