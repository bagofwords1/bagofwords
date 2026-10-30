"""Unit tests for SpectrumClient (DX NetOps Spectrum OneClick Web Services).

Covers:
- OneClick URL normalization (bare host, trailing /spectrum/restful)
- Basic vs Bearer auth headers, `Accept: application/xml` on every call,
  XML request bodies with `Content-Type: application/xml`, 401/403/redirect
  (SSO login page) surfaced as actionable errors, HTML answers rejected
- XML request building: landscape scoping, `rs:model-request` filters
  (equals / has-substring, OR for lists, FIND_ALL_DEVICE_MODELS when
  unfiltered), `rs:alarm-request` attribute-filter + target-models +
  alarm-filter, `rs:get-event-request` window/subcomponents/exclusions
- paging: `link rel="next"` followed with GET (models) or re-POST (events),
  stops at EndOfResults and at `limit`
- row shaping: attribute ids → named columns, typed casts, NoSuchAttribute →
  None, enum labels, epoch seconds → ISO, extra `attrs` as columns
- filtered `devices` intersect the API's own device set
- spec validation and the fixed catalog shape

The `requests` boundary is mocked; nothing here needs a OneClick.
"""
from __future__ import annotations

import base64
import json
import re
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree as ET

import pytest

from app.data_sources.clients.spectrum_client import (
    SpectrumClient,
    _CATALOG,
    A_NAME,
    A_SEVERITY,
)

RESP = "http://www.ca.com/spectrum/restful/schema/response"


# ── fake transport ────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, body: str, status_code: int = 200, content_type: str = "application/xml", headers=None):
        self.text = body
        self.content = body.encode("utf-8")
        self.status_code = status_code
        self.headers = {"Content-Type": content_type, **(headers or {})}


class _FakeRequests:
    """Routes `(METHOD, path)`; a route value is a str/_Resp, a list consumed in
    order, or a callable(params, body_xml: Optional[ET.Element], url) → str/_Resp."""

    class exceptions:
        class SSLError(Exception):
            pass

        class RequestException(Exception):
            pass

    def __init__(self, routes: Dict[Tuple[str, str], object]):
        self.routes = routes
        self.calls: List[dict] = []

    def Session(self):
        fake = self

        class _S:
            def request(self, method, url, params=None, data=None, headers=None, verify=None,
                        timeout=None, allow_redirects=None):
                return fake.request(method, url, params, data, headers)

        return _S()

    def request(self, method, url, params, data, headers):
        parts = urlsplit(url)
        q = {k: v for k, v in parse_qs(parts.query).items()}
        for k, v in (params or {}).items():
            q[k] = [str(x) for x in v] if isinstance(v, (list, tuple)) else [str(v)]
        body = ET.fromstring(data) if data else None
        self.calls.append({"method": method, "url": url, "path": parts.path, "params": q,
                           "headers": dict(headers or {}), "body": body, "raw": data})
        for (m, path), val in self.routes.items():
            if m == method and parts.path.endswith(path):
                if isinstance(val, list):
                    val = val.pop(0) if len(val) > 1 else val[0]
                if callable(val):
                    val = val(q, body, url)
                return val if isinstance(val, _Resp) else _Resp(val)
        return _Resp(f'<error-response xmlns="{RESP}" error="not scripted"/>', status_code=404)


@pytest.fixture
def patch_requests(monkeypatch):
    def _install(routes):
        fake = _FakeRequests(routes)
        monkeypatch.setattr("app.data_sources.clients.spectrum_client.requests", fake)
        return fake
    return _install


def _client(**kw):
    defaults = dict(url="https://oneclick.corp.local:8443", username="bow_api", password="pw", throttlesize=2)
    defaults.update(kw)
    return SpectrumClient(**defaults)


# ── XML fixture builders (shapes per TechDocs) ────────────────────────────────

def landscapes_xml():
    return (f'<landscape-response xmlns="{RESP}" total-landscapes="2">'
            '<landscape><id>0x100000</id><name>ss-dc1</name><isPrimary>true</isPrimary><spectrumVersion>24.3.10</spectrumVersion></landscape>'
            '<landscape><id>0x200000</id><name>ss-dc2</name><isPrimary>false</isPrimary><spectrumVersion>24.3.10</spectrumVersion></landscape>'
            '</landscape-response>')


