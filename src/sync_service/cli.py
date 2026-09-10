from __future__ import annotations

import argparse
import json
from datetime import datetime

from .config import Settings
from .moysklad import MoySkladClient
from .novicloud import NovicloudClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Novicloud/MoySklad API checks")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("novicloud-products")
    sales = subparsers.add_parser("novicloud-sales")
    sales.add_argument("--from", dest="date_from", required=True)
    subparsers.add_parser("moysklad-stocks")
    args = parser.parse_args()

    settings = Settings.from_env()
    if args.command.startswith("novicloud-"):
        client = NovicloudClient(
            base_url=settings.novicloud_base_url,
            version=settings.novicloud_api_version,
            account=settings.novicloud_account,
            password=settings.novicloud_password,
        )
        try:
            payload = (
                client.products()
                if args.command == "novicloud-products"
                else client.sales(date_from=datetime.fromisoformat(args.date_from))
            )
        finally:
            client.close()
    else:
        client = MoySkladClient(
            base_url=settings.moysklad_base_url,
            token=settings.moysklad_token,
        )
        try:
            payload = client.stock_report()
        finally:
            client.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
