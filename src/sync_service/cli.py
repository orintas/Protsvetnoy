from __future__ import annotations

import argparse
import json
import re
from datetime import datetime

from .config import Settings
from .moysklad import MoySkladClient
from .novicloud import NovicloudClient

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def _resolve_order_id(client: MoySkladClient, order_number: str) -> str:
    order = client.customer_order_by_name(order_number)
    if order is None:
        raise SystemExit(f"Заказ покупателя «{order_number}» не найден")
    return order["id"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Novicloud/MoySklad API checks")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("novicloud-products")
    sales = subparsers.add_parser("novicloud-sales")
    sales.add_argument("--from", dest="date_from", required=True)
    subparsers.add_parser("moysklad-stocks")
    subparsers.add_parser("web-server")
    subparsers.add_parser("sync-worker")
    subparsers.add_parser("yandex-market-stock-sync-worker")
    subparsers.add_parser("ozon-stock-sync-worker")
    subparsers.add_parser("ozon-order-sync-worker")
    subparsers.add_parser("shopify-catalog-sync-worker")
    subparsers.add_parser("shopify-stock-sync-worker")
    subparsers.add_parser("moysklad-shift-close-worker")
    subparsers.add_parser("health-check-worker")
    subparsers.add_parser("telegram-qa-worker")
    backfill = subparsers.add_parser(
        "novicloud-shift-backfill",
        description="Split the Novicloud stores' receipts back into one MoySklad shift per day (dry run unless --apply).",
    )
    backfill.add_argument("--since", required=True, help="First day to repair, YYYY-MM-DD")
    backfill.add_argument("--apply", action="store_true", help="Actually write to MoySklad")
    assign_slots = subparsers.add_parser(
        "moysklad-assign-slots",
        description="Create an Отгрузка from each given Заказ покупателя and assign a ячейка to every line.",
    )
    assign_slots.add_argument("--order", dest="orders", action="append", required=True, help="Order id (UUID) or order number; can be repeated")
    args = parser.parse_args()

    if args.command == "web-server":
        from .web import main as web_main
        web_main()
        return
    if args.command == "sync-worker":
        from .novicloud_retail_sync import worker
        worker()
        return
    if args.command == "yandex-market-stock-sync-worker":
        from .yandex_market_stock_sync import worker as ym_stock_worker
        ym_stock_worker()
        return
    if args.command == "ozon-stock-sync-worker":
        from .ozon_stock_sync import worker as ozon_stock_worker
        ozon_stock_worker()
        return
    if args.command == "ozon-order-sync-worker":
        from .ozon_order_sync import worker as ozon_order_worker
        ozon_order_worker()
        return
    if args.command == "shopify-catalog-sync-worker":
        from .shopify_sync import catalog_worker
        catalog_worker()
        return
    if args.command == "shopify-stock-sync-worker":
        from .shopify_sync import stock_worker
        stock_worker()
        return
    if args.command == "moysklad-shift-close-worker":
        from .shift_closer import worker as shift_worker
        shift_worker()
        return
    if args.command == "health-check-worker":
        from .health_check import worker as health_check_worker
        health_check_worker()
        return
    if args.command == "telegram-qa-worker":
        from .telegram_qa_worker import worker as telegram_qa_worker
        telegram_qa_worker()
        return
    if args.command == "novicloud-shift-backfill":
        from zoneinfo import ZoneInfo
        from .shift_backfill import apply_day, describe, plan_store
        from .store_mapping import load_store_mappings
        settings = Settings.from_env()
        client = MoySkladClient(base_url=settings.moysklad_base_url, token=settings.moysklad_token)
        now = datetime.now(ZoneInfo("Europe/Warsaw"))
        today = now.date().isoformat()
        try:
            for store in load_store_mappings():
                # Open today's shift first, so the live sync's new receipts land in it rather than an old shift being repaired.
                if args.apply and client.retail_shift_for_day(store.retail_store_id, today) is None:
                    created = client.create_retail_shift(
                        organization_id=store.organization_id, store_id=store.moysklad_store_id, retail_store_id=store.retail_store_id,
                        department_id=store.department_id, owner_id=store.owner_id, moment=now.strftime("%Y-%m-%d %H:%M:%S.000"),
                    )
                    print(f"===== {store.name}: открыта смена №{created.get('name')} на сегодня", flush=True)
                plans = plan_store(client, store, since_day=args.since, today=today)
                print(f"===== {store.name}: дней к исправлению {len(plans)}")
                for plan in plans:
                    print(("  " if args.apply else "  [тест] ") + describe(plan), flush=True)
                    if args.apply:
                        print(f"    -> готово, смена №{apply_day(client, store, plan)}", flush=True)
        finally:
            client.close()
        return
    if args.command == "moysklad-assign-slots":
        from .slot_assignment import create_demands_with_slots
        settings = Settings.from_env()
        client = MoySkladClient(base_url=settings.moysklad_base_url, token=settings.moysklad_token)
        try:
            order_ids = [o if _UUID_RE.match(o) else _resolve_order_id(client, o) for o in args.orders]
            results = create_demands_with_slots(client, order_ids)
        finally:
            client.close()
        for result in results:
            print(f"Отгрузка {result.demand_number} (заказ {result.order_id}): назначено ячеек {len(result.assigned)}")
            for unresolved in result.unresolved:
                print(f"  ! не хватает остатка в ячейках для {unresolved.assortment_id}: нужно {unresolved.needed}, доступно {unresolved.available} — проставьте вручную")
        return
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