def model_list(models: List[Tuple[str, Dict[str, Optional[str]]]], total=None, end=True, next_href=None):
    out = [f'<model-response-list xmlns="{RESP}" total-models="{total if total is not None else len(models)}" '
           f'throttle="{len(models)}"' + (' error="EndOfResults"' if end else "") + "><model-responses>"]
    for mh, attrs in models:
        out.append(f'<model mh="{mh}">')
        for aid, v in attrs.items():
            out.append(f'<attribute id="{aid}" error="NoSuchAttribute"/>' if v is None else f'<attribute id="{aid}">{v}</attribute>')
        out.append("</model>")
    out.append("</model-responses>")
    if next_href:
        out.append(f'<link rel="next" href="{next_href}" type="application/xml"/>')
    out.append("</model-response-list>")
    return "".join(out)


def alarm_list(alarms: List[Tuple[str, Dict[str, Optional[str]]]], end=True, next_href=None):
    out = [f'<alarm-response-list xmlns="{RESP}" total-alarms="{len(alarms)}" throttle="{len(alarms)}"'
           + (' error="EndOfResults"' if end else "") + "><alarm-responses>"]
    for aid, attrs in alarms:
        out.append(f'<alarm id="{aid}">')
        for k, v in attrs.items():
            out.append(f'<attribute id="{k}" error="NoSuchAttribute"/>' if v is None else f'<attribute id="{k}">{v}</attribute>')
        out.append("</alarm>")
    out.append("</alarm-responses>")
    if next_href:
        out.append(f'<link rel="next" href="{next_href}" type="application/xml"/>')
    out.append("</alarm-response-list>")
    return "".join(out)


def enums_xml(attr_id, pairs):
    return (f'<attribute-enum-response xmlns="{RESP}"><attribute id="{attr_id}">'
            + "".join(f'<enum id="{k}" value="{v}"/>' for k, v in pairs) + "</attribute></attribute-enum-response>")


DEV = {"0x1006e": "rtr-core-01", "0x10000": "Rtr_Cisco", "0x10001": "0x21000c", "0x11ee8": "3", "0x1000a": "5",
       "0x12d7f": "10.1.0.1", "0x23000e": "ASR", "0x133f4": "rtr-core-01.corp", "0x23000d": "DC1", "0x23000c": "noc",
       "0x1290c": "10", "0x1295d": "true", "0x10004": "1", "0x12adb": "Core", "0x129ac": "0x100000", "0x11d42": "ss-dc1"}
DEV2 = {**DEV, "0x1006e": "rtr-core-02", "0x12d7f": "10.1.0.2", "0x1000a": "0"}
PORT = {**DEV, "0x1006e": "rtr-core-01_Gi0/1", "0x10000": "Gen_IF_Port", "0x11ee8": "20", "0x1000a": "6",
        "0x23000e": None, "0x133f4": None}

BASE_ROUTES = {
    ("GET", "/landscapes"): landscapes_xml(),
    ("GET", "/enums"): lambda q, b, url: enums_xml(
        "0x11f56", [(0, "Normal"), (1, "Minor"), (2, "Major"), (3, "Critical")]) if "0x11f56" in url
        else enums_xml("0x11ee8", [(3, "Router"), (20, "Interface")]) if "0x11ee8" in url
        else enums_xml("0x1000a", [(0, "Normal"), (5, "Major"), (6, "Critical")]),
}


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _find(el, name):
    return [c for c in el.iter() if _local(c.tag) == name]


def _leaf_filters(body) -> List[Tuple[str, str, str]]:
    """(op, attr id, value) for every attribute filter leaf in a request body."""
    out = []
    for el in body.iter():
        op = _local(el.tag)
        if op in ("equals", "has-substring", "has-prefix", "greater-than-or-equals", "less-than-or-equals"):
            attr = next((c for c in el if _local(c.tag) == "attribute"), None)
            if attr is not None:
                val = next((v.text for v in attr if _local(v.tag) == "value"), None)
                out.append((op, attr.get("id"), val))
    return out


# ── URL / auth / transport ───────────────────────────────────────────────────

@pytest.mark.parametrize("url", ["oneclick.corp.local:8443", "https://oneclick.corp.local:8443/",
                                 "https://oneclick.corp.local:8443/spectrum/restful",
                                 "https://oneclick.corp.local:8443/spectrum/restful/"])
def test_url_normalization(url):
    assert _client(url=url).base_url == "https://oneclick.corp.local:8443"


