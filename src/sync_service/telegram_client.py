from __future__ import annotations

import time
from typing import Any

import httpx

from .change_log import record

MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.5


class TelegramClient:
    """Minimal Telegram Bot API client — just enough to push a document to a chat."""

    def __init__(self, *, bot_token: str, proxy: str | None = None) -> None:
        """`proxy` routes requests through a SOCKS5/HTTP proxy (e.g.
        "socks5://telegram-proxy:1080") — api.telegram.org is unreachable
        directly from the VPS (blocked at the network level for
        Russian-hosted servers), so production always sets one. That proxy
        (a VLESS tunnel) occasionally resets mid-handshake — transient, so
        retried the same way JsonClient retries MoySklad/Novicloud."""
        self._bot_token = bot_token
        self._client = httpx.Client(base_url=f"https://api.telegram.org/bot{bot_token}", timeout=30.0, proxy=proxy or None)

    def send_document(self, *, chat_id: str, document: bytes, filename: str, caption: str = "", parse_mode: str | None = None) -> dict[str, Any]:
        data = {"chat_id": chat_id, "caption": caption}
        if parse_mode:
            data["parse_mode"] = parse_mode
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._client.post(
                    "/sendDocument",
                    data=data,
                    files={"document": (filename, document, "application/pdf")},
                )
            except httpx.TransportError:
                if attempt <= MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                    continue
                raise
            break
        if response.is_error:
            raise RuntimeError(f"Telegram sendDocument failed with HTTP {response.status_code}: {response.text[:500]}")
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram sendDocument returned ok=false: {payload}")
        record(service="telegram", entity_type="document", entity_id=chat_id, action="send", after={"filename": filename, "caption": caption})
        return payload

    def send_message(self, *, chat_id: str, text: str, reply_to_message_id: int | None = None, parse_mode: str | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode:
            data["parse_mode"] = parse_mode
        if reply_to_message_id:
            data["reply_to_message_id"] = reply_to_message_id
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._client.post("/sendMessage", data=data)
            except httpx.TransportError:
                if attempt <= MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                    continue
                raise
            break
        if response.is_error:
            raise RuntimeError(f"Telegram sendMessage failed with HTTP {response.status_code}: {response.text[:500]}")
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram sendMessage returned ok=false: {payload}")
        record(service="telegram", entity_type="message", entity_id=chat_id, action="send", after={"text": text})
        return payload

    def download_file(self, file_id: str) -> bytes:
        """Fetches a file a user sent (e.g. a receipt/label photo) — two calls,
        per the Bot API: getFile resolves file_id to a temporary file_path,
        then the file itself is downloaded from a different URL prefix
        (/file/bot<token>/... rather than /bot<token>/...)."""
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._client.get("/getFile", params={"file_id": file_id})
            except httpx.TransportError:
                if attempt <= MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                    continue
                raise
            break
        if response.is_error:
            raise RuntimeError(f"Telegram getFile failed with HTTP {response.status_code}: {response.text[:500]}")
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram getFile returned ok=false: {payload}")
        file_path = payload["result"]["file_path"]
        download = self._client.get(f"https://api.telegram.org/file/bot{self._bot_token}/{file_path}")
        if download.is_error:
            raise RuntimeError(f"Telegram file download failed with HTTP {download.status_code}")
        return download.content

    def set_webhook(self, *, url: str, secret_token: str) -> dict[str, Any]:
        """One-time setup call — registers where Telegram delivers updates
        (incoming chat messages) for this bot. `secret_token` is echoed back
        by Telegram on every delivery as the X-Telegram-Bot-Api-Secret-Token
        header, which is what the inbound webhook endpoint checks instead of
        an IP allowlist (Telegram doesn't publish stable source ranges)."""
        response = self._client.post("/setWebhook", data={"url": url, "secret_token": secret_token, "allowed_updates": '["message"]'})
        if response.is_error:
            raise RuntimeError(f"Telegram setWebhook failed with HTTP {response.status_code}: {response.text[:500]}")
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram setWebhook returned ok=false: {payload}")
        return payload

    def close(self) -> None:
        self._client.close()
