from __future__ import annotations

import json
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

    def delete_webhook(self) -> dict[str, Any]:
        """Clears any registered webhook — Telegram refuses long-polling
        (get_updates) while one is set. Safe/idempotent to call with no
        webhook registered."""
        response = self._client.post("/deleteWebhook", data={"drop_pending_updates": "false"})
        if response.is_error:
            raise RuntimeError(f"Telegram deleteWebhook failed with HTTP {response.status_code}: {response.text[:500]}")
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram deleteWebhook returned ok=false: {payload}")
        return payload

    def get_updates(self, *, offset: int | None = None, timeout: int = 25, allowed_updates: list[str] | None = None) -> list[dict[str, Any]]:
        """Long-polling read of new updates — this service's only way to
        receive inbound Telegram messages: Telegram's servers can't reach
        this Russian-hosted VPS on any inbound path (the mirror image of the
        already-documented outbound restriction — setWebhook delivery
        consistently failed with "Connection timed out" even though the
        service itself answered instantly to a direct external request;
        confirmed live 2026-09-29), so this pulls through the same working
        outbound proxy send_message/send_document already use instead of
        waiting for Telegram to push.

        `offset` should be the last seen update_id + 1 — Telegram keeps
        redelivering every update at or after `offset` until told otherwise,
        so the caller is expected to persist and advance it.
        """
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        if allowed_updates is not None:
            params["allowed_updates"] = json.dumps(allowed_updates)
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._client.get("/getUpdates", params=params, timeout=timeout + 10)
            except httpx.TransportError:
                if attempt <= MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF_SECONDS * attempt)
                    continue
                raise
            break
        if response.is_error:
            raise RuntimeError(f"Telegram getUpdates failed with HTTP {response.status_code}: {response.text[:500]}")
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram getUpdates returned ok=false: {payload}")
        return payload.get("result") or []

    def close(self) -> None:
        self._client.close()
