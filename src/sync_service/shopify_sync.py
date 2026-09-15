from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .category_sync import CategorySyncConfig
from .config import Settings
from .error_log import ErrorLog
from .moysklad import MoySkladClient
from .shopify_client import ShopifyClient
from .shopify_warehouses import ShopifyWarehouseConfig

# Matches the Make.com scenario this was ported from.
VENDOR = "TM Varvikas"
RETAIL_PRICE_TYPE_NAME = "Цена ритэйл"

RATE_LIMIT_SLEEP_SECONDS = 1

MOSCOW = ZoneInfo("Europe/Moscow")
STOCK_SYNC_START_HOUR = 9
STOCK_SYNC_END_HOUR = 22
CATALOG_SYNC_HOUR = 3  # once a night, Moscow time


class ShopifySyncLog:
    def __init__(self, path: str = "data/shopify_sync.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, kind TEXT NOT NULL,
                external_id TEXT, status TEXT NOT NULL, message TEXT NOT NULL,
                payload TEXT NOT NULL)"""
            )

    def add(self, kind: str, status: str, message: str, external_id: str | None = None, payload: Any = None) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO sync_log(created_at,kind,external_id,status,message,payload) VALUES(?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), kind, external_id, status, message, json.dumps(payload, ensure_ascii=False, default=str)),
            )

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT ?", (limit,))]

    def search(self, query: str, limit: int = 200) -> list[dict[str, Any]]:
        like = f"%{query}%"
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM sync_log WHERE external_id LIKE ? OR message LIKE ? ORDER BY id DESC LIMIT ?",
                    (like, like, limit),
                )
            ]


class ShopifyProductMap:
    """sku -> Shopify product/variant/inventory item, populated by the catalog
    sync and read by the stock sync (avoids a GraphQL SKU search every 30
    minutes). Also remembers the last pushed stock level so the stock sync
    only calls the API for skus that actually changed."""

    def __init__(self, path: str = "data/shopify_products.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS product_map (
                sku TEXT PRIMARY KEY, product_id INTEGER NOT NULL, variant_id INTEGER NOT NULL,
                inventory_item_id INTEGER NOT NULL, last_available INTEGER, updated_at TEXT NOT NULL)"""
            )

    def get(self, sku: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.path) as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM product_map WHERE sku=?", (sku,)).fetchone()
            return dict(row) if row else None

    def set(self, sku: str, *, product_id: int, variant_id: int, inventory_item_id: int) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                """INSERT INTO product_map(sku, product_id, variant_id, inventory_item_id, updated_at) VALUES (?,?,?,?,?)
                ON CONFLICT(sku) DO UPDATE SET product_id=excluded.product_id, variant_id=excluded.variant_id,
                inventory_item_id=excluded.inventory_item_id, updated_at=excluded.updated_at""",
                (sku, product_id, variant_id, inventory_item_id, datetime.now(timezone.utc).isoformat()),
            )

    def set_last_available(self, sku: str, available: int) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE product_map SET last_available=? WHERE sku=?", (available, sku))

    def all_skus(self) -> list[str]:
        with sqlite3.connect(self.path) as db:
            return [row[0] for row in db.execute("SELECT sku FROM product_map")]


def _retail_price(product: dict[str, Any]) -> float | None:
    for entry in product.get("salePrices") or []:
        if ((entry.get("priceType") or {}).get("name")) == RETAIL_PRICE_TYPE_NAME:
            return round(entry.get("value", 0)) / 100
    return None


def sync_catalog(moysklad: MoySkladClient, shopify: ShopifyClient, categories: list[str], product_map: ShopifyProductMap, log: ShopifySyncLog) -> None:
    for category in categories:
        try:
            products = moysklad.products_by_category(category)
        except Exception as error:
            log.add("catalog_error", "error", f"{category}: ошибка получения товаров из МойСклад: {error}", None)
            continue
        for product in products:
            sku = product.get("code")
            if not sku:
                continue
            try:
                _sync_one_product(moysklad, shopify, product, product_map, log)
            except Exception as error:
                log.add("catalog_error", "error", f"{sku}: ошибка синхронизации в Shopify: {error}", sku, product)
            time.sleep(RATE_LIMIT_SLEEP_SECONDS)


