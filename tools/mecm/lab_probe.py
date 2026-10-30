#!/usr/bin/env python3
"""Read-only probe of a real Configuration Manager AdminService.

Checks every assumption `MecmClient` (backend/app/data_sources/clients/mecm_client.py)
makes about the administration service, and prints a reconciliation report:
what the real site does vs what the client and the simulator
(tools/mecm/mock_adminservice.py) assume. Run it once against a lab before
trusting the connector there, and paste the report into the PR / issue.

Only GET requests are sent. Nothing is changed on the site.

    cd backend
    MECM_URL=cm01.contoso.com MECM_USER='CONTOSO\\svc_bow' MECM_PASSWORD=... \
    MECM_CA_FILE=/path/to/provider.pem \
      uv run python ../tools/mecm/lab_probe.py [--json report.json]

Env:  MECM_URL (SMS Provider host or https://host/AdminService), MECM_USER,
      MECM_PASSWORD, MECM_CA_FILE (PEM; optional), MECM_VERIFY=0 to skip TLS
      verification (lab only).
Exit code is 0 when every REQUIRED check passes.
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.data_sources.clients.mecm_client import (  # noqa: E402
    _DEFAULT_CLASSES,
    MecmClient,
    MecmError,
    parse_metadata,
)

RESULTS = []


def check(name, required, fn):
    """Run one probe; record (name, required, ok, observed)."""
    t = time.monotonic()
    try:
        ok, observed = fn()
    except Exception as e:  # noqa: BLE001 - a probe reports, never raises
        ok, observed = False, f"{type(e).__name__}: {str(e)[:300]}"
    RESULTS.append({"check": name, "required": required, "ok": bool(ok), "observed": observed,
                    "ms": round((time.monotonic() - t) * 1000)})
    mark = "PASS" if ok else ("FAIL" if required else "note")
    print(f"[{mark:4}] {name}: {observed}")


def raw_get(client, path, accept="application/json"):
    return client._get(client._url(path), accept=accept, what=path)


def main():
    pem = Path(os.environ["MECM_CA_FILE"]).read_text() if os.environ.get("MECM_CA_FILE") else None
    client = MecmClient(server_url=os.environ["MECM_URL"], username=os.environ.get("MECM_USER"),
                        password=os.environ.get("MECM_PASSWORD"), ca_certificate=pem,
                        verify_ssl=os.environ.get("MECM_VERIFY", "1") != "0")
    print(f"AdminService root: {client.base_url}\n")
    state = {}

    def connection():
        r = client.test_connection()
        return r["success"], r["message"]
    check("test_connection (SMS_Site)", True, connection)

    def wmi_metadata():
        text = raw_get(client, "wmi/$metadata", "application/xml").text
        parsed = parse_metadata(text)
        state["wmi"] = parsed
        state["wmi_bytes"] = len(text)
        # A real site exposes ~700 classes; the simulator 45.
        return len(parsed) > 0, f"{len(parsed)} entity sets, {len(text) // 1024} KiB"
    check("wmi/$metadata parses into entity sets", True, wmi_metadata)

    def curated_present():
        names = {n.lower() for n in state.get("wmi", {})}
        missing = [c for c in _DEFAULT_CLASSES if c.lower() not in names]
        return not missing, f"missing: {missing}" if missing else f"all {len(_DEFAULT_CLASSES)} curated classes present"
    check("curated default classes exist", True, curated_present)

    def keys():
        wmi = state.get("wmi", {})
        lower = {n.lower(): n for n in wmi}
        out = {c: wmi[lower[c.lower()]]["keys"] for c in _DEFAULT_CLASSES if c.lower() in lower}
        no_key = [c for c, k in out.items() if not k]
        return not no_key, json.dumps(out)
    check("curated classes declare keys", False, keys)

    def join_columns():
        wmi = state.get("wmi", {})
        props = {n: {p["name"] for p in v["properties"]} for n, v in wmi.items()}
        want = {"SMS_R_System": "ResourceId", "SMS_FullCollectionMembership": "ResourceID",
                "SMS_G_System_OPERATING_SYSTEM": "ResourceID", "SMS_UpdateComplianceStatus": "MachineID",
                "SMS_Collection": "CollectionID"}
        bad = {c: p for c, p in want.items() if c in props and p not in props[c]}
        return not bad, f"mismatched: {bad}" if bad else "ResourceId/ResourceID/MachineID/CollectionID as assumed"
    check("join columns spelled as the query guide says", True, join_columns)

    def v1_metadata():
        try:
            parsed = parse_metadata(raw_get(client, "v1.0/$metadata", "application/xml").text)
        except MecmError as e:
            return False, f"no v1.0 route ({e})"
        return True, f"{len(parsed)} entity sets: {sorted(parsed)[:15]}"
    check("v1.0/$metadata available", False, v1_metadata)

    def paging():
        payload = raw_get(client, "wmi/SMS_G_System_INSTALLED_SOFTWARE?$select=ResourceID,ProductName").json()
        page = len(payload.get("value") or [])
        link = payload.get("@odata.nextLink")
        return True, f"first page {page} rows, nextLink={'yes: ' + link[:120] if link else 'none'}"
    check("server paging (@odata.nextLink) on a large class", False, paging)

    def full_read():
        n = len(client.execute_query("SMS_G_System_INSTALLED_SOFTWARE?$select=ResourceID,ProductName"))
        return n > 0, f"{n} rows read through the client (all pages)"
    check("client reads every page", True, full_read)

    for name, query, required in [
        ("$select", "SMS_R_System?$select=Name,ResourceId", True),
        ("$filter eq", "SMS_R_System?$filter=Client eq 1&$select=Name", True),
        ("$filter startswith(...) eq true", "SMS_Collection?$filter=startswith(Name,'All') eq true&$select=Name", True),
        ("$filter contains(...) eq true", "SMS_Collection?$filter=contains(Name,'Sys') eq true&$select=Name", False),
        ("$filter datetime literal", "SMS_R_System?$filter=CreationDate ge 2000-01-01T00:00:00Z&$select=Name", False),
        ("$top", "SMS_R_System?$top=3&$select=Name", False),
        ("$skip", "SMS_R_System?$skip=1&$top=2&$select=Name", False),
        ("$orderby", "SMS_R_System?$orderby=Name desc&$top=3&$select=Name", False),
        ("$count=true", "SMS_Collection?$count=true&$select=Name", False),
    ]:
        def run(query=query):
            payload = raw_get(client, "wmi/" + query).json()
            rows = payload.get("value") or []
            extra = f", @odata.count={payload['@odata.count']}" if "@odata.count" in payload else ""
            return True, f"{len(rows)} rows{extra}"
        check(f"OData {name}", required, run)

    def apply_unsupported():
        try:
            raw_get(client, "wmi/SMS_R_System?$apply=groupby((ADSiteName))")
            return True, "$apply ACCEPTED — the guide says it is not; update the guide"
        except MecmError as e:
            return True, f"rejected as expected: {str(e)[:160]}"
    check("$apply behaviour", False, apply_unsupported)

    def max_rows():
        n = len(client.execute_query("SMS_R_System?$select=Name", max_rows=2))
        return n == 2, f"{n} rows for max_rows=2"
    check("max_rows bound", True, max_rows)

    def lazy():
        listed = raw_get(client, "wmi/SMS_Collection?$top=1").json()["value"][0]
        cid = listed.get("CollectionID")
        single = raw_get(client, f"wmi/SMS_Collection('{cid}')").json()
        lazy_props = listed.get("__LAZYPROPERTIES") or single.get("__LAZYPROPERTIES")
        has_rules_list = "CollectionRules" in listed and listed.get("CollectionRules") is not None
        has_rules_single = single.get("CollectionRules") is not None
        return True, (f"__LAZYPROPERTIES={lazy_props}; CollectionRules in list read: {has_rules_list}; "
                      f"in single read: {has_rules_single}; single-read shape keys: {sorted(single)[:8]}")
    check("lazy properties (SMS_Collection.CollectionRules)", False, lazy)

    def shapes():
        row = raw_get(client, "wmi/SMS_R_System?$top=1").json()["value"][0]
        system = sorted(k for k in row if k.startswith("__") or k.startswith("@"))
        lists = {k: type(v).__name__ for k, v in row.items() if isinstance(v, list)}
        dates = {k: v for k, v in row.items() if isinstance(v, str) and len(v) >= 19 and v[4] == "-" and "T" in v}
        return True, f"system props: {system}; list props: {lists}; date samples: {dict(list(dates.items())[:2])}"
    check("value shapes (system props, lists, dates)", False, shapes)

    def case_rules():
        a = len(raw_get(client, "wmi/sms_collection?$select=Name").json()["value"])
        try:
            raw_get(client, "v1.0/device?$top=1")
            v1 = "v1.0 lower-case ACCEPTED"
        except MecmError as e:
            v1 = f"v1.0 lower-case rejected ({str(e)[:60]})"
        return True, f"wmi lower-case read {a} rows; {v1}"
    check("class-name case rules", False, case_rules)

    def errors():
        out = []
        for path in ["wmi/SMS_R_System?$filter=NoSuchProperty eq 1", "wmi/SMS_NoSuchClass"]:
            try:
                raw_get(client, path)
                out.append(f"{path}: no error?!")
            except MecmError as e:
                out.append(f"{path}: {str(e)[:150]}")
        return True, " | ".join(out)
    check("error bodies are surfaced", False, errors)

    required_failed = [r for r in RESULTS if r["required"] and not r["ok"]]
    print(f"\n{len(RESULTS)} checks, {len(required_failed)} required failed.")
    if "--json" in sys.argv:
        out = sys.argv[sys.argv.index("--json") + 1]
        Path(out).write_text(json.dumps({"base_url": client.base_url, "results": RESULTS}, indent=2))
        print(f"report written to {out}")
    return 1 if required_failed else 0


if __name__ == "__main__":
    sys.exit(main())