def test_basic_auth_and_xml_accept_on_every_call(patch_requests):
    fake = patch_requests(BASE_ROUTES)
    _client().execute_query('{"table": "landscapes"}')
    call = fake.calls[0]
    assert call["url"].startswith("https://oneclick.corp.local:8443/spectrum/restful/")
    assert call["headers"]["Authorization"] == "Basic " + base64.b64encode(b"bow_api:pw").decode()
    assert call["headers"]["Accept"] == "application/xml"


def test_bearer_token_header(patch_requests):
    fake = patch_requests(BASE_ROUTES)
    _client(username=None, password=None, token="eyJ.jwt.sig").execute_query('{"table": "landscapes"}')
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer eyJ.jwt.sig"


def test_missing_credentials_is_actionable(patch_requests):
    patch_requests(BASE_ROUTES)
    with pytest.raises(RuntimeError, match="local user|bearer token"):
        _client(username=None, password=None).execute_query('{"table": "landscapes"}')


def test_401_403_and_sso_redirect_are_explained(patch_requests):
    patch_requests({("GET", "/landscapes"): _Resp("", status_code=401)})
    with pytest.raises(RuntimeError, match="401.*local user database"):
        _client().execute_query('{"table": "landscapes"}')
    patch_requests({("GET", "/landscapes"): _Resp("", status_code=403)})
    with pytest.raises(RuntimeError, match="403"):
        _client().execute_query('{"table": "landscapes"}')
    patch_requests({("GET", "/landscapes"): _Resp("", status_code=302, headers={"Location": "https://idp/login"})})
    with pytest.raises(RuntimeError, match="redirected.*LOCAL"):
        _client().execute_query('{"table": "landscapes"}')


def test_html_answer_is_rejected(patch_requests):
    patch_requests({("GET", "/landscapes"): _Resp("<html><body>login</body></html>", content_type="text/html")})
    with pytest.raises(RuntimeError, match="non-XML"):
        _client().execute_query('{"table": "landscapes"}')


def test_ca_bundle_and_verify_ssl():
    assert _client(ca_bundle="/etc/ssl/corp.pem", verify_ssl=True)._verify == "/etc/ssl/corp.pem"
    assert _client(ca_bundle="/etc/ssl/corp.pem", verify_ssl=False)._verify is False
    assert _client(verify_ssl=True)._verify is True


def test_test_connection_reports_version_landscapes_and_device_count(patch_requests):
    fake = patch_requests({**BASE_ROUTES, ("POST", "/models"): model_list([("0x100010", {A_NAME: "r"})], total=42)})
    out = _client().test_connection()
    assert out["success"] is True
    assert "24.3.10" in out["message"] and "ss-dc1" in out["message"] and "42" in out["message"]
    post = next(c for c in fake.calls if c["method"] == "POST")
    assert post["headers"]["Content-Type"].startswith("application/xml")
    assert post["raw"].startswith(b"<?xml")


def test_test_connection_failure_is_a_message_not_an_exception(patch_requests):
    patch_requests({("GET", "/landscapes"): _Resp("", status_code=401)})
    out = _client().test_connection()
    assert out["success"] is False and "401" in out["message"]


# ── catalog ───────────────────────────────────────────────────────────────────

def test_get_schemas_fixed_catalog_survives_api_errors(patch_requests):
    patch_requests({("GET", "/landscapes"): _Resp("", status_code=500)})
    tables = _client().get_schemas()
    assert [t.name for t in tables] == list(_CATALOG)
    by = {t.name: t for t in tables}
    assert [c.name for c in by["alarms"].pks] == ["alarm_id"]
    assert {fk.references_name for fk in by["alarms"].fks} == {"models", "landscapes"}
    assert {fk.references_name for fk in by["connectivity"].fks} == {"devices"}


def test_get_schemas_enriches_descriptions_and_reports_progress(patch_requests):
    patch_requests({**BASE_ROUTES,
                    ("POST", "/models"): model_list([("0x100010", {A_NAME: "r"})], total=1234),
                    ("POST", "/alarms/count"): f'<alarmcount-response xmlns="{RESP}"><critical>4</critical><major>2</major><minor>1</minor></alarmcount-response>'})
    seen = []
    tables = _client().get_schemas(progress_callback=lambda *a: seen.append(a))
    by = {t.name: t for t in tables}
    assert "ss-dc1" in by["landscapes"].description and "primary" in by["landscapes"].description
    assert "1234" in by["devices"].description
    assert "4 critical" in by["alarms"].description
    assert seen


