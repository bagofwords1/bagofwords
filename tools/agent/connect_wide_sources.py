#!/usr/bin/env python3
"""Connect the synthetic wide sources built by seed_wide_sources.py as BOW
agents through the public API, and activate every discovered table.

    cd backend && uv run python ../tools/agent/connect_wide_sources.py

Env: BOW_BASE_URL (default http://localhost:8000), BOW_ADMIN_EMAIL /
BOW_ADMIN_PASSWORD (default admin@example.com / Password123!), DUCKDB_PATH.
Prints a JSON map {agent name: data source id}. Idempotent: reuses agents
that already exist by name.
"""
from __future__ import annotations

import json
import os
import sys
import time

import httpx

BASE = os.environ.get("BOW_BASE_URL", "http://localhost:8000")
EMAIL = os.environ.get("BOW_ADMIN_EMAIL", "admin@example.com")
PASSWORD = os.environ.get("BOW_ADMIN_PASSWORD", "Password123!")
DUCKDB_PATH = os.environ.get("DUCKDB_PATH", "/tmp/bow-wide/wide_synth.duckdb")

AGENTS = [
    {
        "name": "Wide Postgres",
        "type": "postgresql",
        "config": {"host": "127.0.0.1", "port": 5432, "database": "wide_synth", "schema": "public", "sslmode": "disable"},
        "credentials": {"user": "synth_reader", "password": "synth_reader_pw"},
    },
    {
        "name": "Wide DuckDB",
        "type": "duckdb",
        "config": {"uris": DUCKDB_PATH},
        "credentials": {},
    },
    {
        "name": "Wide Elastic",
        "type": "elasticsearch",
        # auth_type rides in config: the API picks the credentials schema from it.
        "config": {"host": "127.0.0.1", "port": 9200, "secure": False, "verify_certs": False,
                   "index_pattern": "svc-*,obs-*", "auth_type": "none"},
        "credentials": {},
    },
]


def main() -> None:
    c = httpx.Client(base_url=BASE, timeout=600)
    tok = c.post("/api/auth/jwt/login", data={"username": EMAIL, "password": PASSWORD}).json()["access_token"]
    org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
    H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}
    existing = {d["name"]: d["id"] for d in c.get("/api/data_sources", headers=H).json()}
    out = {}
    for spec in AGENTS:
        ds_id = existing.get(spec["name"])
        if not ds_id:
            body = {
                "name": spec["name"], "type": spec["type"], "config": spec["config"],
                "credentials": spec["credentials"], "auth_policy": "system_only",
                "generate_summary": False, "generate_conversation_starters": False, "generate_ai_rules": False,
            }
            r = c.post("/api/data_sources", json=body, headers=H)
            if r.status_code != 200:
                sys.exit(f"create {spec['name']} failed: {r.status_code} {r.text[:400]}")
            ds_id = r.json()["id"]
        # Indexing is async: wait for the catalog, then activate every table.
        names: list[str] = []
        for _ in range(240):
            fr = c.get(f"/api/data_sources/{ds_id}/full_schema", headers=H)
            body = fr.json() if fr.status_code == 200 else {}
            items = body.get("tables", body) if isinstance(body, dict) else body
            names = [t.get("name") for t in items] if isinstance(items, list) else []
            if names:
                break
            time.sleep(2)
        if not names:
            sys.exit(f"{spec['name']}: no tables indexed")
        r = c.put(f"/api/data_sources/{ds_id}/update_tables_status",
                  json={"activate": names, "deactivate": []}, headers=H)
        out[spec["name"]] = {"id": ds_id, "tables": len(names), "activate": r.status_code}
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
