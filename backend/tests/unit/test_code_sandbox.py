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
import platform
import threading
import time
from pathlib import Path

import pandas as pd
import pytest

from app.ai.code_execution.code_execution import (
    QueryCapturingClientWrapper,
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


@pytest.mark.skipif(platform.system() == "Darwin", reason="macOS does not enforce RLIMIT_AS")
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


def test_fork_server_gives_each_run_a_fresh_process_quickly():
    """Consecutive runs come from distinct processes (nothing survives from
    one execution to the next) and, after the first, start in well under the
    cost of a cold interpreter."""
    code = """
def generate_df(ds_clients, excel_files):
    import os as _os
    return pd.DataFrame({"pid": [_os.getpid()]})
"""
    first = run_job(SandboxJob(mode="data", code=code))
    t0 = time.monotonic()
    second = run_job(SandboxJob(mode="data", code=code))
    elapsed = time.monotonic() - t0
    assert int(first.df["pid"][0]) != int(second.df["pid"][0])
    assert int(second.df["pid"][0]) != os.getpid()
    assert elapsed < 0.5, f"second run took {elapsed:.2f}s"


def test_spawn_fallback_when_fork_server_is_disabled(monkeypatch):
    from app.ai.code_execution.sandbox import runner as r

    monkeypatch.setattr(r._zygote, "enabled", False)
    code = """
def generate_df(ds_clients, excel_files):
    import os as _os
    return pd.DataFrame({"pid": [_os.getpid()], "key": ["BOW_ENCRYPTION_KEY" in _os.environ]})
"""
    monkeypatch.setenv("BOW_ENCRYPTION_KEY", "parent-secret")
    result = run_job(SandboxJob(mode="data", code=code))
    assert int(result.df["pid"][0]) != os.getpid()
    assert bool(result.df["key"][0]) is False


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
        return pd.DataFrame({"err": [str(e)]})
"""
    df, _, _ = _run(code, {"main": client})
    assert df["err"].iloc[0] == "relation does not exist"


def test_validator_allows_dunder_name_but_no_other_private_attributes():
    from app.ai.code_execution.code_execution import validate_python_code

    validate_python_code("def generate_df(a, b):\n    return pd.DataFrame({'m': [type(a).__name__]})")
    for attr in ("_bow_access", "_private", "__dict__", "__class__"):
        with pytest.raises(UnsafePythonError):
            validate_python_code(f"def generate_df(a, b):\n    return a.{attr}")


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


def test_wide_integer_columns_keep_their_values_across_the_boundary():
    # Connectors whose counters exceed int64 (Brocade port statistics) build
    # object-dtype frames of Python ints so pandas does not coerce them to
    # lossy floats. Arrow's inference overflows on such a column; it must be
    # re-typed as a nullable integer column, not handed over as text.
    code = """
def generate_df(ds_clients, excel_files):
    return pd.DataFrame(
        [[18446744073709551614, "0/12", True], [None, "0/13", True]],
        columns=["in_octets", "port", "_bow_complete"], dtype=object,
    )
"""
    df, _, _ = _run(code)
    assert df["in_octets"].iloc[0] == 18446744073709551614
    assert df["in_octets"].iloc[1] is None
    assert df["port"].tolist() == ["0/12", "0/13"]
    # Object columns come back as object columns of Python scalars, as they
    # did in-process: `is True` holds, not just truthiness.
    assert df["_bow_complete"].iloc[0] is True
    assert df["in_octets"].dtype == object

    # Still text for a column Arrow genuinely cannot type.
    wide, meta = protocol.dataframe_to_arrow(pd.DataFrame({"w": [1 << 70, 1]}, dtype=object))
    assert meta["stringified"] == ["w"]
    assert protocol.arrow_to_dataframe(wide, meta)["w"].tolist() == [str(1 << 70), "1"]


@pytest.mark.parametrize(
    "build",
    [
        "df.assign(bucket=pd.cut(df.amount, [-1, 10, 50, 1000]))",
        "df.assign(bucket=pd.qcut(df.amount, 3))",
        "df.groupby(pd.cut(df.amount, [-1, 10, 50, 1000]), observed=False).size().to_frame('n')",
        "df.groupby(pd.qcut(df.amount, 4).rename('bucket'), observed=False).amount.sum().reset_index()",
    ],
    ids=["cut-column", "qcut-column", "cut-index", "qcut-reset-index"],
)
def test_binned_frames_cross_the_boundary(build):
    # pd.cut / pd.qcut buckets convert to Arrow but not back to pandas. A
    # frame holding them (as a column or as the index of a groupby) must
    # still reach the parent, with the bucket labels as text.
    code = f"""
def generate_df(ds_clients, excel_files):
    df = pd.DataFrame({{"amount": [3, 17, 42, 99, 250, 7, 61, 12]}})
    return {build}
"""
    expected = eval(build, {"pd": pd}, {"df": pd.DataFrame({"amount": [3, 17, 42, 99, 250, 7, 61, 12]})})
    df, _, _ = _run(code)
    assert df.shape == expected.shape
    for col in expected.columns:
        if isinstance(expected[col].dtype, pd.CategoricalDtype):
            assert df[col].tolist() == expected[col].astype(str).tolist()
    if isinstance(expected.index, pd.CategoricalIndex):
        assert df.index.tolist() == expected.index.astype(str).tolist()


def test_abort_before_the_query_thread_registers_still_cancels_the_query(monkeypatch):
    # The runner's abort thread and the wrapper's query thread race at job
    # cancellation. If the abort lands before the query thread is running,
    # it must be remembered and honoured once the thread starts, not lost.
    from app.data_sources import query_cancellation

    released = threading.Event()
    cancelled = threading.Event()

    class Client:
        def execute_query(self, query):
            released.wait(timeout=10)
            return pd.DataFrame({"ok": [1]})

    client = Client()

    def cancel_thread(target, thread_ident):
        assert target is client and thread_ident
        cancelled.set()
        released.set()
        return "cancelled"

    monkeypatch.setattr(query_cancellation, "cancel_thread", cancel_thread)
    wrapper = QueryCapturingClientWrapper(client, [], [], query_timeout_seconds=30)
    wrapper.cancel_active_query()  # abort arrives first: nothing is running yet
    assert not cancelled.is_set()
    df = wrapper.execute_query("SELECT 1")  # the query path picks the abort up itself
    assert cancelled.is_set() and df["ok"].tolist() == [1]

    # And the ordinary order still works: a running query is cancelled directly.
    released.clear()
    cancelled.clear()
    wrapper2 = QueryCapturingClientWrapper(client, [], [], query_timeout_seconds=30)
    worker = threading.Thread(target=wrapper2.execute_query, args=("SELECT 1",), daemon=True)
    worker.start()
    deadline = time.monotonic() + 5
    while wrapper2._active_query_thread is None or wrapper2._active_query_thread.ident is None:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    wrapper2.cancel_active_query()
    assert cancelled.wait(timeout=5)
    worker.join(timeout=5)
    assert not worker.is_alive()


def test_runner_works_with_more_than_1024_descriptors_open():
    # select(2) refuses fds >= FD_SETSIZE. A busy API worker (many DB
    # connections, sockets, open files) hands out pipe fds past 1024, and
    # the supervision loop must still wait on them; with select it raised
    # "filedescriptor out of range in select()" mid-run.
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = 1300
    if soft < want:
        if hard != resource.RLIM_INFINITY and hard < want:
            pytest.skip(f"RLIMIT_NOFILE hard limit {hard} too low to open {want} fds")
        resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    held = []
    try:
        while len(held) < 1100:
            held.append(os.open(os.devnull, os.O_RDONLY))
        assert held[-1] >= 1024
        df, _, _ = _run("def generate_df(ds_clients, excel_files): return pd.DataFrame({'ok': [1]})")
        assert df["ok"].tolist() == [1]
    finally:
        for fd in held:
            os.close(fd)
        if soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_protocol_refuses_oversized_frames():
    import io
    import struct

    buf = io.BytesIO(struct.pack(">II", 10, 3 * 1024 * 1024 * 1024))
    with pytest.raises(protocol.ProtocolError):
        protocol.read_message(buf)


def test_query_params_render_the_same_sql_as_in_process(monkeypatch):
    # Query arguments cross the boundary as JSON. Params that are not JSON
    # types (a set built from a column, a datetime) must still render the
    # SQL they rendered when the code ran in the API process.
    code = """
def generate_df(ds_clients, excel_files):
    import datetime
    ids = set(pd.DataFrame({"i": [42, 7, 42, 19]})["i"])
    c = ds_clients["db"]
    c.execute_query("select * from t where id in :ids", params={"ids": ids})
    c.execute_query("select * from t where ts >= :ts and d = :d",
                    params={"ts": datetime.datetime(2023, 11, 5, 8, 30), "d": datetime.date(2023, 12, 31)})
    return pd.DataFrame({"a": [1]})
"""
    rendered = {}
    for mode in ("inprocess", "subprocess"):
        monkeypatch.setenv("BOW_CODE_SANDBOX", mode)
        client = _StubClient()
        _run(code, {"db": client})
        rendered[mode] = [q for q, _, _ in client.calls]
    assert len(rendered["subprocess"]) == 2
    assert rendered["subprocess"] == rendered["inprocess"]


def test_child_meta_cannot_multiply_the_parents_decode_work():
    # object_columns is chosen by the child. Repeated or malformed entries
    # must not turn into repeated whole-column copies in the API worker.
    frame = pd.DataFrame({"n": range(100_000), "s": ["a"] * 100_000}, dtype=object)
    payload, meta = protocol.dataframe_to_arrow(frame)
    hostile = dict(meta, object_columns=meta["object_columns"] * 3000 + ["0", True, -1, 99, None, 1.0])

    cpu0 = time.thread_time()
    df = protocol.arrow_to_dataframe(payload, hostile)
    cpu = time.thread_time() - cpu0

    pd.testing.assert_frame_equal(df, protocol.arrow_to_dataframe(payload, meta))
    # One restore per column costs milliseconds; 3000 repeats cost seconds.
    assert cpu < 1.0, f"decode used {cpu:.2f}s CPU"
    assert protocol.arrow_to_dataframe(payload, dict(meta, object_columns="0,1")).shape == frame.shape


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


@pytest.mark.parametrize("mode", ["subprocess", "inprocess"])
def test_pptx_repair_text_points_at_the_failing_line(tmp_path: Path, monkeypatch, mode):
    # The repair prompt needs the line in the generated script that failed,
    # wherever the script ran.
    from app.ai.code_execution.pptx_executor import PptxCodeExecutor
    from app.ai.tools.implementations.create_artifact import CreateArtifactTool

    monkeypatch.setenv("BOW_CODE_SANDBOX", mode)
    code = """
prs = Presentation()
slide = prs.slides.add_slide(prs.slide_layouts[5])
title = visualizations[3]["title"]
prs.save(_pptx_output_path)
"""
    with pytest.raises(Exception) as caught:
        PptxCodeExecutor().execute_pptx_code(
            code=code, visualizations=[{"title": "only one"}], report={}, output_path=tmp_path / "d.pptx", images={}
        )
    text = CreateArtifactTool._pptx_error_text(caught.value)
    assert '<string>", line 4' in text
    assert "IndexError" in text


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


# ---------------------------------------------------------------------------
# Review findings on PR #1095
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "read",
    [
        "pd.read_csv(excel_files[0].path)",
        # The prompt shows the stored relative path, so generated (and saved)
        # code uses it literally, for read_text and for picking a file.
        "pd.read_csv(io.StringIO(read_text({rel!r})))",
        "pd.read_csv([f for f in excel_files if f.path == {rel!r}][0].path)",
    ],
    ids=["path-attr", "read-text-literal", "match-on-path"],
)
def test_relative_upload_paths_work_in_the_child(tmp_path: Path, monkeypatch, read):
    """FileService stores upload paths relative to the backend cwd and the
    child runs from its own scratch dir. Code must still see the stored path
    unchanged, and be able to read the file through it."""
    monkeypatch.chdir(tmp_path)
    rel = str(Path("uploads") / "files" / "sample.csv")
    Path(rel).parent.mkdir(parents=True)
    Path(rel).write_text("a,b\n1,2\n3,4\n")

    class _File:
        path = rel
        filename = "sample.csv"
        content_type = "text/csv"

    code = f"""
def generate_df(ds_clients, excel_files):
    import io
    return {read.format(rel=rel)}
"""
    df, _, _ = StreamingCodeExecutor(organization_settings=None).execute_code(
        code=code, ds_clients={}, excel_files=[_File()]
    )
    assert df["a"].tolist() == [1, 3]
    # Cleaning up the job's scratch dir must not touch the upload itself.
    assert Path(rel).read_text().startswith("a,b")


def test_blocking_rpc_cannot_outlive_the_wall_clock():
    """A trusted-side handler that blocks (a slow query) must not suspend
    the sandbox's own deadline: the child is killed and the timeout raised
    at the limit, not when the handler eventually returns."""
    code = """
def generate_df(ds_clients, excel_files):
    return ds_clients["main"].execute_query("SELECT slow")
"""

    def slow_query(key, args, kwargs):
        time.sleep(4)
        return pd.DataFrame({"a": [1]})

    limits = SandboxLimits(timeout_seconds=1, memory_mb=0, cpu_seconds=0, require_landlock=False)
    t0 = time.monotonic()
    with pytest.raises(SandboxTimeoutError):
        run_job(SandboxJob(mode="data", code=code, client_keys=["main"]), execute_query=slow_query, limits=limits)
    assert time.monotonic() - t0 < 2.5


def test_abandoned_rpc_handler_writes_nothing_into_reused_descriptors():
    """A query still running when the sandbox times out is abandoned, and
    finishes later. Whatever it does then must not reach descriptors the
    process has opened in the meantime (DB sockets, other jobs' pipes)."""
    release = threading.Event()
    code = """
def generate_df(ds_clients, excel_files):
    return ds_clients["main"].execute_query("SELECT slow")
"""

    def slow_query(key, args, kwargs):
        release.wait(timeout=10)
        return pd.DataFrame({"a": [1]})

    limits = SandboxLimits(timeout_seconds=1, memory_mb=0, cpu_seconds=0, require_landlock=False)
    with pytest.raises(SandboxTimeoutError):
        run_job(SandboxJob(mode="data", code=code, client_keys=["main"]), execute_query=slow_query, limits=limits)

    # Freshly opened descriptors take the lowest free numbers, i.e. the ones
    # the runner just released.
    pipes = [os.pipe() for _ in range(16)]
    try:
        release.set()
        for t in threading.enumerate():
            if t.name == "bow_sandbox_rpc":
                t.join(timeout=5)
        for r, _ in pipes:
            os.set_blocking(r, False)
            with pytest.raises(BlockingIOError):
                os.read(r, 16)
    finally:
        for r, w in pipes:
            os.close(r)
            os.close(w)


def test_child_closing_stderr_does_not_make_the_runner_spin():
    """A closed stderr pipe polls ready forever. The runner must stop
    watching it rather than burn a CPU core in its supervision loop for as
    long as the child keeps running."""
    code = """
def generate_df(ds_clients, excel_files):
    import os, time
    os.close(2)
    time.sleep(2)
    return pd.DataFrame({"ok": [1]})
"""
    cpu0 = time.thread_time()
    df = run_job(SandboxJob(mode="data", code=code)).df
    cpu = time.thread_time() - cpu0
    assert df["ok"].tolist() == [1]
    # An idle supervision loop costs milliseconds; a spinning one ~2 s.
    assert cpu < 0.5, f"runner used {cpu:.2f}s CPU while the child slept"


def test_partial_frame_cannot_outlive_the_wall_clock():
    """A child that writes part of a frame and stalls must still be killed
    at the deadline: frame reads are supervised, never blocking."""
    from app.ai.code_execution.sandbox import runner as r

    rfd, wfd = os.pipe()
    os.write(wfd, b"\x00\x00")  # 2 of the 8 header bytes, then silence

    class _FakeChild:
        def __init__(self):
            self.from_child = os.fdopen(rfd, "rb", buffering=0)
            os.set_blocking(rfd, False)
            self.stderr_fd = os.open(os.devnull, os.O_RDONLY)
            self.stderr_eof = False
            self.killed = False

        def drain_stderr(self):
            pass

        def kill(self):
            self.killed = True

    fake = _FakeChild()
    limits = SandboxLimits(timeout_seconds=1, memory_mb=0, cpu_seconds=0, require_landlock=False)
    t0 = time.monotonic()
    try:
        with pytest.raises(SandboxTimeoutError):
            r._read_exact_with_deadline(fake, 8, time.monotonic() + 1, None, limits)
        assert fake.killed and time.monotonic() - t0 < 2.5
    finally:
        os.close(wfd)
        fake.from_child.close()
        os.close(fake.stderr_fd)


def test_child_that_stops_reading_cannot_outlive_the_wall_clock():
    """A child that sends an RPC and then never reads the reply must still
    be killed at the deadline. The reply (a query result) is larger than a
    pipe buffer, so an unsupervised write to the child would block the API
    worker for as long as the child chooses to sleep."""
    code = """
def generate_df(ds_clients, excel_files):
    import sys, time
    from app.ai.code_execution.sandbox.protocol import write_message
    f = sys._getframe()
    while "rpc" not in f.f_locals:
        f = f.f_back
    write_message(f.f_locals["rpc"]._w, {
        "t": "rpc", "id": 1, "method": "execute_query",
        "client": "main", "args": ["SELECT big"], "kwargs": {},
    })
    time.sleep(30)
"""

    def big_query(key, args, kwargs):
        # Distinct values (~4 MB pickled): identical strings would be
        # memoized by pickle and fit in the pipe buffer.
        return pd.DataFrame({"payload": [f"{i:08d}" * 128 for i in range(4096)]})

    limits = SandboxLimits(timeout_seconds=1, memory_mb=0, cpu_seconds=0, require_landlock=False)
    outcome = {}

    def _target():
        try:
            run_job(SandboxJob(mode="data", code=code, client_keys=["main"]), execute_query=big_query, limits=limits)
        except BaseException as e:  # noqa: BLE001 - asserted below
            outcome["exc"] = e

    t0 = time.monotonic()
    worker = threading.Thread(target=_target, daemon=True)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive(), "runner blocked writing to a child that stopped reading"
    assert isinstance(outcome.get("exc"), SandboxTimeoutError)
    assert time.monotonic() - t0 < 2.5


def test_forked_child_uses_its_own_scratch_dir():
    """Each job gets a private cwd/TMPDIR; nothing written by one job is
    visible to the next, and the fork server's directory is never used."""
    code = """
def generate_df(ds_clients, excel_files):
    import os as _os, tempfile as _tf
    marker = "left-by-previous-job.txt"
    seen_before = _os.path.exists(marker)
    with open(marker, "w") as fh:
        fh.write("x")
    return pd.DataFrame({
        "cwd": [_os.getcwd()],
        "tmp": [_tf.gettempdir()],
        "seen_before": [seen_before],
    })
"""
    a = run_job(SandboxJob(mode="data", code=code))
    b = run_job(SandboxJob(mode="data", code=code))
    ra, rb = a.df.iloc[0], b.df.iloc[0]
    assert ra["cwd"] != rb["cwd"]
    assert os.path.realpath(ra["tmp"]) == os.path.realpath(ra["cwd"])
    assert os.path.realpath(rb["tmp"]) == os.path.realpath(rb["cwd"])
    assert bool(ra["seen_before"]) is False and bool(rb["seen_before"]) is False
    assert a.applied.get("cwd") == ra["cwd"]
    assert not os.path.exists(ra["cwd"]) and not os.path.exists(rb["cwd"])


def test_landlock_rights_are_masked_for_plain_files(tmp_path: Path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    assert landlock.rights_for_path(str(f), landlock.READ_RIGHTS) == landlock.FS_READ_FILE
    assert landlock.rights_for_path(str(tmp_path), landlock.READ_RIGHTS) == landlock.READ_RIGHTS
    assert landlock.rights_for_path(str(f), landlock.RW_RIGHTS) & landlock.FS_READ_DIR == 0


@pytest.mark.skipif(landlock.abi_version() == 0, reason="kernel has no Landlock")
def test_landlock_policy_applies_and_confines(tmp_path: Path):
    """On a Landlock-capable kernel the runner's policy must apply as a whole
    (no rule may abort it) and actually confine: allowed inputs readable,
    everything else denied, TCP refused on ABI >= 4."""
    upload = tmp_path / "data.csv"
    upload.write_text("a\n1\n")
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")

    class _File:
        path = str(upload)
        filename = "data.csv"
        content_type = "text/csv"

    code = f"""
def generate_df(ds_clients, excel_files):
    import socket as _sock, json as _json
    out = {{}}
    out["upload"] = len(pd.read_csv(excel_files[0].path))
    try:
        open({str(secret)!r}).read()
        out["secret"] = "readable"
    except PermissionError:
        out["secret"] = "denied"
    try:
        open("/etc/hostname").read()
        out["etc"] = "readable"
    except (PermissionError, FileNotFoundError):
        out["etc"] = "denied"
    import sklearn  # imports after confinement must still work
    try:
        s = _sock.socket(); s.settimeout(1); s.connect(("127.0.0.1", 9)); out["tcp"] = "connected"
    except PermissionError:
        out["tcp"] = "denied"
    except OSError as e:
        out["tcp"] = "refused:" + type(e).__name__
    return pd.DataFrame({{"k": list(out), "v": [str(x) for x in out.values()]}})
"""
    result = run_job(SandboxJob(mode="data", code=code, files=[_File()]))
    ll = result.applied["landlock"]
    assert ll["applied"] is True, ll
    assert ll["rule_errors"] == [], ll
    got = dict(zip(result.df["k"], result.df["v"]))
    assert got["upload"] == "1"
    assert got["secret"] == "denied"
    assert got["etc"] == "denied"
    if ll["abi"] >= 4:
        assert got["tcp"] == "denied", got
