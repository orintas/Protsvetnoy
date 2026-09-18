from __future__ import annotations

import base64
from typing import Any

from .change_log import record
from .http import JsonClient


class ShopifyClient:
    """Shopify Admin API client — REST for products/inventory (matches the
    proven Make.com scenario), GraphQL only for the one thing REST can't do:
    finding a product variant by SKU."""

    def __init__(self, *, shop_domain: str, access_token: str, api_version: str) -> None:
        self._client = JsonClient(
            base_url=f"https://{shop_domain}/admin/api/{api_version}",
            headers={"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"},
        )

    def close(self) -> None:
        self._client.close()

    def find_variant_by_sku(self, sku: str) -> dict[str, Any] | None:
        query = """
        query($q: String!) {
          productVariants(first: 1, query: $q) {
            edges { node { id sku inventoryItem { id } product { id title } } }
          }
        }
        """
        payload = self._client.post("/graphql.json", {"query": query, "variables": {"q": f"sku:{sku}"}})
        edges = (((payload.get("data") or {}).get("productVariants") or {}).get("edges")) or []
        if not edges:
            return None
        node = edges[0]["node"]
        return {
            "variant_id": _numeric_id(node["id"]),
            "product_id": _numeric_id(node["product"]["id"]),
            "inventory_item_id": _numeric_id(node["inventoryItem"]["id"]),
        }

    def create_product(
        self,
        *,
        title: str,
        sku: str,
        price: float,
        vendor: str,
        product_type: str,
        body_html: str,
        weight_kg: float | None = None,
        barcode: str | None = None,
        image_bytes: bytes | None = None,
    ) -> dict[str, Any]:
        variant: dict[str, Any] = {"sku": sku, "price": f"{price:.2f}", "weight_unit": "kg", "inventory_management": "shopify"}
        if weight_kg is not None:
            variant["weight"] = weight_kg
        if barcode:
            variant["barcode"] = barcode
        product: dict[str, Any] = {
            "title": title,
            "body_html": body_html,
            "vendor": vendor,
            "product_type": product_type,
            "status": "draft",
            "published": True,
            "variants": [variant],
        }
        if image_bytes:
            product["images"] = [{"attachment": base64.b64encode(image_bytes).decode("ascii")}]
        result = self._client.post("/products.json", {"product": product})["product"]
        record(service="shopify", entity_type="product", entity_id=sku, action="create",
               after={"title": title, "price": price, "product_type": product_type})
        return result

    def update_product(
        self,
        product_id: int,
        variant_id: int,
        *,
        sku: str,
        price: float,
        vendor: str,
        product_type: str,
        weight_kg: float | None = None,
        barcode: str | None = None,
        image_bytes: bytes | None = None,
    ) -> dict[str, Any]:
        """Never touches title — that's set once on create_product and is
        fair game for a human to edit by hand afterwards in Shopify."""
        variant: dict[str, Any] = {"id": variant_id, "sku": sku, "price": f"{price:.2f}", "weight_unit": "kg", "inventory_management": "shopify"}
        if weight_kg is not None:
            variant["weight"] = weight_kg
        if barcode:
            variant["barcode"] = barcode
        product: dict[str, Any] = {
            "id": product_id,
            "vendor": vendor,
            "product_type": product_type,
            "variants": [variant],
        }
        if image_bytes:
            product["images"] = [{"attachment": base64.b64encode(image_bytes).decode("ascii")}]
        result = self._client.put(f"/products/{product_id}.json", {"product": product})["product"]
        record(service="shopify", entity_type="product", entity_id=sku, action="update",
               after={"price": price, "product_type": product_type, "vendor": vendor})
        return result

    def set_inventory_level(self, *, inventory_item_id: int, location_id: int, available: int, previous_available: int | None = None) -> dict[str, Any]:
        result = self._client.post(
            "/inventory_levels/set.json",
            {"location_id": location_id, "inventory_item_id": inventory_item_id, "available": available},
        )
        record(service="shopify", entity_type="inventory_level", entity_id=str(inventory_item_id), action="update",
               before=previous_available, after=available)
        return result

    def locations(self) -> list[dict[str, Any]]:
        payload = self._client.get("/locations.json")
        rows = payload.get("locations", [])
        return [row for row in rows if isinstance(row, dict)]


def _numeric_id(gid: str) -> int:
    """"gid://shopify/ProductVariant/12345" -> 12345."""
    return int(gid.rsplit("/", 1)[-1])
