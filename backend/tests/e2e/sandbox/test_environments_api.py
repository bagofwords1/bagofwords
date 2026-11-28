"""Settings → Sandbox → Environments: the admin API.

An admin with ``manage_sandboxes`` defines the templates sandboxes are
created from and says who may use each. The contract: secrets go in and
never come out, every change is audited with the environment's name, the
built-in "Default" environment appears once with no internet, and granting
``use`` through this API is what makes an environment usable.
"""
import json
import uuid

import pytest

from app.ee.audit.models import AuditLog
from app.services.sandbox.access import SandboxAccess

from .conftest import headers

pytestmark = pytest.mark.e2e

BASE = "/api/sandbox/environments"


@pytest.fixture
def world(sandbox_world, enterprise_license):
    return sandbox_world


@pytest.fixture
def admin(world):
    return world.cast["admin"]["headers"]


def _create(test_client, admin, **fields):
    body = {"name": f"env-{uuid.uuid4().hex[:6]}", "image": "registry.example.com/team/python:3.12", **fields}
    return test_client.post(BASE, json=body, headers=admin)


def _audited(world, action, resource_id):
    return [a for a in world.rows(AuditLog, AuditLog.action == action) if a.resource_id == resource_id]


# ── who may call it ──────────────────────────────────────────────────────


def test_only_manage_sandboxes_opens_the_environments_api(world, test_client, invite_user_to_org, create_role, assign_role):
    owner = world.cast["admin"]

    def member_with(permission):
        member = invite_user_to_org(org_id=world.org_id, admin_token=owner["token"])
        role = create_role(name=f"r-{uuid.uuid4().hex[:6]}", permissions=[permission], user_token=owner["token"], org_id=world.org_id).json()
        assign_role(role_id=role["id"], principal_type="user", principal_id=member["user_id"], user_token=owner["token"], org_id=world.org_id)
        return headers(member["token"], world.org_id)

    env_id = _create(test_client, owner["headers"]).json()["id"]
    plain = world.cast["member"]["headers"]
    for who in (plain, member_with("manage_settings"), member_with("use_agent_sandbox")):
        assert test_client.get(BASE, headers=who).status_code == 403
        assert _create(test_client, who).status_code == 403
        assert test_client.put(f"{BASE}/{env_id}", json={"description": "x"}, headers=who).status_code == 403
        assert test_client.put(f"{BASE}/{env_id}/access", json={"grants": []}, headers=who).status_code == 403
        assert test_client.delete(f"{BASE}/{env_id}", headers=who).status_code == 403
    sandbox_admin = member_with("manage_sandboxes")
    assert test_client.get(BASE, headers=sandbox_admin).status_code == 200
    assert _create(test_client, sandbox_admin).status_code == 200


# ── the built-in environment ─────────────────────────────────────────────


def test_the_built_in_default_environment_appears_once_with_no_internet(world, test_client, admin):
    first = test_client.get(BASE, headers=admin).json()
    assert [e["name"] for e in first] == ["Default"]
    default = first[0]
    assert default["network_policy"] == "none" and default["is_default"] is True and default["enabled"] is True
    assert default["secret_names"] == [] and default["access"] == []
    assert default["image"].startswith("bagofwords/sandbox:")
    assert [e["name"] for e in test_client.get(BASE, headers=admin).json()] == ["Default"], "not seeded twice"

    # An admin who deletes it does not get it back.
    assert test_client.delete(f"{BASE}/{default['id']}", headers=admin).status_code == 200
    assert test_client.get(BASE, headers=admin).json() == []


def test_the_built_in_environment_does_not_take_the_default_from_one_that_has_it(world, test_client, admin):
    mine = _create(test_client, admin, name="Mine", is_default=True).json()
    listed = {e["name"]: e for e in test_client.get(BASE, headers=admin).json()}
    assert listed["Mine"]["is_default"] is True and listed["Default"]["is_default"] is False
    assert mine["id"] == listed["Mine"]["id"]


# ── create and read ──────────────────────────────────────────────────────


def test_an_environment_is_created_with_safe_defaults(world, test_client, admin):
    created = _create(test_client, admin, name="Analytics", description="Pandas and friends")
    assert created.status_code == 200, created.text
    env = created.json()
    assert env["network_policy"] == "none" and env["require_strong_isolation"] is False
    assert (env["cpus"], env["memory_mb"], env["disk_mb"], env["run_timeout_seconds"]) == (1.0, 2048, 5120, 600)
    assert env["ports"] == [] and env["allowed_domains"] == [] and env["warnings"] == []
    assert test_client.get(f"{BASE}/{env['id']}", headers=admin).json() == env
    audit = _audited(world, "sandbox_environment.created", env["id"])
    assert len(audit) == 1 and audit[0].details["title"] == "Analytics" and audit[0].user_id == world.admin_id


