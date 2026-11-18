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
         (its text is the page's template, e.g. settings["start_template"])

There are 3 alert pages, one per stage of a streak (see wingo.py):
  start  Start Page   sent when the streak reaches "Start at" (e.g. 3 Big)
  loop   Alert Page   sent again for every next result of the same size (4, 5, 6…)
  end    End Page     sent once when the streak breaks
Each page = its own message boxes + its own program message.
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


# The program message for each page. {placeholders} are filled in at send time.
DEFAULT_TEMPLATES: Dict[str, str] = {
    "start": (
        "🎯 <b>{game} Alert!</b> {icon}\n"
        "\n"
        "⚡ <b>{count}x {SIZE}</b> in a row\n"
        "📊 Last results: {results}\n"
        "🔜 Next period: <code>{next_issue}</code>\n"
        "🕒 {time}"
    ),
    "loop": (
        "🔁 <b>Still {SIZE}!</b> {icon}\n"
        "\n"
        "⚡ Streak is now <b>{count}x {SIZE}</b>\n"
        "📊 Last results: {results}\n"
        "🔜 Next period: <code>{next_issue}</code> → <b>{NEXT_SIZE}</b> {next_icon}\n"
        "🕒 {time}"
    ),
    "end": (
        "🛑 <b>Streak ended</b>\n"
        "\n"
        "It was <b>{count}x {SIZE}</b>, then <b>{break_number} {BREAK_SIZE}</b> came\n"
        "🔜 Next period: <code>{next_issue}</code>\n"
        "🕒 {time}"
    ),
}
DEFAULT_PROGRAM_TEMPLATE = DEFAULT_TEMPLATES["start"]

# The Alert Page default before the opposite size was added (used to upgrade saved settings)
_OLD_LOOP_TEMPLATE = DEFAULT_TEMPLATES["loop"].replace(" → <b>{NEXT_SIZE}</b> {next_icon}", "")   # kept for older code

PAGES = ["start", "loop", "end"]
PAGE_TITLES = {"start": "Start Page", "loop": "Alert Page", "end": "End Page"}


def default_blocks():
    """New page layout like the sketch: box 1, box 2, the program message, box 3, box 4."""
    return [empty_box(), empty_box(), {"type": "program"}, empty_box(), empty_box()]


DEFAULT_SETTINGS: Dict[str, Any] = {
    "token": "",
    "channel": "",               # "@mychannel" or "-1001234567890"
    "game": "WinGo_1M",          # WinGo_30S / WinGo_1M / WinGo_3M / WinGo_5M
    "start_at": 3,               # streak length that sends the Start Page
    "poll_seconds": 3,           # how often to ask the WinGo API for new results
    "delay_seconds": 1.0,        # pause between messages (keeps the order + avoids Telegram limits)
    "auto_start": False,         # start watching as soon as the app opens
    # The 3 pages: <page>_blocks = boxes + program message, <page>_template = program message text
    "start_blocks": default_blocks(), "start_template": DEFAULT_TEMPLATES["start"],
    "loop_blocks": default_blocks(), "loop_template": DEFAULT_TEMPLATES["loop"],
    "end_blocks": default_blocks(), "end_template": DEFAULT_TEMPLATES["end"],
    # Broadcast page: your boxes only, sent when you press "Send now"
    "broadcast_blocks": [empty_box(), empty_box(), empty_box()],
    # 🛠 Channel Tools tab (separate from the alerts; can use another bot)
    "tools_bots": [],            # [{"name": "Copy bot", "token": "123:ABC…"}]
    "tools_channels": [],        # [{"name": "Main", "id": "@mychannel" or "-100…"}]
    "tools_bot": "",             # name of the bot the tools use ("" = the alert bot)
    "tools_delay": 1.0,          # seconds between Telegram calls in the tools
    "tools_post_blocks": [],     # message boxes on "Post to many"
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
    # Alert Page saved with the OLD default text (before "→ {NEXT_SIZE}" was added):
    # switch it to the new default. Text you changed yourself is left alone.
    if settings.get("loop_template") == _OLD_LOOP_TEMPLATE:
        settings["loop_template"] = DEFAULT_TEMPLATES["loop"]
    # Old settings.json (one alert page): it becomes the Start Page
    if "start_blocks" not in saved and isinstance(saved.get("alert_blocks"), list):
        settings["start_blocks"] = saved["alert_blocks"]
        settings["start_template"] = saved.get("program_template") or DEFAULT_TEMPLATES["start"]
    if "start_at" not in saved and saved.get("alert_at"):
        settings["start_at"] = min(parse_alert_at(saved["alert_at"]) or {3})
    # Every page must contain exactly one program message
    for page in PAGES:
        key = f"{page}_blocks"
        raw = settings[key] if isinstance(settings[key], list) else []  # FIX: null/non-list crashed
        blocks = [b for b in raw if isinstance(b, dict)]
        if not any(b.get("type") == "program" for b in blocks):
            blocks.append({"type": "program"})
        settings[key] = blocks
    return settings


def get_start_at(settings: Dict[str, Any]) -> int:
    """'Start at' as a whole number of 2 or more (bad values -> 3)."""
    try:
        return max(2, int(float(settings.get("start_at", 3))))
    except (TypeError, ValueError):
        return 3


def page_job(settings: Dict[str, Any], page: str):
    """
    What to send for one page: (blocks, settings copy whose "program_template"
    is THIS page's program message — that's the key sender.py reads).
    """
    snapshot = dict(settings)
    snapshot["program_template"] = settings.get(f"{page}_template") or DEFAULT_TEMPLATES[page]
    return settings[f"{page}_blocks"], snapshot


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
