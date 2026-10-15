from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class ReviewRulesStore:
    """Persists the staff-curated 'house style' rulebook the review/question
    auto-reply assistant is told to follow — one text blob per kind ("review"
    vs "question", since they already use separate system prompts and have a
    different reply structure; OZON vs Yandex Market isn't split, since tone
    doesn't depend on the marketplace).

    Updated two ways: automatically, when a published reply differs from the
    model's own draft — review_worker distills the edit into a rule via
    review_assistant.update_rules_from_edit; and manually, via the "правила"
    chat command — review_assistant.edit_rules_by_instruction."""

    def __init__(self, path: str = "data/review_rules.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS rules (kind TEXT PRIMARY KEY, text TEXT NOT NULL, updated_at TEXT NOT NULL)")

    def get(self, kind: str) -> str:
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT text FROM rules WHERE kind=?", (kind,)).fetchone()
        return row[0] if row else ""

    def set(self, kind: str, text: str) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO rules(kind, text, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(kind) DO UPDATE SET text=excluded.text, updated_at=excluded.updated_at",
                (kind, text, datetime.now(timezone.utc).isoformat()),
            )