def test_secrets_and_the_registry_password_go_in_and_never_come_out(world, test_client, admin):
    secret, password = f"sk-{uuid.uuid4().hex}", f"pw-{uuid.uuid4().hex}"
    env = _create(test_client, admin, secrets={"API_KEY": secret, "DB_URL": "postgres://u:p@h/d"},
                  env_vars={"REGION": "eu"}, registry_username="bot", registry_password=password).json()
    assert env["secret_names"] == ["API_KEY", "DB_URL"] and env["has_registry_password"] is True
    assert env["env_vars"] == {"REGION": "eu"}, "plain variables are readable; secrets are not"
    everything = json.dumps([
        env, test_client.get(f"{BASE}/{env['id']}", headers=admin).json(), test_client.get(BASE, headers=admin).json(),
        test_client.put(f"{BASE}/{env['id']}", json={"description": "edited"}, headers=admin).json(),
    ]) + json.dumps([a.details for a in world.rows(AuditLog, AuditLog.resource_id == env["id"])])
    assert secret not in everything and password not in everything and "postgres://" not in everything


@pytest.mark.parametrize("fields, status", [
    ({"image": "--privileged"}, 422),
    ({"image": "img; rm -rf /"}, 422),
    ({"network_policy": "open"}, 422),
    ({"memory_mb": 1}, 422),
    ({"run_timeout_seconds": 100000}, 422),
    ({"env_vars": {"BAD NAME": "x"}}, 422),
    ({"allowed_domains": ["https://pypi.org/simple"]}, 422),
    ({"ports": [22]}, 422),
    ({"no_such_setting": True}, 422),
])
def test_invalid_environments_are_refused(world, test_client, admin, fields, status):
    assert _create(test_client, admin, **fields).status_code == status


def test_environment_names_are_unique_and_a_deleted_name_can_be_reused(world, test_client, admin):
    first = _create(test_client, admin, name="Reports").json()
    clash = _create(test_client, admin, name="reports")
    assert clash.status_code == 409 and clash.json()["error_code"] == "sandbox.name_taken"
    assert test_client.delete(f"{BASE}/{first['id']}", headers=admin).status_code == 200
    assert _create(test_client, admin, name="Reports").status_code == 200
    assert test_client.get(f"{BASE}/{first['id']}", headers=admin).status_code == 404


def test_there_is_one_default_environment_at_a_time(world, test_client, admin):
    first = _create(test_client, admin, is_default=True).json()
    second = _create(test_client, admin, is_default=True).json()
    listed = {e["id"]: e["is_default"] for e in test_client.get(BASE, headers=admin).json()}
    assert listed[second["id"]] is True and listed[first["id"]] is False
    test_client.put(f"{BASE}/{first['id']}", json={"is_default": True}, headers=admin)
    listed = {e["id"]: e["is_default"] for e in test_client.get(BASE, headers=admin).json()}
    assert listed[first["id"]] is True and listed[second["id"]] is False


# ── edit ─────────────────────────────────────────────────────────────────


def test_an_edit_changes_only_what_it_mentions(world, test_client, admin):
    env = _create(test_client, admin, description="before", memory_mb=1024, secrets={"A": "1", "B": "2"},
                  registry_username="bot", registry_password="pw").json()
    edited = test_client.put(f"{BASE}/{env['id']}", json={"description": "after", "network_policy": "registries"}, headers=admin).json()
    assert (edited["description"], edited["network_policy"]) == ("after", "registries")
    assert edited["memory_mb"] == 1024 and edited["secret_names"] == ["A", "B"] and edited["has_registry_password"] is True
    assert edited["image"] == env["image"] and edited["name"] == env["name"]
    changed = _audited(world, "sandbox_environment.updated", env["id"])
    assert sorted(changed[-1].details["changed"]) == ["description", "network_policy"]


