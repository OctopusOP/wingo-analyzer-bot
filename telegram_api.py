"""
telegram_api.py
===============
A tiny Telegram Bot API client using only Python's standard library
(no `pip install` needed).

Every Telegram feature this app uses is one method here:
  get_me / get_chat              -> check the token and the channel
  get_updates                    -> receive join requests (long polling)
  approve_join_request           -> accept a user into the channel
  send_message / send_file       -> greeting + broadcast
  copy_message(s)                -> re-send messages WITHOUT "Forwarded from" tag
  forward_messages               -> re-send messages WITH the "Forwarded from" tag
"""

import http.client
import json
import mimetypes
import os
import socket
import ssl
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional

# HTTPS certificates: use the "certifi" package (from requirements.txt) when
# it's installed, otherwise Python's default certificate store.
try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()


class TelegramError(Exception):
    """Raised when Telegram answers with ok=false (or the network fails)."""

    def __init__(self, message: str, code: Optional[int] = None, retry_after: int = 0):
        super().__init__(message)
        self.code = code                # e.g. 403 = user blocked the bot, 429 = too fast
        self.retry_after = retry_after  # seconds to wait when code == 429


class TelegramBot:
    def __init__(self, token: str):
        self.token = token.strip()

    # ------------------------------------------------------------------
    # Low-level request
    # ------------------------------------------------------------------
    def call(self, method: str, params: Optional[Dict[str, Any]] = None,
             file_field: Optional[str] = None, file_path: Optional[str] = None,
             timeout: int = 30) -> Any:
        """
        Calls one Bot API method and returns its "result".
        - Normal calls send JSON.
        - When file_path is given, the file is uploaded as multipart/form-data.
        """
        if not self.token:
            raise TelegramError("Bot token is empty. Add it in the ⚙ Settings tab.")
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        params = params or {}

        if file_path:
            body, content_type = self._multipart(params, file_field or "document", file_path)
        else:
            body, content_type = json.dumps(params).encode("utf-8"), "application/json"

        req = urllib.request.Request(url, data=body, headers={"Content-Type": content_type}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # Telegram sends error details as JSON even on HTTP errors
            try:
                data = json.loads(e.read().decode("utf-8"))
            except Exception:
                raise TelegramError(f"HTTP {e.code}: {e.reason}", code=e.code) from e
        except urllib.error.URLError as e:
            raise TelegramError(f"Network error: {e.reason}") from e
        except (socket.timeout, TimeoutError) as e:  # socket.timeout != TimeoutError on Python < 3.10
            raise TelegramError("Network timeout") from e
        except (OSError, http.client.HTTPException) as e:
            # Errors while READING the reply (connection reset, SSL error,
            # truncated response) are not wrapped in URLError by urllib.
            raise TelegramError(f"Network error: {e!r}") from e
        except ValueError as e:  # reply was not JSON (e.g. a proxy error page)
            raise TelegramError(f"Bad reply from Telegram: {e}") from e

        if not isinstance(data, dict):
            raise TelegramError("Bad reply from Telegram")
        if not data.get("ok"):
            params_info = data.get("parameters") or {}
            raise TelegramError(
                data.get("description", "Unknown Telegram error"),
                code=data.get("error_code"),
                retry_after=int(params_info.get("retry_after", 0)),
            )
        return data.get("result")

    @staticmethod
    def _multipart(fields: Dict[str, Any], file_field: str, file_path: str):
        """Builds a multipart/form-data body with normal fields + one file."""
        if not os.path.isfile(file_path):
            raise TelegramError(f"File not found: {file_path}")
        try:
            with open(file_path, "rb") as f:
                file_bytes = f.read()
        except OSError as e:
            raise TelegramError(f"Can't read file {file_path}: {e}") from e
        boundary = uuid.uuid4().hex
        parts: List[bytes] = []
        for key, value in fields.items():
            if value is None:
                continue
            if isinstance(value, bool):
                value = "true" if value else "false"
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        filename = os.path.basename(file_path)
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n".encode()
        )
        parts.append(file_bytes)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        return b"".join(parts), f"multipart/form-data; boundary={boundary}"

    # ------------------------------------------------------------------
    # Bot + chat info
    # ------------------------------------------------------------------
    def get_me(self) -> Dict[str, Any]:
        return self.call("getMe")

    def get_chat(self, chat_id: Any) -> Dict[str, Any]:
        return self.call("getChat", {"chat_id": chat_id})

    def get_webhook_url(self) -> str:
        return (self.call("getWebhookInfo") or {}).get("url", "")

    # ------------------------------------------------------------------
    # Join requests
    # ------------------------------------------------------------------
    def get_updates(self, offset: Optional[int], timeout: int = 20) -> List[Dict[str, Any]]:
        """Waits up to `timeout` seconds for new join requests."""
        params: Dict[str, Any] = {"timeout": timeout, "allowed_updates": ["chat_join_request"]}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", params, timeout=timeout + 15)

    def approve_join_request(self, chat_id: Any, user_id: int) -> bool:
        return bool(self.call("approveChatJoinRequest", {"chat_id": chat_id, "user_id": user_id}))

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------
    def send_message(self, chat_id: Any, text: str, parse_mode: Optional[str] = "HTML",
                     disable_preview: bool = False) -> Dict[str, Any]:
        params: Dict[str, Any] = {"chat_id": chat_id, "text": text,
                                  "link_preview_options": {"is_disabled": disable_preview}}
        if parse_mode:
            params["parse_mode"] = parse_mode
        return self.call("sendMessage", params)

    # file kind -> (Telegram method, form field)
    FILE_METHODS = {
        "photo": ("sendPhoto", "photo"),
        "video": ("sendVideo", "video"),
        "audio": ("sendAudio", "audio"),
        "animation": ("sendAnimation", "animation"),
        "document": ("sendDocument", "document"),
    }

    def send_file(self, chat_id: Any, kind: str, path: Optional[str] = None,
                  file_id: Optional[str] = None, caption: str = "",
                  parse_mode: Optional[str] = "HTML") -> Dict[str, Any]:
        """
        Sends a local file (path) or an already-uploaded file (file_id).
        Re-using file_id avoids uploading the same bytes for every user.
        """
        method, field = self.FILE_METHODS.get(kind, self.FILE_METHODS["document"])
        params: Dict[str, Any] = {"chat_id": chat_id}
        if caption:
            params["caption"] = caption
            if parse_mode:
                params["parse_mode"] = parse_mode
        if file_id:
            params[field] = file_id
            return self.call(method, params, timeout=60)
        return self.call(method, params, file_field=field, file_path=path, timeout=300)

    def copy_message(self, chat_id: Any, from_chat_id: Any, message_id: int) -> Dict[str, Any]:
        """Copies ONE message, no 'Forwarded from' header."""
        return self.call("copyMessage", {"chat_id": chat_id, "from_chat_id": from_chat_id,
                                         "message_id": message_id})

    def copy_messages(self, chat_id: Any, from_chat_id: Any, message_ids: List[int]) -> List[Dict[str, Any]]:
        """
        Copies up to 100 messages from one chat in a single call, no
        'Forwarded from' header. Albums (grouped photos) stay grouped.
        message_ids must be in increasing order.
        """
        return self.call("copyMessages", {"chat_id": chat_id, "from_chat_id": from_chat_id,
                                          "message_ids": message_ids}, timeout=120)

    def forward_messages(self, chat_id: Any, from_chat_id: Any, message_ids: List[int]) -> List[Dict[str, Any]]:
        """Same as copy_messages but KEEPS the 'Forwarded from' header."""
        return self.call("forwardMessages", {"chat_id": chat_id, "from_chat_id": from_chat_id,
                                             "message_ids": message_ids}, timeout=120)

    @staticmethod
    def file_id_of(message: Dict[str, Any]) -> Optional[str]:
        """Returns the reusable file_id from a sent message (largest photo size)."""
        if message.get("photo"):
            return message["photo"][-1]["file_id"]
        for key in ("video", "audio", "animation", "document"):
            if message.get(key):
                return message[key]["file_id"]
        return None


def guess_kind(path: str) -> str:
    """Decides how to send a file by its extension."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jpg", ".jpeg", ".png", ".webp"):
        return "photo"
    if ext == ".gif":
        return "animation"
    if ext in (".mp4", ".mov", ".m4v"):
        return "video"
    if ext in (".mp3", ".m4a", ".ogg", ".wav", ".flac"):
        return "audio"
    return "document"
