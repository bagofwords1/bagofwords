"""An environment's hooks: once at creation, on every start, before a stop.

Stop and resume keep the files but not processes, memory or /tmp, so a start
script rebuilds what a stop loses. Each hook's result is in the sandbox's
timeline. A script that keeps failing stops the environment from creating
sandboxes, and the admin is told, rather than failing every attempt forever.
"""
import uuid

import pytest

from app.ee.audit.models import AuditLog
from app.models.sandbox import Sandbox, SandboxEnvironment, SandboxEvent
from app.services.sandbox import errors
from app.services.sandbox.errors import SandboxError
from app.services.sandbox.service import RunRequest

pytestmark = pytest.mark.e2e

CAT = lambda path: RunRequest(language="bash", code=f"cat {path}")  # noqa: E731
ENVS = "/api/sandbox/environments"


@pytest.fixture
def world(sandbox_world, enterprise_license):
    sandbox_world.add_compute("fake-1")
    sandbox_world.configure(max_running_per_user=20)
    return sandbox_world


@pytest.fixture
def admin(world):
    return world.cast["admin"]["headers"]


@pytest.fixture
def report(world, create_report):
    def _new():
        return create_report(title=f"Report {uuid.uuid4().hex[:6]}",
                             user_token=world.cast["admin"]["token"], org_id=world.org_id)
    return _new


def _hook_events(world, sandbox_id):
    rows = sorted(world.rows(SandboxEvent, SandboxEvent.sandbox_id == sandbox_id), key=lambda e: (e.created_at, e.id))
    return [(e.type, e.details.get("status")) for e in rows if e.type.endswith("_script")]


def _stop(world, sandbox_id):
    async def go(db):
        await world.service.stop(db, await world.service.get(db, world.org_id, sandbox_id), world.admin_id)
    world.call(go)


def test_setup_runs_once_start_runs_on_every_start_and_stop_runs_before_a_stop(world, report):
    world.add_environment(
        "Hooks", is_default=True,
        setup_script="echo setup >> hooks.log",
        start_script="echo start >> hooks.log",
        stop_script="echo stop >> hooks.log",
    )
    rep = report()
    first = world.run_code(rep, world.admin_id, CAT("hooks.log"))
    assert first.stdout == "setup\nstart\n", "setup first, then start, when the sandbox is created"
    assert world.run_code(rep, world.admin_id, CAT("hooks.log")).stdout == "setup\nstart\n", "nothing runs between runs"

    _stop(world, first.sandbox_id)
    resumed = world.run_code(rep, world.admin_id, CAT("hooks.log"))
    assert resumed.resumed is True
    assert resumed.stdout == "setup\nstart\nstop\nstart\n", "stop before the stop, start again on resume, setup never again"
    assert _hook_events(world, first.sandbox_id) == [
        ("setup_script", "succeeded"), ("start_script", "succeeded"), ("stop_script", "succeeded"), ("start_script", "succeeded"),
    ], "each hook's result is in the timeline"


def test_a_failing_stop_script_never_prevents_the_stop_or_the_delete(world, report):
    world.add_environment("Hooks", is_default=True, stop_script="echo bye; exit 7")
    rep = report()
    sandbox_id = world.run_code(rep, world.admin_id, CAT("/dev/null")).sandbox_id

    _stop(world, sandbox_id)
    assert world.rows(Sandbox, Sandbox.id == sandbox_id)[0].state == "stopped"
    assert _hook_events(world, sandbox_id) == [("stop_script", "failed")]

    world.run_code(rep, world.admin_id, CAT("/dev/null"))
    world.call(lambda db: world.service.delete_for_report(db, world.report_ref(rep), world.admin_id))
    assert world.rows(Sandbox, Sandbox.id == sandbox_id)[0].state == "deleted"
    assert _hook_events(world, sandbox_id)[-1] == ("stop_script", "failed"), "it ran before the delete too"


