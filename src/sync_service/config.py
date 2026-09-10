from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    novicloud_account: str
    novicloud_password: str
    novicloud_api_version: str
    novicloud_base_url: str
    moysklad_token: str
    moysklad_base_url: str
    dry_run: bool

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls(
            novicloud_account=_required("NOVICLOUD_ACCOUNT"),
            novicloud_password=_required("NOVICLOUD_PASSWORD"),
            novicloud_api_version=os.getenv("NOVICLOUD_API_VERSION", "v2"),
            novicloud_base_url=os.getenv(
                "NOVICLOUD_BASE_URL", "https://system.novicloud.pl/rest/api"
            ).rstrip("/"),
            moysklad_token=_required("MOYSKLAD_TOKEN"),
            moysklad_base_url=os.getenv(
                "MOYSKLAD_BASE_URL", "https://api.moysklad.ru/api/remap/1.2"
            ).rstrip("/"),
            dry_run=os.getenv("DRY_RUN", "true").lower() in {"1", "true", "yes"},
        )


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value
