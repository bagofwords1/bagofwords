"""The sandbox service against the fake compute.

What the layers above rely on: one persistent sandbox per report, created on
first use, stopped and resumed with its files, recreated when lost; a compute
chosen by labels, features and load; quotas; one run at a time; the record of
runs and events; and an audit entry for every action, with no secret in it.

The acting user here is the organization admin (who holds every permission);
who else may use sandboxes is covered in ``test_sandbox_permissions.py``.
"""
import asyncio
import json
import uuid

import pytest
from sqlalchemy import select

from app.ee.audit.models import AuditLog
from app.models.sandbox import Sandbox, SandboxEvent, SandboxRun
from app.services.sandbox import errors
from app.services.sandbox import service as service_module
from app.services.sandbox.errors import SandboxError
from app.services.sandbox.service import TRANSITIONS, InputFile, RunRequest
from app.services.sandbox.testing.sandboxd_stub import get_stub_host

from .conftest import run

pytestmark = pytest.mark.e2e


@pytest.fixture
def world(sandbox_world):
    sandbox_world.compute_id = sandbox_world.add_compute("fake-1")
    sandbox_world.environment_id = sandbox_world.add_environment("Default", is_default=True)
    return sandbox_world


@pytest.fixture
def report(world, create_report):
    def _new(title=None):
        return create_report(title=title or f"Sales {uuid.uuid4().hex[:6]}",
                             user_token=world.cast["admin"]["token"], org_id=world.org_id)
    return _new


def _events(world, sandbox_id):
    rows = world.rows(SandboxEvent, SandboxEvent.sandbox_id == sandbox_id)
    return [e.type for e in sorted(rows, key=lambda e: (e.created_at, e.id))]


def _audit(world, **where):
    rows = world.rows(AuditLog, AuditLog.organization_id == world.org_id)
    return [r for r in rows if all(getattr(r, k) == v for k, v in where.items())]


def _python(code, **kw):
    return RunRequest(language="python", code=code, **kw)


# ── create, run, artifacts ───────────────────────────────────────────────


def test_first_run_creates_the_reports_sandbox_and_returns_what_the_code_wrote(world, report):
    rep = report("Quarterly sales")
    saved = {}

    async def save(name, size, chunks):
        saved[name] = b"".join([c async for c in chunks])
        return service_module.Artifact(name=name, size=size, file_id=f"file-{name}")

    outcome = world.run_code(rep, world.admin_id, _python(
        "import os\nprint('hello from the sandbox')\n"
        "open('outputs/chart.png','wb').write(b'\\x89PNG-bytes')\n"
        "os.makedirs('outputs/tables', exist_ok=True)\nopen('outputs/tables/t.csv','w').write('a,b\\n')\n"
    ), save_artifact=save)

    assert outcome.created and not outcome.resumed and not outcome.recreated
    assert (outcome.status, outcome.exit_code) == ("succeeded", 0)
    assert outcome.stdout == "hello from the sandbox\n"
    assert {a.name: a.file_id for a in outcome.artifacts} == {"chart.png": "file-chart.png", "tables/t.csv": "file-tables/t.csv"}
    assert saved["chart.png"] == b"\x89PNG-bytes"

    sandbox = world.sandbox_of(rep)
    assert sandbox.state == "ready" and sandbox.user_id == world.admin_id
    assert sandbox.compute_id == world.compute_id and sandbox.environment_id == world.environment_id
    events = _events(world, sandbox.id)
    assert events[0] == "created" and events[-1] == "ready"
    assert ["run_started", "run_finished"] == [e for e in events if e.startswith("run_")]

    stored = world.rows(SandboxRun, SandboxRun.sandbox_id == sandbox.id)
    assert len(stored) == 1
    assert stored[0].status == "succeeded" and stored[0].exit_code == 0
    assert stored[0].stdout == "hello from the sandbox\n"
    assert stored[0].user_id == world.admin_id and len(stored[0].code_sha256) == 64
    assert sorted(stored[0].artifact_file_ids) == ["file-chart.png", "file-tables/t.csv"]


def test_a_report_keeps_one_sandbox_and_its_files_between_runs(world, report):
    rep = report()
    first = world.run_code(rep, world.admin_id, _python("open('state.txt','w').write('kept')"))
    second = world.run_code(rep, world.admin_id, _python("print(open('state.txt').read())"))
    assert first.sandbox_id == second.sandbox_id
    assert second.created is False and second.stdout == "kept\n"
    assert len(world.rows(Sandbox, Sandbox.report_id == rep["id"])) == 1


def test_only_new_and_changed_output_files_are_returned(world, report):
    rep = report()
    world.run_code(rep, world.admin_id, _python("open('outputs/a.txt','w').write('1'); open('outputs/b.txt','w').write('1')"))
    again = world.run_code(rep, world.admin_id, _python("open('outputs/b.txt','w').write('changed'); open('outputs/c.txt','w').write('new')"))
    assert sorted(a.name for a in again.artifacts) == ["b.txt", "c.txt"]
    nothing = world.run_code(rep, world.admin_id, _python("print('no files')"))
    assert nothing.artifacts == []