def test_get_schema_unknown_table():
    with pytest.raises(ValueError, match="Unknown DX NetOps Spectrum table"):
        _client().get_schema("nope")


# ── request building ─────────────────────────────────────────────────────────

def test_unfiltered_devices_use_find_all_device_models_action(patch_requests):
    fake = patch_requests({**BASE_ROUTES, ("POST", "/models"): model_list([("0x100010", DEV)])})
    df = _client().execute_query('{"table": "devices"}')
    body = next(c for c in fake.calls if c["path"].endswith("/models"))["body"]
    assert _local(body.tag) == "model-request"
    assert [a.text for a in _find(body, "action")] == ["FIND_ALL_DEVICE_MODELS"]
    assert [m.text for m in _find(body, "model-type")] == ["SearchManager"]
    requested = [a.get("id") for a in _find(body, "requested-attribute")]
    assert A_NAME in requested and "0x12d7f" in requested
    assert len(df) == 1 and df.iloc[0]["name"] == "rtr-core-01" and df.iloc[0]["model_class_label"] == "Router"
    assert df.iloc[0]["condition_label"] == "Major" and bool(df.iloc[0]["is_managed"]) is True


def test_filtered_devices_build_and_or_filters_and_keep_only_devices(patch_requests):
    # GET devices (handles only) defines what a device is; the filtered search also
    # matches a port named rtr-core-01_Gi0/1 which must be dropped.
    fake = patch_requests({
        **BASE_ROUTES,
        ("GET", "/devices"): model_list([("0x100010", {}), ("0x100011", {})]),
        ("POST", "/models"): model_list([("0x100010", DEV), ("0x101002", PORT), ("0x100011", DEV2)]),
    })
    df = _client(landscapes="ss-dc1").execute_query(
        '{"table": "devices", "name_contains": "rtr-core", "model_type": ["Rtr_Cisco", "SwCiscoIOS"]}')
    assert list(df["name"]) == ["rtr-core-01", "rtr-core-02"]
    post = next(c for c in fake.calls if c["method"] == "POST")
    leaves = _leaf_filters(post["body"])
    assert ("has-substring", A_NAME, "rtr-core") in leaves
    assert ("equals", "0x10000", "Rtr_Cisco") in leaves and ("equals", "0x10000", "SwCiscoIOS") in leaves
    ors = _find(post["body"], "or")
    assert len(ors) == 1 and len(list(ors[0])) == 2
    # the connection-level landscape name resolved to its handle on both requests
    assert [l.get("id") for l in _find(post["body"], "landscape")] == ["0x100000"]
    get_dev = next(c for c in fake.calls if c["path"].endswith("/devices"))
    assert get_dev["params"]["landscape"] == ["0x100000"]


def test_models_by_handle_list_targets_models_directly(patch_requests):
    fake = patch_requests({**BASE_ROUTES, ("POST", "/models"): model_list([("0x100010", DEV)])})
    _client().execute_query('{"table": "models", "mh": ["0x100010", "0x100011"]}')
    body = next(c for c in fake.calls if c["method"] == "POST")["body"]
    assert [m.get("mh") for m in _find(body, "model")] == ["0x100010", "0x100011"]
    assert not _find(body, "models-search")


def test_models_requires_a_filter():
    with pytest.raises(ValueError, match="at least one filter"):
        _client().execute_query('{"table": "models"}')


def test_extra_attrs_become_columns_and_missing_attrs_are_none(patch_requests):
    patch_requests({**BASE_ROUTES, ("POST", "/models"): model_list([("0x100010", {**DEV, "0x10032": "9123", "0xbeef": None})])})
    df = _client().execute_query('{"table": "devices", "attrs": ["0x10032", "0xbeef"]}')
    assert df.iloc[0]["sys_uptime"] == "9123"
    assert df.iloc[0]["attr_0xbeef"] is None


def test_interfaces_search_by_device_name_prefix(patch_requests):
    def models(q, body, url):
        leaves = _leaf_filters(body)
        if ("equals", A_NAME, "rtr-core-01") in leaves:
            return model_list([("0x100010", DEV)])
        assert ("has-substring", A_NAME, "rtr-core-01_") in leaves
        return model_list([("0x101002", PORT), ("0x100010", DEV)])
    patch_requests({**BASE_ROUTES, ("POST", "/models"): models})
    df = _client().execute_query('{"table": "interfaces", "device": "rtr-core-01"}')
    assert list(df["name"]) == ["rtr-core-01_Gi0/1"]          # the device itself is excluded
    assert df.iloc[0]["device_mh"] == "0x100010" and df.iloc[0]["condition_label"] == "Critical"


