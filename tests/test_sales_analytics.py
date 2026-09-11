from datetime import datetime

from sync_service.sales_analytics import COUNTRY_ORGANIZATIONS, country_sales_summary


class FakeMoySkladClient:
    """Stub matching the subset of MoySkladClient used by sales_analytics."""

    base_url = "https://api.moysklad.ru/api/remap/1.2"

    def documents(self, entity, *, document_filter, limit=1000):
        organization_id = COUNTRY_ORGANIZATIONS["PL"]["organization_id"]
        if organization_id not in document_filter:
            return []
        if entity == "retaildemand":
            return [{"sum": 10000}, {"sum": 5000}]
        if entity == "retailsalesreturn":
            return [{"sum": 2000}]
        raise AssertionError(f"Unexpected entity: {entity}")


def test_country_sales_summary_aggregates_sales_and_returns():
    client = FakeMoySkladClient()
    summary = country_sales_summary(
        client,
        moment_from=datetime(2026, 9, 1),
        moment_to=datetime(2026, 9, 10),
    )
    by_country = {row["country"]: row for row in summary}
    assert set(by_country) == set(COUNTRY_ORGANIZATIONS)
    poland = by_country["PL"]
    assert poland["sales_sum"] == 150.0
    assert poland["sales_count"] == 2
    assert poland["returns_sum"] == 20.0
    assert poland["returns_count"] == 1
    assert poland["net_sum"] == 130.0
    lithuania = by_country["LT"]
    assert lithuania["sales_sum"] == 0.0
    assert lithuania["returns_count"] == 0