def test_artifact_limits_are_enforced_and_what_was_left_out_is_reported(world, report, monkeypatch):
    monkeypatch.setattr(service_module, "MAX_ARTIFACTS_PER_RUN", 2)
    monkeypatch.setattr(service_module, "MAX_ARTIFACT_BYTES", 1000)
    outcome = world.run_code(report(), world.admin_id, _python(
        "for n in ('a','b','c'):\n    open(f'outputs/{n}.txt','w').write(n)\n"
        "open('outputs/0-big.bin','wb').write(b'x'*5000)\n"
    ))
    assert [a.name for a in outcome.artifacts] == ["a.txt", "b.txt"]
    skipped = {s["name"]: s["reason"] for s in outcome.skipped_artifacts}
    assert set(skipped) == {"0-big.bin", "c.txt"}
    assert "larger" in skipped["0-big.bin"] and "files" in skipped["c.txt"]


def test_a_failing_program_is_a_result_not_an_error(world, report):
    rep = report()
    outcome = world.run_code(rep, world.admin_id, _python("import sys\nprint('partial')\nsys.stderr.write('boom\\n')\nsys.exit(4)"))
    assert (outcome.status, outcome.exit_code) == ("failed", 4)
    assert outcome.stdout == "partial\n" and outcome.stderr == "boom\n"
    assert world.sandbox_of(rep).state == "ready", "the sandbox is usable again"


def test_a_run_past_its_timeout_is_stopped_and_the_sandbox_stays_usable(world, report):
    rep = report()
    outcome = world.run_code(rep, world.admin_id, _python("import time\nprint('started', flush=True)\ntime.sleep(60)", timeout_seconds=1))
    assert outcome.status == "timeout" and outcome.stdout == "started\n"
    assert world.sandbox_of(rep).state == "ready"
    assert world.run_code(rep, world.admin_id, _python("print('next')")).stdout == "next\n"


def test_the_run_timeout_cannot_exceed_the_environments(world, report, create_report):
    world.add_environment("Quick", run_timeout_seconds=10)
    rep = report()
    outcome = world.run_code(rep, world.admin_id, RunRequest(
        language="bash", command="sleep 0.1; echo ok", timeout_seconds=99999, environment="Quick"))
    assert outcome.status == "succeeded" and outcome.environment == "Quick"
    assert world.sandbox_of(rep).limits["run_timeout_seconds"] == 10


def test_output_is_stored_while_the_run_is_in_progress(world, report, monkeypatch):
    """An admin page on any worker can tail a run: output reaches the row before the run ends."""
    monkeypatch.setattr(service_module, "OUTPUT_FLUSH_SECONDS", 0.0)
    rep = report()
    seen_mid_run = []

    async def go(db):
        async def on_output(kind, chunk):
            if b"second" in chunk:
                async with world.session() as other:
                    rows = (await other.execute(select(SandboxRun))).scalars().all()
                    seen_mid_run.extend((r.status, r.stdout) for r in rows)
        return await world.service.run(db, world.report_ref(rep), world.admin_id, _python(
            "import time\nprint('first', flush=True)\ntime.sleep(0.5)\nprint('second', flush=True)\n"
        ), on_output=on_output)

    world.call(go)
    assert seen_mid_run and seen_mid_run[0][0] == "running"
    assert "first" in (seen_mid_run[0][1] or "")


def test_stored_output_is_capped_and_keeps_the_end(world, report, monkeypatch):
    monkeypatch.setattr(service_module, "STORED_OUTPUT_LIMIT_CHARS", 2000)
    monkeypatch.setattr(service_module, "MODEL_OUTPUT_TAIL_BYTES", 500)
    rep = report()
    outcome = world.run_code(rep, world.admin_id, _python("for i in range(3000): print('line', i)"))
    assert outcome.stdout_truncated and outcome.stdout.endswith("line 2999\n") and len(outcome.stdout) <= 500
    stored = world.rows(SandboxRun, SandboxRun.id == outcome.run_id)[0].stdout
    assert len(stored) <= 2000 and stored.endswith("line 2999\n") and "truncated" in stored.splitlines()[0]


# ── languages, files, inputs, packages ───────────────────────────────────


def test_code_command_and_extra_files(world, report):
    rep = report()
    bash = world.run_code(rep, world.admin_id, RunRequest(language="bash", code="echo from-bash; cat data/in.txt",
                                                         files={"data/in.txt": "extra file"}))
    assert bash.stdout == "from-bash\nextra file"
    both = world.run_code(rep, world.admin_id, RunRequest(language="python", code="print('unused')", command="echo {braces} $((1+2))"))
    assert both.stdout == "{braces} 3\n"


