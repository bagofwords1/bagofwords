"""Fixtures for audit-log stream / query e2e tests.

Reuses the RBAC cast builders (fresh admin + org per call, member invites,
custom roles) and runs one mock SIEM consumer per module.
"""
import pytest

from tests.e2e.rbac.conftest import (  # noqa: F401  (pytest fixtures)
    _rbac_relaxed_signup_flags,
    assign_role,
    bootstrap_admin,
    create_role,
    enterprise_license,
    invite_user_to_org,
)
from tests.mocks.siem_consumer import MockSiem


@pytest.fixture(scope="module")
def siem():
    m = MockSiem(port=0, syslog_port=None).start()
    yield m
    m.stop()


@pytest.fixture(autouse=True)
def _clean_siem(request):
    if "siem" in request.fixturenames:
        m = request.getfixturevalue("siem")
        m.state.faults.clear()
        m.state.reset()
        m.state.config.update({"hec_token": "demo-hec-token", "hmac_secret": "demo-hmac-secret"})
    yield


def h(token: str, org_id: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}
