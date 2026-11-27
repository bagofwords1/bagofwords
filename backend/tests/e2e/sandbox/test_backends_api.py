"""Settings → Sandbox → Backends and Settings: the admin API.

An admin with ``manage_sandboxes`` adds a backend (a form rendered from the
backend type's own schemas), tests the connection, bootstraps it, enables it,
and finally turns the feature on. The contract: keys and passwords go in and
never come out, a backend cannot be enabled before bootstrap succeeded, a
failed step is recorded and leaves the backend disabled, a backend with
sandboxes is not deleted by accident, and every change is audited.
"""
import json
import uuid

import pytest
from sqlalchemy import text

from app.ee.audit.models import AuditLog
from app.models.sandbox import Sandbox, SandboxBackend
from app.services.sandbox import errors
from app.services.sandbox.drivers import fake as fake_driver
from app.services.sandbox.errors import SandboxError
from app.services.sandbox.service import RunRequest

from .conftest import headers

pytestmark = pytest.mark.e2e

BASE = "/api/sandbox/backends"
ECHO = RunRequest(language="bash", code="echo ran")


@pytest.fixture
def world(sandbox_world, enterprise_license):
    return sandbox_world


@pytest.fixture
def admin(world):
    return world.cast["admin"]["headers"]


def _create(test_client, admin, **fields):
    body = {"name": f"backend-{uuid.uuid4().hex[:6]}", "type": "fake", **fields}
    return test_client.post(BASE, json=body, headers=admin)


def _ready(test_client, admin, **fields):
    """A bootstrapped, enabled fake backend."""
    backend = _create(test_client, admin, **fields).json()
    done = test_client.post(f"{BASE}/{backend['id']}/bootstrap", json={"wait": True}, headers=admin)
    assert done.status_code == 200 and done.json()["bootstrap_status"] == "ready", done.text
    return test_client.put(f"{BASE}/{backend['id']}", json={"enabled": True}, headers=admin).json()


def _audit(world, action, resource_id):
    return [a for a in world.rows(AuditLog, AuditLog.action == action) if a.resource_id == resource_id]


# ── who may call it ──────────────────────────────────────────────────────


def test_only_manage_sandboxes_opens_the_backends_and_settings_api(world, test_client, invite_user_to_org, create_role, assign_role):
    owner = world.cast["admin"]

    def member_with(permission):
        member = invite_user_to_org(org_id=world.org_id, admin_token=owner["token"])
        role = create_role(name=f"r-{uuid.uuid4().hex[:6]}", permissions=[permission], user_token=owner["token"], org_id=world.org_id).json()
        assign_role(role_id=role["id"], principal_type="user", principal_id=member["user_id"], user_token=owner["token"], org_id=world.org_id)
        return headers(member["token"], world.org_id)

    backend_id = _create(test_client, owner["headers"]).json()["id"]
    for who in (world.cast["member"]["headers"], member_with("manage_settings"), member_with("use_agent_sandbox")):
        assert test_client.get("/api/sandbox/backend-types", headers=who).status_code == 403
        assert test_client.get(BASE, headers=who).status_code == 403
        assert _create(test_client, who).status_code == 403
        assert test_client.post(f"{BASE}/{backend_id}/test", headers=who).status_code == 403
        assert test_client.post(f"{BASE}/{backend_id}/bootstrap", json={"wait": True}, headers=who).status_code == 403
        assert test_client.put(f"{BASE}/{backend_id}", json={"capacity": 5}, headers=who).status_code == 403
        assert test_client.delete(f"{BASE}/{backend_id}", headers=who).status_code == 403
        assert test_client.get("/api/sandbox/settings", headers=who).status_code == 403
        assert test_client.put("/api/sandbox/settings", json={"enabled": True}, headers=who).status_code == 403
    manager = member_with("manage_sandboxes")
    assert test_client.get(BASE, headers=manager).status_code == 200
    assert test_client.put("/api/sandbox/settings", json={"max_running_per_user": 4}, headers=manager).status_code == 200