def _sync_one_product(moysklad: MoySkladClient, shopify: ShopifyClient, product: dict[str, Any], product_map: ShopifyProductMap, log: ShopifySyncLog) -> None:
    sku = str(product["code"])
    price = _retail_price(product)
    if price is None:
        log.add("catalog_error", "error", f"{sku}: у товара нет цены «{RETAIL_PRICE_TYPE_NAME}» в МойСклад", sku, product)
        return

    title = f"{sku} - {product.get('name') or ''}"
    weight = product.get("weight")
    barcode = ((product.get("barcodes") or [{}])[0]).get("ean13")
    category = str(product.get("pathName") or "")
    try:
        image_bytes = moysklad.product_image_bytes(product)
    except Exception:
        image_bytes = None

    cached = product_map.get(sku)
    if cached is None:
        found = shopify.find_variant_by_sku(sku)
        if found is not None:
            product_map.set(sku, product_id=found["product_id"], variant_id=found["variant_id"], inventory_item_id=found["inventory_item_id"])
            cached = product_map.get(sku)

    if cached is not None:
        shopify.update_product(
            cached["product_id"], cached["variant_id"],
            title=title, sku=sku, price=price, vendor=VENDOR, product_type=category,
            weight_kg=weight, barcode=barcode, image_bytes=image_bytes,
        )
        log.add("catalog_update", "success", f"{sku}: товар обновлён в Shopify ({title})", sku, {"title": title, "price": price})
        return

    created = shopify.create_product(
        title=title, sku=sku, price=price, vendor=VENDOR, product_type=category,
        body_html=product.get("description") or "", weight_kg=weight, barcode=barcode, image_bytes=image_bytes,
    )
    variant = created["variants"][0]
    product_map.set(sku, product_id=created["id"], variant_id=variant["id"], inventory_item_id=variant["inventory_item_id"])
    log.add("catalog_created", "success", f"{sku}: товар создан в Shopify ({title})", sku, {"title": title, "price": price})


def sync_stock(moysklad: MoySkladClient, shopify: ShopifyClient, warehouse_ids: list[str], location_id: int, product_map: ShopifyProductMap, log: ShopifySyncLog) -> None:
    totals: dict[str, float] = {}
    for warehouse_id in warehouse_ids:
        for row in moysklad.stock_by_store(warehouse_id):
            code = row.get("code")
            if not code:
                continue
            totals[code] = totals.get(code, 0) + (row.get("quantity") or 0)

    changed = 0
    for sku in product_map.all_skus():
        cached = product_map.get(sku)
        if cached is None:
            continue
        available = max(0, round(totals.get(sku, 0)))
        if cached.get("last_available") == available:
            continue
        try:
            shopify.set_inventory_level(inventory_item_id=cached["inventory_item_id"], location_id=location_id, available=available)
        except Exception as error:
            log.add("stock_error", "error", f"{sku}: ошибка обновления остатка: {error}", sku)
            continue
        before = cached.get("last_available")
        product_map.set_last_available(sku, available)
        log.add("stock_sync", "success", f"{sku}: остаток {before if before is not None else '—'}→{available}", sku, {"before": before, "after": available})
        changed += 1
        time.sleep(RATE_LIMIT_SLEEP_SECONDS)
    log.add("stock_run", "success", f"Синхронизация остатков завершена, изменилось {changed} из {len(product_map.all_skus())}")


def run_catalog_once(settings: Settings) -> None:
    categories = CategorySyncConfig().load().get("shopify", [])
    if not categories:
        return
    moysklad = MoySkladClient(base_url=settings.moysklad_base_url, token=settings.moysklad_token)
    shopify = ShopifyClient(shop_domain=settings.shopify_shop_domain, access_token=settings.shopify_access_token, api_version=settings.shopify_api_version)
    log = ShopifySyncLog()
    product_map = ShopifyProductMap()
    try:
        sync_catalog(moysklad, shopify, categories, product_map, log)
    finally:
        moysklad.close()
        shopify.close()


def run_stock_once(settings: Settings) -> None:
    warehouse_ids = ShopifyWarehouseConfig().load()
    if not warehouse_ids:
        return
    moysklad = MoySkladClient(base_url=settings.moysklad_base_url, token=settings.moysklad_token)
    shopify = ShopifyClient(shop_domain=settings.shopify_shop_domain, access_token=settings.shopify_access_token, api_version=settings.shopify_api_version)
    log = ShopifySyncLog()
    product_map = ShopifyProductMap()
    try:
        locations = shopify.locations()
        if not locations:
            log.add("stock_error", "error", "В Shopify не найдено ни одной локации", None)
            return
        sync_stock(moysklad, shopify, warehouse_ids, locations[0]["id"], product_map, log)
    finally:
        moysklad.close()
        shopify.close()


def catalog_worker() -> None:
    settings = Settings.from_env()
    errors = ErrorLog()
    last_run_date: str | None = None
    while True:
        now = datetime.now(MOSCOW)
        today = now.date().isoformat()
        if now.hour == CATALOG_SYNC_HOUR and last_run_date != today:
            try:
                run_catalog_once(settings)
            except Exception as error:
                errors.log_exception("shopify_catalog_sync_worker", error, context="Ошибка синхронизации ассортимента с Shopify")
            last_run_date = today
        time.sleep(300)


def stock_worker() -> None:
    settings = Settings.from_env()
    errors = ErrorLog()
    while True:
        now = datetime.now(MOSCOW)
        if STOCK_SYNC_START_HOUR <= now.hour < STOCK_SYNC_END_HOUR:
            try:
                run_stock_once(settings)
            except Exception as error:
                errors.log_exception("shopify_stock_sync_worker", error, context="Ошибка синхронизации остатков с Shopify")
        time.sleep(1800)
