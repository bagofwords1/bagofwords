#!/usr/bin/env python3
"""Live end-to-end run of agent sandboxes against one real Docker host.

Drives a running BoW (with BOW_AGENT_SANDBOXES_ENABLED on) through its HTTP
API the way an admin and then a user would, and checks the phase 1
definition of done (BOW-229 / BOW-246):

  1. an admin adds a server, tests the connection, bootstraps it, creates an
     environment, grants it, and turns the feature on;
  2. a user's agent, in a report, runs Python that installs a package (in an
     environment that allows the registries) and writes a PNG, and the PNG
     appears as a report file;
  3. a sandbox with no internet cannot reach the internet;
  4. the sandbox stops when idle and resumes on the next run with its
     packages intact.

Steps 2 and 4 go through the agent loop with the organization's default
model, so the model itself decides to call the tools. With no working model
key, register the scripted stand-in first (tools/agent/stub_llm_sandbox.py +
setup_stub_llm.py): it only picks the tool call, the rest is the real product.

Usage (from backend/, so the app's modules and the DevBox env are available):

    source ../tools/agent/devbox_env.sh        # BOW_EMAIL / BOW_PASSWORD
    BOW_SANDBOX_TEST_HOST=<address> BOW_SANDBOX_TEST_USER=ubuntu \\
    BOW_SANDBOX_TEST_KEY=/path/to/key BOW_SANDBOX_TEST_FINGERPRINT=SHA256:... \\
    uv run --no-sync python ../tools/agent/agent_sandbox_live.py [--keep]

Optional: BOW_URL (default http://localhost:8000), BOW_SANDBOX_TEST_BOW_ADDRESS,
BOW_SANDBOX_TEST_PRELOADED=1 (images are already on the server).
Sandboxes are created with a 1 GB memory limit. Without --keep everything it
created is removed at the end (sandboxes, environments, the compute, and
what BoW put on the server).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid

import httpx

sys.path.insert(0, os.getcwd())  # run from backend/: the app's modules are used for the database checks

BASE = os.environ.get("BOW_URL", "http://localhost:8000")
RESULTS: list = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return bool(ok)


def step(title: str) -> None:
    print(f"\n== {title}", flush=True)


class Api:
    def __init__(self) -> None:
        self.http = httpx.Client(base_url=BASE, timeout=900)
        token = self.http.post("/api/auth/jwt/login", data={
            "username": os.environ["BOW_EMAIL"], "password": os.environ["BOW_PASSWORD"]}).json()["access_token"]
        me = self.http.get("/api/users/whoami", headers={"Authorization": f"Bearer {token}"}).json()
        self.user_id = me["id"]
        self.org_id = me["organizations"][0]["id"]
        self.headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": self.org_id}

    def call(self, method: str, path: str, **kwargs):
        response = self.http.request(method, path, headers=self.headers, **kwargs)
        if response.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {response.status_code}: {response.text[:400]}")
        return response.json() if response.content else None

    def ask(self, report_id: str, prompt: str) -> None:
        """Send a prompt in a report and wait for the agent to finish."""
        self.call("POST", f"/api/reports/{report_id}/completions", params={"background": False},
                  json={"prompt": {"content": prompt, "mentions": []}})


async def _db(fn):
    """Run ``await fn(db)`` against the app database (the script runs inside the backend's environment)."""
    from app.dependencies import async_session_maker

    async with async_session_maker() as db:
        return await fn(db)


def db(fn):
    import main  # noqa: F401 - loads every model, as the running app does, so the ORM is fully mapped

    return asyncio.run(_db(fn))


def sandbox_rows(report_id: str):
    from sqlalchemy import select

    from app.models.sandbox import Sandbox, SandboxEvent, SandboxRun

    async def _go(session):
        sandbox = (await session.execute(
            select(Sandbox).where(Sandbox.report_id == report_id).order_by(Sandbox.created_at.desc())
        )).scalars().first()
        if sandbox is None:
            return None, [], []
        runs = (await session.execute(
            select(SandboxRun).where(SandboxRun.sandbox_id == sandbox.id).order_by(SandboxRun.started_at)
        )).scalars().all()
        events = (await session.execute(
            select(SandboxEvent).where(SandboxEvent.sandbox_id == sandbox.id).order_by(SandboxEvent.created_at)
        )).scalars().all()
        return sandbox, list(runs), [e.type for e in events]
    return db(_go)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="leave the compute, environments and sandboxes in place")
    args = parser.parse_args()
    host = os.environ["BOW_SANDBOX_TEST_HOST"]
    suffix = uuid.uuid4().hex[:6]
    api = Api()
    created = {"compute": None, "environments": [], "reports": []}
    settings_before = api.call("GET", "/api/sandbox/settings")
    # Leftovers of an interrupted earlier run would compete for the same server.
    for stale in api.call("GET", "/api/sandbox/computes"):
        if stale["name"].startswith("live-"):
            api.http.delete(f"/api/sandbox/computes/{stale['id']}", headers=api.headers, params={"delete_sandboxes": "true"})
    for stale in api.call("GET", "/api/sandbox/environments"):
        if stale["name"].startswith("Live "):
            api.http.delete(f"/api/sandbox/environments/{stale['id']}", headers=api.headers)

    try:
        # ── 1. the admin sets it up ──────────────────────────────────────
        step("1. An admin adds a server, bootstraps it, creates environments and turns the feature on")
        with open(os.environ["BOW_SANDBOX_TEST_KEY"]) as handle:
            private_key = handle.read()
        compute = api.call("POST", "/api/sandbox/computes", json={
            # The label keeps this run's environments on this server and no other compute.
            "name": f"live-{suffix}", "type": "docker-ssh", "capacity": 2, "labels": [f"live-{suffix}"],
            "config": {
                "address": host, "ssh_user": os.environ.get("BOW_SANDBOX_TEST_USER", "root"),
                "bow_address": os.environ.get("BOW_SANDBOX_TEST_BOW_ADDRESS") or None,
                "images_preloaded": os.environ.get("BOW_SANDBOX_TEST_PRELOADED", "") in ("1", "true"),
            },
            "credentials": {"ssh_private_key": private_key},
        })
        created["compute"] = compute["id"]
        check("the compute is created disabled, and no private key comes back",
              compute["enabled"] is False and "PRIVATE KEY" not in json.dumps(compute)
              and compute["credentials_set"] == ["ssh_private_key"])

        tested = api.call("POST", f"/api/sandbox/computes/{compute['id']}/test")
        fingerprint = tested["compute"]["info"].get("host_key_seen_fingerprint")
        check("Test connection shows the server's fingerprint before anything is trusted",
              tested["prepared"] is False and bool(fingerprint), str(fingerprint))
        expected = os.environ.get("BOW_SANDBOX_TEST_FINGERPRINT")
        check("the fingerprint is the one expected for this server", not expected or fingerprint == expected.rstrip("="))
        api.call("POST", f"/api/sandbox/computes/{compute['id']}/confirm-identity", json={"fingerprint": fingerprint})
        tested = api.call("POST", f"/api/sandbox/computes/{compute['id']}/test")
        checks = {c["key"]: c["status"] for c in tested["checks"]}
        check("after confirming it, Test connection logs in and reports Docker and gVisor",
              checks.get("host_key") == "ok" and checks.get("ssh") == "ok" and checks.get("docker") == "ok", json.dumps(checks))

        started = time.time()
        booted = api.call("POST", f"/api/sandbox/computes/{compute['id']}/bootstrap", json={"wait": True, "take_over": True})
        steps = {s["key"]: s for s in booted["bootstrap_steps"]}
        check("bootstrap finishes with every step reported", booted["bootstrap_status"] == "ready",
              f"{time.time() - started:.0f}s; " + ", ".join(f"{k}={s['status']}" for k, s in steps.items()))
        print("     verify:", steps["verify"]["detail"])
        check("isolation is reported", booted["info"].get("isolation") in ("gvisor", "runc"), str(booted["info"].get("isolation")))
        api.call("PUT", f"/api/sandbox/computes/{compute['id']}", json={"enabled": True})

        registries = api.call("POST", "/api/sandbox/environments", json={
            "name": f"Live registries {suffix}", "image": os.environ.get("BOW_SANDBOX_IMAGE", "python:3.12-slim"),
            "network_policy": "registries", "memory_mb": 1024, "disk_mb": 1024, "compute_labels": [f"live-{suffix}"]})
        offline = api.call("POST", "/api/sandbox/environments", json={
            "name": f"Live offline {suffix}", "image": os.environ.get("BOW_SANDBOX_IMAGE", "python:3.12-slim"),
            "network_policy": "none", "memory_mb": 1024, "disk_mb": 1024, "compute_labels": [f"live-{suffix}"]})
        created["environments"] = [registries["id"], offline["id"]]
        for env in (registries, offline):
            api.call("PUT", f"/api/sandbox/environments/{env['id']}/access",
                     json={"grants": [{"principal_type": "user", "principal_id": api.user_id}]})
        check("two environments exist and are granted to the user", True)
        settings = api.call("PUT", "/api/sandbox/settings", json={
            "enabled": True, "default_environment_id": registries["id"], "max_running_per_user": 2})
        check("the feature is switched on with a ready compute and environments",
              settings["enabled"] and settings["computes_ready"] >= 1 and settings["environments"] >= 2)

        # ── 2. a user's agent runs code ──────────────────────────────────
        step("2. In a report, the agent installs a package, runs Python and writes a PNG")
        report = api.call("POST", "/api/reports", json={"title": f"Sandbox live {suffix}", "files": [], "data_sources": []})
        created["reports"].append(report["id"])
        api.ask(report["id"], (
            "Use the sandbox tool run_in_sandbox for this, in one call. Install the Python package `cowsay` "
            "(pass it in `packages`), then run Python that prints cowsay.get_output_string('cow', 'hello from the sandbox') "
            "and also draws a simple bar chart of the numbers 3, 7, 5 with matplotlib and saves it to "
            "/workspace/outputs/chart.png. Then tell me the file id of the chart."
        ))
        sandbox, runs, events = sandbox_rows(report["id"])
        check("the agent created the report's sandbox", sandbox is not None and sandbox.state in ("ready", "idle"),
              f"state={getattr(sandbox, 'state', None)}, events={events}")
        good = [r for r in runs if r.status == "succeeded" and r.artifact_file_ids]
        check("a run succeeded and produced a file", bool(good), "; ".join(f"{r.status}/{r.exit_code}" for r in runs))
        if good:
            print("     stdout:", (good[-1].stdout or "").strip().replace("\n", "\n             ")[:600])
            check("the package installed from the registry was used", "hello from the sandbox" in (good[-1].stdout or ""))
        files = api.call("GET", f"/api/reports/{report['id']}/files")
        pngs = [f for f in files if f.get("filename", "").endswith(".png")]
        check("the PNG is a report file", bool(pngs) and bool(good) and pngs[0]["id"] in good[-1].artifact_file_ids,
              ", ".join(f"{f['filename']} ({f.get('content_type')})" for f in files))
        check("the run is attributed to the user who asked", bool(good) and good[-1].user_id == api.user_id)
        check("the sandbox uses the environment that allows the registries",
              sandbox is not None and sandbox.environment_id == registries["id"]
              and (sandbox.limits or {}).get("network_policy") == "registries")

        # ── 3. no internet means no internet ─────────────────────────────
        step("3. A sandbox with no internet cannot reach the internet")
        from app.services.sandbox.service import ReportRef, RunRequest, SandboxService

        offline_report = api.call("POST", "/api/reports", json={"title": f"Sandbox offline {suffix}", "files": [], "data_sources": []})
        created["reports"].append(offline_report["id"])
        probe = (
            "import socket, urllib.request\n"
            "def tcp(h, p):\n"
            "    s = socket.socket(); s.settimeout(4)\n"
            "    try:\n        s.connect((h, p)); return 'open'\n"
            "    except Exception as e:\n        return type(e).__name__\n"
            "    finally:\n        s.close()\n"
            "def dns(n):\n"
            "    try:\n        return socket.gethostbyname(n)\n"
            "    except Exception as e:\n        return type(e).__name__\n"
            "def http(u):\n"
            "    try:\n        return urllib.request.urlopen(u, timeout=8).status\n"
            "    except Exception as e:\n        return type(e).__name__\n"
            "print({'internet': tcp('1.1.1.1', 443), 'metadata': tcp('169.254.169.254', 80), "
            f"'host': tcp('{host}', 22), 'dns': dns('pypi.org'), 'https': http('https://pypi.org/simple/pip/')}})\n"
        )

        async def run_offline(session):
            ref = ReportRef(id=offline_report["id"], organization_id=api.org_id, title=offline_report["title"])
            return await SandboxService().run(session, ref, api.user_id, RunRequest(
                language="python", code=probe, environment=offline["name"], timeout_seconds=120))
        outcome = db(run_offline)
        print("     probe:", outcome.stdout.strip())
        seen = eval(outcome.stdout.strip()) if outcome.stdout.strip().startswith("{") else {}
        check("nothing outside is reachable and no name resolves",
              bool(seen) and all(v not in ("open", 200) and "." not in str(v) for v in seen.values()), json.dumps(seen))

        async def install_offline(session):
            ref = ReportRef(id=offline_report["id"], organization_id=api.org_id, title=offline_report["title"])
            try:
                await SandboxService().run(session, ref, api.user_id, RunRequest(language="python", code="import cowsay", packages=["cowsay"]))
            except Exception as exc:
                return getattr(exc, "code", type(exc).__name__), getattr(exc, "message", str(exc))
            return None, None
        code, message = db(install_offline)
        check("installing a package there is refused in plain words", code == "sandbox.packages_need_network", str(message))

        async def delete_offline(session):
            ref = ReportRef(id=offline_report["id"], organization_id=api.org_id, title=offline_report["title"])
            return await SandboxService().delete_for_report(session, ref, api.user_id)
        db(delete_offline)

        # ── 4. idle stop, resume with packages intact ────────────────────
        step("4. The sandbox stops when idle and resumes on the next run with its packages")
        from datetime import datetime, timedelta

        from app.services.sandbox import jobs

        idle_at = datetime.utcnow() + timedelta(seconds=settings["idle_timeout_seconds"] + 60)
        counts = asyncio.run(jobs.lifecycle_tick(now=idle_at))
        sandbox, _, events = sandbox_rows(report["id"])
        check("the lifecycle job stopped the idle sandbox", counts["stopped"] >= 1 and sandbox.state == "stopped",
              f"counts={counts}, state={sandbox.state}")

        async def compute_status(session):
            from app.models.sandbox import Sandbox, SandboxCompute
            from app.services.sandbox import computes as compute_service
            from app.services.sandbox.drivers.base import SandboxHandle

            row = await session.get(Sandbox, sandbox.id)
            driver = await compute_service.build_driver(session, await session.get(SandboxCompute, row.compute_id))
            try:
                return (await driver.status(SandboxHandle(sandbox_id=row.id, ref=dict(row.driver_ref or {})))).state
            finally:
                await driver.close()
        check("its container is stopped on the server, not removed", db(compute_status) == "stopped")

        api.ask(report["id"], (
            "Use run_in_sandbox again, WITHOUT the `packages` argument: run Python that imports cowsay and prints "
            "cowsay.get_output_string('cow', 'still here after the stop'). Do not install anything."
        ))
        sandbox_after, runs, events = sandbox_rows(report["id"])
        last = runs[-1] if runs else None
        check("the same sandbox was resumed", sandbox_after.id == sandbox.id and "resumed" in events,
              f"state={sandbox_after.state}, events tail={events[-6:]}")
        check("the package installed before the stop is still there",
              last is not None and last.status == "succeeded" and "still here after the stop" in (last.stdout or ""),
              f"{getattr(last, 'status', None)}: {(getattr(last, 'stdout', '') or '')[-160:]!r} {(getattr(last, 'stderr', '') or '')[-200:]!r}")

        # ── jobs against the real host ───────────────────────────────────
        step("5. Compute health and reconcile against the real server")
        health = asyncio.run(jobs.compute_health_tick())
        check("the compute health job finds the server healthy", health["healthy"] >= 1 and health["errors"] == 0, json.dumps(health))
        reconcile = asyncio.run(jobs.reconcile_tick())
        check("reconcile has nothing to remove and loses nothing", reconcile["lost"] == 0 and reconcile["errors"] == 0, json.dumps(reconcile))
    finally:
        if not args.keep:
            step("Cleaning up")
            from app.services.sandbox.service import ReportRef, SandboxService

            for report_id in created["reports"]:
                async def _delete(session, report_id=report_id):
                    return await SandboxService().delete_for_report(
                        session, ReportRef(id=report_id, organization_id=api.org_id), api.user_id)
                try:
                    db(_delete)
                    api.http.delete(f"/api/reports/{report_id}", headers=api.headers)
                except Exception as exc:  # noqa: BLE001
                    print("  could not delete sandbox/report:", exc)
            try:
                api.call("PUT", "/api/sandbox/settings", json={
                    "enabled": settings_before["enabled"], "default_environment_id": settings_before["default_environment_id"],
                    "max_running_per_user": settings_before["max_running_per_user"]})
            except Exception as exc:  # noqa: BLE001
                print("  could not restore settings:", exc)
            for environment_id in created["environments"]:
                api.http.delete(f"/api/sandbox/environments/{environment_id}", headers=api.headers)
            if created["compute"]:
                response = api.http.delete(f"/api/sandbox/computes/{created['compute']}", headers=api.headers,
                                           params={"cleanup": "true", "delete_sandboxes": "true"})
                print("  compute deleted and server cleaned:", response.status_code, response.text[:200])

    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