@pytest.mark.parametrize("request_kwargs, fragment", [
    ({"language": "cobol", "code": "x"}, "language"),
    ({"language": "python"}, "code"),
    ({"language": "python", "code": "x", "packages": ["--index-url=http://evil"]}, "package"),
    ({"language": "python", "code": "x", "packages": ["a b"]}, "package"),
    ({"language": "bash", "code": "x", "packages": ["curl"]}, "Packages"),
    ({"language": "python", "code": "x", "files": {"../escape.txt": "x"}}, "path"),
])
def test_invalid_requests_are_refused_before_anything_runs(world, report, request_kwargs, fragment):
    rep = report()
    with pytest.raises(SandboxError) as refused:
        world.run_code(rep, world.admin_id, RunRequest(**request_kwargs))
    assert refused.value.code == errors.INVALID and fragment in refused.value.message
    assert world.sandbox_of(rep) is None, "nothing was created for an invalid request"


def test_packages_need_a_network_policy_that_allows_them(world, report):
    rep = report()
    with pytest.raises(SandboxError) as refused:
        world.run_code(rep, world.admin_id, _python("import requests", packages=["requests"]))
    assert refused.value.code == errors.PACKAGES_NEED_NETWORK
    assert "internet" in refused.value.message and "admin" in refused.value.message
    assert world.sandbox_of(rep).state == "ready"
    assert world.rows(SandboxRun, SandboxRun.report_id == rep["id"]) == [], "no run was recorded"


def test_input_files_land_in_inputs_and_each_is_audited(world, report):
    rep = report("Budget review")
    outcome = world.run_code(rep, world.admin_id, _python("import os\nprint(sorted(os.listdir('inputs')), open('inputs/q1.csv').read())", inputs=[
        InputFile(file_id="file-1", name="q1.csv", data=b"a,b"),
        InputFile(file_id="file-2", name="../../notes.txt", data=b"n"),
    ]))
    assert outcome.stdout == "['notes.txt', 'q1.csv'] a,b\n"
    entries = _audit(world, action="sandbox.input_file")
    assert {(e.details["file_id"], e.details["file_name"]) for e in entries} == {("file-1", "q1.csv"), ("file-2", "notes.txt")}
    assert all(e.details["title"] == "Budget review" and e.user_id == world.admin_id for e in entries)
    assert world.rows(SandboxRun, SandboxRun.id == outcome.run_id)[0].input_file_ids == ["file-1", "file-2"]


# ── stop, resume, lost, delete ───────────────────────────────────────────


def test_a_stopped_sandbox_resumes_on_the_next_run_with_its_files(world, report):
    rep = report("Churn analysis")
    world.run_code(rep, world.admin_id, _python("open('model.bin','w').write('trained')"))
    sandbox = world.sandbox_of(rep)

    async def stop(db):
        return await world.service.stop(db, await world.service.get(db, world.org_id, sandbox.id), world.admin_id, title=rep["title"])
    world.call(stop)
    stopped = world.sandbox_of(rep)
    assert stopped.state == "stopped" and stopped.stopped_at is not None

    outcome = world.run_code(rep, world.admin_id, _python("print(open('model.bin').read())"))
    assert outcome.resumed and not outcome.created and outcome.sandbox_id == sandbox.id
    assert outcome.stdout == "trained\n"
    assert world.sandbox_of(rep).state == "ready" and world.sandbox_of(rep).stopped_at is None
    events = _events(world, sandbox.id)
    assert events.index("stopped") < events.index("resumed")
    for action in ("sandbox.stopped", "sandbox.resumed"):
        entry = _audit(world, action=action, resource_id=sandbox.id)
        assert len(entry) == 1 and entry[0].details["title"] == "Churn analysis"


def test_a_sandbox_found_stopped_is_resumed_too(world, report):
    """A reboot or an admin stopped the container behind BoW's back."""
    rep = report()
    world.run_code(rep, world.admin_id, _python("open('f','w').write('x')"))
    sandbox = world.sandbox_of(rep)
    get_stub_host().set_running(sandbox.driver_ref["stub_id"], False)
    outcome = world.run_code(rep, world.admin_id, _python("print(open('f').read())"))
    assert outcome.resumed and outcome.stdout == "x\n"


def test_a_lost_sandbox_is_recreated_and_the_caller_is_told(world, report):
    rep = report()
    world.run_code(rep, world.admin_id, _python("open('precious.txt','w').write('x')"))
    lost = world.sandbox_of(rep)
    get_stub_host().remove_sandbox(lost.driver_ref["stub_id"])  # gone from its compute

    outcome = world.run_code(rep, world.admin_id, _python("import os\nprint(os.path.exists('precious.txt'))"))
    assert outcome.recreated and outcome.sandbox_id != lost.id
    assert outcome.stdout == "False\n", "the filesystem is fresh"
    rows = {s.id: s for s in world.rows(Sandbox, Sandbox.report_id == rep["id"])}
    assert rows[lost.id].state == "deleted" and rows[lost.id].deleted_at is not None
    assert rows[outcome.sandbox_id].state == "ready"
    assert "failed" in _events(world, lost.id)


