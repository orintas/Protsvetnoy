from __future__ import annotations

import time
from typing import Any

import httpx

from .change_log import record

DEFAULT_URL = "https://store.tilda.cc/connectors/commerceml/"
# How many times mode=import may answer "progress" before giving up — Tilda
# processes the file in steps and expects the same request to be repeated.
MAX_IMPORT_STEPS = 200
IMPORT_STEP_PAUSE_SECONDS = 1.0


class TildaExchangeError(RuntimeError):
    """Tilda answered a CommerceML exchange step with something other than success."""


class TildaCommerceMLClient:
    """The "1С" side of Tilda's CommerceML connector (standard 1С ↔ сайт
    exchange protocol): this service acts as the accounting system and
    uploads a catalog (import.xml) and an offers package (offers.xml) with
    stock counts. Tilda's own connector settings decide what it actually
    applies — only "Обновлять остатки" is switched on there.

    Every step's raw answer is kept in `transcript`, so a one-product test
    run can show exactly what Tilda said at each step."""

    def __init__(self, *, login: str, password: str, url: str = DEFAULT_URL, timeout: float = 60.0) -> None:
        self._url = url
        self._client = httpx.Client(auth=(login, password), timeout=timeout)
        self._sessid: str | None = None
        self.file_limit: int | None = None
        self.transcript: list[tuple[str, str]] = []

    def _request(self, method: str, params: dict[str, str], content: bytes | None = None) -> str:
        if self._sessid:
            params = {**params, "sessid": self._sessid}
        headers = {"Content-Type": "application/octet-stream"} if content is not None else {}
        response = self._client.request(method, self._url, params=params, content=content, headers=headers)
        text = response.content.decode("utf-8", errors="replace").lstrip("﻿").strip()
        self.transcript.append((f"{method} mode={params.get('mode')} {params.get('filename', '')}".strip(), text[:500]))
        if response.is_error:
            raise TildaExchangeError(f"Tilda CommerceML {params.get('mode')} failed with HTTP {response.status_code}: {text[:500]}")
        return text

    @staticmethod
    def _status(text: str) -> str:
        return text.splitlines()[0].strip().lower() if text else ""

    def checkauth(self) -> None:
        text = self._request("GET", {"type": "catalog", "mode": "checkauth"})
        lines = [line.strip() for line in text.splitlines()]
        if not lines or lines[0].lower() != "success":
            raise TildaExchangeError(f"Tilda CommerceML checkauth: {text[:500]}")
        if len(lines) >= 3 and lines[1]:
            self._client.cookies.set(lines[1], lines[2])
        if len(lines) >= 4 and lines[3]:
            self._sessid = lines[3]

    def init(self) -> None:
        text = self._request("GET", {"type": "catalog", "mode": "init"})
        for line in text.splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "file_limit" and value.strip().isdigit():
                self.file_limit = int(value.strip())
        if text.lower().startswith("failure"):
            raise TildaExchangeError(f"Tilda CommerceML init: {text[:500]}")

    def upload(self, filename: str, body: bytes) -> None:
        """Sent in file_limit-sized pieces when Tilda sets one — repeated
        mode=file requests for the same filename are appended, per protocol."""
        size = self.file_limit if self.file_limit and self.file_limit > 0 else len(body) or 1
        for start in range(0, max(len(body), 1), size):
            text = self._request("POST", {"type": "catalog", "mode": "file", "filename": filename}, content=body[start:start + size])
            if self._status(text) != "success":
                raise TildaExchangeError(f"Tilda CommerceML file {filename}: {text[:500]}")

    def import_file(self, filename: str) -> str:
        for _ in range(MAX_IMPORT_STEPS):
            text = self._request("GET", {"type": "catalog", "mode": "import", "filename": filename})
            status = self._status(text)
            if status == "success":
                return text
            if status != "progress":
                raise TildaExchangeError(f"Tilda CommerceML import {filename}: {text[:500]}")
            time.sleep(IMPORT_STEP_PAUSE_SECONDS)
        raise TildaExchangeError(f"Tilda CommerceML import {filename}: всё ещё progress после {MAX_IMPORT_STEPS} запросов")

    def exchange(self, files: list[tuple[str, bytes]]) -> None:
        """Full exchange: checkauth → init → (file → import) for each file, in order."""
        self.checkauth()
        self.init()
        for filename, body in files:
            self.upload(filename, body)
            self.import_file(filename)
        record(service="tilda", entity_type="commerceml", entity_id=",".join(name for name, _ in files), action="send",
               after={"files": {name: len(body) for name, body in files}})

    def close(self) -> None:
        self._client.close()


def transcript_text(client: TildaCommerceMLClient) -> list[dict[str, Any]]:
    return [{"step": step, "answer": answer} for step, answer in client.transcript]
