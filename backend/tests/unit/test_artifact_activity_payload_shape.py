"""Payload bounding and the data-free "shape" the agent gets when the org
hides data from the model (services/artifact_activity.py)."""
import json

from app.services import artifact_activity as svc


def test_clean_payload_keeps_the_three_parts_bounded_and_drops_the_rest():
    raw = {"request": {"method": "records", "resource": "orders"}, "response": {"rows": [{"a": 1}]},
           "stream": [{"type": "text_delta", "text": "x" * 2000, "extra": {"deep": list(range(2000))}}], "other": 1}
    out = svc.clean_payload(raw)
    assert set(out) == {"request", "response", "stream"}
    assert out["stream"][0]["text"].endswith("…") and len(out["stream"][0]["text"]) == svc.MAX_STREAM_TEXT + 1
    assert out["stream"][0]["extra"]["truncated"] is True
    assert svc.clean_payload({"request": None}) is None and svc.clean_payload("nope") is None


def test_a_long_stream_is_cut_and_says_so():
    events = [{"type": "text_delta", "text": "a"} for _ in range(500)]
    out = svc.clean_payload({"stream": events})["stream"]
    assert len(out) == svc.MAX_STREAM_EVENTS + 1 and out[-1] == {"type": "truncated", "events_total": 500}


def test_a_big_grid_keeps_its_first_rows_and_anything_else_becomes_a_stub():
    grid = {"columns": [{"field": "n"}], "rows": [{"n": "x" * 100, "i": i} for i in range(2000)]}
    out = svc._bound(grid)
    assert out["rows"][0]["i"] == 0 and out["truncated"]["rows_total"] == 2000
    assert len(json.dumps(out)) <= svc.MAX_PAYLOAD_CHARS
    blob = svc._bound({"text": "y" * 100_000})
    assert blob["truncated"] is True and blob["chars"] > svc.MAX_PAYLOAD_CHARS and len(blob["preview"]) < svc.MAX_PAYLOAD_CHARS


def test_shape_has_keys_types_and_counts_but_no_values():
    payload = {"request": {"method": "records", "filters": {"region": "north", "ids": [1, 2, 3]}, "limit": 50, "dry": False},
               "response": {"columns": [{"field": "name"}, {"field": "total"}], "rows": [{"name": "Dana", "total": 3}] * 7},
               "stream": [{"type": "started"}, {"type": "text_delta", "text": "hello"}, {"type": "text", "text": "hello world"},
                          {"type": "completed"}]}
    shape = svc.shape(payload)
    assert shape["request"] == {"method": "str(7)", "filters": {"region": "str(5)", "ids": {"list": 3, "item": "number"}},
                                "limit": "number", "dry": "bool"}
    assert shape["response"] == {"rows": 7, "columns": ["name", "total"]}
    assert shape["stream"] == {"events": 4, "types": {"started": 1, "text_delta": 1, "text": 1, "completed": 1},
                               "text_chars": 16, "ended": "completed"}
    assert "Dana" not in json.dumps(shape) and "north" not in json.dumps(shape)
