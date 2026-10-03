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
