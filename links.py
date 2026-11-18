"""
links.py
========
Reads Telegram message links that you paste (one or many) and turns them
into (chat, message number) pairs the bot can copy/forward.

How to get a link: in Telegram, right-click (desktop) or long-press (phone)
a message -> "Copy Message Link".

Supported formats:
  https://t.me/c/1234567890/55        private channel, message 55
  https://t.me/mychannel/55           public channel,  message 55
  https://t.me/c/1234567890/7/55      topic in a group (last number = message)
  https://t.me/c/1234567890/10-20     RANGE: messages 10 to 20 (this app's shortcut)
"""

import re
from typing import List, Tuple

# Biggest range we accept in one line, to avoid accidental huge numbers
MAX_RANGE = 1000

_PRIVATE = re.compile(r"(?:https?://)?(?:www\.)?(?:t|telegram)\.me/c/(\d+)/(?:\d+/)?(\d+)(?:-(\d+))?")
_PUBLIC = re.compile(r"(?:https?://)?(?:www\.)?(?:t|telegram)\.me/([A-Za-z][A-Za-z0-9_]{3,})/(?:\d+/)?(\d+)(?:-(\d+))?")


def parse_link(line: str) -> List[Tuple[str, int]]:
    """
    One link -> list of (chat_id, message_id).
    Usually one item; a range like /10-20 gives several.
    Raises ValueError if the line isn't a message link.
    """
    text = line.strip()
    m = _PRIVATE.search(text)
    if m:
        chat = f"-100{m.group(1)}"   # private channel IDs start with -100
    else:
        m = _PUBLIC.search(text)
        if not m or m.group(1).lower() == "c":
            raise ValueError(f"Not a message link: {text[:60]}")
        chat = f"@{m.group(1)}"
    start = int(m.group(2))
    end = int(m.group(3)) if m.group(3) else start
    if end < start:
        start, end = end, start
    if end - start + 1 > MAX_RANGE:
        raise ValueError(f"Range too big (max {MAX_RANGE} messages): {text[:60]}")
    return [(chat, mid) for mid in range(start, end + 1)]


def parse_many(text: str) -> Tuple[List[Tuple[str, int]], List[str]]:
    """
    Many lines -> (all (chat, message_id) pairs in pasted order, list of errors).
    Empty lines are skipped. Links can also be separated by spaces.
    """
    pairs: List[Tuple[str, int]] = []
    errors: List[str] = []
    for token in text.split():
        try:
            pairs.extend(parse_link(token))
        except ValueError as e:
            errors.append(str(e))
    return pairs, errors


def group_into_batches(pairs: List[Tuple[str, int]], batch_size: int = 100) -> List[Tuple[str, List[int]]]:
    """
    Groups messages so they can be sent with ONE API call per batch, while
    keeping EXACTLY the order you pasted them in.

    A message joins the current batch only if it is from the same channel,
    has a higher number than the batch's last message (Telegram requires
    increasing order inside one call), and the batch has < 100 messages.
    Otherwise a new batch starts.

    Example: [(A,10),(A,11),(A,5),(B,9),(A,7)]
          -> [(A,[10,11]), (A,[5]), (B,[9]), (A,[7])]
    """
    batches: List[Tuple[str, List[int]]] = []
    for chat, mid in pairs:
        last = batches[-1] if batches else None
        if last and last[0] == chat and mid == last[1][-1]:
            continue  # same message twice in a row: skip the duplicate
        if last and last[0] == chat and mid > last[1][-1] and len(last[1]) < batch_size:
            last[1].append(mid)
        else:
            batches.append((chat, [mid]))
    return batches