# ── backend types: the form ──────────────────────────────────────────────


def test_each_backend_type_describes_its_own_form(world, test_client, admin):
    types = {t["name"]: t for t in test_client.get("/api/sandbox/backend-types", headers=admin).json()}
    server = types["docker-ssh"]
    properties = server["config_schema"]["properties"]
    assert {"address", "ssh_port", "ssh_user", "sandbox_port", "bow_address", "images_preloaded"} <= set(properties)
    assert server["config_schema"]["required"] == ["address"]
    assert properties["ssh_port"]["default"] == 22 and properties["sandbox_port"]["default"] == 8443
    assert properties["ssh_public_key"]["readOnly"] is True, "filled in by BoW, shown to the admin"
    credentials = server["credentials_schema"]["properties"]
    assert credentials["ssh_private_key"]["secret"] is True
    assert credentials["one_time_password"]["transient"] is True, "used once, never stored"
    assert {"stop_resume", "strong_isolation", "domain_allowlist", "live_stats"} <= set(server["capabilities"])
    assert "fake" in types, "offered here because the suite runs in test mode"


# ── create ───────────────────────────────────────────────────────────────


def test_a_new_backend_is_disabled_unprepared_and_shows_no_secret(world, test_client, admin):
    token = f"tok-{uuid.uuid4().hex}"
    created = _create(test_client, admin, name="sbx-1", labels=["GPU", "gpu", " eu "], capacity=7, credentials={"api_token": token})
    assert created.status_code == 200, created.text
    backend = created.json()
    assert backend["enabled"] is False and backend["bootstrap_status"] == "none" and backend["status"] == "unknown"
    assert backend["labels"] == ["gpu", "eu"] and backend["capacity"] == 7 and backend["running_sandboxes"] == 0
    assert backend["credentials_set"] == ["api_token"], "names of what is stored, never the values"
    everything = json.dumps([backend, test_client.get(BASE, headers=admin).json(),
                             test_client.get(f"{BASE}/{backend['id']}", headers=admin).json()])
    assert token not in everything
    entry = _audit(world, "sandbox_backend.created", backend["id"])
    assert len(entry) == 1 and entry[0].details["title"] == "sbx-1" and entry[0].details["stored_fields"] == ["api_token"]
    assert token not in json.dumps(entry[0].details)


def test_a_one_time_password_is_used_once_and_stored_nowhere(world, test_client, admin):
    password = f"pw-{uuid.uuid4().hex}"
    backend = _create(test_client, admin, config={"server": "pw-server"}, credentials={"api_token": "t", "one_time_password": password}).json()
    assert backend["credentials_set"] == ["api_token"]
    assert fake_driver._BACKENDS["pw-server"].passwords_used == 1, "it reached the backend exactly once"

    stored = world.rows(SandboxBackend, SandboxBackend.id == backend["id"])[0]
    assert "one_time_password" not in (stored.credentials or {})

    def raw(db):
        async def _go():
            row = (await db.execute(text("SELECT credentials, driver_state, config FROM sandbox_backends WHERE id = :id"), {"id": backend["id"]})).one()
            return " ".join(str(v) for v in row)
        return _go()
    assert password not in world.call(raw)
    assert password not in json.dumps([a.details for a in world.rows(AuditLog, AuditLog.organization_id == world.org_id)])
    # Editing with a new one uses that once too, and still stores nothing.
    test_client.put(f"{BASE}/{backend['id']}", json={"credentials": {"one_time_password": password}}, headers=admin)
    assert fake_driver._BACKENDS["pw-server"].passwords_used == 2
    assert test_client.get(f"{BASE}/{backend['id']}", headers=admin).json()["credentials_set"] == ["api_token"]


