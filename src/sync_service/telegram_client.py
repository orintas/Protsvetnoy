from __future__ import annotations

from typing import Any

import httpx


class TelegramClient:
    """Minimal Telegram Bot API client — just enough to push a document to a chat."""

    def __init__(self, *, bot_token: str, proxy: str | None = None) -> None:
        """`proxy` routes requests through a SOCKS5/HTTP proxy (e.g.
        "socks5://telegram-proxy:1080") — api.telegram.org is unreachable
        directly from the VPS (blocked at the network level for
        Russian-hosted servers), so production always sets one."""
        self._client = httpx.Client(base_url=f"https://api.telegram.org/bot{bot_token}", timeout=30.0, proxy=proxy or None)

    def send_document(self, *, chat_id: str, document: bytes, filename: str, caption: str = "") -> dict[str, Any]:
        response = self._client.post(
            "/sendDocument",
            data={"chat_id": chat_id, "caption": caption},
            files={"document": (filename, document, "application/pdf")},
        )
        if response.is_error:
            raise RuntimeError(f"Telegram sendDocument failed with HTTP {response.status_code}: {response.text[:500]}")
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram sendDocument returned ok=false: {payload}")
        return payload

    def close(self) -> None:
        self._client.close()
