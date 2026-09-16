from __future__ import annotations

import time
from typing import Any

import httpx

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
        self._client = httpx.Client(base_url=f"https://api.telegram.org/bot{bot_token}", timeout=30.0, proxy=proxy or None)

    def send_document(self, *, chat_id: str, document: bytes, filename: str, caption: str = "") -> dict[str, Any]:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._client.post(
                    "/sendDocument",
                    data={"chat_id": chat_id, "caption": caption},
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
        return payload

    def close(self) -> None:
        self._client.close()