def test_deleting_removes_the_sandbox_and_the_report_can_get_a_new_one(world, report):
    rep = report("Forecast")
    first = world.run_code(rep, world.admin_id, _python("open('f','w').write('x')"))

    async def delete(db):
        return await world.service.delete_for_report(db, world.report_ref(rep), world.admin_id)
    assert world.call(delete) is True
    assert world.sandbox_of(rep) is None
    assert get_stub_host().get(f"sbx-{first.sandbox_id}") is None, "nothing is left on the compute"
    assert world.call(delete) is False  # nothing to delete now
    entry = _audit(world, action="sandbox.deleted", resource_id=first.sandbox_id)
    assert len(entry) == 1 and entry[0].details["title"] == "Forecast"
    row = world.rows(Sandbox, Sandbox.id == first.sandbox_id)[0]
    assert row.state == "deleted" and row.token is None

    second = world.run_code(rep, world.admin_id, _python("import os\nprint(os.path.exists('f'))"))
    assert second.created and second.sandbox_id != first.sandbox_id and second.stdout == "False\n"


def test_status_describes_the_sandbox_and_its_recent_runs(world, report):
    rep = report()
    assert world.call(lambda db: world.service.describe(db, world.report_ref(rep))) is None
    world.run_code(rep, world.admin_id, _python("print(1)"))
    world.run_code(rep, world.admin_id, _python("import sys; sys.exit(2)"))
    described = world.call(lambda db: world.service.describe(db, world.report_ref(rep)))
    assert described["state"] == "ready" and described["environment"] == "Default"
    assert described["network_policy"] == "none" and described["expires_at"]
    assert [r["status"] for r in described["recent_runs"]] == ["failed", "succeeded"]


# ── one run at a time ────────────────────────────────────────────────────


def test_a_second_run_is_refused_while_one_is_in_progress(world, report):
    rep = report()
    world.run_code(rep, world.admin_id, _python("print('warm')"))

    async def both():
        cancel = asyncio.Event()
        started = asyncio.Event()

        async def long_run():
            async with world.session() as db:
                return await world.service.run(db, world.report_ref(rep), world.admin_id, _python(
                    "import time\nprint('go', flush=True)\ntime.sleep(60)"),
                    on_output=lambda kind, chunk: started.set(), cancel=cancel)

        task = asyncio.ensure_future(long_run())
        await asyncio.wait_for(started.wait(), 30)
        try:
            async with world.session() as db:
                with pytest.raises(SandboxError) as refused:
                    await world.service.run(db, world.report_ref(rep), world.admin_id, _python("print('second')"))
                assert refused.value.code == errors.BUSY
                with pytest.raises(SandboxError) as no_stop:
                    await world.service.stop(db, await world.service.get_for_report(db, rep["id"]), world.admin_id)
                assert no_stop.value.code == errors.BUSY
        finally:
            cancel.set()
        return await task

    first = run(both())
    assert first.status == "cancelled"
    assert world.sandbox_of(rep).state == "ready"
    assert world.run_code(rep, world.admin_id, _python("print('after')")).stdout == "after\n"


# ── quotas and choosing a compute ────────────────────────────────────────


def test_running_sandboxes_per_user_are_limited_and_stopping_one_frees_a_slot(world, report):
    world.configure(max_running_per_user=2)
    reports = [report() for _ in range(3)]
    for rep in reports[:2]:
        world.run_code(rep, world.admin_id, _python("print(1)"))
    with pytest.raises(SandboxError) as refused:
        world.run_code(reports[2], world.admin_id, _python("print(1)"))
    assert refused.value.code == errors.QUOTA_USER
    assert world.sandbox_of(reports[2]) is None

    first = world.sandbox_of(reports[0])
    world.call(lambda db: _stop(world, db, first.id))
    assert world.run_code(reports[2], world.admin_id, _python("print('ok')")).stdout == "ok\n"
    # The stopped one cannot come back while the user is at the limit again.
    with pytest.raises(SandboxError) as full:
        world.run_code(reports[0], world.admin_id, _python("print(1)"))
    assert full.value.code == errors.QUOTA_USER
    assert world.sandbox_of(reports[0]).state == "stopped"


async def _stop(world, db, sandbox_id):
    return await world.service.stop(db, await world.service.get(db, world.org_id, sandbox_id), world.admin_id)


def test_the_organization_wide_limit_applies_to_everyone(world, report):
    world.configure(max_running_total=1, max_running_per_user=5)
    world.run_code(report(), world.admin_id, _python("print(1)"))
    with pytest.raises(SandboxError) as refused:
        world.run_code(report(), world.admin_id, _python("print(1)"))
    assert refused.value.code == errors.QUOTA_TOTAL


