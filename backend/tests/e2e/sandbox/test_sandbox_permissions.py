"""Who may use agent sandboxes (RBAC contract).

Three permissions, each off by default:

- ``use_agent_sandbox`` (organization): the tools are offered to this user's
  agent. Without it the user is neither offered them nor able to use them.
- ``use`` on an environment (per resource, to a user, group or role): without
  it the environment is neither listed nor usable.
- ``manage_sandboxes`` (organization): the admin endpoints. Separate from
  ``manage_settings``.

A sandbox belongs to its report and is shared, but every action is checked
against, and attributed to, the user who triggered it.
"""
import uuid

import pytest

from app.ee.audit.models import AuditLog
from app.models.sandbox import SandboxRun
from app.services.sandbox import access as access_module
from app.services.sandbox import environments as environment_service
from app.services.sandbox import errors
from app.services.sandbox.access import SandboxAccess
from app.services.sandbox.errors import SandboxError
from app.services.sandbox.service import RunRequest

from .conftest import headers

pytestmark = pytest.mark.e2e

RUN = RunRequest(language="bash", code="echo ran")


@pytest.fixture
def world(sandbox_world, enterprise_license):
    sandbox_world.add_compute("fake-1")
    return sandbox_world


@pytest.fixture
def member_with(world, invite_user_to_org, create_role, assign_role):
    """A new member holding exactly these extra organization permissions."""
    admin = world.cast["admin"]

    def _member(*permissions):
        member = invite_user_to_org(org_id=world.org_id, admin_token=admin["token"])
        role_id = None
        if permissions:
            created = create_role(name=f"role-{uuid.uuid4().hex[:8]}", permissions=list(permissions),
                                  user_token=admin["token"], org_id=world.org_id)
            assert created.status_code == 200, created.text
            role_id = created.json()["id"]
            assigned = assign_role(role_id=role_id, principal_type="user", principal_id=member["user_id"],
                                   user_token=admin["token"], org_id=world.org_id)
            assert assigned.status_code == 200, assigned.text
        return {**member, "role_id": role_id, "headers": headers(member["token"], world.org_id)}

    return _member


@pytest.fixture
def report(world, create_report):
    def _new(title=None):
        return create_report(title=title or f"Report {uuid.uuid4().hex[:6]}",
                             user_token=world.cast["admin"]["token"], org_id=world.org_id)
    return _new


def _grant(world, environment_id, *principals):
    async def _go(db):
        env = await environment_service.get_environment(db, world.org_id, environment_id)
        return await access_module.set_environment_grants(
            db, world.org_id, world.admin_id, env,
            [{"principal_type": kind, "principal_id": pid} for kind, pid in principals])
    return world.call(_go)


def _offered(world, user_id):
    return world.call(lambda db: SandboxAccess().tools_offered(db, world.org_id, user_id))


def _usable(world, user_id):
    return {e.name for e in world.call(lambda db: SandboxAccess().usable_environments(db, world.org_id, user_id))}


# ── use_agent_sandbox ────────────────────────────────────────────────────


def test_a_member_does_not_hold_use_agent_sandbox_by_default(world, member_with, report):
    env = world.add_environment("Default", is_default=True)
    plain = member_with()
    _grant(world, env, ("user", plain["user_id"]))  # even with the environment granted

    assert _offered(world, plain["user_id"]) is False
    with pytest.raises(SandboxError) as refused:
        world.run_code(report(), plain["user_id"], RUN)
    assert refused.value.code == errors.FORBIDDEN and refused.value.status == 403


def test_a_member_granted_use_agent_sandbox_and_the_environment_can_run(world, member_with, report):
    env = world.add_environment("Default", is_default=True)
    user = member_with("use_agent_sandbox")
    _grant(world, env, ("user", user["user_id"]))

    assert _offered(world, user["user_id"]) is True
    outcome = world.run_code(report(), user["user_id"], RUN)
    assert outcome.stdout == "ran\n"


def test_nobody_acts_without_a_user(world, report):
    world.add_environment("Default", is_default=True)
    with pytest.raises(SandboxError) as refused:
        world.run_code(report(), None, RUN)
    assert refused.value.code == errors.FORBIDDEN


