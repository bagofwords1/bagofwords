"""Process-level sandbox for generated code (app/ai/code_execution/sandbox).

What these tests pin down is the *boundary*, not the happy path the rest of
the suite already covers:

- generated code runs in a different process with a scrubbed environment
- the parent's secrets (encryption key, DB URL) are unreachable from it
- a runaway loop is killed at the wall-clock limit; a memory hog hits the cap
- errors raised on the trusted side (query wrappers) come back as the SAME
  exception objects, so the retry loop's type checks keep working
- client proxies expose only execute_query
- the pptx path round-trips a deck as bytes
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pandas as pd
import pytest

from app.ai.code_execution.code_execution import (
    QueryTimeoutError,
    StreamingCodeExecutor,
    UnsafePythonError,
)
from app.ai.code_execution.sandbox import landlock, protocol
from app.ai.code_execution.sandbox.config import SandboxLimits
from app.ai.code_execution.sandbox.runner import (
    SandboxCrashError,
    SandboxExecutionError,
    SandboxJob,
    SandboxTimeoutError,
    run_job,
)


class _StubClient:
    def __init__(self, df=None, raises=None, sleep=0.0):
        self._df = df if df is not None else pd.DataFrame({"a": [1, 2, 3]})
        self._raises = raises
        self._sleep = sleep
        self.calls = []
        self.secret = "do-not-leak"

    def execute_query(self, query=None, *args, **kwargs):
        self.calls.append((query, args, kwargs))
        if self._sleep:
            time.sleep(self._sleep)
        if self._raises:
            raise self._raises
        return self._df.copy()


def _run(code, clients=None, **kw):
    return StreamingCodeExecutor(organization_settings=None).execute_code(
        code=code, ds_clients=clients or {}, excel_files=[], **kw
    )


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------

def test_runs_in_a_separate_process_with_scrubbed_env(monkeypatch):
    monkeypatch.setenv("BOW_ENCRYPTION_KEY", "parent-secret")
    monkeypatch.setenv("BOW_DATABASE_URL", "postgresql://user:pw@db/app")
    code = """
def generate_df(ds_clients, excel_files):
    import os as _os
    return pd.DataFrame({
        "pid": [_os.getpid()],
        "env_keys": [",".join(sorted(_os.environ.keys()))],
        "has_key": ["BOW_ENCRYPTION_KEY" in _os.environ],
        "has_db": ["BOW_DATABASE_URL" in _os.environ],
    })
"""
    # `os` is AST-forbidden for real generated code; probe the child directly.
    df = run_job(SandboxJob(mode="data", code=code)).df
    row = df.iloc[0]
    assert int(row["pid"]) != os.getpid()
    assert bool(row["has_key"]) is False
    assert bool(row["has_db"]) is False
    assert "BOW_SANDBOX_CHILD" in row["env_keys"]


def test_parent_memory_is_not_inherited():
    """spawn, not fork: a module-level value set in the parent is not present
    in the child's copy of the same module. (Uses the runner directly: the
    dunder access is AST-forbidden for real generated code.)"""
    import app.ai.code_execution.sandbox.config as cfg

    cfg._canary = "parent-only-secret"  # type: ignore[attr-defined]
    try:
        code = """
def generate_df(ds_clients, excel_files):
    import app.ai.code_execution.sandbox.config as cfg
    return pd.DataFrame({"seen": [str(cfg.__dict__.get("_canary"))]})
"""
        result = run_job(SandboxJob(mode="data", code=code))
        assert result.df.iloc[0]["seen"] == "None"
    finally:
        del cfg._canary  # type: ignore[attr-defined]


def test_client_object_never_crosses_the_boundary():
    client = _StubClient()
    code = """
def generate_df(ds_clients, excel_files):
    c = ds_clients["main"]
    try:
        _ = c.secret
        leaked = "yes"
    except AttributeError as e:
        leaked = "no: " + str(e)[:40]
    df = c.execute_query("SELECT 1")
    df["leaked"] = leaked
    return df
"""
    df, _, queries = _run(code, {"main": client})
    assert queries == ["SELECT 1"]
    assert df["leaked"].iloc[0].startswith("no:")
    assert client.calls[0][0] == "SELECT 1"


def test_stdout_is_captured_and_returned():
    code = """
def generate_df(ds_clients, excel_files):
    print("hello", 42)
    return pd.DataFrame({"x": [1]})
"""
    _, log, _ = _run(code)
    assert log == "hello 42\n"


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

def test_infinite_loop_is_killed_at_the_wall_clock_limit():
    code = """
def generate_df(ds_clients, excel_files):
    while True:
        pass
"""
    limits = SandboxLimits(timeout_seconds=2, memory_mb=0, cpu_seconds=0, require_landlock=False)
    t0 = time.monotonic()
    with pytest.raises(SandboxTimeoutError):
        run_job(SandboxJob(mode="data", code=code), limits=limits)
    assert time.monotonic() - t0 < 10


def test_memory_hog_hits_the_cap_instead_of_the_host():
    code = """
def generate_df(ds_clients, excel_files):
    blobs = []
    for _ in range(64):
        blobs.append(bytes(256 * 1024 * 1024))
    return pd.DataFrame({"n": [len(blobs)]})
"""
    limits = SandboxLimits(timeout_seconds=60, memory_mb=1024, cpu_seconds=0, require_landlock=False)
    with pytest.raises((SandboxExecutionError, SandboxCrashError)) as ei:
        run_job(SandboxJob(mode="data", code=code), limits=limits)
    if isinstance(ei.value, SandboxExecutionError):
        assert ei.value.exc_type == "MemoryError"


def test_cancel_event_kills_the_child():
    code = """