def test_model_attributes_long_format(patch_requests):
    patch_requests({**BASE_ROUTES,
                    ("POST", "/models"): model_list([("0x100014", {**DEV, A_NAME: "fw-edge-01"})]),
                    ("GET", "/model/0x100014"): f'<model-response xmlns="{RESP}"><model mh="0x100014">'
                                                '<attribute id="0x10032">9123</attribute><attribute id="0xdead" error="NoSuchAttribute"/>'
                                                '</model></model-response>'})
    df = _client().execute_query('{"table": "model_attributes", "name": "fw-edge-01", "attrs": ["0x10032", "0xdead"]}')
    assert df.to_dict("records") == [
        {"mh": "0x100014", "model_name": "fw-edge-01", "attr_id": "0x10032", "attr_name": "sys_uptime", "value": "9123"},
        {"mh": "0x100014", "model_name": "fw-edge-01", "attr_id": "0xdead", "attr_name": "", "value": None},
    ]


# ── alarms ────────────────────────────────────────────────────────────────────

ALARM = {"0x11f9c": "0x1000003", "0x11f56": "3", "0x12b4c": "LINK DOWN", "0x11f4e": "1793330400",
         "0x1321a": "1793330520", "0x11fc5": "1", "0x11f4d": "false", "0x11f4f": "", "0x11f50": "0x10d35",
         "0x11f54": "true", "0x11fc4": "0", "0x11f57": None, "0x12022": "", "0x1296e": "0x10d35",
         "0x12a06": "3", "0x12a04": "0x1000004,0x1000005", "0x12a07": "0", "0x12a05": "", "0x12a63": "https://oc/a",
         "0x11f53": "0x101002", "0x1006e": "rtr-core-01_Gi0/1", "0x10000": "Gen_IF_Port", "0x11ee8": "20",
         "0x12d7f": "10.1.0.1", "0x129ac": "0x100000", "0x11d42": "ss-dc1"}
ALARM2 = {**ALARM, "0x11f9c": "0x1000004", "0x11f56": "2", "0x11f54": "false", "0x12a05": "0x1000003",
          "0x11f4e": "1793330520"}


def test_alarm_request_filters_severity_ack_title_window_scope_and_filter(patch_requests):
    fake = patch_requests({**BASE_ROUTES, ("POST", "/alarms"): alarm_list([("0x1000003", ALARM)])})
    df = _client().execute_query(json.dumps({
        "table": "alarms", "severity": ["critical", 2], "acknowledged": False, "title_contains": "LINK",
        "start_time": 1793330000000, "end_time": 1793340000000, "name": "rtr-core-01_Gi0/1",
        "alarm_filter": "Core Network", "primary_only": True, "since_hours": 3}))
    call = next(c for c in fake.calls if c["path"].endswith("/alarms"))
    assert call["params"]["symptoms"] == ["no"] and call["params"]["lasthour"] == ["3"]
    body = call["body"]
    assert _local(body.tag) == "alarm-request"
    leaves = _leaf_filters(body)
    assert ("equals", A_SEVERITY, "3") in leaves and ("equals", A_SEVERITY, "2") in leaves
    assert ("equals", "0x11f4d", "false") in leaves
    assert ("has-substring", "0x12b4c", "LINK") in leaves
    assert ("greater-than-or-equals", "0x11f4e", "1793330000") in leaves   # epoch ms → seconds
    assert ("less-than-or-equals", "0x11f4e", "1793340000") in leaves
    assert ("equals", A_NAME, "rtr-core-01_Gi0/1") in leaves              # inside target-models
    assert _find(body, "target-models") and _find(body, "models-search")
    assert [f.get("name") for f in _find(body, "alarm-filter")] == ["Core Network"]
    row = df.iloc[0]
    assert row["alarm_id"] == "0x1000003" and row["severity"] == 3 and row["severity_label"] == "Critical"
    assert row["created_at"] == "2026-10-30T03:20:00+00:00" and row["created_epoch"] == 1793330400
    assert bool(row["is_primary"]) is True and row["symptom_alarm_ids"] == "0x1000004,0x1000005"
    assert row["troubleshooter"] is None and row["model_name"] == "rtr-core-01_Gi0/1"


