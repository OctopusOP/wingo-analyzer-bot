"""
tools.py
========
The work behind the 🛠 Channel Tools tab. It has nothing to do with WinGo
alerts and can use a DIFFERENT bot (see "tools_bots" in settings.json).

  CopyJob     copy (or forward) a range of messages from one channel into
              one or more other channels, in the same order
  DeleteJob   delete a range of messages from a channel
  PostJob     send message boxes (text / file / link) to many channels
  find_last_id      learn the newest message number of a channel
  channel_report    channel name, members, and what the bot is allowed to do

Every job is a background thread with a Stop button (stop()) and reports to
the window through three callbacks:
  on_log(text)               one line for the log
  on_progress(done, total)   for the progress bar
  on_done()                  finished (or stopped)

Telegram rules that matter here:
  * The bot must be an ADMIN in every channel it reads from / writes to.
  * Copy reads the source channel: the bot must be admin there too.
  * Up to 100 messages per copy / forward / delete call; missing numbers
    (already deleted messages) are skipped by Telegram, no error.
  * Telegram may refuse to delete messages older than 48 hours.
"""

import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from links import parse_link
from telegram_api import TelegramBot, TelegramError

BATCH = 100          # Telegram's max messages per copy/forward/delete call
MAX_RANGE = 100_000  # safety limit for one job


# =============================================================================
# Small helpers
# =============================================================================
def message_number(value: str) -> Tuple[Optional[str], int]:
    """
    '55' -> (None, 55)
    'https://t.me/c/1234567890/55' -> ('-1001234567890', 55)
    'https://t.me/mychannel/55'    -> ('@mychannel', 55)
    Raises ValueError for anything else.
    """
    value = (value or "").strip()
    if value.isdecimal():
        return None, int(value)
    pairs = parse_link(value)  # ValueError if it isn't a message link
    return pairs[0]


def parse_range(start: str, end: str) -> Tuple[Optional[str], List[int]]:
    """
    Two boxes (number or message link each) -> (chat from the links or None, [ids…]).
    'end' may be empty = just one message. The order doesn't matter (10..5 = 5..10).
    """
    chat1, a = message_number(start)
    chat2, b = message_number(end) if (end or "").strip() else (chat1, a)
    if a < 1 or b < 1:
        raise ValueError("Message numbers start at 1")
    if a > b:
        a, b = b, a
    if b - a + 1 > MAX_RANGE:
        raise ValueError(f"Range too big: {b - a + 1} messages (max {MAX_RANGE})")
    return chat1 or chat2, list(range(a, b + 1))


def batches(ids: List[int], size: int = BATCH) -> List[List[int]]:
    return [ids[i:i + size] for i in range(0, len(ids), size)]


def with_retry(action: Callable, on_log: Callable[[str], None], stop: threading.Event, tries: int = 5):
    """Runs one Telegram call; 'too many requests' (429) -> waits as told, then tries again."""
    for attempt in range(tries):
        try:
            return action()
        except TelegramError as e:
            last = attempt == tries - 1
            if e.code == 429 and not last:
                wait = (e.retry_after or 5) + 1
                on_log(f"  … Telegram says slow down, waiting {wait}s")
                if stop.wait(wait):          # Stop pressed while waiting
                    raise
                continue
            if e.code is None and not last:  # network hiccup
                if stop.wait(3):
                    raise
                continue
            raise


def find_last_id(bot: TelegramBot, chat: Any) -> int:
    """
    The Bot API can't ask "what is the newest message?", so: post a tiny
    message, read its number, delete it straight away. Newest real message
    = that number - 1. (Subscribers don't get a notification: it's silent.)
    """
    msg = bot.call("sendMessage", {"chat_id": chat, "text": "·", "disable_notification": True})
    mid = int(msg["message_id"])
    try:
        bot.delete_message(chat, mid)
    except TelegramError:
        pass
    return mid - 1


def channel_report(bot: TelegramBot, chat: Any) -> str:
    """A few lines about a channel and the bot's rights there."""
    info = bot.get_chat(chat)
    lines = [f"📢 {info.get('title', chat)}   id {info.get('id')}   ({info.get('type')})"]
    if info.get("username"):
        lines.append(f"   public link: https://t.me/{info['username']}")
    try:
        lines.append(f"   members: {bot.get_member_count(chat)}")
    except TelegramError:
        pass
    me = bot.get_me()
    member = bot.get_chat_member(chat, me["id"])
    status = member.get("status")
    if status in ("administrator", "creator"):
        rights = {"post": member.get("can_post_messages", status == "creator"),
                  "edit": member.get("can_edit_messages", status == "creator"),
                  "delete": member.get("can_delete_messages", status == "creator"),
                  "invite": member.get("can_invite_users", status == "creator")}
        text = "  ".join(("✔ " if ok else "✖ ") + name for name, ok in rights.items())
        lines.append(f"   bot @{me.get('username')} is admin: {text}")
    else:
        lines.append(f"   ✖ bot @{me.get('username')} is NOT an admin here (status: {status})")
    return "\n".join(lines)


