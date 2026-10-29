"""Security invariants for the generated-code process boundary."""

import platform
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.ai.code_execution.code_execution import StreamingCodeExecutor
from app.ai.code_execution.sandbox.config import SandboxLimits
from app.ai.code_execution.sandbox.runner import SandboxCancelled, SandboxExecutionError, SandboxJob, run_job


def test_production_requires_kernel_confinement_by_default(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("BOW_SANDBOX_REQUIRE_LANDLOCK", raising=False)
    limits = SandboxLimits.from_env()
    assert limits.require_landlock


def test_production_runs_only_with_filesystem_and_tcp_confinement(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("BOW_SANDBOX_REQUIRE_LANDLOCK", raising=False)
    code = "def generate_df(ds_clients, excel_files): return pd.DataFrame({'ok': [1]})"
    try:
        result = run_job(SandboxJob(mode="data", code=code))
    except SandboxExecutionError as exc:
        assert "SandboxUnavailable" in str(exc)
    else:
        assert "rlimits" in result.applied
        assert result.applied["landlock"]["applied"]
        assert result.applied["landlock"]["net_blocked"]
        assert result.applied["process_creation_blocked"]


def test_sandbox_result_respects_parent_memory_budget(monkeypatch):
    monkeypatch.setenv("BOW_SANDBOX_MAX_RESULT_MB", "1")
    code = """
def generate_df(ds_clients, excel_files):
    return pd.DataFrame({"payload": ["x" * 2_000_000]})
"""
    with pytest.raises(SandboxExecutionError, match="result.*large|large.*result"):
        run_job(SandboxJob(mode="data", code=code))


def test_calendar_date_bounds_is_available_inside_sandbox():
    code = """
def generate_df(ds_clients, excel_files, calendar_date_bounds):
    start, exclusive_end = calendar_date_bounds({'from': '2024-06-01', 'to': '2024-06-02'})
    return pd.DataFrame({'start': [start], 'end': [exclusive_end]})
"""
    df = run_job(SandboxJob(mode="data", code=code)).df
    assert df.loc[0, "start"] == "2024-06-01"
    assert df.loc[0, "end"] == "2024-06-03"


def test_failed_sandbox_run_preserves_printed_diagnostics():
    code = """
def generate_df(ds_clients, excel_files):
    print('columns: missing amount')
    raise ValueError('bad input')
"""
    with pytest.raises(SandboxExecutionError, match="bad input") as caught:
        run_job(SandboxJob(mode="data", code=code))
    assert "columns: missing amount" in caught.value.captured_stdout


@pytest.mark.skipif(platform.system() != "Linux", reason="seccomp is Linux-only")
def test_generated_code_cannot_create_a_detached_process():
    code = """
def generate_df(ds_clients, excel_files):
    import os
    os.fork()
    return pd.DataFrame({"ok": [1]})
"""
    with pytest.raises(SandboxExecutionError, match="PermissionError"):
        run_job(SandboxJob(mode="data", code=code))


@pytest.mark.skipif(platform.system() != "Linux", reason="seccomp is Linux-only")
def test_generated_code_can_still_use_threads():
    code = """
def generate_df(ds_clients, excel_files):
    import threading
    values = []
    worker = threading.Thread(target=lambda: values.append(1))
    worker.start()
    worker.join()
    return pd.DataFrame({"ok": values})
"""
    assert run_job(SandboxJob(mode="data", code=code)).df["ok"].tolist() == [1]


def test_cancelled_job_requests_source_query_cancellation(monkeypatch):
    from app.data_sources import query_cancellation

    started = threading.Event()
    released = threading.Event()
    cancellation_requested = threading.Event()
    cancel_event = threading.Event()

    class Client:
        def execute_query(self, query):
            started.set()
            released.wait(timeout=10)
            return __import__("pandas").DataFrame({"ok": [1]})

    client = Client()

    def cancel_thread(target, thread_ident):
        assert target is client and thread_ident
        cancellation_requested.set()
        released.set()
        return "cancelled"

    monkeypatch.setattr(query_cancellation, "cancel_thread", cancel_thread)
    code = "def generate_df(ds_clients, excel_files): return ds_clients['main'].execute_query('SELECT 1')"
    executor = StreamingCodeExecutor(organization_settings=None)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            executor.execute_code, code=code, ds_clients={"main": client},
            excel_files=[], cancel_event=cancel_event,
        )
        assert started.wait(timeout=5)
        cancel_event.set()
        with pytest.raises(SandboxCancelled):
            future.result(timeout=5)
        assert cancellation_requested.wait(timeout=5)


@pytest.mark.parametrize(
    "abi, seccomp_reason, env, refuses",
    [
        (0, "", {}, True),
        (3, "", {}, True),
        (4, "", {}, False),
        (6, "", {}, False),
        (6, "unsupported seccomp architecture: riscv64", {}, True),
        (0, "", {"BOW_SANDBOX_REQUIRE_LANDLOCK": "0"}, False),
        (0, "", {"BOW_CODE_SANDBOX": "inprocess"}, False),
    ],
)
def test_startup_check_predicts_when_production_refuses_to_execute(monkeypatch, abi, seccomp_reason, env, refuses):
    # The kernel probes are the boundary: the check must flag exactly the
    # hosts on which the required confinement cannot be applied.
    from app.ai.code_execution.sandbox import landlock, seccomp
    from app.ai.code_execution.sandbox.runner import required_confinement_problem

    monkeypatch.setenv("ENVIRONMENT", "production")
    for name in ("BOW_SANDBOX_REQUIRE_LANDLOCK", "BOW_CODE_SANDBOX"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(landlock, "abi_version", lambda: abi)
    monkeypatch.setattr(seccomp, "unsupported_reason", lambda: seccomp_reason)

    assert bool(required_confinement_problem()) is refuses


@pytest.mark.asyncio
async def test_unavailable_confinement_is_not_retried_with_new_code(monkeypatch):
    # A host that cannot confine generated code fails every attempt the same
    # way. The retry loop must stop at once (no further code generation) and
    # tell the operator which setting decides it.
    from app.ai.code_execution.sandbox.runner import required_confinement_problem
    from app.ai.schemas.codegen import CodeGenContext, CodeGenRequest

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("BOW_SANDBOX_REQUIRE_LANDLOCK", raising=False)
    monkeypatch.delenv("BOW_CODE_SANDBOX", raising=False)
    if not required_confinement_problem():
        pytest.skip("this host provides the required confinement")

    generated = []

    async def codegen(**kwargs):
        generated.append(kwargs)
        return "def generate_df(ds_clients, excel_files):\n    return pd.DataFrame({'v': [1]})\n"

    events = [
        e async for e in StreamingCodeExecutor(organization_settings=None).generate_and_execute_stream_v2(
            request=CodeGenRequest(context=CodeGenContext(user_prompt="x", schemas_excerpt=""), retries=3),
            ds_clients={}, excel_files=[], code_generator_fn=codegen,
        )
    ]
    done = [e for e in events if e["type"] == "done"][-1]["payload"]
    assert len(generated) == 1
    assert done["errors"] and "BOW_SANDBOX_REQUIRE_LANDLOCK" in str(done["errors"])