def test_secrets_are_set_and_removed_one_by_one_and_only_their_names_are_audited(world, test_client, admin):
    env = _create(test_client, admin, secrets={"KEEP": "k", "DROP": "d"}).json()
    new_value = f"v-{uuid.uuid4().hex}"
    edited = test_client.put(f"{BASE}/{env['id']}", json={"secrets": {"DROP": None, "NEW": new_value}}, headers=admin).json()
    assert edited["secret_names"] == ["KEEP", "NEW"]
    audit = _audited(world, "sandbox_environment.secrets_changed", env["id"])
    assert len(audit) == 1 and audit[0].details["protected_variable_names"] == ["DROP", "NEW"]
    assert new_value not in json.dumps(audit[0].details)
    cleared = test_client.put(f"{BASE}/{env['id']}", json={"registry_password": ""}, headers=admin).json()
    assert cleared["has_registry_password"] is False


def test_a_new_image_is_audited_and_pulled_on_the_computes_that_can_run_it(world, test_client, admin):
    world.add_compute("plain")
    world.add_compute("gpu", labels=["gpu"])
    world.add_compute("not-bootstrapped", bootstrap=False)
    env = _create(test_client, admin, compute_labels=["gpu"]).json()
    assert env["image_digest"] is None

    edited = test_client.put(f"{BASE}/{env['id']}", json={"image": "registry.example.com/team/python:3.13"}, headers=admin).json()
    assert edited["image"].endswith(":3.13")
    assert len(_audited(world, "sandbox_environment.image_changed", env["id"])) == 1

    pulled = test_client.post(f"{BASE}/{env['id']}/pull", headers=admin)
    assert pulled.status_code == 200 and pulled.json()["computes"] == {"gpu": "pulled"}, "only the matching, bootstrapped compute"
    after = test_client.get(f"{BASE}/{env['id']}", headers=admin).json()
    assert after["image_digest"].startswith("sha256:"), "the image is pinned by the digest it resolved to"
    # Changing the image again clears the pin until it is pulled again.
    again = test_client.put(f"{BASE}/{env['id']}", json={"image": "registry.example.com/team/python:3.14"}, headers=admin).json()
    assert again["image_digest"] is None


# ── warnings ─────────────────────────────────────────────────────────────


def test_secrets_with_full_internet_get_a_warning(world, test_client, admin):
    risky = _create(test_client, admin, secrets={"API_KEY": "x"}, network_policy="full").json()
    assert len(risky["warnings"]) == 1 and "secrets" in risky["warnings"][0] and "internet" in risky["warnings"][0]
    safe = _create(test_client, admin, secrets={"API_KEY": "x"}, network_policy="registries").json()
    assert safe["warnings"] == []


def test_an_environment_no_compute_can_run_says_so(world, test_client, admin):
    assert _create(test_client, admin, require_strong_isolation=True).json()["warnings"] == [], "no compute yet: nothing to compare with"
    world.add_compute("standard", strong_isolation=False)
    strict = _create(test_client, admin, require_strong_isolation=True).json()
    assert len(strict["warnings"]) == 1 and "strong isolation" in strict["warnings"][0]
    labelled = _create(test_client, admin, compute_labels=["gpu"]).json()
    assert len(labelled["warnings"]) == 1 and "labels" in labelled["warnings"][0]
    world.add_compute("strong", labels=["gpu"])
    assert test_client.get(f"{BASE}/{strict['id']}", headers=admin).json()["warnings"] == []
    assert test_client.get(f"{BASE}/{labelled['id']}", headers=admin).json()["warnings"] == []


# ── who may use it ───────────────────────────────────────────────────────


def test_granting_use_through_the_api_is_what_makes_an_environment_usable(world, test_client, admin, invite_user_to_org,
                                                                        create_role, assign_role):
    owner = world.cast["admin"]
    world.add_compute("b")
    env = _create(test_client, admin, name="Team env").json()
    member = invite_user_to_org(org_id=world.org_id, admin_token=owner["token"])
    role = create_role(name="Sandbox users", permissions=["use_agent_sandbox"], user_token=owner["token"], org_id=world.org_id).json()
    assign_role(role_id=role["id"], principal_type="user", principal_id=member["user_id"], user_token=owner["token"], org_id=world.org_id)

    def usable():
        return {e.name for e in world.call(lambda db: SandboxAccess().usable_environments(db, world.org_id, member["user_id"]))}

    assert usable() == set()
    granted = test_client.put(f"{BASE}/{env['id']}/access", json={"grants": [{"principal_type": "role", "principal_id": role["id"]}]}, headers=admin)
    assert granted.status_code == 200 and granted.json() == [{"principal_type": "role", "principal_id": role["id"]}]
    assert usable() == {"Team env"}
    assert test_client.get(f"{BASE}/{env['id']}", headers=admin).json()["access"] == granted.json()
    assert world.call(lambda db: SandboxAccess().tools_offered(db, world.org_id, member["user_id"])) is True

    assert test_client.put(f"{BASE}/{env['id']}/access", json={"grants": []}, headers=admin).json() == []
    assert usable() == set()
    changes = _audited(world, "sandbox_environment.access_changed", env["id"])
    assert len(changes) == 2 and all(c.details["title"] == "Team env" for c in changes)