def test_alarm_ids_and_model_handles_target_directly(patch_requests):
    fake = patch_requests({**BASE_ROUTES, ("POST", "/alarms"): alarm_list([("0x1000004", ALARM2)])})
    df = _client().execute_query('{"table": "alarms", "alarm_id": ["0x1000004"], "model_mh": ["0x100014"]}')
    body = fake.calls[-1]["body"] if fake.calls[-1]["method"] == "POST" else next(c for c in fake.calls if c["method"] == "POST")["body"]
    assert [a.get("id") for a in _find(body, "alarm")] == ["0x1000004"]
    assert [m.get("mh") for m in _find(body, "model")] == ["0x100014"]
    assert bool(df.iloc[0]["is_primary"]) is False and df.iloc[0]["cause_alarm_ids"] == "0x1000003"


def test_min_severity_expands_to_or_of_levels(patch_requests):
    fake = patch_requests({**BASE_ROUTES, ("POST", "/alarms"): alarm_list([])})
    _client().execute_query('{"table": "alarms", "min_severity": "major"}')
    leaves = _leaf_filters(next(c for c in fake.calls if c["method"] == "POST")["body"])
    assert {v for op, a, v in leaves if a == A_SEVERITY} == {"2", "3"}


def test_unknown_severity_name_rejected(patch_requests):
    patch_requests(BASE_ROUTES)
    with pytest.raises(ValueError, match="severity"):
        _client().execute_query('{"table": "alarms", "severity": "purple"}')


def test_alarms_sorted_oldest_first_and_paged_until_end(patch_requests):
    first = alarm_list([("0x1000004", ALARM2)], end=False,
                       next_href="https://oneclick.corp.local:8443/spectrum/restful/alarms?start=1&amp;throttlesize=1")
    second = alarm_list([("0x1000003", ALARM)])
    fake = patch_requests({**BASE_ROUTES, ("POST", "/alarms"): first, ("GET", "/alarms"): second})
    df = _client(throttlesize=1).execute_query('{"table": "alarms"}')
    assert list(df["alarm_id"]) == ["0x1000003", "0x1000004"]
    nxt = next(c for c in fake.calls if c["method"] == "GET" and c["path"].endswith("/alarms"))
    assert nxt["params"]["start"] == ["1"] and nxt["params"]["throttlesize"] == ["1"]


def test_alarm_counts_and_filters(patch_requests):
    fake = patch_requests({**BASE_ROUTES,
                           ("POST", "/alarms/count"): f'<alarmcount-response xmlns="{RESP}"><critical>4</critical><major>2</major><minor>1</minor></alarmcount-response>',
                           ("GET", "/alarms/filters"): f'<alarmfilters-response xmlns="{RESP}"><alarmfilter>All</alarmfilter><alarmfilter>Core</alarmfilter></alarmfilters-response>'})
    df = _client().execute_query('{"table": "alarm_counts", "alarm_filter": "Core"}')
    assert df.to_dict("records") == [{"alarm_filter": "Core", "critical": 4, "major": 2, "minor": 1}]
    assert [f.get("name") for f in _find(fake.calls[-1]["body"], "alarm-filter")] == ["Core"]
    assert list(_client().execute_query('{"table": "alarm_filters"}')["name"]) == ["All", "Core"]


# ── events ────────────────────────────────────────────────────────────────────

def events_xml(events, end=True, next_href=None):
    out = [f'<get-event-response-list xmlns="{RESP}" total-events="{len(events)}" throttle="{len(events)}"'
           + (' error="EndOfResults"' if end else "") + "><event-responses>"]
    for eid, mh, ts, etype, attrs in events:
        out.append(f'<event id="{eid}" mh="{mh}" time="{ts}" type="{etype}">')
        out += [f'<attribute id="{k}">{v}</attribute>' for k, v in attrs.items()]
        out.append("</event>")
    out.append("</event-responses>")
    if next_href:
        out.append(f'<link rel="next" href="{next_href}" type="application/xml"/>')
    out.append("</get-event-response-list>")
    return "".join(out)