def test_a_server_backend_gets_a_generated_key_pair_and_only_the_public_key_is_shown(world, test_client, admin):
    created = test_client.post(BASE, json={"name": "sbx-linux", "type": "docker-ssh", "config": {
        "address": "sbx.example.com", "ssh_user": "ubuntu", "ssh_public_key": "ssh-ed25519 FORGED"}}, headers=admin)
    assert created.status_code == 200, created.text
    backend = created.json()
    public_key = backend["config"]["ssh_public_key"]
    assert public_key.startswith("ssh-ed25519 ") and "FORGED" not in public_key, "the public key is BoW's, not the caller's"
    assert backend["credentials_set"] == ["ssh_private_key"]
    assert "PRIVATE KEY" not in json.dumps(backend)
    assert backend["info"]["host_key_pinned"] is False

    # Editing something else keeps the same key pair.
    edited = test_client.put(f"{BASE}/{backend['id']}", json={"config": {"address": "sbx2.example.com", "ssh_user": "ubuntu"}}, headers=admin).json()
    assert edited["config"]["address"] == "sbx2.example.com" and edited["config"]["ssh_public_key"] == public_key
    # Nothing was sent to the (non-existent) server on create or edit, and it cannot be enabled yet.
    assert test_client.put(f"{BASE}/{backend['id']}", json={"enabled": True}, headers=admin).status_code == 409


@pytest.mark.parametrize("body, status", [
    ({"type": "kubernetes"}, 400),
    ({"type": "fake", "config": {"no_such_setting": 1}}, 400),
    ({"type": "docker-ssh", "config": {"address": "host; reboot"}}, 400),
    ({"type": "docker-ssh", "config": {}}, 400),
    ({"type": "docker-ssh", "config": {"address": "h"}, "credentials": {"ssh_private_key": "not a key"}}, 400),
    ({"type": "fake", "capacity": 0}, 422),
    ({"type": "fake", "name": ""}, 422),
])
def test_invalid_backends_are_refused(world, test_client, admin, body, status):
    response = test_client.post(BASE, json={"name": f"b-{uuid.uuid4().hex[:6]}", **body}, headers=admin)
    assert response.status_code == status, response.text
    assert "not a key" not in response.text


def test_backend_names_are_unique(world, test_client, admin):
    first = _create(test_client, admin, name="sbx-1").json()
    clash = _create(test_client, admin, name="SBX-1")
    assert clash.status_code == 409 and clash.json()["error_code"] == "sandbox.name_taken"
    other = _create(test_client, admin, name="sbx-2").json()
    assert test_client.put(f"{BASE}/{other['id']}", json={"name": "sbx-1"}, headers=admin).status_code == 409
    assert test_client.delete(f"{BASE}/{first['id']}", headers=admin).status_code == 200
    assert test_client.put(f"{BASE}/{other['id']}", json={"name": "sbx-1"}, headers=admin).status_code == 200


# ── test connection, bootstrap, enable ───────────────────────────────────


def test_test_connection_reports_each_check_and_changes_nothing(world, test_client, admin):
    backend = _create(test_client, admin).json()
    tested = test_client.post(f"{BASE}/{backend['id']}/test", headers=admin).json()
    assert tested["reachable"] is True and tested["prepared"] is False and tested["healthy"] is False
    assert {c["key"]: c["status"] for c in tested["checks"]} == {"connect": "ok", "isolation": "ok", "prepared": "warning"}
    assert tested["backend"]["bootstrap_status"] == "none" and tested["backend"]["last_checked_at"]

    down = _create(test_client, admin, config={"unreachable": True}).json()
    tested = test_client.post(f"{BASE}/{down['id']}/test", headers=admin).json()
    assert tested["reachable"] is False and tested["error"] and tested["backend"]["status"] == "unreachable"
    assert tested["backend"]["last_error"]


