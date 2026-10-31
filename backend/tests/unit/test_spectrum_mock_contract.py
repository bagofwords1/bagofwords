"""Synthetic client-to-ASGI contract checks for the Spectrum connector.

Pins `SpectrumClient` to `tools/spectrum/mock_oneclick.py`, whose bodies are
shaped after the XML TechDocs reproduces for each OneClick noun. The mock is
an external boundary, not evidence of compatibility with a deployed OneClick;
its undocumented assumptions are listed in its module docstring.
"""
import base64
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.data_sources.clients.spectrum_client import SpectrumClient, _CATALOG


@pytest.fixture
def oneclick(monkeypatch):
    path = Path(__file__).resolve().parents[3] / "tools/spectrum/mock_oneclick.py"
    spec = importlib.util.spec_from_file_location("synthetic_oneclick_mock", path)
    mock = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mock)
    with TestClient(mock.app) as http:
        class Session:
            def request(self, method, url, **kwargs):
                for k in ("verify", "timeout", "allow_redirects"):
                    kwargs.pop(k, None)
                return http.request(method, url, **kwargs)
        monkeypatch.setattr("app.data_sources.clients.spectrum_client.requests.Session", Session)
        yield mock, http


def _client(mock, **kw):
    defaults = dict(url="http://testserver", username="bow_api", password="Spectrum!2024", throttlesize=3)
    defaults.update(kw)
    return SpectrumClient(**defaults)


def test_every_catalog_table_round_trips(oneclick):
    mock, _ = oneclick
    client = _client(mock)
    assert client.test_connection()["success"]
    tables = client.get_schemas()
    assert [t.name for t in tables] == list(_CATALOG)
    window = {"start_time": mock.T0 - 20 * mock.MIN, "end_time": mock.T0 + 10 * mock.MIN}
    specs = {
        "landscapes": {}, "devices": {}, "models": {"name_contains": "rtr-core-01_"},
        "interfaces": {"device": "rtr-core-01"},
        "model_attributes": {"name": "fw-edge-01", "attrs": ["0x10032"]},
        "alarms": {"min_severity": 2}, "alarm_counts": {}, "alarm_filters": {},
        "events": {"device": ["rtr-core-01", "fw-edge-01"], "include_interfaces": True, **window},
        "associations": {"relation": mock.IF_RELATION, "name": "rtr-core-01"},
        "connectivity": {"name": "rtr-core-01"},
        "attribute_enums": {"attr_id": "0x11f56"},
    }
    for table, options in specs.items():
        df = client.execute_query({"table": table, **options})
        assert not df.empty, table
        assert set(df.columns) >= {c for c, _ in _CATALOG[table]["columns"]}, table
    # Paging through the stored result set (throttlesize 3 < 13 devices) is transparent.
    assert len(client.execute_query({"table": "devices"})) == len([m for m in mock.MODELS.values() if m.get("_kind") == "device"])


def test_seeded_incident_is_reconstructable(oneclick):
    """The root cause alarm, its symptoms and the earliest deviating event agree with SCENARIO."""
    mock, _ = oneclick
    client = _client(mock)
    primary = client.execute_query({"table": "alarms", "min_severity": 3, "primary_only": True})
    assert list(primary["alarm_id"]) == [mock.SCENARIO["root_cause_alarm"]]
    symptoms = client.execute_query({"table": "alarms", "alarm_id": primary.iloc[0]["symptom_alarm_ids"].split(",")})
    assert set(symptoms["model_name"]) == {"fw-edge-01", "sw-acc-11", "sw-acc-12"}
    assert (symptoms["cause_alarm_ids"] == mock.SCENARIO["root_cause_alarm"]).all()
    events = client.execute_query({"table": "events", "device": "rtr-core-01", "include_interfaces": True,
                                   "start_time": mock.T0 - 30 * mock.MIN, "end_time": mock.T0 + 5 * mock.MIN,
                                   "exclude_types": ["0x10d26"]})
    assert events.iloc[0]["event_type"] == "0x10e0c"            # utilisation climbs first…
    assert "0x10d35" in set(events["event_type"])               # …then the link goes down
    nb = client.execute_query({"table": "connectivity", "name": "rtr-core-01"})
    down = nb[nb["local_port_condition"] == 6]
    assert list(down["neighbor"]) == ["fw-edge-01"]


def test_mock_enforces_auth_read_only_and_xml_input(oneclick):
    mock, http = oneclick
    assert http.get("/spectrum/restful/landscapes").status_code == 401
    basic = {"Authorization": "Basic " + base64.b64encode(b"bow_api:Spectrum!2024").decode()}
    assert http.get("/spectrum/restful/landscapes", headers=basic).status_code == 200
    assert http.get("/spectrum/restful/landscapes", headers={"Authorization": f"Bearer {mock.STATIC_TOKEN}"}).status_code == 200
    assert http.put("/spectrum/restful/alarms/0x1000001?attr=0x11f4d&val=true", headers=basic).status_code == 403
    assert http.post("/spectrum/restful/models", json={"x": 1}, headers=basic).status_code == 415
    # Bad credentials surface through the client as a message, not a crash.
    assert _client(mock, password="wrong").test_connection()["success"] is False
    assert _client(mock, username=None, password=None, token=mock.STATIC_TOKEN).test_connection()["success"]
    assert _client(mock, username=None, password=None, token="forged").test_connection()["success"] is False
