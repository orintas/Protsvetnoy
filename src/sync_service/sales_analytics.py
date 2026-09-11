from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .moysklad import MoySkladClient

# MoySklad organizations, one per country storefront. Novicloud only covers
# Poland; Lithuania, Latvia and Estonia are tracked directly in MoySklad.
COUNTRY_ORGANIZATIONS: dict[str, dict[str, str]] = {
    "PL": {"label": "Польша", "organization_id": "a623f6a9-dde8-11ed-0a80-01540011a4a0"},
    "LT": {"label": "Литва", "organization_id": "147a9f38-1896-11ed-0a80-0e5d000a5470"},
    "LV": {"label": "Латвия", "organization_id": "94ad58fb-beec-11ec-0a80-092400291358"},
    "EE": {"label": "Эстония", "organization_id": "0b3fbc43-e0dc-11ec-0a80-0076000f9c69"},
}


def _moment(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _sum_documents(
    moysklad: MoySkladClient,
    *,
    entity: str,
    organization_id: str,
    moment_from: datetime,
    moment_to: datetime,
) -> tuple[float, int]:
    organization_href = f"{moysklad.base_url}/entity/organization/{organization_id}"
    document_filter = (
        f"organization={organization_href}"
        f";moment>={_moment(moment_from)}"
        f";moment<={_moment(moment_to)}"
    )
    total = 0.0
    count = 0
    for row in moysklad.documents(entity, document_filter=document_filter):
        total += float(row.get("sum") or 0) / 100
        count += 1
    return total, count


def country_sales_summary(
    moysklad: MoySkladClient,
    *,
    moment_from: datetime,
    moment_to: datetime,
) -> list[dict[str, Any]]:
    """Aggregate retail sales and returns per country storefront."""
    summary: list[dict[str, Any]] = []
    for code, info in COUNTRY_ORGANIZATIONS.items():
        sales_sum, sales_count = _sum_documents(
            moysklad,
            entity="retaildemand",
            organization_id=info["organization_id"],
            moment_from=moment_from,
            moment_to=moment_to,
        )
        returns_sum, returns_count = _sum_documents(
            moysklad,
            entity="retailsalesreturn",
            organization_id=info["organization_id"],
            moment_from=moment_from,
            moment_to=moment_to,
        )
        summary.append(
            {
                "country": code,
                "label": info["label"],
                "sales_sum": round(sales_sum, 2),
                "sales_count": sales_count,
                "returns_sum": round(returns_sum, 2),
                "returns_count": returns_count,
                "net_sum": round(sales_sum - returns_sum, 2),
            }
        )
    return summary
