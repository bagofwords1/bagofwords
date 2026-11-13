"""End-to-end tests for the liveness (/health) and readiness (/health/ready)
endpoints.

The database is up for every e2e test (the run_migrations fixture), so the
happy path is what we assert over HTTP here; the database-down branch is
covered as a unit test in tests/unit/test_health.py, where the dependency can
be made to fail deterministically without taking the test database offline.
"""
from main import settings


def test_health_is_public_and_reports_version_and_uptime(test_client):
    # No auth header: a probe cannot log in, so this must answer anonymously.
    resp = test_client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    # Version echoes the deployed build so an operator can read it over HTTP.
    assert body["version"] == settings.PROJECT_VERSION
    assert isinstance(body["uptime_seconds"], (int, float))
    assert body["uptime_seconds"] >= 0
    assert body["timestamp"]
    assert body["worker"]["hostname"]
    assert isinstance(body["worker"]["pid"], int)


def test_readiness_reports_passing_dependency_checks(test_client):
    resp = test_client.get("/health/ready")

    # Database is up in the e2e environment, so the critical check passes.
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("ok", "degraded")
    assert body["version"] == settings.PROJECT_VERSION
    assert isinstance(body["uptime_seconds"], (int, float))

    assert body["timestamp"]
    assert body["worker"]["hostname"]

    checks = body["checks"]
    # The critical check must be present and passing for a 200.
    assert checks["database"]["status"] == "ok"
    assert checks["database"]["critical"] is True
    # A passing DB check reports how long the probe query took.
    assert isinstance(checks["database"]["latency_ms"], (int, float))
    assert checks["database"]["latency_ms"] >= 0
    # The scheduler is always reported, critical or not.
    assert "scheduler" in checks
    assert checks["scheduler"]["critical"] is False


def test_readiness_is_public(test_client):
    # Like /health, the readiness endpoint must be reachable without a token so
    # it can serve as a Kubernetes readiness probe / monitoring target.
    resp = test_client.get("/health/ready")
    assert resp.status_code != 401
    assert resp.status_code != 403
