"""normalize_flat_records: records sent without the ``fields`` wrapper.

Live, an agent filling a list sometimes sent
``{"row_id": ..., "agreement_name": "...", "supplier_name": "..."}`` and the
whole batch was rejected with "'fields' is a required property". The
end-to-end save path is covered in ``tests/e2e/rbac/test_agent_lists.py``;
these pin the shape contract and the never-mutate-the-input contract.
"""
import copy

import pytest

from app.services.agent_lists.records import normalize_flat_records

NAMES = ["supplier", "amount", "signed"]


@pytest.mark.parametrize("row_id", [None, "r-9"])
def test_flat_record_is_wrapped_under_fields(row_id):
    rec = {"supplier": "Northwind", "amount": 7, "signed": {"value": True, "status": "inferred"}}
    if row_id:
        rec["row_id"] = row_id
    out = normalize_flat_records([rec], NAMES)
    assert out[0]["row_id"] == row_id
    f = out[0]["fields"]
    assert set(f) == {"supplier", "amount", "signed"}
    assert (f["supplier"]["value"], f["supplier"]["status"]) == ("Northwind", "found")
    assert (f["amount"]["value"], f["amount"]["status"]) == (7, "found")
    # an envelope is kept as sent
    assert (f["signed"]["value"], f["signed"]["status"]) == (True, "inferred")


def test_null_flat_value_stays_null():
    out = normalize_flat_records([{"row_id": "r", "amount": None}], NAMES)
    assert out[0]["fields"] == {"amount": None}


@pytest.mark.parametrize("rec", [
    {"row_id": "r", "supplier": "X", "not_a_field": 1},   # unknown key
    {"row_id": "r"},                                      # nothing to wrap
    {"row_id": "r", "fields": {"supplier": None}},        # already wrapped
])
def test_other_shapes_are_left_alone(rec):
    assert normalize_flat_records([rec], NAMES) == [rec]


def test_input_is_never_mutated():
    records = [{"row_id": None, "supplier": "X"}, {"row_id": None, "fields": {}}]
    before = copy.deepcopy(records)
    out = normalize_flat_records(records, NAMES)
    assert records == before
    assert out is not records and out[0] is not records[0]