# =============================================================================
# Jobs (background threads)
# =============================================================================
class Job(threading.Thread):
    """Shared parts: stop button, callbacks, delay between calls."""

    title = "Job"

    def __init__(self, token: str, delay: float, on_log: Callable[[str], None],
                 on_progress: Callable[[int, int], None], on_done: Callable[[], None]):
        super().__init__(daemon=True, name=self.title)
        self.bot = TelegramBot(token)
        self.delay = max(0.0, float(delay))
        self.on_log, self.on_progress, self.on_done = on_log, on_progress, on_done
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()

    def run(self):
        try:
            self.work()
        except Exception as e:   # never crash silently
            self.on_log(f"✖ {self.title}: {e}")
        finally:
            self.on_done()

    def pause(self) -> bool:
        """Waits `delay` seconds. Returns True if Stop was pressed."""
        return self.stop_event.wait(self.delay)

    def work(self):
        raise NotImplementedError


class CopyJob(Job):
    """
    Copies messages `ids` from `source` into every channel in `targets`.
    mode "copy"    = no "Forwarded from" tag (looks like the bot posted it)
    mode "forward" = keeps the "Forwarded from" tag
    Albums stay grouped; the order stays the same.
    """

    title = "Copy"

    def __init__(self, token, source, ids: List[int], targets: List[str], mode="copy", delay=1.0, **cb):
        super().__init__(token, delay, **cb)
        self.source, self.ids, self.targets, self.mode = source, ids, targets, mode

    def work(self):
        parts = batches(self.ids)
        total = len(parts) * len(self.targets)
        done = copied = 0
        verb = "Forwarding" if self.mode == "forward" else "Copying"
        self.on_log(f"▶ {verb} {len(self.ids)} message number(s) from {self.source} "
                    f"to {len(self.targets)} channel(s)…")
        call = self.bot.forward_messages if self.mode == "forward" else self.bot.copy_messages
        for target in self.targets:
            sent_here = 0
            for part in parts:
                if self.stop_event.is_set():
                    self.on_log(f"■ Stopped. {copied} message(s) copied so far.")
                    return
                try:
                    result = with_retry(lambda: call(target, self.source, part), self.on_log, self.stop_event)
                    sent_here += len(result or [])
                except TelegramError as e:
                    self.on_log(f"  ✖ {target} · {part[0]}–{part[-1]}: {e}")
                    if any(w in str(e).lower() for w in ("not found", "rights", "admin", "forbidden")):
                        self.on_log("  (check that the bot is ADMIN in both channels)")
                done += 1
                self.on_progress(done, total)
                if self.pause():
                    break
            copied += sent_here
            self.on_log(f"  ✔ {target}: {sent_here} message(s)")
        skipped = len(self.ids) * len(self.targets) - copied
        self.on_log(f"✔ Copy finished: {copied} message(s)"
                    + (f" ({skipped} number(s) were empty/deleted and skipped)" if skipped > 0 else ""))


class DeleteJob(Job):
    """Deletes messages `ids` from `chat`, 100 per call."""

    title = "Delete"

    def __init__(self, token, chat, ids: List[int], delay=0.5, **cb):
        super().__init__(token, delay, **cb)
        self.chat, self.ids = chat, ids

    def work(self):
        parts = batches(self.ids)
        self.on_log(f"▶ Deleting messages {self.ids[0]}–{self.ids[-1]} in {self.chat}…")
        ok = 0
        for i, part in enumerate(parts, start=1):
            if self.stop_event.is_set():
                self.on_log("■ Stopped.")
                return
            try:
                with_retry(lambda: self.bot.delete_messages(self.chat, part), self.on_log, self.stop_event)
                ok += len(part)
            except TelegramError as e:
                self.on_log(f"  ✖ {part[0]}–{part[-1]}: {e}")
                if "can't be deleted" in str(e).lower() or "rights" in str(e).lower():
                    self.on_log("  (the bot needs the 'Delete messages' admin right; "
                                "Telegram may refuse messages older than 48 hours)")
            self.on_progress(i, len(parts))
            if self.pause():
                break
        self.on_log(f"✔ Delete finished: {ok} message number(s) handled "
                    "(numbers that were already empty count too).")


class PostJob(Job):
    """Sends message boxes (same boxes as the alert pages) to many channels, one by one."""

    title = "Post"

    def __init__(self, token, targets: List[str], blocks: List[Dict[str, Any]], delay=1.0, **cb):
        super().__init__(token, delay, **cb)
        self.targets, self.blocks = targets, blocks

    def work(self):
        from sender import Sender  # re-uses the exact same "send one box" rules
        sender = Sender(self.on_log)  # not started as a thread: we call it directly
        for i, target in enumerate(self.targets, start=1):
            if self.stop_event.is_set():
                self.on_log("■ Stopped.")
                return
            settings = {"token": self.bot.token, "channel": target, "delay_seconds": self.delay}
            sender._send_all(f"Post → {target}", self.blocks, {}, settings)
            self.on_progress(i, len(self.targets))
