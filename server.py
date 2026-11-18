"""
server.py — WinGo Alert WITHOUT a window (for a Linux server / VPS / SSH)
========================================================================
Watches the WinGo results and sends your saved pages to the Telegram
channel, one message at a time:
  Start Page  when a Big/Small streak reaches "Start at" (e.g. 3)
  Alert Page  again for every next result of the same size (4, 5, 6…)
  End Page    once, when the streak breaks
Everything is logged to the terminal.

    ./start.sh --server        (recommended: sets everything up first)
    start.bat --server         (Windows)
    python3 server.py          (direct)

It uses the same settings.json as the window app. Easiest setup:
  1. Fill in ⚙ Settings and the 3 pages in the window app on a computer
     with a screen, and press 💾 Save.
  2. Copy settings.json (and any files used in your message boxes, at the
     same paths) into this folder on the server.

settings.json is read again on every poll, so edits apply without a restart.
Only run ONE copy per bot (not the window app and the server at once).
"""

import signal
import sys
import time
from datetime import datetime
from typing import Any, Dict

import storage
import wingo
from sender import get_sender


def log(text: str) -> None:
    """Prints one log line with the time in front, e.g. '2026-11-18 14:02:11  ▶ ...'."""
    try:
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {text}", flush=True)
    except (BrokenPipeError, OSError):
        pass  # terminal closed (e.g. SSH disconnected): keep running anyway


def get_settings() -> Dict[str, Any]:
    """Fresh settings from settings.json (called on every poll and every alert)."""
    return storage.load_settings()


def on_alert(info: Dict[str, str]) -> None:
    """Start / loop / end of a streak: queue that page (as last saved) for sending."""
    settings = get_settings()  # fresh copy, so the latest saved boxes are used
    page = info.get("stage", "start")
    blocks, snapshot = storage.page_job(settings, page)
    get_sender().add_job(f"{storage.PAGE_TITLES[page]} ({info['count']}x {info['SIZE']})", blocks, info, snapshot)


def main() -> None:
    # ---- 1. token + channel must be set (the token itself is never printed) ----
    settings = get_settings()
    if not str(settings.get("token", "")).strip() or not str(settings.get("channel", "")).strip():
        print("The bot token and/or channel are not set yet.\n"
              f"Settings file: {storage.SETTINGS_FILE}\n\n"
              "How to set them:\n"
              "  • Open the window app (./start.sh or start.bat) on a computer with a screen,\n"
              "    fill in ⚙ Settings + the pages, press 💾 Save, then copy settings.json here.\n"
              "  • Or edit settings.json yourself, for example:\n"
              '      {"token": "123456789:AA...your bot token...", "channel": "@mychannel"}\n')
        sys.exit(1)

    # ---- 2. start the sender (one queue: messages never mix) and the watcher ----
    get_sender(log)
    watcher = wingo.Watcher(get_settings, on_alert, on_log=log)

    # Ctrl+C or `kill` -> stop cleanly
    def stop(*_):
        log("Stopping…")
        watcher.stop()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    log(f"Server mode · channel {settings['channel']} · game {settings.get('game')} · "
        f"start at {storage.get_start_at(settings)} · Ctrl+C to stop")
    log("Alerts fire only for NEW results from now on.")
    watcher.start()

    # Keep the program alive while the watcher runs (short sleeps so Ctrl+C
    # is handled quickly on every OS, including Windows).
    while watcher.is_alive():
        time.sleep(0.5)
    log("Stopped.")


if __name__ == "__main__":
    main()
