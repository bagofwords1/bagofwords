#!/usr/bin/env python3
"""Loop B — live audit log streams against the mock SIEM consumer.

Runs against the real stack (backend + scheduler) and the mock consumer
(tools/agent/mock_siem_consumer.py). Creates one stream per destination via the
API, generates events through real audited actions, injects faults through the
mock's control API, and checks convergence against the database (the audit
list endpoint), printing a PASS/FAIL table. Exits non-zero on any FAIL.

    # stack up with a short lag/interval so the loop runs in seconds, and a
    # pinned encryption key so stream secrets survive the B5 restart:
    export BOW_AUDIT_STREAM_LAG_SECONDS=2 BOW_AUDIT_STREAM_INTERVAL_SECONDS=5
    export BOW_ENCRYPTION_KEY=$(python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
    tools/agent/boot_stack.sh
    cd backend && uv run python ../tools/agent/seed_org.py --demo
    uv run python ../tools/agent/mock_siem_consumer.py --state-dir /tmp/siem-mock &
    uv run python ../tools/agent/audit_streams_loop.py --scenario all \
        --restart-cmd "../tools/agent/restart_backend.sh $PWD/db/agent.db"

Scenarios: B1 happy path · B2 transient faults · B3 invalid → fixed ·
B4 tool-audit burst through the real queue (in-process, same DB) ·
B5 backend restart mid-batch.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
import uuid

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DESTS = ["datadog", "splunk", "sentinel", "s3", "gcs", "https", "syslog"]


class Loop:
    def __init__(self, a):
        self.a = a
        self.api = httpx.Client(base_url=a.base_url, timeout=60)
        self.mock = httpx.Client(base_url=a.mock, timeout=30)
        self.login()
        self.results: list[tuple[str, bool, str]] = []
        self.streams: dict[str, dict] = {}

    def login(self):
        a = self.a
        tok = self.api.post("/api/auth/jwt/login", data={"username": a.email, "password": a.password}).json()["access_token"]
        me = self.api.get("/api/users/whoami", headers={"Authorization": f"Bearer {tok}"}).json()
        self.org = me["organizations"][0]["id"]
        self.h = {"Authorization": f"Bearer {tok}", "X-Organization-Id": self.org, "User-Agent": "audit_streams_loop/1"}

    # -- helpers ---------------------------------------------------------------
    def check(self, name, ok, detail=""):
        self.results.append((name, bool(ok), detail))
        print(f"{'PASS' if ok else 'FAIL'}  {name}{('  — ' + detail) if detail else ''}", flush=True)

    def db_ids(self) -> list[str]:
        ids, page = [], 1
        while True:
            r = self.api.get("/api/enterprise/audit", params={"page": page, "page_size": 100}, headers=self.h)
            r.raise_for_status()
            b = r.json()
            ids += [i["id"] for i in b["items"]]
            if page >= max(b["total_pages"], 1):
                return ids
            page += 1

    def stats(self, dest, ids):
        return self.mock.post("/_stats", json={"dest": dest, "expect_ids": ids}).json()

    def stream_state(self, dest):
        return self.api.get(f"/api/enterprise/audit/streams/{self.streams[dest]['id']}", headers=self.h).json()

    def converge(self, dests, timeout, label):
        ids = self.db_ids()
        deadline = time.time() + timeout
        pending = set(dests)
        last = {}
        while pending and time.time() < deadline:
            for d in list(pending):
                s = self.stats(d, ids)
                last[d] = s
                if not s["missing"]:
                    pending.discard(d)
            if pending:
                time.sleep(2)
        for d in dests:
            s = last.get(d) or self.stats(d, ids)
            self.check(f"{label}: {d} received every DB event", not s["missing"],
                       f"expected={len(ids)} missing={len(s['missing'])} dups={s['duplicates']} fmt_err={s['format_errors']}")
        return ids, last

    def events(self, n):
        for i in range(n):
            if i % 4 == 3:
                r = self.api.post("/api/reports", json={"title": f"Loop report {uuid.uuid4().hex[:6]}"}, headers=self.h)
                if r.status_code == 200:
                    self.api.put(f"/api/reports/{r.json()['id']}", json={"title": "Loop report (edited)"}, headers=self.h)
            else:
                r = self.api.post("/api/api_keys", json={"name": f"loop-{i}"}, headers=self.h)
                if r.status_code == 200 and i % 2:
                    self.api.delete(f"/api/api_keys/{r.json()['id']}", headers=self.h)

    def fault(self, dest, mode, count, sleep=None):
        self.mock.post("/_control/fault", json={"dest": dest, "mode": mode, "count": count, "sleep": sleep}).raise_for_status()

    # -- setup -----------------------------------------------------------------
    def setup(self):
        for s in self.api.get("/api/enterprise/audit/streams", headers=self.h).json():
            self.api.delete(f"/api/enterprise/audit/streams/{s['id']}", headers=self.h)
        self.mock.post("/_control/reset").raise_for_status()
        m = self.a.mock
        ca = open(self.a.ca_cert).read() if self.a.ca_cert and os.path.exists(self.a.ca_cert) else None
        cfg = {
            "datadog": ({"site": "datadoghq.com", "intake_url": f"{m}/dd/api/v2/logs"}, {"api_key": "demo-dd-key"}),
            "splunk": ({"hec_url": f"{m}/splunk", "index": "audit"}, {"token": "demo-hec-token"}),
            "sentinel": ({"tenant_id": "t1", "client_id": "demo-client", "dce_url": f"{m}/sentinel",
                          "dcr_immutable_id": "dcr-1", "stream_name": "Custom-BagOfWordsAudit_CL",
                          "authority_url": f"{m}/entra"}, {"client_secret": "demo-secret"}),
            "s3": ({"bucket": "audit", "region": "us-east-1", "prefix": "bow/audit", "endpoint_url": f"{m}/s3",
                    "role_arn": "arn:aws:iam::123456789012:role/bow-audit", "sts_endpoint_url": f"{m}/sts"},
                   {"access_key_id": "demo-access-key", "secret_access_key": "x"}),
            "gcs": ({"bucket": "gcs-audit", "prefix": "bow", "endpoint_url": f"{m}/s3", "gzip": True},
                    {"access_key_id": "demo-access-key", "secret_access_key": "x"}),
            "https": ({"url": f"{m}/https/loop"}, {"hmac_secret": "demo-hmac-secret"}),
            "syslog": ({"host": "127.0.0.1", "port": self.a.syslog_port, "tls": True, "ca_cert": ca, "format": "json"}, {}),
        }
        for d in DESTS:
            config, secrets = cfg[d]
            r = self.api.post("/api/enterprise/audit/streams", headers=self.h, json={
                "name": f"Loop {d}", "destination": d, "config": config, "secrets": secrets, "start_from": "beginning",
            })
            r.raise_for_status()
            self.streams[d] = r.json()
            if d == "s3":
                # The external id is generated by Bag of Words; the mock STS
                # plays the customer's trust policy that requires it.
                self.mock.post("/_control/config", json={"external_id": r.json()["config"]["external_id"]})
        self.check("setup: one stream per destination", len(self.streams) == len(DESTS), ", ".join(DESTS))

    # -- scenarios ---------------------------------------------------------------
    def b1(self):
        self.events(self.a.events)
        self.converge(DESTS, 120, "B1 happy path")

    def b2(self):
        self.fault("splunk", "503", 5)
        self.fault("datadog", "429", 3)
        self.events(50)
        self.converge(["splunk", "datadog"], 240, "B2 transient")
        for d in ("splunk", "datadog"):
            st = self.stream_state(d)
            self.check(f"B2 transient: {d} stayed active", st["state"] == "active", f"failures={st['consecutive_failures']}")
        self.check("B2 transient: mock saw the injected failures",
                   self.mock.get("/_stats", params={"dest": "splunk"}).json()["requests"] >= 5)

    def b3(self):
        self.fault("sentinel", "401", -1)
        self.mock.post("/_control/config", json={"client_secret": "rotated-secret"})  # token endpoint now rejects too
        self.events(30)
        deadline = time.time() + 60
        while time.time() < deadline and self.stream_state("sentinel")["state"] != "invalid":
            time.sleep(2)
        st = self.stream_state("sentinel")
        self.check("B3 invalid: stream moved to invalid", st["state"] == "invalid", (st.get("last_error") or "")[:80])
        acts = self.api.get("/api/enterprise/audit", params={"action": "audit_stream.state_changed"}, headers=self.h).json()
        self.check("B3 invalid: state change is audited", acts["total"] >= 1)
        self.mock.delete("/_control/fault")
        r = self.api.patch(f"/api/enterprise/audit/streams/{self.streams['sentinel']['id']}", headers=self.h,
                           json={"secrets": {"client_secret": "rotated-secret"}, "state": "active"})
        self.check("B3 fixed: credentials updated and resumed", r.status_code == 200 and r.json()["state"] == "active")
        self.converge(["sentinel"], 120, "B3 invalid → fixed")

    def b4(self):
        n = self.a.burst
        cmd = [sys.executable, os.path.abspath(__file__), "--burst-worker", str(n), "--org", self.org]
        t0 = time.time()
        out = subprocess.run(cmd, cwd=os.path.join(ROOT, "backend"), capture_output=True, text=True, timeout=600)
        line = next((ln for ln in out.stdout.splitlines() if ln.startswith('{"rows"')), "{}")
        stats = json.loads(line)
        self.check("B4 burst: queue dropped nothing", stats.get("dropped") == 0 and stats.get("rows") == n,
                   f"burst={n} rows={stats.get('rows')} {json.dumps(stats.get('queue', {}))} in {time.time() - t0:.1f}s")
        self.converge(DESTS, 300, "B4 burst")

    def b5(self):
        if not self.a.restart_cmd:
            self.check("B5 restart mid-batch: skipped (no --restart-cmd)", True)
            return
        before = self.mock.get("/_stats", params={"dest": "https"}).json()["requests"]
        self.fault("https", "timeout", 1, sleep=25)
        self.events(10)
        deadline = time.time() + 60
        while time.time() < deadline and self.mock.get("/_stats", params={"dest": "https"}).json()["requests"] <= before:
            time.sleep(1)
        print("B5: request in flight; restarting backend", flush=True)
        subprocess.run(self.a.restart_cmd, shell=True, check=True, timeout=240,
                       env={**os.environ, "BOW_AUDIT_STREAM_LAG_SECONDS": os.environ.get("BOW_AUDIT_STREAM_LAG_SECONDS", "2"),
                            "BOW_AUDIT_STREAM_INTERVAL_SECONDS": os.environ.get("BOW_AUDIT_STREAM_INTERVAL_SECONDS", "5")})
        self.login()  # a restarted sandbox backend may sign tokens with a new secret
        # The interrupted claim holds a 120s lease; delivery resumes after it.
        ids, last = self.converge(["https"], 300, "B5 restart mid-batch")
        evs = self.mock.get("/_received", params={"dest": "https"}).json()
        by_id = {}
        for e in evs:
            by_id.setdefault(e["id"], []).append(e)
        dup_ok = all(all(x == group[0] for x in group) for group in by_id.values())
        self.check("B5 restart mid-batch: any duplicate is the identical event (same id)", dup_ok,
                   f"duplicates={last['https']['duplicates']}")

    def run(self, scenarios):
        self.setup()
        for s in scenarios:
            getattr(self, s.lower())()
        failed = [r for r in self.results if not r[1]]
        print(f"\n{len(self.results) - len(failed)}/{len(self.results)} checks passed")
        with open(os.path.join(self.a.out, "loop_b_results.json"), "w") as f:
            json.dump([{"check": n, "ok": ok, "detail": d} for n, ok, d in self.results], f, indent=2)
        return 1 if failed else 0


async def burst_worker(n: int, org_id: str) -> dict:
    """Fire n tool-audit events concurrently through the real queue into the
    sandbox database, then report queue stats and the row count."""
    os.environ.setdefault("TESTING", "true")
    sys.path.insert(0, os.path.join(ROOT, "backend"))
    import main  # noqa: F401  registers every ORM model
    from types import SimpleNamespace

    from sqlalchemy import func, select

    import app.ee.audit.tool_audit as ta
    from app.dependencies import async_session_maker
    from app.ee.audit.models import AuditLog

    marker = uuid.uuid4().hex
    await ta.start_tool_audit_worker()
    ctx = {"organization": SimpleNamespace(id=org_id), "user": None, "agent_execution_id": f"burst-{marker}", "mode": "chat"}
    await asyncio.gather(*[
        ta.log_tool_audit(ctx, "tool.data_queried", "data_source", None,
                          {"tool": "create_data", "burst": marker, "i": i, "queries": [f"SELECT {i}"]})
        for i in range(n)
    ])
    await ta.drain_tool_audit_queue(timeout=300)
    await ta.stop_tool_audit_worker(timeout=60)
    async with async_session_maker() as s:
        rows = (await s.execute(select(func.count(AuditLog.id)).where(
            AuditLog.organization_id == org_id,
            AuditLog.details["burst"].as_string() == marker,
        ))).scalar()
    stats = ta.get_tool_audit_queue_stats()
    return {"rows": rows, "dropped": stats["dropped"], "queue": stats}


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument("--mock", default="http://127.0.0.1:8790")
    p.add_argument("--syslog-port", type=int, default=6514)
    p.add_argument("--ca-cert", default="/tmp/siem-mock/tls/cert.pem")
    p.add_argument("--email", default="admin@example.com")
    p.add_argument("--password", default="Password123!")
    p.add_argument("--scenario", default="all", help="all or comma list of B1,B2,B3,B4,B5")
    p.add_argument("--events", type=int, default=200)
    p.add_argument("--burst", type=int, default=10000)
    p.add_argument("--restart-cmd", default=None)
    p.add_argument("--out", default=".")
    p.add_argument("--burst-worker", type=int, default=None, help=argparse.SUPPRESS)
    p.add_argument("--org", default=None, help=argparse.SUPPRESS)
    a = p.parse_args()
    if a.burst_worker is not None:
        print(json.dumps(asyncio.run(burst_worker(a.burst_worker, a.org))), flush=True)
        return 0
    scenarios = ["B1", "B2", "B3", "B4", "B5"] if a.scenario == "all" else [s.strip().upper() for s in a.scenario.split(",")]
    return Loop(a).run(scenarios)


if __name__ == "__main__":
    sys.exit(main())
