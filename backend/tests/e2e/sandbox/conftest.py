"""Fixtures shared by the agent-sandbox e2e tests."""
import pytest

# Reused RBAC cast builders (real HTTP routes only).
from tests.e2e.rbac.conftest import (  # noqa: F401  (pytest fixtures)
    _rbac_relaxed_signup_flags,
    assign_role,
    bootstrap_admin,
    create_role,
    enterprise_license,
    get_system_role,
    invite_user_to_org,
    list_roles,
)


def headers(token: str, org_id: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def sandbox_cast(bootstrap_admin, invite_user_to_org):
    """An org with its admin and one plain member: {org_id, admin, member} with ready headers."""
    admin = bootstrap_admin("sbx_admin")
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    return {
        "org_id": admin["org_id"],
        "admin": {**admin, "headers": headers(admin["token"], admin["org_id"])},
        "member": {**member, "headers": headers(member["token"], admin["org_id"])},
    }