def test_a_full_compute_takes_no_more_sandboxes(sandbox_world, create_report):
    world = sandbox_world
    world.add_compute("small", capacity=1)
    world.add_environment("Default", is_default=True)
    new = lambda: create_report(title=uuid.uuid4().hex, user_token=world.cast["admin"]["token"], org_id=world.org_id)  # noqa: E731
    world.run_code(new(), world.admin_id, _python("print(1)"))
    with pytest.raises(SandboxError) as refused:
        world.run_code(new(), world.admin_id, _python("print(1)"))
    assert refused.value.code == errors.NO_COMPUTE and "capacity" in refused.value.message


def test_new_sandboxes_go_to_the_least_loaded_compute(sandbox_world, create_report):
    world = sandbox_world
    computes = {world.add_compute("a", capacity=10), world.add_compute("b", capacity=10)}
    world.add_environment("Default", is_default=True)
    world.configure(max_running_per_user=10)
    placed = []
    for _ in range(4):
        rep = create_report(title=uuid.uuid4().hex, user_token=world.cast["admin"]["token"], org_id=world.org_id)
        world.run_code(rep, world.admin_id, _python("print(1)"))
        placed.append(world.sandbox_of(rep).compute_id)
    assert set(placed) == computes
    assert sorted(placed.count(b) for b in computes) == [2, 2]


def test_a_compute_must_match_the_environments_labels_and_features(sandbox_world, create_report):
    world = sandbox_world
    plain = world.add_compute("plain", strong_isolation=False)
    gpu = world.add_compute("gpu", labels=["GPU"])
    world.add_environment("Plain", is_default=True)
    world.add_environment("GPU", compute_labels=["gpu"])
    world.add_environment("Strict", require_strong_isolation=True)
    world.add_environment("Nowhere", compute_labels=["arm"])
    world.configure(max_running_per_user=10)

    def place(environment):
        rep = create_report(title=uuid.uuid4().hex, user_token=world.cast["admin"]["token"], org_id=world.org_id)
        world.run_code(rep, world.admin_id, RunRequest(language="bash", code="true", environment=environment))
        return world.sandbox_of(rep).compute_id

    assert place("GPU") == gpu
    assert place("Strict") == gpu, "only the compute with strong isolation qualifies"
    with pytest.raises(SandboxError) as nowhere:
        place("Nowhere")
    assert nowhere.value.code == errors.NO_COMPUTE
    assert plain in {place("Plain") for _ in range(3)}


@pytest.mark.parametrize("make_unusable", ["disable", "never_bootstrapped", "unreachable"])
def test_computes_that_are_disabled_unprepared_or_unreachable_get_no_sandboxes(sandbox_world, create_report, make_unusable):
    world = sandbox_world
    if make_unusable == "never_bootstrapped":
        world.add_compute("b", bootstrap=False)
    else:
        compute_id = world.add_compute("b")

        async def change(db):
            from app.services.sandbox import computes as compute_service
            compute = await compute_service.get_compute(db, world.org_id, compute_id)
            if make_unusable == "disable":
                await compute_service.set_enabled(db, world.org_id, world.admin_id, compute, False)
            else:
                compute.status = "unreachable"
                await db.commit()
        world.call(change)
    world.add_environment("Default", is_default=True)
    rep = create_report(title="r", user_token=world.cast["admin"]["token"], org_id=world.org_id)
    with pytest.raises(SandboxError) as refused:
        world.run_code(rep, world.admin_id, _python("print(1)"))
    assert refused.value.code == errors.NO_COMPUTE


def test_the_organization_switch_gates_everything(world, report):
    world.configure(enabled=False)
    with pytest.raises(SandboxError) as refused:
        world.run_code(report(), world.admin_id, _python("print(1)"))
    assert refused.value.code == errors.DISABLED


# ── choosing the environment ─────────────────────────────────────────────


def test_the_environment_is_the_one_asked_for_else_the_agents_else_the_default(sandbox_world, create_report):
    world = sandbox_world
    world.add_compute("b")
    default = world.add_environment("Default", is_default=True)
    agent_env = world.add_environment("Analytics")
    other = world.add_environment("Other")
    world.configure(max_running_per_user=10)
    data_source_id = str(uuid.uuid4())
    from app.services.sandbox import environments as environment_service
    world.call(lambda db: environment_service.set_agent_default(db, world.org_id, world.admin_id, data_source_id, agent_env))

    def created_in(environment=None, data_source_ids=()):
        rep = create_report(title=uuid.uuid4().hex, user_token=world.cast["admin"]["token"], org_id=world.org_id)
        ref = world.report_ref(rep, data_source_ids=data_source_ids)
        world.run_code(ref, world.admin_id, RunRequest(language="bash", code="true", environment=environment))
        return world.sandbox_of(rep).environment_id

    assert created_in() == default
    assert created_in(data_source_ids=[data_source_id]) == agent_env
    assert created_in("Other", data_source_ids=[data_source_id]) == other
    with pytest.raises(SandboxError) as unknown:
        created_in("No such environment")
    assert unknown.value.code == errors.ENVIRONMENT_NOT_ALLOWED and "Analytics" in unknown.value.message