def test_bootstrap_runs_the_types_steps_and_a_backend_can_only_be_enabled_after_it(world, test_client, admin):
    backend = _create(test_client, admin, name="sbx-1").json()
    early = test_client.put(f"{BASE}/{backend['id']}", json={"enabled": True}, headers=admin)
    assert early.status_code == 409 and early.json()["error_code"] == "sandbox.backend_not_ready"

    done = test_client.post(f"{BASE}/{backend['id']}/bootstrap", json={"wait": True}, headers=admin).json()
    assert done["bootstrap_status"] == "ready" and done["bootstrapped_at"] and done["status"] == "healthy"
    assert [s["status"] for s in done["bootstrap_steps"]] == ["ok"] * 5
    assert all(s["title"] and s["started_at"] and s["finished_at"] for s in done["bootstrap_steps"])
    assert set(done["capabilities"]) == {"stop_resume", "strong_isolation", "domain_allowlist", "live_stats"}
    assert done["info"]["isolation"] == "strong" and done["enabled"] is False, "bootstrap does not enable it by itself"

    enabled = test_client.put(f"{BASE}/{backend['id']}", json={"enabled": True}, headers=admin).json()
    assert enabled["enabled"] is True
    assert [a.details["result"] for a in _audit(world, "sandbox_backend.bootstrapped", backend["id"])] == ["ready"]
    assert len(_audit(world, "sandbox_backend.enabled", backend["id"])) == 1
    # Running it again is safe.
    again = test_client.post(f"{BASE}/{backend['id']}/bootstrap", json={"wait": True}, headers=admin).json()
    assert again["bootstrap_status"] == "ready" and again["enabled"] is True


def test_a_backend_without_strong_isolation_bootstraps_with_a_warning(world, test_client, admin):
    backend = _create(test_client, admin, config={"strong_isolation": False}).json()
    done = test_client.post(f"{BASE}/{backend['id']}/bootstrap", json={"wait": True}, headers=admin).json()
    assert done["bootstrap_status"] == "ready"
    step = next(s for s in done["bootstrap_steps"] if s["key"] == "isolation")
    assert step["status"] == "warning" and "Standard isolation" in step["detail"]
    assert "strong_isolation" not in done["capabilities"] and done["info"]["isolation"] == "standard"
    assert _audit(world, "sandbox_backend.bootstrapped", backend["id"])[0].details["warnings"] == ["isolation"]


def test_a_failed_step_is_shown_and_leaves_the_backend_disabled(world, test_client, admin):
    backend = _ready(test_client, admin)
    assert backend["enabled"] is True
    test_client.put(f"{BASE}/{backend['id']}", json={"config": {"fail_step": "network"}}, headers=admin)

    failed = test_client.post(f"{BASE}/{backend['id']}/bootstrap", json={"wait": True}, headers=admin).json()
    assert failed["bootstrap_status"] == "failed" and failed["enabled"] is False
    statuses = {s["key"]: s["status"] for s in failed["bootstrap_steps"]}
    assert statuses == {"connect": "ok", "isolation": "ok", "network": "failed", "images": "skipped", "verify": "skipped"}
    assert "network" in next(s["detail"] for s in failed["bootstrap_steps"] if s["key"] == "network")
    assert failed["last_error"]
    assert test_client.put(f"{BASE}/{backend['id']}", json={"enabled": True}, headers=admin).status_code == 409
    entry = _audit(world, "sandbox_backend.bootstrapped", backend["id"])[-1]
    assert entry.details["result"] == "failed" and entry.details["failed_step"] == "network" and entry.outcome == "failure"

    # Fix it and bootstrap again: the same button repairs.
    test_client.put(f"{BASE}/{backend['id']}", json={"config": {}}, headers=admin)
    repaired = test_client.post(f"{BASE}/{backend['id']}/bootstrap", json={"wait": True}, headers=admin).json()
    assert repaired["bootstrap_status"] == "ready" and repaired["last_error"] is None


def test_bootstrap_started_from_the_ui_returns_at_once_and_cannot_be_started_twice(world, test_client, admin):
    backend = _create(test_client, admin).json()
    started = test_client.post(f"{BASE}/{backend['id']}/bootstrap", json={}, headers=admin)
    assert started.status_code == 200
    assert started.json()["bootstrap_status"] == "running"
    assert [s["status"] for s in started.json()["bootstrap_steps"]] == ["pending"] * 5, "the steps are known before they run"
    again = test_client.post(f"{BASE}/{backend['id']}/bootstrap", json={}, headers=admin)
    assert again.status_code == 409 and again.json()["error_code"] == "sandbox.bootstrap_running"


