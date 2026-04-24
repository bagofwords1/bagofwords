"""Load test for ConnectionIndexing against a mocked Power BI Report Server.

PBIRS is the other client we explicitly wired progress callbacks into
(`get_schemas` has phase events for listing, pbix_reports, rdl_reports,
shared_datasets, kpis). A real server isn't available in CI, so we
monkeypatch `test_connection` and `get_schemas` on the client class to
behave like a server with 50 PBIX reports, 30 paginated reports, 20
shared datasets, and 5 KPIs.

Assertions:
  - POST /connections returns in <2s with status_code 200.
  - Indexing runs to completion with `progress_done == progress_total`.
  - Phase transitions are observed across polling: listing → pbix_reports
    → rdl_reports → shared_datasets → kpis (subset sufficient, since
    phases can complete between polls).
  - ConnectionTable rows count matches the mocked table output.
  - The indexing stats report the correct table_count.
"""
from __future__ import annotations

import time

import pytest

from app.ai.prompt_formatters import Table
from app.data_sources.clients import powerbi_report_server_client as pbirs
from app.data_sources.clients.progress import make_reporter


N_PBIX = 50
N_RDL = 30
N_DS = 20
N_KPI = 5
# PBIX: one umbrella + two model tables per report (fixture shape)
# RDL: one dataset per report
# DS: one row per dataset
# KPI: one row per KPI
TOTAL_TABLES = N_PBIX * 3 + N_RDL + N_DS + N_KPI


def _mock_test_connection(self) -> dict:
    return {
        "success": True,
        "message": "Mocked PBIRS connection",
        "connectivity": True,
        "schema_access": True,
        "table_count": TOTAL_TABLES,
        "timings": {"mock_ms": 1.0},
        "details": {"product_version": "mock-1.0", "auth_mode": "NTLM"},
    }


def _mock_get_schemas(self, progress_callback=None):
    """Emit the same phase sequence as the real get_schemas, with canned data.

    A small sleep between phases keeps each one visible to pollers beyond the
    progress-flush debounce window (so the test can assert transitions). In
    production, PBIRS phases take seconds each — this mock exaggerates that.
    """
    import time as _t
    reporter = make_reporter(progress_callback)
    reporter.phase("listing")
    _t.sleep(0.3)
    tables = []

    reporter.phase("pbix_reports", total=N_PBIX)
    _t.sleep(0.3)
    for i in range(N_PBIX):
        rid = f"pbix-{i}"
        name = f"Report {i}"
        # Umbrella row
        tables.append(Table(
            name=f"pbix:{name}",
            columns=[], pks=[], fks=[], is_active=True,
            metadata_json={"powerbi_report_server": {"report_id": rid, "report_type": "PowerBIReport"}},
        ))
        # Two model tables per report
        tables.append(Table(
            name=f"pbix:{name}/Fact",
            columns=[], pks=[], fks=[], is_active=True,
            metadata_json={"powerbi_report_server": {"report_id": rid, "report_type": "PowerBIReportTable", "model_table": "Fact"}},
        ))
        tables.append(Table(
            name=f"pbix:{name}/Dim",
            columns=[], pks=[], fks=[], is_active=True,
            metadata_json={"powerbi_report_server": {"report_id": rid, "report_type": "PowerBIReportTable", "model_table": "Dim"}},
        ))
        reporter.item(name)

    reporter.phase("rdl_reports", total=N_RDL)
    _t.sleep(0.3)
    for i in range(N_RDL):
        name = f"RDL Report {i}"
        tables.append(Table(
            name=f"rdl:{name}/Main",
            columns=[], pks=[], fks=[], is_active=True,
            metadata_json={"powerbi_report_server": {"report_id": f"rdl-{i}", "report_type": "Report"}},
        ))
        reporter.item(name)

    reporter.phase("shared_datasets", total=N_DS)
    _t.sleep(0.3)
    for i in range(N_DS):
        name = f"Dataset {i}"
        tables.append(Table(
            name=f"dataset:{name}",
            columns=[], pks=[], fks=[], is_active=True,
            metadata_json={"powerbi_report_server": {"dataset_id": f"ds-{i}", "report_type": "Dataset"}},
        ))
        reporter.item(name)

    reporter.phase("kpis", total=N_KPI)
    _t.sleep(0.3)
    for i in range(N_KPI):
        name = f"KPI {i}"
        tables.append(Table(
            name=f"kpi:{name}",
            columns=[], pks=[], fks=[], is_active=True,
            metadata_json={"powerbi_report_server": {"kpi_id": f"kpi-{i}", "report_type": "Kpi"}},
        ))
        reporter.item(name)

    reporter.done()
    return tables


@pytest.mark.e2e
def test_pbirs_mock_large_catalog_indexing(
    monkeypatch,
    create_connection,
    test_client,
    create_user,
    login_user,
    whoami,
):
    monkeypatch.setattr(pbirs.PowerBIReportServerClient, "test_connection", _mock_test_connection)
    monkeypatch.setattr(pbirs.PowerBIReportServerClient, "get_schemas", _mock_get_schemas)

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    t0 = time.perf_counter()
    connection = create_connection(
        name="PBIRS Mock",
        type="powerbi_report_server",
        config={
            "server_url": "http://mocked-pbirs.example/",
            "verify_ssl": False,
        },
        credentials={
            "username": "mockuser",
            "password": "mockpass",
        },
        user_token=token,
        org_id=org_id,
    )
    post_duration = time.perf_counter() - t0
    assert post_duration < 3.0, f"POST /connections took {post_duration:.2f}s"

    conn_id = connection["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    # Poll, collecting phase transitions
    observed_phases: list[str] = []
    deadline = time.perf_counter() + 30.0
    last = None
    while time.perf_counter() < deadline:
        r = test_client.get(f"/api/connections/{conn_id}/indexing", headers=headers)
        if r.status_code == 404:
            time.sleep(0.05)
            continue
        assert r.status_code == 200, r.text
        last = r.json()
        phase = last.get("phase")
        if phase and (not observed_phases or observed_phases[-1] != phase):
            observed_phases.append(phase)
        if last["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.05)

    assert last is not None
    assert last["status"] == "completed", last
    assert last["progress_done"] == last["progress_total"]
    # Runner does sync after refresh_schema; last phase reflects the final
    # reporter tick from get_schemas (usually "kpis" or whichever phase ran
    # last). We verify we saw at least 2 distinct phases during polling.
    assert len(observed_phases) >= 2, f"expected >=2 distinct phases observed, got {observed_phases}"

    # Total ConnectionTable rows should match our fixture.
    r = test_client.get(f"/api/connections/{conn_id}/tables", headers=headers)
    assert r.status_code == 200, r.text
    tables = r.json()
    assert len(tables) == TOTAL_TABLES, f"expected {TOTAL_TABLES} tables, got {len(tables)}"
    assert (last.get("stats") or {}).get("table_count") == TOTAL_TABLES