# ── setup script, failures ───────────────────────────────────────────────


def test_the_setup_script_runs_once_when_the_sandbox_is_created(sandbox_world, create_report):
    world = sandbox_world
    world.add_compute("b")
    world.add_environment("Prepared", is_default=True, setup_script="echo ready > prepared.txt; echo setup-output")
    rep = create_report(title="r", user_token=world.cast["admin"]["token"], org_id=world.org_id)
    assert world.run_code(rep, world.admin_id, _python("print(open('prepared.txt').read().strip())")).stdout == "ready\n"
    world.run_code(rep, world.admin_id, _python("print(1)"))
    setup = [e for e in world.rows(SandboxEvent, SandboxEvent.sandbox_id == world.sandbox_of(rep).id) if e.type == "setup_script"]
    assert len(setup) == 1 and "setup-output" in setup[0].details["output"]


def test_a_sandbox_that_cannot_start_is_failed_and_the_next_run_starts_over(sandbox_world, create_report):
    world = sandbox_world
    world.add_compute("b")
    env_id = world.add_environment("Broken", is_default=True, setup_script="echo nope >&2; exit 7")
    rep = create_report(title="r", user_token=world.cast["admin"]["token"], org_id=world.org_id)
    with pytest.raises(SandboxError) as failed:
        world.run_code(rep, world.admin_id, _python("print(1)"))
    assert failed.value.code == errors.UNAVAILABLE and "setup script" in failed.value.message
    broken = world.sandbox_of(rep)
    assert broken.state == "failed" and "setup script" in broken.error
    assert get_stub_host().get(f"sbx-{broken.id}") is None, "the half-made sandbox was removed from the compute"

    from app.schemas.sandbox_schema import SandboxEnvironmentUpdate
    from app.services.sandbox import environments as environment_service
    world.call(lambda db: environment_service.update_environment(
        db, world.org_id, world.admin_id, env_id, SandboxEnvironmentUpdate(setup_script="true")))
    outcome = world.run_code(rep, world.admin_id, _python("print('recovered')"))
    assert outcome.recreated and outcome.stdout == "recovered\n" and outcome.sandbox_id != broken.id


# ── audit and secrets ────────────────────────────────────────────────────


def test_every_action_is_audited_with_the_report_name_and_no_secret(sandbox_world, create_report):
    world = sandbox_world
    world.add_compute("b")
    secret, variable = f"secret-{uuid.uuid4().hex}", "eu-west"
    world.add_environment("With secrets", is_default=True, secrets={"API_KEY": secret}, env_vars={"REGION": variable})
    rep = create_report(title="Margin by region", user_token=world.cast["admin"]["token"], org_id=world.org_id)
    outcome = world.run_code(rep, world.admin_id, _python(
        "import os\nprint(len(os.environ['API_KEY']), os.environ['REGION'])\nopen('outputs/r.csv','w').write('x')"))
    assert outcome.stdout == f"{len(secret)} {variable}\n", "the secret reached the code"
    sandbox = world.sandbox_of(rep)
    world.call(lambda db: _stop(world, db, sandbox.id))
    world.run_code(rep, world.admin_id, _python("print(1)"))
    world.call(lambda db: world.service.delete_for_report(db, world.report_ref(rep), world.admin_id))

    entries = _audit(world, resource_id=sandbox.id)
    assert {e.action for e in entries} == {"sandbox.created", "sandbox.run", "sandbox.stopped", "sandbox.resumed", "sandbox.deleted"}
    assert all(e.details["title"] == "Margin by region" and e.user_id == world.admin_id for e in entries)
    first_run = sorted((e for e in entries if e.action == "sandbox.run"), key=lambda e: e.created_at)[0]
    assert first_run.details["exit_code"] == 0 and first_run.details["files_produced"] == ["r.csv"]
    assert first_run.details["environment"] == "With secrets" and len(first_run.details["code_sha256"]) == 64
    assert first_run.outcome == "success"

    everything = json.dumps([e.details for e in _audit(world)]) + json.dumps(
        [e.details for e in world.rows(SandboxEvent, SandboxEvent.sandbox_id == sandbox.id)])
    assert secret not in everything
    assert sandbox.token not in everything
    assert "print(len(os.environ" not in everything, "the code itself is not in the audit trail, only its hash"


