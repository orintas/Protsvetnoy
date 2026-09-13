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
    yandex_market_api_key: str
    yandex_market_business_id: str
    yandex_market_campaign_id: str
    yandex_market_base_url: str
    dry_run: bool
    moysklad_shift_close_dry_run: bool

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
            yandex_market_api_key=os.getenv("YANDEX_MARKET_API_KEY", ""),
            yandex_market_business_id=os.getenv("YANDEX_MARKET_BUSINESS_ID", ""),
            yandex_market_campaign_id=os.getenv("YANDEX_MARKET_CAMPAIGN_ID", ""),
            yandex_market_base_url=os.getenv(
                "YANDEX_MARKET_BASE_URL", "https://api.partner.market.yandex.ru"
            ).rstrip("/"),
            dry_run=os.getenv("DRY_RUN", "true").lower() in {"1", "true", "yes"},
            moysklad_shift_close_dry_run=os.getenv("MOYSKLAD_SHIFT_CLOSE_DRY_RUN", "true").lower() in {"1", "true", "yes"},
        )


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value
