"""
storage.py
==========
Everything this app saves lives in JSON files next to the app:

  settings.json   bot token, channel, game, alert rules, your message boxes
  file_ids.json   Telegram's id for files already uploaded (so a file is
                  uploaded once, then re-used instantly on every alert)

A "block" is one thing in the send list (sent top to bottom, one by one):

  {"type": "box", "text": "Hi!", "file": "/path/pic.jpg", "link": "https://t.me/c/123/55"}
      -> a message box YOU write. Any part may be empty.
  {"type": "program"}
      -> the place where the program's alert message goes
         (its text is settings["program_template"])
"""

import copy
import json
import shutil
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

APP_DIR = Path(__file__).parent
SETTINGS_FILE = APP_DIR / "settings.json"
FILE_IDS_FILE = APP_DIR / "file_ids.json"

_lock = threading.Lock()  # stops two threads writing the same file at once


def empty_box() -> Dict[str, str]:
    """A blank message box."""
    return {"type": "box", "text": "", "file": "", "link": ""}


# The alert message the program writes. {placeholders} are filled in at send time.
DEFAULT_PROGRAM_TEMPLATE = (
    "🎯 <b>{game} Alert!</b> {icon}\n"
    "\n"
    "⚡ <b>{count}x {SIZE}</b> in a row\n"
    "📊 Last results: {results}\n"
    "🔜 Next period: <code>{next_issue}</code>\n"
    "🕒 {time}"
)

DEFAULT_SETTINGS: Dict[str, Any] = {
    "token": "",
    "channel": "",               # "@mychannel" or "-1001234567890"
    "game": "WinGo_1M",          # WinGo_30S / WinGo_1M / WinGo_3M / WinGo_5M
    "alert_at": "3,4",           # send when a Big/Small streak reaches these lengths
    "poll_seconds": 3,           # how often to ask the WinGo API for new results
    "delay_seconds": 1.0,        # pause between messages (keeps the order + avoids Telegram limits)
    "auto_start": False,         # start watching as soon as the app opens
    # Alert page, same layout as the sketch: boxes 1-3, the program message, boxes 4-5
    "alert_blocks": [empty_box(), empty_box(), empty_box(), {"type": "program"}, empty_box(), empty_box()],
    "program_template": DEFAULT_PROGRAM_TEMPLATE,
    # Broadcast page: your boxes only, sent when you press "Send now"
    "broadcast_blocks": [empty_box(), empty_box(), empty_box()],
}


_backed_up = set()  # broken files we already made a copy of


def _read(path: Path, default):
    """
    Reads a JSON file. Missing file -> `default`.
    A broken file also gives `default`, but a copy is saved first
    (e.g. settings.broken-20261118-140211.json) so nothing is lost.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, type(default)):
            return data
        problem = f"expected a JSON {type(default).__name__}"
    except FileNotFoundError:
        return default
    except Exception as e:
        problem = str(e)
    try:
        key = (str(path), path.stat().st_mtime_ns)
        if key not in _backed_up:
            backup = path.with_name(f"{path.stem}.broken-{datetime.now():%Y%m%d-%H%M%S}{path.suffix}")
            shutil.copy2(path, backup)
            _backed_up.add(key)
            print(f"WARNING: {path.name} could not be read ({problem}). Copy saved as {backup.name}.",
                  file=sys.stderr)
    except OSError:
        pass
    return default


def _write(path: Path, data) -> None:
    # Write a temp file, then rename: a crash never leaves half a file
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


# ---------------- settings ----------------

def load_settings() -> Dict[str, Any]:
    with _lock:
        saved = _read(SETTINGS_FILE, {})
    # FIX: deepcopy, otherwise the default block lists were shared and editing
    # the returned settings in place changed DEFAULT_SETTINGS itself.
    settings = {**copy.deepcopy(DEFAULT_SETTINGS), **saved}
    # The alert list must always contain exactly one program message
    raw = settings["alert_blocks"] if isinstance(settings["alert_blocks"], list) else []  # FIX: null/non-list crashed
    blocks = [b for b in raw if isinstance(b, dict)]
    if not any(b.get("type") == "program" for b in blocks):
        blocks.append({"type": "program"})
    settings["alert_blocks"] = blocks
    return settings


def save_settings(settings: Dict[str, Any]) -> None:
    with _lock:
        _write(SETTINGS_FILE, settings)


def parse_alert_at(text: str):
    """'3,4' -> {3, 4}. Ignores junk; numbers below 2 make no sense for a streak."""
    numbers = set()
    for part in str(text).replace(" ", ",").split(","):
        if part.strip().isdecimal() and int(part) >= 2:   # FIX: isdigit() accepted "²", then int() crashed
            numbers.add(int(part))
    return numbers


# ---------------- uploaded file ids ----------------

def get_file_id(key: str) -> str:
    with _lock:
        return _read(FILE_IDS_FILE, {}).get(key, "")


def remember_file_id(key: str, file_id: str) -> None:
    with _lock:
        ids = _read(FILE_IDS_FILE, {})
        ids[key] = file_id
        _write(FILE_IDS_FILE, ids)
