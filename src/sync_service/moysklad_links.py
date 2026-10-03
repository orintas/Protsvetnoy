from __future__ import annotations

from typing import Any


def moysklad_link_fields(entity: dict[str, Any] | None, label: str) -> dict[str, str]:
    """Standard payload keys the web UI journal rows look for to render a
    clickable link to the MoySklad entity instead of showing (or burying)
    its raw UUID. `entity` is any MoySklad API response object that carries
    a `meta.uuidHref` — i.e. almost anything returned by MoySkladClient.
    """
    url = (entity or {}).get("meta", {}).get("uuidHref", "")
    return {"moysklad_url": url, "moysklad_label": label}