def test_events_request_window_scope_and_repost_paging(patch_requests):
    page1 = events_xml([("0xe2", "0x100010", 1793330460000, "0x10e14", {"0x4820007": "CPU 88%", "0x11f56": "2", "0x1006e": "rtr-core-01"})],
                       end=False, next_href="https://oneclick.corp.local:8443/spectrum/restful/events/getEvents?start=1&amp;throttlesize=1")
    page2 = events_xml([("0xe1", "0x101002", 1793330400000, "0x10d35", {"0x4820007": "LINK DOWN", "0x11f56": "3", "0x1006e": "rtr-core-01_Gi0/1"})])
    fake = patch_requests({**BASE_ROUTES,
                           ("POST", "/models"): model_list([("0x100010", DEV)]),
                           ("POST", "/events/getEvents"): [page1, page2]})
    df = _client(throttlesize=1).execute_query(json.dumps({
        "table": "events", "device": "rtr-core-01", "start_time": 1793329200000, "end_time": 1793331000000,
        "include_interfaces": True, "exclude_types": ["0x10d26", "0x10d27"]}))
    posts = [c for c in fake.calls if c["path"].endswith("/events/getEvents")]
    assert len(posts) == 2 and posts[1]["params"]["start"] == ["1"]
    assert posts[0]["raw"] == posts[1]["raw"]                      # the next page re-POSTs the same body
    body = posts[0]["body"]
    f = _find(body, "get-events-filter")[0]
    assert f.get("start-time") == "1793329200000" and f.get("end-time") == "1793331000000"
    assert f.get("subcomponents-events") == "true" and f.get("exclude-events") == "0x10d26,0x10d27"
    assert [m.get("mh") for m in _find(body, "model")] == ["0x100010"]
    assert [l.get("id") for l in _find(body, "landscape")] == ["0x100000"]
    assert list(df["event_id"]) == ["0xe1", "0xe2"]                 # sorted by time
    assert df.iloc[0]["time_at"] == "2026-10-30T03:20:00+00:00" and df.iloc[0]["severity_label"] == "Critical"


def test_events_reject_two_landscapes_and_unknown_device(patch_requests):
    patch_requests({**BASE_ROUTES, ("POST", "/models"): model_list([("0x100010", DEV), ("0x200010", {**DEV, "0x129ac": "0x200000"})])})
    with pytest.raises(ValueError, match="one landscape"):
        _client().execute_query('{"table": "events", "device": ["rtr-core-01", "rtr-core-11"]}')
    patch_requests({**BASE_ROUTES, ("POST", "/models"): model_list([])})
    with pytest.raises(ValueError, match="No model matched"):
        _client().execute_query('{"table": "events", "device": "ghost"}')


def test_events_window_validation():
    with pytest.raises(ValueError, match="epoch-MILLISECOND"):
        _client().execute_query('{"table": "events", "device": "x", "start_time": -1}')
    with pytest.raises(ValueError, match="positive integer"):
        _client().execute_query('{"table": "events", "device": "x", "duration_in_mins": 0}')


# ── topology ──────────────────────────────────────────────────────────────────

def test_connectivity_rows(patch_requests):
    conn = (f'<connection-response-list xmlns="{RESP}"><connection-response>'
            '<connection-element-left><ipaddress>10.1.0.1</ipaddress><mh>0x101002</mh><name>rtr-core-01_Gi0/1</name><type>Gen_IF_Port</type><class>Interface</class><condition>6</condition></connection-element-left>'
            '<connection-element-right><ipaddress>10.1.2.1</ipaddress><mh>0x100014</mh><name>fw-edge-01</name><type>GnSNMPDev</type><class>Firewall</class><condition>6</condition></connection-element-right>'
            '<connection-element-right-port><mh>0x10100e</mh><name>fw-edge-01_eth1/1</name></connection-element-right-port>'
            '</connection-response></connection-response-list>')
    fake = patch_requests({**BASE_ROUTES, ("POST", "/models"): model_list([("0x100010", DEV)]),
                           ("GET", "/connectivity/10.1.0.1"): conn})
    df = _client().execute_query('{"table": "connectivity", "name": "rtr-core-01"}')
    row = df.iloc[0]
    assert row["device_mh"] == "0x100010" and row["local_port"] == "rtr-core-01_Gi0/1" and row["local_port_condition"] == 6
    assert row["neighbor"] == "fw-edge-01" and row["neighbor_class"] == "Firewall" and row["neighbor_port"] == "fw-edge-01_eth1/1"
    assert any(c["path"].endswith("/connectivity/10.1.0.1") for c in fake.calls)


