"""Foreign keys in the agent's schema context must not point outside it.

An edge renders as DDL the agent reads as fact — `foreign key (region_code)
references ref.regions(code)`. When the referenced table is absent from the
context (deselected by the agent's owner, filtered out, or hidden from this
user by an access overlay), that line instructs a join no query can satisfy:
the agent writes it, execution fails, and nothing in the context explains why.

This became reachable when SQL sources started reporting foreign keys at all.
Before that only semantic sources carried edges, and their tables tend to be
selected as a unit; now any partial selection of a relational schema produces
dangling ones, which is the normal case rather than the exotic one.
"""

from __future__ import annotations

from app.ai.context.builders.schema_context_builder import _prune_unresolvable_fks


def _fk(column: str, target: str, target_column: str = "id") -> dict:
    return {
        "column": {"name": column, "dtype": "integer"},
        "references_name": target,
        "references_column": {"name": target_column, "dtype": "integer"},
    }


def _targets(item: dict) -> set[str]:
    return {fk["references_name"] for fk in item["fks"]}


def test_edge_to_an_absent_table_is_dropped():
    """The case that motivated this: orders selected, regions not."""
    normalized = [
        {
            "name": "sales.orders",
            "fks": [_fk("customer_id", "ref.customers"), _fk("region_code", "ref.regions", "code")],
        },
        {"name": "ref.customers", "fks": []},
    ]

    _prune_unresolvable_fks(normalized)

    assert _targets(normalized[0]) == {"ref.customers"}


def test_edges_are_kept_when_every_target_is_present():
    """Pruning must not be a blunt instrument — a complete selection loses nothing."""
    normalized = [
        {
            "name": "sales.orders",
            "fks": [_fk("customer_id", "ref.customers"), _fk("region_code", "ref.regions", "code")],
        },
        {"name": "ref.customers", "fks": []},
        {"name": "ref.regions", "fks": []},
    ]

    _prune_unresolvable_fks(normalized)

    assert _targets(normalized[0]) == {"ref.customers", "ref.regions"}


def test_a_self_reference_survives():
    """A hierarchy table references itself; it is trivially present."""
    normalized = [{"name": "hr.employees", "fks": [_fk("manager_id", "hr.employees")]}]

    _prune_unresolvable_fks(normalized)

    assert _targets(normalized[0]) == {"hr.employees"}


def test_a_reference_spelled_differently_does_not_survive():
    """Resolution is exact string equality, and silence here would be the bug.

    An edge naming `customers` when the table is emitted as `ref.customers`
    cannot be matched by anything downstream, so it must be pruned rather than
    passed through as a reference the agent will read as usable.
    """
    normalized = [
        {"name": "sales.orders", "fks": [_fk("customer_id", "customers")]},
        {"name": "ref.customers", "fks": []},
    ]

    _prune_unresolvable_fks(normalized)

    assert normalized[0]["fks"] == []


def test_every_surviving_edge_resolves_to_an_emitted_table():
    """The invariant itself, over a mixed set."""
    normalized = [
        {
            "name": "sales.orders",
            "fks": [_fk("customer_id", "ref.customers"), _fk("region_code", "ref.regions", "code")],
        },
        {"name": "sales.line_items", "fks": [_fk("order_id", "sales.orders"), _fk("sku", "ref.products", "sku")]},
        {"name": "ref.customers", "fks": []},
        {"name": "sales.audit_log", "fks": []},
    ]

    _prune_unresolvable_fks(normalized)

    emitted = {item["name"] for item in normalized}
    for item in normalized:
        for fk in item["fks"]:
            assert fk["references_name"] in emitted


def test_tables_without_edges_are_untouched():
    normalized = [{"name": "sales.audit_log", "fks": []}, {"name": "ref.customers", "fks": None}]

    _prune_unresolvable_fks(normalized)

    assert normalized[0]["fks"] == []
    assert normalized[1]["fks"] is None


def test_object_shaped_edges_are_handled():
    """Overlay rows can carry pydantic ForeignKeys rather than dicts."""

    class _FK:
        def __init__(self, target):
            self.references_name = target

    normalized = [
        {"name": "sales.orders", "fks": [_FK("ref.customers"), _FK("ref.regions")]},
        {"name": "ref.customers", "fks": []},
    ]

    _prune_unresolvable_fks(normalized)

    assert [fk.references_name for fk in normalized[0]["fks"]] == ["ref.customers"]


def test_empty_context_is_not_an_error():
    normalized = []
    _prune_unresolvable_fks(normalized)
    assert normalized == []


class TestPowerBINaming:
    """Power BI names tables `{dataset}/{Table}`, and pruning must respect it.

    Shapes taken from a live tenant (a FleetOps star schema and Power BI's
    auto-generated date tables), because this connector family is the one that
    has always carried relationships — a naming mismatch here would silently
    empty the edges that already worked, which is the worst outcome this change
    could have.
    """

    def test_dataset_qualified_references_survive(self):
        normalized = [
            {
                "name": "FleetOps/fact_service_event",
                "fks": [
                    _fk("svc_vehicle_key", "FleetOps/dim_vehicle", "vehicle_key"),
                    _fk("svc_depot_key", "FleetOps/dim_depot", "depot_key"),
                ],
            },
            {"name": "FleetOps/dim_vehicle", "fks": []},
            {"name": "FleetOps/dim_depot", "fks": []},
        ]

        _prune_unresolvable_fks(normalized)

        assert _targets(normalized[0]) == {"FleetOps/dim_vehicle", "FleetOps/dim_depot"}

    def test_edges_to_auto_generated_date_tables_are_dropped(self):
        """`LocalDateTable_<guid>` is generated by Power BI and never surfaced.

        The connector does not emit it as a table, so an edge naming it points
        at something the agent can neither see nor query in DAX.
        """
        normalized = [
            {
                "name": "deals2/Opportunities",
                "fks": [
                    _fk("Close Date", "deals2/LocalDateTable_fb22216f-60fb", "Date"),
                    _fk("Company", "deals2/Accounts", "Company"),
                ],
            },
            {"name": "deals2/Accounts", "fks": []},
        ]

        _prune_unresolvable_fks(normalized)

        assert _targets(normalized[0]) == {"deals2/Accounts"}

    def test_same_table_name_in_two_datasets_stays_distinct(self):
        """Two models can both hold `Orders`; the dataset prefix is what separates them.

        Matching on the bare table name would let one model's fact table point
        into another model's dimension — a join DAX cannot express.
        """
        normalized = [
            {"name": "SalesPush/Sales", "fks": [_fk("order_id", "SalesPush/Orders", "id")]},
            {"name": "shared_orders/Orders", "fks": []},
        ]

        _prune_unresolvable_fks(normalized)

        assert normalized[0]["fks"] == []
