from sync_service.slot_assignment import UnresolvedPosition, create_demands_with_slots


class FakeMoySkladClient:
    """Stands in for MoySkladClient's address-storage calls. `stock` is
    `{store_id: {assortment_id: {slot_id: quantity}}}`, mutated by real
    MoySklad as positions ship — here just read by `stock_by_slot`."""

    def __init__(self, *, demands_by_order, positions_by_demand, store_by_demand, stock, order_names=None):
        self._demands_by_order = demands_by_order
        self._positions_by_demand = positions_by_demand
        self._store_by_demand = store_by_demand
        self._stock = stock
        self.set_slot_calls: list[tuple[str, str, str, str]] = []

    def demand_template_from_customer_order(self, order_id):
        return {"customerOrder": {"meta": {"href": f".../entity/customerorder/{order_id}"}}}

    def create_demand(self, template):
        order_id = template["customerOrder"]["meta"]["href"].rsplit("/", 1)[-1]
        demand = dict(self._demands_by_order[order_id])
        demand["store"] = {"meta": {"href": f".../entity/store/{self._store_by_demand[demand['id']]}"}}
        return demand

    def demand_positions(self, demand_id):
        return self._positions_by_demand[demand_id]

    def stock_by_slot(self, assortment_ids, *, store_id):
        rows = []
        for assortment_id in assortment_ids:
            for slot_id, qty in self._stock.get(store_id, {}).get(assortment_id, {}).items():
                rows.append({"assortmentId": assortment_id, "storeId": store_id, "slotId": slot_id, "stock": qty})
        return rows

    def set_position_slot(self, demand_id, position_id, *, store_id, slot_id):
        self.set_slot_calls.append((demand_id, position_id, store_id, slot_id))
        return {"id": position_id}


def _position(position_id, assortment_id, quantity):
    return {"id": position_id, "quantity": quantity, "assortment": {"meta": {"href": f".../entity/product/{assortment_id}"}}}


def test_assigns_the_only_cell_holding_the_product():
    client = FakeMoySkladClient(
        demands_by_order={"order-1": {"id": "demand-1", "name": "00001"}},
        positions_by_demand={"demand-1": [_position("pos-1", "sku-a", 2)]},
        store_by_demand={"demand-1": "store-1"},
        stock={"store-1": {"sku-a": {"slot-A": 10}}},
    )

    results = create_demands_with_slots(client, ["order-1"])

    assert len(results) == 1
    assert results[0].unresolved == []
    assert results[0].assigned == [{"position_id": "pos-1", "assortment_id": "sku-a", "slot_id": "slot-A", "quantity": 2}]
    assert client.set_slot_calls == [("demand-1", "pos-1", "store-1", "slot-A")]


def test_prefers_the_fullest_cell_that_covers_the_need():
    client = FakeMoySkladClient(
        demands_by_order={"order-1": {"id": "demand-1", "name": "00001"}},
        positions_by_demand={"demand-1": [_position("pos-1", "sku-a", 5)]},
        store_by_demand={"demand-1": "store-1"},
        stock={"store-1": {"sku-a": {"slot-small": 6, "slot-big": 40}}},
    )

    results = create_demands_with_slots(client, ["order-1"])

    assert results[0].assigned[0]["slot_id"] == "slot-big"


def test_does_not_double_book_the_same_cell_across_two_orders_in_one_batch():
    client = FakeMoySkladClient(
        demands_by_order={
            "order-1": {"id": "demand-1", "name": "00001"},
            "order-2": {"id": "demand-2", "name": "00002"},
        },
        positions_by_demand={
            "demand-1": [_position("pos-1", "sku-a", 6)],
            "demand-2": [_position("pos-2", "sku-a", 6)],
        },
        store_by_demand={"demand-1": "store-1", "demand-2": "store-1"},
        # Only one cell exists, with 10 units — enough for either order alone, not both.
        stock={"store-1": {"sku-a": {"slot-A": 10}}},
    )

    results = create_demands_with_slots(client, ["order-1", "order-2"])

    assert results[0].assigned[0]["slot_id"] == "slot-A"
    assert results[1].unresolved == [UnresolvedPosition(assortment_id="sku-a", needed=6, available=4)]


def test_flags_unresolved_when_no_cell_has_enough_stock():
    client = FakeMoySkladClient(
        demands_by_order={"order-1": {"id": "demand-1", "name": "00001"}},
        positions_by_demand={"demand-1": [_position("pos-1", "sku-a", 100)]},
        store_by_demand={"demand-1": "store-1"},
        stock={"store-1": {"sku-a": {"slot-A": 10, "slot-B": 5}}},
    )

    results = create_demands_with_slots(client, ["order-1"])

    assert results[0].assigned == []
    assert len(results[0].unresolved) == 1
    assert results[0].unresolved[0].needed == 100
    assert results[0].unresolved[0].available == 15
    assert client.set_slot_calls == []
