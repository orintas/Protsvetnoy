from __future__ import annotations

from .http import JsonClient

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5"


class AnthropicClient:
    """Minimal Claude Messages API client — one system prompt, one user
    message, one text reply. Both LLM features in this service (order Q&A
    for warehouse staff, marketplace review/question drafts) are single-turn
    "here's the context, write the reply" calls, not a back-and-forth
    conversation the model itself needs to track."""

    def __init__(self, *, api_key: str, model: str = DEFAULT_MODEL, base_url: str = "https://api.anthropic.com") -> None:
        self._client = JsonClient(
            base_url=base_url,
            headers={"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"},
        )
        self._model = model

    def complete(self, *, system: str, user_message: str, max_tokens: int = 1024) -> str:
        payload = {
            "model": self._model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user_message}],
        }
        result = self._client.post("/v1/messages", payload)
        blocks = result.get("content") or []
        text = "".join(block.get("text", "") for block in blocks if isinstance(block, dict) and block.get("type") == "text")
        return text.strip()

    def close(self) -> None:
        self._client.close()
