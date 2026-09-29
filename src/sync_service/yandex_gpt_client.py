from __future__ import annotations

from .http import JsonClient

DEFAULT_MODEL = "yandexgpt/latest"
BASE_URL = "https://llm.api.cloud.yandex.net"


class YandexGPTClient:
    """Minimal client for Yandex AI Studio's OpenAI-compatible chat
    completions endpoint — one system prompt, one user message, one text
    reply. Matches AnthropicClient.complete()'s signature so either can be
    dropped into order_assistant.py/telegram_qa_worker.py unchanged; switched
    to this one because Anthropic's API doesn't serve Russian-hosted
    infrastructure at all (confirmed against their supported-regions policy
    2026-09-30), which this VPS is.

    Endpoint/auth confirmed 2026-09-30 against Yandex's own docs
    (aistudio.yandex.ru/docs/en/ai-studio/api-ref/authentication and search
    results for /v1/chat/completions): POST .../v1/chat/completions,
    "Authorization: Api-Key <key>", model field is the same gpt://<folder>/
    <model>/latest URI the legacy foundationModels API uses. NOT verified
    with a real key/folder yet, though — needs a live call before trusting
    it in production, per this project's own rule for every other
    integration here.

    No image support: this endpoint has no vision input, unlike Claude.
    `image_bytes` is accepted for signature compatibility but raises if
    actually given one — callers should check `supports_images` first (see
    order_assistant.read_order_number_from_image's callers in
    telegram_qa_worker.py) rather than hit this.
    """

    supports_images = False

    def __init__(self, *, api_key: str, folder_id: str, model: str = DEFAULT_MODEL, base_url: str = BASE_URL) -> None:
        self._client = JsonClient(
            base_url=base_url,
            headers={"Authorization": f"Api-Key {api_key}", "content-type": "application/json"},
        )
        self._folder_id = folder_id
        self._model = model

    def complete(self, *, system: str, user_message: str, max_tokens: int = 1024, image_bytes: bytes | None = None, image_media_type: str = "image/jpeg") -> str:
        if image_bytes is not None:
            raise NotImplementedError("YandexGPT chat completions has no image input — check supports_images before calling with an image")
        payload = {
            "model": f"gpt://{self._folder_id}/{self._model}",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_message},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }
        result = self._client.post("/v1/chat/completions", payload)
        choices = result.get("choices") or []
        if not choices:
            return ""
        text = ((choices[0].get("message")) or {}).get("content", "")
        return text.strip()

    def close(self) -> None:
        self._client.close()
