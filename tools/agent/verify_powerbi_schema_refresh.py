#!/usr/bin/env python3
"""Read-only live verification. Run with backend/.venv/bin/python.

Required env: PBI_TENANT_ID, PBI_CLIENT_ID, PBI_CLIENT_SECRET.
Optional delegated identity: PBI_USERNAME, PBI_PASSWORD (demo accounts only).
No database writes, schema changes, admin scan jobs, or permission refreshes.
"""

import json
import os
import sys
from collections import Counter
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from app.data_sources.clients.powerbi_client import PowerBIClient


def main():
    tenant = os.environ["PBI_TENANT_ID"]
    form = {
        "client_id": os.environ["PBI_CLIENT_ID"],
        "client_secret": os.environ["PBI_CLIENT_SECRET"],
        "scope": "https://analysis.windows.net/powerbi/api/.default",
        "grant_type": "client_credentials",
    }
    if os.environ.get("PBI_USERNAME"):
        form.update(
            grant_type="password",
            username=os.environ["PBI_USERNAME"],
            password=os.environ["PBI_PASSWORD"],
        )
    response = requests.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data=form,
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Authentication failed: HTTP {response.status_code}")
    token = response.json()["access_token"]

    def client():
        c = PowerBIClient(access_token=token)
        c._perms_refreshed = True
        c._batch_admin_scan = lambda *_: {}  # Do not create upstream scan jobs.
        return c

    discovery = client()
    tables = discovery.get_schemas(force_refresh=True)
    assert tables, "No queryable model tables discovered"
    assert len({t.name for t in tables}) == len(tables), (
        "Distinct model tables collided by name"
    )

    def identity(table):
        meta = table.metadata_json["powerbi"]
        return meta["workspaceId"], meta["datasetId"], meta["tableName"]

    assert len({identity(t) for t in tables}) == len(tables)
    labels = Counter(
        (
            t.metadata_json["powerbi"]["datasetName"],
            t.metadata_json["powerbi"]["tableName"],
        )
        for t in tables
    )
    chosen = next(t for t in tables if t.columns)
    meta = chosen.metadata_json["powerbi"]
    known = {
        chosen.name: {
            "columns": [{"name": "__STALE_TEST_COLUMN__", "dtype": "string"}],
            "metadata_json": chosen.metadata_json,
        }
    }

    def scoped_client():
        c = client()
        c.list_workspaces = lambda: [
            {"id": meta["workspaceId"], "name": meta["workspaceName"]}
        ]
        c.list_datasets = lambda *_: [
            {"id": meta["datasetId"], "name": meta["datasetName"]}
        ]
        c.list_reports = lambda *_: []
        return c

    incremental = scoped_client().get_schemas(prior_tables=known)
    assert any(
        c.name == "__STALE_TEST_COLUMN__" for t in incremental for c in t.columns
    )
    fresh = scoped_client().get_schemas(force_refresh=True, prior_tables=known)
    assert any(identity(t) == identity(chosen) for t in fresh)
    assert all(c.name != "__STALE_TEST_COLUMN__" for t in fresh for c in t.columns)
    print(
        json.dumps(
            {
                "identity": "delegated"
                if os.environ.get("PBI_USERNAME")
                else "service_principal",
                "tables": len(tables),
                "distinct_names": len({t.name for t in tables}),
                "same_display_name_groups": sum(n > 1 for n in labels.values()),
                "incremental_reuses_prior": True,
                "explicit_refresh_replaces_stale_columns": True,
                "unreadable_models": len(discovery.discovery_diagnostics),
            }
        )
    )


if __name__ == "__main__":
    main()