def test_access_can_only_name_principals_of_this_organization(world, test_client, admin):
    env = _create(test_client, admin).json()
    for grant in ({"principal_type": "user", "principal_id": str(uuid.uuid4())},
                  {"principal_type": "role", "principal_id": str(uuid.uuid4())}):
        assert test_client.put(f"{BASE}/{env['id']}/access", json={"grants": [grant]}, headers=admin).status_code == 404
    bad_kind = test_client.put(f"{BASE}/{env['id']}/access", json={"grants": [{"principal_type": "everyone", "principal_id": "x"}]}, headers=admin)
    assert bad_kind.status_code == 422
    assert test_client.put(f"{BASE}/{uuid.uuid4()}/access", json={"grants": []}, headers=admin).status_code == 404


def test_environment_grants_do_not_show_up_in_the_generic_grants_list(world, test_client, admin):
    env = _create(test_client, admin).json()
    test_client.put(f"{BASE}/{env['id']}/access", json={"grants": [{"principal_type": "user", "principal_id": world.admin_id}]}, headers=admin)
    listed = test_client.get(f"/api/organizations/{world.org_id}/resource-grants", headers=admin)
    assert listed.status_code == 200
    assert not [g for g in listed.json() if g["resource_type"] == "sandbox_environment"]


# ── delete ───────────────────────────────────────────────────────────────


def test_deleting_an_environment_removes_it_as_a_default_everywhere(world, test_client, admin):
    from app.services.sandbox import environments as environment_service
    from app.services.sandbox.settings import load_settings

    env = _create(test_client, admin, name="Going away", secrets={"S": "x"}).json()
    agent_id = str(uuid.uuid4())
    world.call(lambda db: environment_service.set_agent_default(db, world.org_id, world.admin_id, agent_id, env["id"]))
    world.configure(default_environment_id=env["id"])

    assert test_client.delete(f"{BASE}/{env['id']}", headers=admin).json() == {"deleted": True}
    assert test_client.get(f"{BASE}/{env['id']}", headers=admin).status_code == 404
    assert test_client.delete(f"{BASE}/{env['id']}", headers=admin).status_code == 404
    assert world.call(lambda db: environment_service.agent_default_environment_id(db, [agent_id])) is None
    assert world.call(lambda db: load_settings(db, world.org_id)).default_environment_id is None
    audit = _audited(world, "sandbox_environment.deleted", env["id"])
    assert len(audit) == 1 and audit[0].details["title"] == "Going away"


def test_deleting_an_environment_removes_its_grants_and_is_refused_while_its_sandboxes_exist(world, test_client, admin, create_report, create_role):
    from app.models.sandbox import Sandbox
    from app.services.sandbox import access as access_module
    from app.services.sandbox.service import RunRequest

    world.add_compute("fake-1")
    env = _create(test_client, admin, name="Internet", network_policy="full", is_default=True, image="fake-image").json()
    role = create_role(name="Analysts", permissions=["use_agent_sandbox"], user_token=world.cast["admin"]["token"], org_id=world.org_id)
    role = role if isinstance(role, dict) else role.json()
    granted = test_client.put(f"{BASE}/{env['id']}/access", json={"grants": [{"principal_type": "role", "principal_id": role["id"]}]}, headers=admin)
    assert granted.status_code == 200 and len(granted.json()) == 1, granted.text
    rep = create_report(title="r", user_token=world.cast["admin"]["token"], org_id=world.org_id)
    sandbox_id = world.run_code(rep, world.admin_id, RunRequest(language="python", code="print('x')")).sandbox_id

    refused = test_client.delete(f"{BASE}/{env['id']}", headers=admin)
    assert refused.status_code == 409 and refused.json()["error_code"] == "sandbox.environment_in_use"
    assert test_client.get(f"{BASE}/{env['id']}", headers=admin).status_code == 200, "nothing was deleted"

    assert test_client.delete(f"{BASE}/{env['id']}", params={"delete_sandboxes": "true"}, headers=admin).json() == {"deleted": True}
    assert world.rows(Sandbox, Sandbox.id == sandbox_id)[0].state == "deleted", "it no longer runs with the deleted environment's network policy"
    assert world.call(lambda db: access_module.list_environment_grants(db, world.org_id, env["id"])) == []
