"""A delegated dataset's foreign keys must serialize into the overlay.

Confirmed against a live Power BI dataset with relationships (FleetOps): the
per-user overlay upsert crashed with "Object of type ForeignKey is not JSON
serializable" because the table's fks stayed as pydantic ForeignKey objects on
their way into DataSourceTable.fks (a JSON column). Columns and pks were
normalized; fks were not. Any modeled dataset with relationships could never
build a delegated overlay — blocking the owner from authoring over it.
"""
import json

from app.ai.prompt_formatters import Table, TableColumn, ForeignKey
from app.services.data_source_service import normalize_overlay_fks


def _fk():
    return ForeignKey(
        column=TableColumn(name="vehicle_id", dtype="Integer"),
        references_name="FleetOps/dim_vehicle",
        references_column=TableColumn(name="id", dtype="Integer"),
    )


def test_foreignkey_objects_become_json_serializable_dicts():
    out = normalize_overlay_fks([_fk()])
    # Must survive the JSON column round-trip that previously raised.
    json.dumps(out)
    assert isinstance(out[0], dict)
    assert out[0]["references_name"] == "FleetOps/dim_vehicle"
    assert out[0]["column"]["name"] == "vehicle_id"


def test_dicts_pass_through_and_empty_is_safe():
    d = [{"column": {"name": "x"}, "references_name": "T", "references_column": {"name": "id"}}]
    assert normalize_overlay_fks(d) == d
    assert normalize_overlay_fks([]) == []
    assert normalize_overlay_fks(None) == []


def test_table_fks_from_prompt_formatter_serialize():
    t = Table(name="FleetOps/fact_service_event",
              columns=[TableColumn(name="vehicle_id", dtype="Integer")],
              pks=[], fks=[_fk()])
    json.dumps(normalize_overlay_fks(t.fks))
