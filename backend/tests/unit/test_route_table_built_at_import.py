"""The app's route table is built once, when `main` is imported, not by the
first request that reaches a router.

FastAPI builds the routes of an included router (dependency graph, request
and response models) the first time a request is matched against them, and
a request is matched against every router included before the one it lands
in. Without the build at import, the first request to a router included late
(`/api/connections`, the SCIM routes, any unknown path) paid for building
hundreds of routes: about a second on a fast machine, more than the two
seconds `POST /api/connections` is allowed in the e2e suite on a loaded CI
runner.

The process is a fresh interpreter, so no earlier test has warmed the routes.
Builds are counted on FastAPI's route-state builder; a route included after
import must still be counted, which shows the counter sees builds at all.
With SERVE_FRONTEND the app also registers the embed frame router and the
SPA catch-all, after every API router.
"""
import json
import os
import subprocess
import sys
import textwrap

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SCRIPT = textwrap.dedent("""
    import json
    import fastapi.routing as routing
    from fastapi import APIRouter
    from fastapi.testclient import TestClient

    import main

    if not hasattr(routing, "_populate_api_route_state"):
        print("ROUTE_BUILDS_UNAVAILABLE")
        raise SystemExit(0)

    builds = 0
    build = routing._populate_api_route_state

    def counting(*args, **kwargs):
        global builds
        builds += 1
        return build(*args, **kwargs)

    routing._populate_api_route_state = counting

    client = TestClient(main.app, raise_server_exceptions=False)

    def builds_for(method, path):
        before = builds
        client.request(method, path)
        return builds - before

    out = {}
    for method, path in (
        ("POST", "/api/connections"),
        ("GET", "/scim/v2/Users"),
        ("GET", "/api/route-table-probe/not-a-route"),
    ):
        out[f"{method} {path}"] = builds_for(method, path)

    # POST: the SPA catch-all, when served, answers every GET before it.
    late = APIRouter()

    @late.post("/route-table-probe/late")
    async def late_route():
        return {}

    main.app.include_router(late, prefix="/api")
    out["late"] = builds_for("POST", "/api/route-table-probe/late")
    print("ROUTE_BUILDS " + json.dumps(out))
""")


@pytest.mark.timeout(240)
@pytest.mark.parametrize("serve_frontend", [False, True], ids=["api_only", "serve_frontend"])
def test_no_request_builds_routes_after_import(serve_frontend, tmp_path):
    env = dict(os.environ)
    env.pop("SERVE_FRONTEND", None)
    if serve_frontend:
        (tmp_path / "index.html").write_text("<!doctype html>")
        env["SERVE_FRONTEND"] = "1"
        env["FRONTEND_DIST_DIR"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-c", SCRIPT], cwd=BACKEND, env=env,
        capture_output=True, text=True, timeout=230,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    lines = [line for line in proc.stdout.splitlines() if line.startswith("ROUTE_BUILDS")]
    assert lines, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert lines[-1] != "ROUTE_BUILDS_UNAVAILABLE", (
        "FastAPI no longer builds route state in routing._populate_api_route_state; "
        "check how it builds routes now before changing this test"
    )
    counts = json.loads(lines[-1].split(" ", 1)[1])
    late = counts.pop("late")
    assert late > 0, "a route included after import was not counted: the counter sees no builds"
    assert counts == {path: 0 for path in counts}, counts
