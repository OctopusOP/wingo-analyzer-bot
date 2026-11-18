"""
sender.py
=========
Sends a list of blocks (your message boxes + the program message) to the
channel ONE BY ONE, top to bottom, waiting for each to finish before the next.

One Sender thread handles all jobs in a queue, so if a new alert fires while
the previous one is still sending, it waits its turn: messages never mix.

How one message box is sent:
  text + file  -> the file with the text as its caption (if text <= 1024 chars),
                  otherwise the text first, then the file
  text only    -> a normal message
  file only    -> just the file
  link         -> the linked message(s) copied in, WITHOUT "Forwarded from"
"""

import os
import queue
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import storage
from links import group_into_batches, parse_many
from telegram_api import TelegramBot, TelegramError, guess_kind

CAPTION_LIMIT = 1024  # Telegram's max caption length


def fill(text: str, info: Dict[str, str]) -> str:
    """Replaces {count}, {SIZE}, ... with real values. Unknown {words} stay as they are."""
    return re.sub(r"\{(\w+)\}", lambda m: info.get(m.group(1), m.group(0)), text or "")


def _file_key(bot_token: str, path: str) -> str:
    """Same file (same size + modified time) on the same bot -> re-use Telegram's file_id."""
    st = os.stat(path)
    return f"{bot_token.split(':')[0]}|{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}"


