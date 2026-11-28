"""What is recorded about a sandbox beyond its runs: the domains it reached,
and the programs a run left running."""
import json
import time
import uuid

import pytest

from app.models.sandbox import SandboxEgressLog, SandboxEvent
from app.services.sandbox import jobs
from app.services.sandbox import service as service_module
from app.services.sandbox.service import RunRequest
from app.services.sandbox.testing.sandboxd_stub import get_stub_host

from .conftest import run

pytestmark = pytest.mark.e2e

ECHO = RunRequest(language="bash", code="echo ran")


@pytest.fixture
def world(sandbox_world, enterprise_license):
    sandbox_world.add_compute("fake-1")
    sandbox_world.add_environment("Net", is_default=True, network_policy="registries")
    sandbox_world.add_environment("Offline", network_policy="none")
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


def _reach(sandbox_id, *entries):
    """As if code in the sandbox had made these requests through the egress proxy."""
    log = get_stub_host().get(f"sbx-{sandbox_id}").egress_log
    with log.open("a") as handle:
        for domain, decision in entries:
            handle.write(json.dumps({"at": time.time(), "domain": domain, "decision": decision}) + "\n")
    time.sleep(0.01)


def _stored(world, sandbox_id):
    return {(r.domain, r.decision): r.count for r in world.rows(SandboxEgressLog, SandboxEgressLog.sandbox_id == sandbox_id)}


def test_the_health_job_stores_which_domains_a_sandbox_reached_and_was_refused(world, report, test_client, admin):
    sandbox_id = world.run_code(report(), world.admin_id, ECHO).sandbox_id
    _reach(sandbox_id, ("pypi.org", "allowed"), ("pypi.org", "allowed"), ("evil.example.com", "refused"))

    counts = run(jobs.compute_health_tick())
    assert counts["egress_requests"] == 3
    assert _stored(world, sandbox_id) == {("pypi.org", "allowed"): 2, ("evil.example.com", "refused"): 1}

    # Collected again: nothing is counted twice, and new requests are added.
    run(jobs.compute_health_tick())
    assert _stored(world, sandbox_id)[("pypi.org", "allowed")] == 2
    _reach(sandbox_id, ("pypi.org", "allowed"), ("files.pythonhosted.org", "allowed"))
    run(jobs.compute_health_tick())
    assert _stored(world, sandbox_id) == {
        ("pypi.org", "allowed"): 3, ("evil.example.com", "refused"): 1, ("files.pythonhosted.org", "allowed"): 1,
    }

    detail = test_client.get(f"/api/sandbox/sandboxes/{sandbox_id}", headers=admin).json()
    assert [(e["domain"], e["decision"], e["count"]) for e in detail["egress"]][0] == ("evil.example.com", "refused", 1), \
        "what was refused comes first"
    assert {e["domain"] for e in detail["egress"]} == {"pypi.org", "evil.example.com", "files.pythonhosted.org"}


def test_the_last_requests_are_collected_when_a_sandbox_is_stopped_or_deleted(world, report):
    rep = report()
    sandbox_id = world.run_code(rep, world.admin_id, ECHO).sandbox_id
    _reach(sandbox_id, ("pypi.org", "allowed"))

    async def stop(db):
        await world.service.stop(db, await world.service.get(db, world.org_id, sandbox_id), world.admin_id)
    world.call(stop)
    assert _stored(world, sandbox_id) == {("pypi.org", "allowed"): 1}, "without waiting for the next health check"

    world.run_code(rep, world.admin_id, ECHO)
    _reach(sandbox_id, ("registry.npmjs.org", "allowed"))
    world.call(lambda db: world.service.delete_for_report(db, world.report_ref(rep), world.admin_id))
    assert _stored(world, sandbox_id)[("registry.npmjs.org", "allowed")] == 1


def test_a_sandbox_with_no_network_has_no_domain_log(world, report):
    sandbox_id = world.run_code(report(), world.admin_id, RunRequest(language="bash", code="echo ran", environment="Offline")).sandbox_id
    assert run(jobs.compute_health_tick()).get("egress_requests", 0) == 0
    assert _stored(world, sandbox_id) == {}


def test_programs_a_run_leaves_running_are_recorded_with_the_run(world, report, monkeypatch):
    async def left(runtime):
        return ["41 python -m http.server 8000", "57 sleep 3600"]
    monkeypatch.setattr(service_module, "_left_running", left)

    outcome = world.run_code(report(), world.admin_id, ECHO)
    assert outcome.background_processes == ["41 python -m http.server 8000", "57 sleep 3600"]
    finished = [e for e in world.rows(SandboxEvent, SandboxEvent.sandbox_id == outcome.sandbox_id) if e.type == "run_finished"]
    assert finished[-1].details["background_processes"] == outcome.background_processes


def test_the_process_listing_ignores_itself_and_survives_anything():
    class Result:
        stdout = b"12 python server.py\n13 tr \\0  \n14 cut -c1-160\n15 sh -c grep -qa sandboxd /proc/1/cmdline\n\n16 node app.js\n"

    class Runtime:
        async def run(self, command, timeout):
            return Result()

    class Broken:
        async def run(self, command, timeout):
            raise RuntimeError("the sandbox went away")

    assert run(service_module._left_running(Runtime())) == ["12 python server.py", "16 node app.js"]
    assert run(service_module._left_running(Broken())) == []


def test_a_run_ends_when_its_program_exits_even_if_it_left_a_child_holding_the_output(world, report):
    """A server started with `&` and no redirect keeps the output pipes open.
    The run must not wait for it: it ends shortly after the program that was
    started exits."""
    import time as clock

    rep = report()
    world.run_code(rep, world.admin_id, ECHO)
    started = clock.monotonic()
    outcome = world.run_code(rep, world.admin_id, RunRequest(language="bash", code="sleep 30 & echo started", timeout_seconds=20))
    took = clock.monotonic() - started
    assert outcome.status == "succeeded" and outcome.stdout == "started\n"
    assert took < 10, f"the run waited {took:.1f}s for a background child"
    world.call(lambda db: world.service.delete_for_report(db, world.report_ref(rep), world.admin_id))
