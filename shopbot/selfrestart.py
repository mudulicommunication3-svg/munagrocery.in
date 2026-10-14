"""Self-restart: periodic auto-restart (default 5.5h) with a live countdown,
plus a manual restart used by the Admin Panel button.

Why: a long-running polling bot occasionally stalls; a periodic self-restart
keeps ``getUpdates`` fresh. The bot respawns ITSELF - a detached successor
process runs the same command line, then the current process exits. This works
whether the bot was started directly (``python main.py``) or by a supervisor.

Config (.env):
    AUTO_RESTART_SECONDS - auto-restart interval (default 5.5h = 19800s).
    RESTART_COUNTDOWN    - "1"/"0" to show/hide the live countdown message.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import time

logger = logging.getLogger(__name__)

AUTO_RESTART_SECONDS = float(
    os.getenv("AUTO_RESTART_SECONDS", str(int(5.5 * 3600)))
)
_COUNTDOWN_ENABLED = (
    os.getenv("RESTART_COUNTDOWN", "1").strip().lower() not in ("0", "false", "no")
)


def fmt_hms(seconds: float) -> str:
    """HH:MM:SS for a countdown (never negative)."""
    total = max(0, int(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _secret_dir() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".secret")


def spawn_successor_and_exit(code: int = 0) -> None:
    """Start a detached copy of this bot, then exit so it takes over."""
    secret = _secret_dir()
    try:
        os.makedirs(secret, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    log_path = os.path.join(secret, "bot.log")
    err = None
    try:
        err = open(log_path, "ab")
    except Exception:  # noqa: BLE001
        err = None
    try:
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(
            [sys.executable] + sys.argv,
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=err if err is not None else subprocess.DEVNULL,
            **kwargs,
        )
        logger.info("Spawned successor bot process; exiting current one.")
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not spawn successor: %s", exc)
    finally:
        if err is not None:
            try:
                err.close()
            except Exception:  # noqa: BLE001
                pass
    os._exit(code)


def _admin_chat_ids():
    raw = (os.getenv("ADMIN_ID") or os.getenv("ADMINS") or "").strip()
    ids = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids


async def auto_restart_watcher(app) -> None:
    """Edit one admin message with a live countdown, then respawn at zero."""
    if not _COUNTDOWN_ENABLED or AUTO_RESTART_SECONDS <= 0:
        logger.info("Auto-restart countdown disabled.")
        return
    deadline = time.time() + AUTO_RESTART_SECONDS
    msg = None
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        text = (
            "♻️ <b>Auto-restart scheduled</b>\n"
            f"Time left: <code>{fmt_hms(remaining)}</code>\n"
            "<i>The bot restarts by itself to stay healthy.</i>"
        )
        if msg is None:
            for cid in _admin_chat_ids():
                try:
                    msg = await app.bot.send_message(
                        chat_id=cid, text=text, parse_mode="HTML"
                    )
                    break
                except Exception:  # noqa: BLE001 - admin may not have /start-ed
                    continue
        else:
            try:
                await msg.edit_text(text, parse_mode="HTML")
            except Exception:  # noqa: BLE001 - "not modified" / stale
                pass
        step = 1 if remaining <= 60 else 60
        await asyncio.sleep(min(step, max(1, remaining)))
    try:
        if msg is not None:
            await msg.edit_text("♻️ <b>Restarting now…</b>", parse_mode="HTML")
    except Exception:  # noqa: BLE001
        pass
    await asyncio.sleep(1)
    spawn_successor_and_exit(0)