def test_every_state_change_writes_an_event(world, report):
    rep = report()
    world.run_code(rep, world.admin_id, _python("print(1)"))
    sandbox = world.sandbox_of(rep)
    world.call(lambda db: _stop(world, db, sandbox.id))
    world.run_code(rep, world.admin_id, _python("print(1)"))
    world.call(lambda db: world.service.delete_for_report(db, world.report_ref(rep), world.admin_id))
    changes = [(e.details.get("from"), e.details.get("to")) for e in sorted(
        world.rows(SandboxEvent, SandboxEvent.sandbox_id == sandbox.id), key=lambda e: (e.created_at, e.id)
    ) if e.details.get("to")]
    states = [changes[0][0]] + [to for _, to in changes]
    assert states[0] == "provisioning" and states[-1] == "deleted"
    assert "stopped" in states and states.count("busy") == 2
    for previous, following in zip(states, states[1:]):
        assert following in TRANSITIONS[previous] | {"failed", "deleted"}, f"{previous} -> {following}"


# ── a sandbox that dies during a run ─────────────────────────────────────


@pytest.mark.parametrize("fate, state_after, next_run", [
    ("stopped", "stopped", "resumed"),   # e.g. out of memory under a sandboxed kernel
    ("removed", "failed", "recreated"),  # really gone from its compute
])
def test_a_sandbox_that_dies_mid_run_is_resumed_if_it_only_stopped(world, report, monkeypatch, fate, state_after, next_run):
    from app.services.sandbox.runtime_client import SandboxRuntimeClient, SandboxRuntimeError

    rep = report()
    world.run_code(rep, world.admin_id, _python("open('kept.txt','w').write('x')"))
    sandbox = world.sandbox_of(rep)
    stub_id = sandbox.driver_ref["stub_id"]
    real_run = SandboxRuntimeClient.run

    async def dies(self, command, **kwargs):
        if fate == "stopped":
            get_stub_host().set_running(stub_id, False)
        else:
            get_stub_host().remove_sandbox(stub_id)
        raise SandboxRuntimeError("The sandbox stopped answering while the command was running.", code="unreachable")

    monkeypatch.setattr(SandboxRuntimeClient, "run", dies)
    with pytest.raises(SandboxError) as died:
        world.run_code(rep, world.admin_id, _python("x = bytearray(10**12)"))
    monkeypatch.setattr(SandboxRuntimeClient, "run", real_run)

    assert died.value.code == errors.UNAVAILABLE
    after = world.rows(Sandbox, Sandbox.id == sandbox.id)[0]
    assert after.state == state_after
    failed_run = sorted(world.rows(SandboxRun, SandboxRun.sandbox_id == sandbox.id), key=lambda r: r.started_at)[-1]
    assert failed_run.status == "failed" and failed_run.finished_at is not None
    if fate == "stopped":
        assert "memory" in died.value.message and "keeps its files" in died.value.message

    outcome = world.run_code(rep, world.admin_id, _python("import os\nprint(os.path.exists('kept.txt'))"))
    assert getattr(outcome, next_run) is True
    assert outcome.stdout == ("True\n" if fate == "stopped" else "False\n")


# ── races between runs, cancellation and the lifecycle job ───────────────


def _set_state(world, sandbox_id, state):
    async def _go(db):
        sandbox = await db.get(Sandbox, sandbox_id)
        sandbox.state = state
        await db.commit()
    world.call(_go)


@pytest.mark.parametrize("state", ["provisioning", "busy"])
def test_a_run_never_destroys_a_sandbox_another_call_is_creating_or_running_in(world, report, monkeypatch, state):
    rep = report()
    first = world.run_code(rep, world.admin_id, _python("open('keep.txt','w').write('kept')"))
    _set_state(world, first.sandbox_id, state)  # as another call would have it right now

    async def no_answer(self):
        return False  # its daemon is not up yet, or is slow under load
    from app.services.sandbox import runtime_client
    monkeypatch.setattr(runtime_client.SandboxRuntimeClient, "health", no_answer)

    with pytest.raises(SandboxError) as refused:
        world.run_code(rep, world.admin_id, _python("print('second')"))
    assert refused.value.code == errors.BUSY
    sandbox = world.sandbox_of(rep)
    assert (sandbox.id, sandbox.state) == (first.sandbox_id, state), "left exactly as the other call has it"
    assert "failed" not in _events(world, first.sandbox_id)

    monkeypatch.undo()
    _set_state(world, first.sandbox_id, "ready")
    after = world.run_code(rep, world.admin_id, _python("print(open('keep.txt').read())"))
    assert (after.sandbox_id, after.recreated, after.stdout) == (first.sandbox_id, False, "kept\n")


def test_a_cancelled_run_gives_the_sandbox_back_at_once(world, report):
    rep = report()
    world.run_code(rep, world.admin_id, _python("print('warm')"))

    async def cancelled_mid_run():
        started = asyncio.Event()

        async def long_run():
            async with world.session() as db:
                return await world.service.run(db, world.report_ref(rep), world.admin_id, _python(
                    "import time\nprint('go', flush=True)\ntime.sleep(60)"),
                    on_output=lambda kind, chunk: started.set())

        task = asyncio.ensure_future(long_run())
        await asyncio.wait_for(started.wait(), 30)
        task.cancel()  # the turn was torn down: no cancel event, the task itself is cancelled
        with pytest.raises(asyncio.CancelledError):
            await task

    run(cancelled_mid_run())
    sandbox = world.sandbox_of(rep)
    assert sandbox.state == "ready"
    runs = sorted(world.rows(SandboxRun, SandboxRun.sandbox_id == sandbox.id), key=lambda r: r.started_at)
    assert runs[-1].status == "cancelled" and runs[-1].finished_at is not None
    assert world.run_code(rep, world.admin_id, _python("print('after')")).stdout == "after\n"