def test_a_user_from_another_organization_is_refused(world, bootstrap_admin, report):
    world.add_environment("Default", is_default=True)
    outsider = bootstrap_admin("outsider")  # a full admin, but of a different organization
    assert _offered(world, outsider["user_id"]) is False
    with pytest.raises(SandboxError) as refused:
        world.run_code(report(), outsider["user_id"], RUN)
    assert refused.value.code == errors.FORBIDDEN


# ── use on an environment ────────────────────────────────────────────────


def test_without_use_an_environment_is_neither_listed_nor_usable(world, member_with, report):
    world.add_environment("Open", is_default=True)
    locked = world.add_environment("With internet", network_policy="full")
    user = member_with("use_agent_sandbox")

    # No grant at all: nothing to offer, nothing to run in.
    assert _usable(world, user["user_id"]) == set() and _offered(world, user["user_id"]) is False
    with pytest.raises(SandboxError) as nothing:
        world.run_code(report(), user["user_id"], RUN)
    assert nothing.value.code == errors.NO_ENVIRONMENT

    open_env = next(e for e in world.call(lambda db: environment_service.list_environments(db, world.org_id)) if e.name == "Open")
    _grant(world, open_env.id, ("user", user["user_id"]))
    assert _usable(world, user["user_id"]) == {"Open"}

    with pytest.raises(SandboxError) as not_allowed:
        world.run_code(report(), user["user_id"], RunRequest(language="bash", code="true", environment="With internet"))
    assert not_allowed.value.code == errors.ENVIRONMENT_NOT_ALLOWED
    assert "Open" in not_allowed.value.message
    # By id is no way round it either.
    with pytest.raises(SandboxError) as by_id:
        world.run_code(report(), user["user_id"], RunRequest(language="bash", code="true", environment=locked))
    assert by_id.value.code == errors.ENVIRONMENT_NOT_ALLOWED


@pytest.mark.parametrize("through", ["user", "role", "group"])
def test_use_can_be_granted_to_a_user_a_role_or_a_group(world, member_with, report, through, test_client):
    env = world.add_environment("Default", is_default=True)
    user = member_with("use_agent_sandbox")
    other = member_with("use_agent_sandbox")
    admin = world.cast["admin"]
    if through == "user":
        principal = ("user", user["user_id"])
    elif through == "role":
        principal = ("role", user["role_id"])
    else:
        created = test_client.post(f"/api/organizations/{world.org_id}/groups", json={"name": f"g-{uuid.uuid4().hex[:6]}"},
                                   headers=admin["headers"])
        assert created.status_code in (200, 201), created.text
        group_id = created.json()["id"]
        added = test_client.post(f"/api/organizations/{world.org_id}/groups/{group_id}/members",
                                 json={"user_id": user["user_id"]}, headers=admin["headers"])
        assert added.status_code in (200, 201), added.text
        principal = ("group", group_id)

    assert _grant(world, env, principal) == [{"principal_type": principal[0], "principal_id": principal[1]}]
    assert _usable(world, user["user_id"]) == {"Default"}
    assert _usable(world, other["user_id"]) == set(), "the grant reaches only its principal"
    assert world.run_code(report(), user["user_id"], RUN).status == "succeeded"


def test_revoking_use_takes_the_environment_away_again(world, member_with, report):
    env = world.add_environment("Default", is_default=True)
    user = member_with("use_agent_sandbox")
    rep = report()
    _grant(world, env, ("user", user["user_id"]))
    world.run_code(rep, user["user_id"], RUN)

    assert _grant(world, env) == []
    assert _usable(world, user["user_id"]) == set()
    with pytest.raises(SandboxError) as refused:
        world.run_code(rep, user["user_id"], RUN)  # the sandbox exists, but its environment is no longer theirs
    assert refused.value.code == errors.ENVIRONMENT_NOT_ALLOWED
    changes = world.rows(AuditLog, AuditLog.action == "sandbox_environment.access_changed", AuditLog.resource_id == env)
    assert len(changes) == 2 and all(c.details["title"] == "Default" for c in changes)
    assert {bool(c.details["granted"]) for c in changes} == {True, False}