def test_associations_resolve_names_and_validate_relation(patch_requests):
    assoc = (f'<association-response-list xmlns="{RESP}" total-associations="1"><association relation="0x10002">'
             '<leftmh>0x100001</leftmh><rightmh>0x100010</rightmh></association></association-response-list>')

    def models(q, body, url):
        handles = [m.get("mh") for m in _find(body, "model")]
        if handles:
            return model_list([(h, {A_NAME: {"0x100001": "DC1 Site", "0x100010": "rtr-core-01"}[h]}) for h in handles])
        return model_list([("0x100010", DEV)])
    fake = patch_requests({**BASE_ROUTES, ("POST", "/models"): models, ("GET", "/model/0x100010"): assoc})
    df = _client().execute_query('{"table": "associations", "relation": "0x10002", "name": "rtr-core-01", "side": "right"}')
    assert df.to_dict("records") == [{"relation": "0x10002", "left_mh": "0x100001", "left_name": "DC1 Site",
                                      "right_mh": "0x100010", "right_name": "rtr-core-01"}]
    get = next(c for c in fake.calls if "/associations/" in c["path"])
    assert get["path"].endswith("/associations/relation/0x10002/model/0x100010") and get["params"]["side"] == ["right"]
    with pytest.raises(ValueError, match="hex relation handle"):
        _client().execute_query('{"table": "associations", "relation": "Collects", "name": "x"}')


def test_attribute_enums(patch_requests):
    patch_requests(BASE_ROUTES)
    df = _client().execute_query('{"table": "attribute_enums", "attr_id": "0x11f56"}')
    assert list(df["label"]) == ["Normal", "Minor", "Major", "Critical"]
    with pytest.raises(ValueError, match="hex attribute id"):
        _client().execute_query('{"table": "attribute_enums", "attr_id": "severity"}')


def test_enum_fallback_when_endpoint_unavailable(patch_requests):
    patch_requests({**BASE_ROUTES, ("GET", "/enums"): _Resp("", status_code=500),
                    ("POST", "/alarms"): alarm_list([("0x1000003", ALARM)])})
    df = _client().execute_query('{"table": "alarms"}')
    assert df.iloc[0]["severity_label"] == "Critical"


# ── paging / limits / validation ─────────────────────────────────────────────

def test_models_next_link_followed_with_get_and_limit_respected(patch_requests):
    first = model_list([("0x1", {A_NAME: "a"}), ("0x2", {A_NAME: "b"})], total=5, end=False,
                       next_href="https://oneclick.corp.local:8443/spectrum/restful/models?id=rs1&amp;start=2&amp;throttlesize=2")
    second = model_list([("0x3", {A_NAME: "c"}), ("0x4", {A_NAME: "d"})], total=5, end=False,
                        next_href="https://oneclick.corp.local:8443/spectrum/restful/models?id=rs1&amp;start=4&amp;throttlesize=2")
    fake = patch_requests({**BASE_ROUTES, ("POST", "/models"): first, ("GET", "/models"): second})
    df = _client().execute_query('{"table": "devices", "limit": 3}')
    assert list(df["name"]) == ["a", "b", "c"]
    gets = [c for c in fake.calls if c["method"] == "GET" and c["path"].endswith("/models")]
    assert len(gets) == 1 and gets[0]["params"]["id"] == ["rs1"] and gets[0]["params"]["start"] == ["2"]
    post = next(c for c in fake.calls if c["method"] == "POST")
    assert post["body"].get("throttlesize") == "2"                    # min(throttlesize, limit)


def test_unknown_table_and_fields_rejected():
    with pytest.raises(ValueError, match="Unknown DX NetOps Spectrum table"):
        _client().execute_query('{"table": "metrics"}')
    with pytest.raises(ValueError, match="Unsupported fields for alarms: foo"):
        _client().execute_query('{"table": "alarms", "foo": 1}')
    with pytest.raises(ValueError, match="JSON object"):
        _client().execute_query("SELECT 1")
    with pytest.raises(ValueError, match="attrs must be hex"):
        _client().execute_query('{"table": "devices", "attrs": ["name"]}')
    with pytest.raises(ValueError, match="boolean"):
        _client().execute_query('{"table": "alarms", "acknowledged": "yes"}')


def test_unknown_landscape_lists_known_ones(patch_requests):
    patch_requests(BASE_ROUTES)
    with pytest.raises(ValueError, match="Unknown landscape 'dc9'.*ss-dc1"):
        _client().execute_query('{"table": "devices", "landscape": "dc9"}')


def test_query_alias_and_kwargs(patch_requests):
    patch_requests(BASE_ROUTES)
    assert len(_client().query(table="landscapes")) == 2