def test_stop_and_idle_marking_leave_a_sandbox_alone_once_a_run_has_claimed_it(world, report):
    """The lifecycle job reads its sandboxes first and acts later. A run that
    claims one in between must not be stopped or relabelled."""
    rep = report()
    outcome = world.run_code(rep, world.admin_id, _python("print('warm')"))

    async def stale_then_act(db):
        stale = await db.get(Sandbox, outcome.sandbox_id)  # read as ready
        assert stale.state == "ready"
        async with world.session() as other:  # a run on another worker claims it
            claimed = await other.get(Sandbox, outcome.sandbox_id)
            claimed.state = "busy"
            await other.commit()
        assert await world.service.mark_idle(db, stale) is False
        with pytest.raises(SandboxError) as refused:
            await world.service.stop(db, stale, None, reason="idle")
        return refused.value.code

    assert world.call(stale_then_act) == errors.BUSY
    assert world.sandbox_of(rep).state == "busy"
    assert not {"idle", "stopped"} & set(_events(world, outcome.sandbox_id))
    assert get_stub_host().get(f"sbx-{outcome.sandbox_id}").running is True, "the container was never stopped"


def test_deleting_is_refused_while_the_compute_exists_but_cannot_be_used(world, report, monkeypatch):
    rep = report()
    outcome = world.run_code(rep, world.admin_id, _python("print('warm')"))
    from app.services.sandbox import computes as compute_service

    real = compute_service.build_driver

    async def cannot_build(db, compute, **kwargs):
        raise SandboxError(errors.COMPUTE_NOT_READY, "The compute's settings are not valid")
    monkeypatch.setattr(compute_service, "build_driver", cannot_build)

    async def delete(db):
        await world.service.delete(db, await db.get(Sandbox, outcome.sandbox_id), world.admin_id)
    with pytest.raises(SandboxError) as refused:
        world.call(delete)
    assert refused.value.code == errors.UNAVAILABLE
    assert world.sandbox_of(rep).state == "ready", "still known, so the delete can be retried"

    # A compute that is really gone has nothing left to remove.
    async def drop_compute(db):
        from datetime import datetime

        from app.models.sandbox import SandboxCompute
        (await db.get(SandboxCompute, world.compute_id)).deleted_at = datetime.utcnow()
        await db.commit()
    world.call(drop_compute)
    world.call(delete)
    assert world.sandbox_of(rep) is None
    monkeypatch.setattr(compute_service, "build_driver", real)


def test_a_file_rewritten_to_the_same_size_within_the_same_second_is_still_returned(world, report, monkeypatch):
    """Some filesystems stamp files to the second. Size and time then look
    unchanged after a quick rewrite; the sandbox's own clock settles it."""
    real_snapshot, real_clock = service_module._snapshot, service_module._sandbox_clock

    def to_the_second(stamp):
        return stamp[:19] + "Z" if stamp else stamp

    async def coarse_snapshot(runtime):
        return {name: (size, to_the_second(modified)) for name, (size, modified) in (await real_snapshot(runtime)).items()}

    async def coarse_clock(runtime):
        return to_the_second(await real_clock(runtime))
    monkeypatch.setattr(service_module, "_snapshot", coarse_snapshot)
    monkeypatch.setattr(service_module, "_sandbox_clock", coarse_clock)

    rep = report()
    world.run_code(rep, world.admin_id, _python("open('outputs/report.csv','w').write('aaa')"))
    again = world.run_code(rep, world.admin_id, _python("open('outputs/report.csv','w').write('bbb')"))
    assert [a.name for a in again.artifacts] == ["report.csv"]


@pytest.mark.parametrize("modified, began, expected", [
    ("2026-11-27T10:00:05Z", "2026-11-27T10:00:05Z", True),    # same second: may be this run's
    ("2026-11-27T10:00:06Z", "2026-11-27T10:00:05Z", True),
    ("2026-11-27T10:00:04Z", "2026-11-27T10:00:05Z", False),   # written before the run began
    ("2026-11-27T10:00:05.123456Z", "2026-11-27T10:00:05Z", False),  # precise stamps need no help
    ("2026-11-27T10:00:05+02:00", "2026-11-27T10:00:05Z", False),    # not comparable as text
    (None, "2026-11-27T10:00:05Z", False), ("2026-11-27T10:00:05Z", None, False),
])
def test_the_same_second_rule_only_applies_to_whole_second_stamps(modified, began, expected):
    assert service_module._not_before(modified, began) is expected