def test_a_start_script_that_fails_on_resume_leaves_the_sandbox_stopped_with_its_files(world, report):
    world.add_environment("Hooks", is_default=True, start_script="test ! -f break || { echo broken >&2; exit 3; }")
    rep = report()
    sandbox_id = world.run_code(rep, world.admin_id, RunRequest(language="bash", code="echo data > keep.txt; touch break")).sandbox_id
    _stop(world, sandbox_id)

    with pytest.raises(SandboxError) as failed:
        world.run_code(rep, world.admin_id, CAT("keep.txt"))
    assert "start script failed" in failed.value.message and "broken" in failed.value.message
    sandbox = world.rows(Sandbox, Sandbox.id == sandbox_id)[0]
    assert sandbox.state == "stopped", "not failed: a failed sandbox would lose its files"

    async def fix(db):
        env = (await db.execute(__import__("sqlalchemy").select(SandboxEnvironment).where(SandboxEnvironment.name == "Hooks"))).scalar_one()
        env.start_script = "true"
        await db.commit()
    world.call(fix)
    again = world.run_code(rep, world.admin_id, CAT("keep.txt"))
    assert (again.sandbox_id, again.stdout) == (sandbox_id, "data\n")


def test_a_setup_script_that_keeps_failing_stops_the_environment_and_tells_the_admin(world, report, test_client, admin):
    env_id = world.add_environment("Broken", is_default=True, setup_script="echo 'pip: no such package' >&2; exit 1")
    attempts = []
    for _ in range(3):
        with pytest.raises(SandboxError) as failed:
            world.run_code(report(), world.admin_id, CAT("/dev/null"))
        attempts.append(failed.value)
    assert all("setup script failed" in a.message and "pip: no such package" in a.message for a in attempts)

    with pytest.raises(SandboxError) as refused:
        world.run_code(report(), world.admin_id, CAT("/dev/null"))
    assert refused.value.code == errors.ENVIRONMENT_BROKEN and "3 times in a row" in refused.value.message
    assert len(world.rows(Sandbox, Sandbox.environment_id == env_id)) == 3, "the fourth attempt created nothing"

    env = test_client.get(f"{ENVS}/{env_id}", headers=admin).json()
    assert env["hook_failures"] == 3 and "pip: no such package" in env["hook_last_error"]
    assert any("failed 3 times in a row" in w for w in env["warnings"]), env["warnings"]
    alerts = [r for r in world.rows(AuditLog, AuditLog.organization_id == world.org_id)
              if r.action == "sandbox_environment.hook_failing" and r.resource_id == env_id]
    assert len(alerts) == 1 and alerts[0].details["failures"] == 3

    # Changing the script lets it try again.
    fixed = test_client.put(f"{ENVS}/{env_id}", json={"setup_script": "echo ok > ready.txt"}, headers=admin).json()
    assert fixed["hook_failures"] == 0 and not any("in a row" in w for w in fixed["warnings"])
    assert world.run_code(report(), world.admin_id, CAT("ready.txt")).stdout == "ok\n"


def test_one_failure_is_forgotten_after_a_success(world, report):
    env_id = world.add_environment("Flaky", is_default=True, start_script="test ! -f /tmp/never || exit 1")

    async def set_script(db, script):
        env = await db.get(SandboxEnvironment, env_id)
        env.start_script = script
        await db.commit()
    world.call(lambda db: set_script(db, "exit 1"))
    with pytest.raises(SandboxError):
        world.run_code(report(), world.admin_id, CAT("/dev/null"))
    assert world.rows(SandboxEnvironment, SandboxEnvironment.id == env_id)[0].hook_failures == 1
    world.call(lambda db: set_script(db, "true"))
    world.run_code(report(), world.admin_id, CAT("/dev/null"))
    assert world.rows(SandboxEnvironment, SandboxEnvironment.id == env_id)[0].hook_failures == 0


def test_a_sandbox_set_up_before_the_script_changed_is_marked(world, report, test_client, admin):
    env_id = world.add_environment("Std", is_default=True, setup_script="echo v1 > version")
    old = world.run_code(report(), world.admin_id, CAT("version")).sandbox_id
    test_client.put(f"{ENVS}/{env_id}", json={"setup_script": "echo v2 > version"}, headers=admin)
    new_report = report()
    new = world.run_code(new_report, world.admin_id, CAT("version"))
    assert new.stdout == "v2\n"

    rows = {r["id"]: r for r in test_client.get("/api/sandbox/sandboxes", headers=admin).json()}
    assert rows[old]["setup_outdated"] is True, "it still holds what the old script made"
    assert rows[new.sandbox_id]["setup_outdated"] is False