class Sender(threading.Thread):
    def __init__(self, on_log: Callable[[str], None] = print):
        super().__init__(daemon=True, name="Sender")
        self.jobs: "queue.Queue" = queue.Queue()
        self.on_log = on_log

    # ------------------------------------------------------------------
    def add_job(self, title: str, blocks: List[Dict[str, Any]], info: Dict[str, str],
                settings: Dict[str, Any]) -> None:
        """Queue a list of blocks to send. `settings` is a snapshot (token, channel, template, delay)."""
        self.jobs.put((title, blocks, info, settings))
        if self.jobs.qsize() > 1:
            self.on_log(f"⏳ {title}: queued (another send is still running)")

    def run(self):
        while True:
            title, blocks, info, settings = self.jobs.get()
            try:
                self._send_all(title, blocks, info, settings)
            except Exception as e:  # never let the thread die
                self.on_log(f"✖ {title}: stopped — {e}")

    # ------------------------------------------------------------------
    def _send_all(self, title, blocks, info, settings):
        token = settings.get("token", "").strip()
        chat = settings.get("channel", "").strip()
        if not token or not chat:
            self.on_log(f"✖ {title}: set the bot token and channel in ⚙ Settings first")
            return
        bot = TelegramBot(token)
        delay = max(0.0, float(settings.get("delay_seconds", 1.0) or 0))
        sent = failed = 0
        self.on_log(f"▶ {title}: sending…")

        for number, block in enumerate(blocks, start=1):
            # Turn the block into a list of small "send steps", then run them in order
            for step in self._steps(block, info, settings):
                try:
                    self._with_retry(lambda: step(bot, chat))
                    sent += 1
                except Exception as e:
                    failed += 1
                    self.on_log(f"  ✖ block {number}: {e}")
                time.sleep(delay)  # keeps the order and stays under Telegram's limits

        mark = "✔" if not failed else "⚠"
        self.on_log(f"{mark} {title}: {sent} message(s) sent" + (f", {failed} failed" if failed else ""))

    def _steps(self, block, info, settings) -> List[Callable]:
        """What to send for one block, as a list of functions(bot, chat)."""
        steps: List[Callable] = []

        if block.get("type") == "program":
            template = settings.get("program_template")
            if template is None:                      # not set at all -> default text
                template = storage.DEFAULT_PROGRAM_TEMPLATE
            text = fill(template, info).strip()
            if not text:                              # you cleared it -> send no program message
                return steps
            steps.append(lambda bot, chat: self._text(bot, chat, text))
            return steps

        text = fill(block.get("text", ""), info).strip()
        path = (block.get("file") or "").strip()
        link = (block.get("link") or "").strip()

        if path and not os.path.isfile(path):
            self.on_log(f"  ⚠ file not found, skipped: {path}")
            path = ""

        if path:
            if text and len(text) <= CAPTION_LIMIT:
                steps.append(lambda bot, chat: self._file(bot, chat, path, text))       # file + caption
            else:
                if text:
                    steps.append(lambda bot, chat: self._text(bot, chat, text))         # long text first
                steps.append(lambda bot, chat: self._file(bot, chat, path, ""))
        elif text:
            steps.append(lambda bot, chat: self._text(bot, chat, text))

        if link:
            pairs, errors = parse_many(link)
            for err in errors:
                self.on_log(f"  ⚠ {err}")
            # Messages from the same channel go in one call (keeps their order)
            for from_chat, ids in group_into_batches(pairs):
                steps.append(lambda bot, chat, f=from_chat, i=ids: bot.copy_messages(chat, f, i))
        return steps

    # ------------------------------------------------------------------
    @staticmethod
    def _text(bot: TelegramBot, chat, text: str):
        try:
            bot.send_message(chat, text, parse_mode="HTML")
        except TelegramError as e:
            # A stray "<" in your text breaks HTML mode: send it as plain text instead
            if "parse entities" in str(e).lower():
                bot.send_message(chat, text, parse_mode=None)
            else:
                raise

    @staticmethod
    def _file(bot: TelegramBot, chat, path: str, caption: str):
        kind = guess_kind(path)
        key = _file_key(bot.token, path)
        file_id = storage.get_file_id(key)

        def send(**src):
            # FIX: like _text, a stray "<" in a caption broke HTML mode and the
            # whole file failed. Retry the same send with a plain-text caption.
            try:
                return bot.send_file(chat, kind, caption=caption, **src)
            except TelegramError as e:
                if caption and "parse entities" in str(e).lower():
                    return bot.send_file(chat, kind, caption=caption, parse_mode=None, **src)
                raise

        if file_id:
            try:
                return send(file_id=file_id)
            except TelegramError as e:
                if e.code == 429:
                    raise  # FIX: "slow down" is not a bad file_id; let _with_retry wait instead of re-uploading
                # old id not valid any more -> upload again below
        msg = send(path=path)
        new_id = TelegramBot.file_id_of(msg)
        if new_id:
            storage.remember_file_id(key, new_id)
        return msg

    def _with_retry(self, action: Callable, tries: int = 3):
        """Runs one send. 'Too many requests' (429) -> wait as Telegram asks, then retry."""
        for attempt in range(tries):
            try:
                return action()
            except TelegramError as e:
                if e.code == 429 and attempt < tries - 1:
                    wait = (e.retry_after or 3) + 1
                    self.on_log(f"  … Telegram says slow down, waiting {wait}s")
                    time.sleep(wait)
                    continue
                if e.code is None and attempt < tries - 1:   # network hiccup
                    time.sleep(2)
                    continue
                raise


# Example values used by the "Send test" buttons
def sample_info(game: str, stage: str = "start") -> Dict[str, str]:
    """Pretend values for a test send: start = 3x Big, loop = 4x Big, end = 4x Big then a 2 (Small)."""
    from wingo import build_info
    numbers = {"start": (7, 9, 8), "loop": (6, 7, 9, 8), "end": (2, 6, 7, 9, 8)}[stage]
    history = [{"issue": str(20261118100010644 - i), "number": n, "size": "Big" if n >= 5 else "Small",
                "color": ""} for i, n in enumerate(numbers)]
    if stage == "end":
        return build_info(game, history, "Big", 4, "20261118100010645", stage, history[0])
    return build_info(game, history, "Big", len(numbers), "20261118100010645", stage)


_sender: Optional[Sender] = None


def get_sender(on_log: Callable[[str], None] = print) -> Sender:
    """The one shared Sender thread (started on first use)."""
    global _sender
    if _sender is None:
        _sender = Sender(on_log)
        _sender.start()
    return _sender