def test_a_server_owned_by_another_bow_instance_is_refused_until_taken_over(world, test_client, admin, bootstrap_admin):
    """Two organizations here stand for two BoW instances pointing at the same server."""
    other_admin = bootstrap_admin("other")
    other = headers(other_admin["token"], other_admin["org_id"])
    server = f"shared-{uuid.uuid4().hex[:6]}"
    first = _ready(test_client, other, name="theirs", config={"server": server})
    assert first["bootstrap_status"] == "ready"

    mine = _create(test_client, admin, name="mine", config={"server": server}).json()
    refused = test_client.post(f"{BASE}/{mine['id']}/bootstrap", json={"wait": True}, headers=admin).json()
    assert refused["bootstrap_status"] == "failed"
    assert "another BoW instance" in refused["bootstrap_steps"][0]["detail"] and "Take over" in refused["bootstrap_steps"][0]["detail"]
    assert refused["bootstrap_steps"][0]["code"] == "owned_by_other"  # what the UI offers Take over on

    taken = test_client.post(f"{BASE}/{mine['id']}/bootstrap", json={"wait": True, "take_over": True}, headers=admin).json()
    assert taken["bootstrap_status"] == "ready" and "Taken over" in taken["bootstrap_steps"][0]["detail"]
    assert [a.details["result"] for a in _audit(world, "sandbox_backend.taken_over", mine["id"])] == ["ready"]


# ── edit, disable, delete ────────────────────────────────────────────────


def test_disabling_a_backend_drains_it(world, test_client, admin, create_report):
    backend = _ready(test_client, admin)
    world.add_environment("Default", is_default=True)
    new_report = lambda: create_report(title=uuid.uuid4().hex, user_token=world.cast["admin"]["token"], org_id=world.org_id)  # noqa: E731
    existing = new_report()
    world.run_code(existing, world.admin_id, ECHO)
    assert test_client.get(f"{BASE}/{backend['id']}", headers=admin).json()["running_sandboxes"] == 1

    disabled = test_client.put(f"{BASE}/{backend['id']}", json={"enabled": False}, headers=admin).json()
    assert disabled["enabled"] is False and disabled["bootstrap_status"] == "ready"
    assert world.run_code(existing, world.admin_id, ECHO).stdout == "ran\n", "existing sandboxes stay usable"
    with pytest.raises(SandboxError) as refused:
        world.run_code(new_report(), world.admin_id, ECHO)
    assert refused.value.code == errors.NO_BACKEND
    assert len(_audit(world, "sandbox_backend.disabled", backend["id"])) == 1


def test_a_backend_with_sandboxes_is_not_deleted_until_they_are(world, test_client, admin, create_report):
    backend = _ready(test_client, admin, name="busy-backend")
    world.add_environment("Default", is_default=True)
    rep = create_report(title="r", user_token=world.cast["admin"]["token"], org_id=world.org_id)
    sandbox_id = world.run_code(rep, world.admin_id, ECHO).sandbox_id

    refused = test_client.delete(f"{BASE}/{backend['id']}", headers=admin)
    assert refused.status_code == 409 and refused.json()["error_code"] == "sandbox.backend_in_use"
    assert "1 sandbox" in refused.json()["detail"]
    assert test_client.get(f"{BASE}/{backend['id']}", headers=admin).status_code == 200

    deleted = test_client.delete(f"{BASE}/{backend['id']}?delete_sandboxes=true&cleanup=true", headers=admin)
    assert deleted.status_code == 200 and deleted.json() == {"deleted": True}
    assert world.rows(Sandbox, Sandbox.id == sandbox_id)[0].state == "deleted"
    assert test_client.get(f"{BASE}/{backend['id']}", headers=admin).status_code == 404
    assert backend["id"] not in [b["id"] for b in test_client.get(BASE, headers=admin).json()]
    entry = _audit(world, "sandbox_backend.deleted", backend["id"])
    assert len(entry) == 1 and entry[0].details == {"title": "busy-backend", "cleaned_up": True}
    assert world.rows(SandboxBackend, SandboxBackend.id == backend["id"])[0].credentials is None, "stored credentials are dropped"


