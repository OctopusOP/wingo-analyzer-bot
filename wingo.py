"""
wingo.py
========
Reads WinGo results from the public API and spots Big/Small streaks.

  Number 0-4 = Small, 5-9 = Big.
  A "streak" = how many of the newest results in a row have the same size.

The Watcher thread asks the API every few seconds. When a NEW result comes
out and the streak length is one of your alert numbers (e.g. 3 or 4), it
calls on_alert(info) exactly once for that result.
"""

import json
import threading
import time
import urllib.request
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from telegram_api import SSL_CONTEXT  # same HTTPS certificates as Telegram calls

BASE_URL = "https://draw.ar-lottery06.com/WinGo"
GAMES = ["WinGo_30S", "WinGo_1M", "WinGo_3M", "WinGo_5M"]
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "application/json"}


def size_of(number: int) -> str:
    return "Big" if number >= 5 else "Small"


def _get_json(url: str) -> Dict[str, Any]:
    # ?ts=... stops caches from giving us an old answer
    req = urllib.request.Request(f"{url}?ts={int(time.time() * 1000)}", headers=HEADERS)
    with urllib.request.urlopen(req, timeout=8, context=SSL_CONTEXT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_history(game: str) -> List[Dict[str, Any]]:
    """Last 10 results, NEWEST FIRST: [{"issue": "2026...", "number": 7, "size": "Big", "color": "green"}, ...]"""
    data = _get_json(f"{BASE_URL}/{game}/GetHistoryIssuePage.json")
    results = []
    for item in (data.get("data") or {}).get("list") or []:
        try:
            number = int(item.get("number"))
        except (TypeError, ValueError):
            continue
        results.append({"issue": str(item.get("issueNumber", "")), "number": number,
                        "size": size_of(number), "color": str(item.get("color", ""))})
    return results


def fetch_next_issue(game: str) -> str:
    """The period number that is running now (the one people bet on next)."""
    try:
        return str((_get_json(f"{BASE_URL}/{game}.json").get("current") or {}).get("issueNumber", ""))
    except Exception:
        return ""


def current_streak(history: List[Dict[str, Any]]):
    """(size, count) of the newest results in a row, e.g. ("Big", 3)."""
    if not history:
        return "", 0
    size = history[0]["size"]
    count = 0
    for r in history:
        if r["size"] != size:
            break
        count += 1
    return size, count


def build_info(game: str, history: List[Dict[str, Any]], size: str, count: int,
               next_issue: str = "") -> Dict[str, str]:
    """All values the {placeholders} in your messages can use."""
    streak = history[:count]
    if not next_issue and history:
        # Best guess when the API didn't answer: last period + 1
        try:
            next_issue = str(int(history[0]["issue"]) + 1)
        except ValueError:
            next_issue = ""
    return {
        "game": game.replace("_", " "),                       # WinGo 1M
        "size": size,                                         # Big
        "SIZE": size.upper(),                                 # BIG
        "count": str(count),                                  # 3
        "icon": "🔥" if size == "Big" else "💧",
        "results": " ".join(str(r["number"]) for r in streak),  # 7 9 8 (newest first)
        "issue": history[0]["issue"] if history else "",      # period that made the streak
        "next_issue": next_issue,                             # period running now
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _issue_key(issue: str):
    """Period numbers are digit strings; compare them as numbers (falls back to text)."""
    return (0, int(issue), "") if issue.isdigit() else (1, 0, issue)


class Watcher(threading.Thread):
    """
    Background thread that polls the API.
      on_update(history, size, count)  every successful poll (for the Home tab)
      on_alert(info)                   when a streak reaches an alert length
      on_log(text)                     status / errors
    Settings are read through get_settings() on every poll, so changes
    saved in the window apply without restarting.
    """

    def __init__(self, get_settings: Callable[[], Dict[str, Any]], on_alert: Callable[[Dict[str, str]], None],
                 on_update: Optional[Callable] = None, on_log: Callable[[str], None] = print):
        super().__init__(daemon=True, name="WinGoWatcher")
        self.get_settings = get_settings
        self.on_alert = on_alert
        self.on_update = on_update
        self.on_log = on_log
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()

    def run(self):
        from storage import parse_alert_at
        last_issue: Optional[str] = None   # newest period we have already looked at
        last_game: Optional[str] = None
        failures = 0
        while not self.stop_event.is_set():
            # FIX: a bad poll_seconds value (e.g. "2.5" or "") used to raise here and kill the thread
            settings = self.get_settings()
            game = settings.get("game", "WinGo_1M")
            try:
                wait = max(2, int(float(settings.get("poll_seconds", 3) or 3)))
            except (TypeError, ValueError):
                wait = 3
            if game != last_game:            # game changed -> start fresh
                last_issue, last_game = None, game
                self.on_log(f"Watching {game}…")
            try:
                history = fetch_history(game)
                if failures:
                    self.on_log("WinGo API is reachable again.")
                failures = 0
            except Exception as e:
                failures += 1
                if failures in (1, 5) or failures % 20 == 0:   # don't flood the log
                    self.on_log(f"WinGo API error ({failures}x): {e}")
                self.stop_event.wait(wait)
                continue

            if history:
                size, count = current_streak(history)
                newest = history[0]["issue"]
                if last_issue is not None and _issue_key(newest) < _issue_key(last_issue):
                    # FIX: a cached/stale API reply can be OLDER than what we already saw.
                    # It used to count as "new", so the next real reply alerted a 2nd time.
                    self.stop_event.wait(wait)
                    continue
                previous, last_issue = last_issue, newest   # remember BEFORE callbacks run
                # FIX: errors in on_update/on_alert used to kill the thread (watching silently stopped)
                try:
                    if self.on_update:
                        self.on_update(history, size, count)
                    if previous is None:
                        # First look after starting: don't alert for an old result,
                        # only for results that come out from now on.
                        self.on_log(f"Latest period {newest[-5:]}: {history[0]['number']} {size} "
                                    f"(streak {count}). Waiting for new results…")
                    elif newest != previous:
                        self.on_log(f"New result #{newest[-5:]}: {history[0]['number']} {size} → {count}x {size}")
                        if count in parse_alert_at(settings.get("alert_at", "3,4")):
                            info = build_info(game, history, size, count, fetch_next_issue(game))
                            self.on_alert(info)
                except Exception as e:
                    self.on_log(f"Watcher error: {e}")
            self.stop_event.wait(wait)
