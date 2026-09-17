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
    yandex_market_campaign_ids: tuple[str, ...]
    yandex_market_base_url: str
    shopify_shop_domain: str
    shopify_access_token: str
    shopify_api_version: str
    shopify_api_secret: str
    ozon_client_id: str
    ozon_api_key: str
    make_api_token: str
    make_base_url: str
    telegram_bot_token: str
    telegram_label_chat_id: str
    telegram_proxy_url: str
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
            yandex_market_campaign_ids=tuple(
                value.strip() for value in os.getenv("YANDEX_MARKET_CAMPAIGN_ID", "").split(",") if value.strip()
            ),
            yandex_market_base_url=os.getenv(
                "YANDEX_MARKET_BASE_URL", "https://api.partner.market.yandex.ru"
            ).rstrip("/"),
            shopify_shop_domain=os.getenv("SHOPIFY_SHOP_DOMAIN", ""),
            shopify_access_token=os.getenv("SHOPIFY_ACCESS_TOKEN", ""),
            shopify_api_version=os.getenv("SHOPIFY_API_VERSION", "2026-07"),
            shopify_api_secret=os.getenv("SHOPIFY_API_SECRET", ""),
            ozon_client_id=os.getenv("OZON_CLIENT_ID", ""),
            ozon_api_key=os.getenv("OZON_API_KEY", ""),
            make_api_token=os.getenv("MAKE_API_TOKEN", ""),
            make_base_url=os.getenv("MAKE_BASE_URL", "https://eu1.make.com/api/v2").rstrip("/"),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_label_chat_id=os.getenv("TELEGRAM_LABEL_CHAT_ID", ""),
            telegram_proxy_url=os.getenv("TELEGRAM_PROXY_URL", ""),
            dry_run=os.getenv("DRY_RUN", "true").lower() in {"1", "true", "yes"},
            moysklad_shift_close_dry_run=os.getenv("MOYSKLAD_SHIFT_CLOSE_DRY_RUN", "true").lower() in {"1", "true", "yes"},
        )


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value