def test_grants_only_name_principals_of_this_organization(world, bootstrap_admin):
    env = world.add_environment("Default")
    outsider = bootstrap_admin("outsider")
    for principal in (("user", outsider["user_id"]), ("group", str(uuid.uuid4())), ("role", str(uuid.uuid4()))):
        with pytest.raises(SandboxError):
            _grant(world, env, principal)
    with pytest.raises(SandboxError):
        _grant(world, env, ("service", "x"))


def test_a_disabled_environment_is_not_offered(world, member_with):
    env = world.add_environment("Paused", is_default=True, enabled=False)
    user = member_with("use_agent_sandbox")
    _grant(world, env, ("user", user["user_id"]))
    assert _usable(world, user["user_id"]) == set() and _offered(world, user["user_id"]) is False


# ── the shared sandbox: every action checked against who triggered it ────


def test_each_run_in_a_shared_sandbox_is_checked_and_attributed_to_its_user(world, member_with, report):
    env = world.add_environment("Default", is_default=True)
    rep = report("Shared analysis")
    created = world.run_code(rep, world.admin_id, RUN)  # the admin creates the report's sandbox

    allowed = member_with("use_agent_sandbox")
    not_granted = member_with("use_agent_sandbox")
    no_permission = member_with()
    _grant(world, env, ("user", allowed["user_id"]), ("user", no_permission["user_id"]))

    second = world.run_code(rep, allowed["user_id"], RUN)
    assert second.sandbox_id == created.sandbox_id, "the report's one sandbox is shared"
    for user, code in ((not_granted, errors.ENVIRONMENT_NOT_ALLOWED), (no_permission, errors.FORBIDDEN)):
        with pytest.raises(SandboxError) as refused:
            world.run_code(rep, user["user_id"], RUN)
        assert refused.value.code == code

    runs = {r.id: r.user_id for r in world.rows(SandboxRun, SandboxRun.sandbox_id == created.sandbox_id)}
    assert runs == {created.run_id: world.admin_id, second.run_id: allowed["user_id"]}
    audited = {a.details["run_id"]: a.user_id for a in world.rows(AuditLog, AuditLog.action == "sandbox.run")}
    assert audited == runs
    assert world.sandbox_of(rep).user_id == world.admin_id, "the creator stays the creator"


def test_the_tools_are_not_offered_without_a_compute_that_can_run_an_environment(sandbox_world, enterprise_license,
                                                                               invite_user_to_org, create_role, assign_role):
    world = sandbox_world
    env = world.add_environment("Strict", is_default=True, require_strong_isolation=True)
    assert _offered(world, world.admin_id) is False, "no compute at all"
    world.add_compute("standard", strong_isolation=False)
    assert _offered(world, world.admin_id) is False, "no compute with what the environment requires"
    disabled = world.add_compute("strong-but-disabled", enabled=False)
    assert _offered(world, world.admin_id) is False
    world.add_compute("strong")
    assert _offered(world, world.admin_id) is True
    assert env and disabled


# ── manage_sandboxes ─────────────────────────────────────────────────────


def test_admin_endpoints_need_manage_sandboxes_not_manage_settings(world, member_with, test_client):
    settings_admin = member_with("manage_settings")
    sandbox_admin = member_with("manage_sandboxes")
    sandbox_user = member_with("use_agent_sandbox")

    assert test_client.get("/api/sandbox/status", headers=settings_admin["headers"]).status_code == 403
    assert test_client.get("/api/sandbox/status", headers=sandbox_user["headers"]).status_code == 403
    assert test_client.get("/api/sandbox/status", headers=sandbox_admin["headers"]).status_code == 200
    assert test_client.get("/api/sandbox/status", headers=world.cast["admin"]["headers"]).status_code == 200


def test_managing_sandboxes_does_not_by_itself_let_a_user_run_code(world, member_with, report):
    world.add_environment("Default", is_default=True)
    manager = member_with("manage_sandboxes")
    assert _offered(world, manager["user_id"]) is False
    with pytest.raises(SandboxError) as refused:
        world.run_code(report(), manager["user_id"], RUN)
    assert refused.value.code == errors.FORBIDDEN