def test_an_edit_changes_only_what_it_mentions_and_is_audited(world, test_client, admin):
    backend = _create(test_client, admin, labels=["a"], capacity=3, credentials={"api_token": "first"}).json()
    edited = test_client.put(f"{BASE}/{backend['id']}", json={"capacity": 9}, headers=admin).json()
    assert edited["capacity"] == 9 and edited["labels"] == ["a"] and edited["credentials_set"] == ["api_token"]
    assert _audit(world, "sandbox_backend.updated", backend["id"])[-1].details["changed"] == ["capacity"]
    test_client.put(f"{BASE}/{backend['id']}", json={"capacity": 9}, headers=admin)
    assert len(_audit(world, "sandbox_backend.updated", backend["id"])) == 1, "an edit that changes nothing is not an event"


# ── settings ─────────────────────────────────────────────────────────────


def test_settings_start_with_the_designs_defaults_and_the_feature_switched_off(sandbox_cast, agent_sandboxes_on, test_client):
    admin = sandbox_cast["admin"]["headers"]
    settings = test_client.get("/api/sandbox/settings", headers=admin).json()
    assert settings["enabled"] is False
    assert (settings["mark_idle_after_seconds"], settings["idle_timeout_seconds"]) == (300, 1800)
    assert (settings["stopped_retention_seconds"], settings["max_lifetime_seconds"]) == (7 * 86400, 30 * 86400)
    assert settings["max_running_per_user"] == 3 and settings["max_running_total"] is None
    assert settings["registry_domains"] == ["pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "proxy.golang.org", "sum.golang.org"]
    assert (settings["backends_ready"], settings["environments"], settings["running_sandboxes"]) == (0, 0, 0)
    assert "instance_id" not in settings


def test_settings_changes_are_partial_validated_and_audited(world, test_client, admin):
    env = world.add_environment("Analytics")
    changed = test_client.put("/api/sandbox/settings", json={
        "idle_timeout_seconds": 600, "registry_domains": ["PyPI.org", "pypi.org", "mirror.example.com"],
        "default_environment_id": env}, headers=admin)
    assert changed.status_code == 200, changed.text
    settings = changed.json()
    assert settings["idle_timeout_seconds"] == 600 and settings["default_environment_id"] == env
    assert settings["registry_domains"] == ["pypi.org", "mirror.example.com"]
    assert settings["enabled"] is True and settings["max_running_per_user"] == 3, "what was not mentioned is unchanged"
    entry = world.rows(AuditLog, AuditLog.action == "sandbox_settings.updated")[-1]
    assert entry.details["changed"]["idle_timeout_seconds"] == {"from": 1800, "to": 600}
    assert set(entry.details["changed"]) == {"idle_timeout_seconds", "registry_domains", "default_environment_id"}

    for bad in ({"idle_timeout_seconds": 1}, {"max_running_per_user": 0}, {"registry_domains": ["https://x/y"]},
                {"instance_id": "mine"}, {"no_such_setting": 1}):
        assert test_client.put("/api/sandbox/settings", json=bad, headers=admin).status_code == 422, bad
    refused_domain = test_client.put("/api/sandbox/settings", json={"registry_domains": ["https://x/y"]}, headers=admin)
    message = json.dumps(refused_domain.json())
    assert "registry_domains" in message and "pydantic" not in message and "validation error" not in message, message
    assert test_client.put("/api/sandbox/settings", json={"default_environment_id": str(uuid.uuid4())}, headers=admin).status_code == 404
    assert test_client.get("/api/sandbox/settings", headers=admin).json()["idle_timeout_seconds"] == 600


def test_the_switch_on_the_settings_tab_is_what_turns_sandboxes_on(sandbox_cast, agent_sandboxes_on, test_client, create_report, enterprise_license):
    """The whole admin path through the API: backend, bootstrap, environment, switch."""
    from .conftest import SandboxWorld
    admin = sandbox_cast["admin"]["headers"]
    world = SandboxWorld(sandbox_cast["org_id"], sandbox_cast["admin"]["user_id"])
    try:
        _ready(test_client, admin, name="sbx-1")
        env = test_client.post("/api/sandbox/environments", json={"name": "Team", "image": "fake-image", "is_default": True}, headers=admin).json()
        rep = create_report(title="r", user_token=sandbox_cast["admin"]["token"], org_id=sandbox_cast["org_id"])
        summary = test_client.get("/api/sandbox/settings", headers=admin).json()
        assert (summary["backends_ready"], summary["environments"]) == (1, 1) and summary["enabled"] is False

        with pytest.raises(SandboxError) as off:
            world.run_code(rep, world.admin_id, ECHO)
        assert off.value.code == errors.DISABLED

        assert test_client.put("/api/sandbox/settings", json={"enabled": True}, headers=admin).json()["enabled"] is True
        assert test_client.get("/api/sandbox/status", headers=admin).json() == {"feature_enabled": True, "enabled": True}
        assert world.run_code(rep, world.admin_id, ECHO).environment == env["name"]
        assert test_client.get("/api/sandbox/settings", headers=admin).json()["running_sandboxes"] == 1
    finally:
        fake_driver.reset_fake_backends()


# ── review fixes: a changed address, and two bootstraps at once ──────────


def test_pointing_a_server_backend_at_another_address_forgets_what_was_learned_about_the_old_one(world, test_client, admin):
    from sqlalchemy.orm.attributes import flag_modified

    from app.models.sandbox import SandboxBackend

    backend = test_client.post(BASE, json={"name": "sbx-linux", "type": "docker-ssh", "config": {
        "address": "old.example.com", "ssh_user": "ubuntu"}}, headers=admin).json()

    import asyncssh
    old_host_key = asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode().strip()

    async def as_if_bootstrapped(db):
        row = await db.get(SandboxBackend, backend["id"])
        row.driver_state = {"ssh_host_key": old_host_key, "prepared": True,
                            "ca_cert": "-----BEGIN CERTIFICATE-----old"}
        flag_modified(row, "driver_state")
        row.bootstrap_status, row.enabled, row.status = "ready", True, "healthy"
        await db.commit()
    world.call(as_if_bootstrapped)

    # Something that is not the server's address keeps everything.
    same = test_client.put(f"{BASE}/{backend['id']}", json={"config": {"address": "old.example.com", "ssh_user": "deploy"}}, headers=admin).json()
    assert (same["enabled"], same["bootstrap_status"], same["info"]["host_key_pinned"]) == (True, "ready", True)

    moved = test_client.put(f"{BASE}/{backend['id']}", json={"config": {"address": "new.example.com", "ssh_user": "deploy"}}, headers=admin)
    assert moved.status_code == 200, moved.text
    moved = moved.json()
    assert moved["config"]["address"] == "new.example.com"
    assert (moved["enabled"], moved["bootstrap_status"], moved["bootstrap_steps"], moved["status"]) == (False, "none", [], "unknown")
    assert moved["info"]["host_key_pinned"] is False, "the old server's host key is not the new server's"
    assert world.rows(SandboxBackend, SandboxBackend.id == backend["id"])[0].driver_state == {}
    assert test_client.put(f"{BASE}/{backend['id']}", json={"enabled": True}, headers=admin).status_code == 409
    # Rows come back unordered (Postgres keeps no insertion order): of the
    # three updates, only the move to another address records "server".
    changes = [a.details["changed"] for a in _audit(world, "sandbox_backend.updated", backend["id"])]
    assert sum("server" in c for c in changes) == 1, changes


def test_only_the_address_and_port_make_it_another_server():
    from app.services.sandbox.drivers import registry

    base = {"address": "a.example.com", "ssh_port": 22, "ssh_user": "root", "sandbox_port": 8443}
    assert registry.is_another_backend("docker-ssh", base, {**base, "address": "b.example.com"}) is True
    assert registry.is_another_backend("docker-ssh", base, {**base, "ssh_port": 2222}) is True
    assert registry.is_another_backend("docker-ssh", base, {**base, "ssh_user": "ubuntu", "sandbox_port": 9443}) is False
    assert registry.is_another_backend("fake", {"x": 1}, {"x": 2}) is False


def test_two_bootstrap_requests_at_the_same_moment_cannot_both_start(world, test_client, admin):
    from datetime import datetime, timedelta

    from app.models.sandbox import SandboxBackend
    from app.services.sandbox import backends as backend_service

    backend = _create(test_client, admin).json()

    async def both_claim():
        # Each request has its own session and read the backend before the other committed.
        async with world.session() as one, world.session() as two:
            first, second = await one.get(SandboxBackend, backend["id"]), await two.get(SandboxBackend, backend["id"])
            assert first.bootstrap_status == second.bootstrap_status == "none"
            return (await backend_service.claim_bootstrap(one, first, stale_seconds=1800),
                    await backend_service.claim_bootstrap(two, second, stale_seconds=1800))
    assert world.call(lambda db: both_claim()) == (True, False)

    async def claim_after_the_worker_died(db):
        row = await db.get(SandboxBackend, backend["id"])
        row.updated_at = datetime.utcnow() - timedelta(hours=1)
        await db.commit()
        return await backend_service.claim_bootstrap(db, row, stale_seconds=1800)
    assert world.call(claim_after_the_worker_died) is True, "a claim nobody is working on is taken over"


# ── bootstrap does not depend on the environments ────────────────────────


def test_bootstrap_is_about_the_backend_only_and_environment_images_are_fetched_after_it(world, test_client, admin, monkeypatch):
    from app.services.sandbox import backends as backend_service
    from app.services.sandbox.drivers.base import SandboxDriverError

    world.add_environment("Analytics", image="registry.example.com/team/analytics:1")
    world.add_environment("Broken", image="registry.example.com/team/does-not-exist:1")
    world.add_environment("Off", image="registry.example.com/team/off:1", enabled=False)

    async def pull(self, image, **kwargs):
        if "does-not-exist" in image:
            raise SandboxDriverError("pull access denied for team/does-not-exist", code="command_failed")
        self._backend.pulled_images.append(image)
        return None
    monkeypatch.setattr(fake_driver.FakeDriver, "pull_image", pull)

    backend = _create(test_client, admin, name="img-server", config={"server": "img-server"}).json()
    done = test_client.post(f"{BASE}/{backend['id']}/bootstrap", json={"wait": True}, headers=admin).json()
    assert done["bootstrap_status"] == "ready", "an environment whose image cannot be fetched does not fail bootstrap"
    assert [s["status"] for s in done["bootstrap_steps"]] == ["ok"] * 5
    assert test_client.put(f"{BASE}/{backend['id']}", json={"enabled": True}, headers=admin).json()["enabled"] is True

    outcomes = world.call(lambda db: backend_service.prefetch_environment_images(world.org_id, backend["id"]))
    assert outcomes["Analytics"] == "ok" and "pull access denied" in outcomes["Broken"]
    assert "Off" not in outcomes, "a disabled environment is not fetched"
    # (The route also starts the same fetch in the background once bootstrap is done.)
    assert set(fake_driver._BACKENDS["img-server"].pulled_images) == {"registry.example.com/team/analytics:1"}
    after = test_client.get(f"{BASE}/{backend['id']}", headers=admin).json()
    assert (after["bootstrap_status"], after["enabled"]) == ("ready", True), "a failed fetch changes nothing about the backend"
    entry = _audit(world, "sandbox_backend.images_fetched", backend["id"])[-1]
    assert entry.details["failed"] == ["Broken"] and entry.outcome == "failure"