def generate_df(ds_clients, excel_files):
    import time as _t
    _t.sleep(30)
    return pd.DataFrame()
"""
    ev = threading.Event()
    threading.Timer(0.5, ev.set).start()
    t0 = time.monotonic()
    with pytest.raises(Exception) as ei:
        run_job(SandboxJob(mode="data", code=code), cancel_event=ev)
    assert "cancel" in str(ei.value).lower()
    assert time.monotonic() - t0 < 10


# ---------------------------------------------------------------------------
# Error propagation
# ---------------------------------------------------------------------------

def test_user_code_errors_carry_type_and_message():
    code = """
def generate_df(ds_clients, excel_files):
    return {}["nope"]
"""
    with pytest.raises(SandboxExecutionError) as ei:
        _run(code)
    assert ei.value.exc_type == "KeyError"
    assert "nope" in str(ei.value)


def test_wrapper_exceptions_surface_as_the_original_object():
    """A timeout raised by the trusted-side wrapper must come back as a
    QueryTimeoutError (not a look-alike), so callers keep their handling."""
    client = _StubClient(sleep=3)
    code = """
def generate_df(ds_clients, excel_files):
    return ds_clients["main"].execute_query("SELECT slow")
"""
    executor = StreamingCodeExecutor(organization_settings=None)
    from app.ai.code_execution import code_execution as ce

    original = ce.resolve_query_timeout
    ce.resolve_query_timeout = lambda client, settings: 1
    try:
        with pytest.raises(QueryTimeoutError):
            executor.execute_code(code=code, ds_clients={"main": client}, excel_files=[])
    finally:
        ce.resolve_query_timeout = original


def test_wrapper_exception_can_be_caught_by_generated_code():
    client = _StubClient(raises=RuntimeError("relation does not exist"))
    code = """
def generate_df(ds_clients, excel_files):
    try:
        return ds_clients["main"].execute_query("SELECT * FROM missing")
    except Exception as e:
        return pd.DataFrame({"err": [type(e).__name__ + ": " + str(e)]})
"""
    df, _, _ = _run(code, {"main": client})
    assert df["err"].iloc[0] == "RuntimeError: relation does not exist"


def test_ast_validation_still_runs_before_spawn():
    with pytest.raises(UnsafePythonError):
        _run("import os\ndef generate_df(a, b):\n    return pd.DataFrame()")


def test_missing_client_key_is_a_keyerror_like_before():
    code = """
def generate_df(ds_clients, excel_files):
    return ds_clients["ghost"].execute_query("SELECT 1")
"""
    with pytest.raises(SandboxExecutionError) as ei:
        _run(code, {"main": _StubClient()})
    assert ei.value.exc_type == "KeyError"


# ---------------------------------------------------------------------------
# Data transport
# ---------------------------------------------------------------------------

def test_dataframe_roundtrip_keeps_dtypes_and_index():
    client = _StubClient(pd.DataFrame({
        "i": [1, 2], "f": [1.5, 2.5], "s": ["a", "b"],
        "d": pd.to_datetime(["2024-01-01", "2024-02-01"]),
    }))
    code = """
def generate_df(ds_clients, excel_files):
    df = ds_clients["main"].execute_query("SELECT 1")
    return df.set_index("s")
"""
    df, _, _ = _run(code, {"main": client})
    assert list(df.index) == ["a", "b"]
    assert str(df["d"].dtype).startswith("datetime64")
    assert df["i"].dtype.kind == "i"


def test_untypeable_columns_fall_back_to_text():
    code = """
def generate_df(ds_clients, excel_files):
    return pd.DataFrame({"mixed": [1, "two", {"three": 3}], "ok": [1, 2, 3]})
"""
    df, _, _ = _run(code)
    assert df["ok"].tolist() == [1, 2, 3]
    assert df["mixed"].tolist() == ["1", "two", "{'three': 3}"]


def test_protocol_refuses_oversized_frames():
    import io
    import struct

    buf = io.BytesIO(struct.pack(">II", 10, 3 * 1024 * 1024 * 1024))
    with pytest.raises(protocol.ProtocolError):
        protocol.read_message(buf)


# ---------------------------------------------------------------------------
# pptx
# ---------------------------------------------------------------------------

def test_pptx_executes_in_sandbox_and_writes_output(tmp_path: Path):
    from app.ai.code_execution.pptx_executor import PptxCodeExecutor

    code = """
prs = Presentation()
slide = prs.slides.add_slide(prs.slide_layouts[5])
slide.shapes.title.text = report["title"]
prs.save(_pptx_output_path)
print("saved")
"""
    out = tmp_path / "deck.pptx"
    path, log = PptxCodeExecutor().execute_pptx_code(
        code=code, visualizations=[], report={"title": "Hello"}, output_path=out, images={}
    )
    assert path == out and out.stat().st_size > 1000
    assert log == "saved\n"


# ---------------------------------------------------------------------------
# Landlock bindings (the syscall itself is exercised only where the kernel
# has the LSM; the ABI probe and fail-open contract are testable everywhere)
# ---------------------------------------------------------------------------

def test_landlock_probe_is_non_negative_and_fail_open():
    abi = landlock.abi_version()
    assert abi >= 0
    if abi == 0:
        rep = landlock.restrict_self(read_paths=["/usr"])
        assert rep.applied is False and rep.abi == 0


def test_ruleset_attr_layout_matches_abi():
    assert len(landlock._ruleset_attr_bytes(1, 1, 0, 0)) == 8
    assert len(landlock._ruleset_attr_bytes(4, 1, 3, 0)) == 16
    assert len(landlock._ruleset_attr_bytes(6, 1, 3, 3)) == 24
