"""Untrusted-side entrypoint: ``python -m app.ai.code_execution.sandbox.child``.

Started by `runner.py` with two inherited pipe fds (job in, results out).
Sequence:

1. read the job (pickle from the trusted parent)
2. drop privileges: rlimits, no_new_privs, Landlock (when available)
3. report what was applied (`ready`)
4. build the namespace, exec the code, call `generate_df` (or run the
   python-pptx script), brokering `execute_query` / `http.*` back to the
   parent over the pipe
5. send `result` (Arrow IPC for the DataFrame) or `error`

Nothing here may import application modules that reach the database, the
settings loader or any credential store. Keep it to the stdlib, pandas,
numpy, pyarrow and the sibling `sandbox.*` modules.
"""
from __future__ import annotations

import io
import os
import pickle
import resource
import sys
import traceback
from typing import Any, Dict, List, Optional

# Imported before restrictions so their lazy submodule loads don't trip a
# read-denied path later; the venv stays readable regardless.
import numpy as np  # noqa: F401
import pandas as pd

from app.ai.code_execution.sandbox import landlock
from app.ai.code_execution.sandbox.namespace import (
    SandboxFile,
    build_loadable_closures,
    build_read_text,
    invoke_generate_df,
)
from app.ai.code_execution.sandbox.protocol import (
    dataframe_to_arrow,
    json_safe,
    read_message,
    write_message,
)

STDOUT_CAP_CHARS = 2_000_000


class _CappedBuffer(io.StringIO):
    """StringIO that stops growing past STDOUT_CAP_CHARS (a runaway print
    loop must not turn into a runaway pipe write)."""

    def __init__(self):
        super().__init__()
        self._truncated = False

    def write(self, s):
        if self._truncated:
            return len(s)
        if self.tell() + len(s) > STDOUT_CAP_CHARS:
            keep = max(0, STDOUT_CAP_CHARS - self.tell())
            super().write(s[:keep])
            super().write("\n[stdout truncated by sandbox]\n")
            self._truncated = True
            return len(s)
        return super().write(s)


class _Rpc:
    """Blocking request/response channel to the parent."""

    def __init__(self, reader, writer):
        self._r = reader
        self._w = writer
        self._next_id = 1
        self._exc_types: Dict[str, type] = {}

    def call(self, method: str, **fields) -> Any:
        rid = self._next_id
        self._next_id += 1
        write_message(self._w, {"t": "rpc", "id": rid, "method": method, **fields})
        header, payload = read_message(self._r)
        if header.get("t") != "rpc_result" or header.get("id") != rid:
            raise RuntimeError(f"sandbox protocol violation: unexpected {header.get('t')!r}")
        if header.get("ok"):
            return pickle.loads(payload) if payload else None
        raise self._remote_exception(header, rid)

    def _remote_exception(self, header: Dict[str, Any], rid: int) -> BaseException:
        name = str(header.get("exc_type") or "Exception")
        cls = self._exc_types.get(name)
        if cls is None:
            cls = type(name, (Exception,), {})
            self._exc_types[name] = cls
        exc = cls(str(header.get("message") or ""))
        # Tag so the parent can re-raise the ORIGINAL exception object (with
        # its real class and attributes) when this one propagates unchanged
        # out of the user's code.
        exc._bow_rpc_id = rid  # type: ignore[attr-defined]
        return exc


class _RemoteClient:
    """What generated code sees as `ds_clients[key]`.

    Only the query surface is forwarded. Every other attribute the real
    client has (credentials, connection objects, schema helpers) stays on
    the trusted side, which is the whole point.
    """

    __slots__ = ("_rpc", "_key")

    def __init__(self, rpc: _Rpc, key: str):
        self._rpc = rpc
        self._key = key

    def execute_query(self, *args, **kwargs):
        return self._rpc.call(
            "execute_query",
            client=self._key,
            args=json_safe(list(args)),
            kwargs=json_safe(dict(kwargs)),
        )

    def query(self, *args, **kwargs):
        return self.execute_query(*args, **kwargs)

    def __getattr__(self, name):
        raise AttributeError(
            f"ds_clients[{self._key!r}] exposes only execute_query(...) inside the "
            f"sandbox; {name!r} is not available. Fetch data with execute_query and "
            "work on the returned DataFrame."
        )

    def __repr__(self):
        return f"<DataSourceClient {self._key!r}>"


class _RemoteHttp:
    """`http.get` / `http.batch_get` proxy; the SafeHttpClient (SSRF policy,
    size caps, audit) runs in the parent."""

    def __init__(self, rpc: _Rpc):
        self._rpc = rpc

    def get(self, url, *, timeout=None):
        return self._rpc.call("http_get", url=str(url), timeout=timeout)

    def batch_get(self, urls, *, concurrency=None, timeout=None):
        if not isinstance(urls, (list, tuple)):
            raise TypeError("urls must be a list")
        return self._rpc.call(
            "http_batch_get", urls=[str(u) for u in urls], concurrency=concurrency, timeout=timeout
        )


# ---------------------------------------------------------------------------
# Restrictions
# ---------------------------------------------------------------------------

def _apply_rlimits(limits: Dict[str, Any]) -> Dict[str, Any]:
    applied: Dict[str, Any] = {}
    mem_mb = int(limits.get("memory_mb") or 0)
    if mem_mb > 0:
        cap = mem_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
        applied["memory_mb"] = mem_mb
    cpu = int(limits.get("cpu_seconds") or 0)
    if cpu > 0:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
        applied["cpu_seconds"] = cpu
    # No core dumps (they would land in a writable dir with data in them).
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # Files the child may create are bounded (pptx output, matplotlib cache).
    fsize = int(limits.get("fsize_mb") or 256) * 1024 * 1024
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
        applied["fsize_mb"] = fsize // (1024 * 1024)
    except (ValueError, OSError):
        pass
    return applied


def _apply_landlock(job: Dict[str, Any]) -> Dict[str, Any]:
    fs = job.get("fs") or {}
    report = landlock.restrict_self(
        read_paths=fs.get("read_paths") or [],
        rw_paths=fs.get("rw_paths") or [],
        read_files=fs.get("read_files") or [],
        rw_files=fs.get("rw_files") or [],
        block_tcp=True,
        scope_signals=True,
    )
    return report.as_dict()


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _run_data_job(job: Dict[str, Any], rpc: _Rpc):
    excel_files = [SandboxFile(a) for a in (job.get("files") or [])]
    clients = {key: _RemoteClient(rpc, key) for key in (job.get("client_keys") or [])}
    http = _RemoteHttp(rpc) if job.get("http_enabled") else None
    load_step, load_entity = build_loadable_closures(
        job.get("loadables"), enable_load_step=bool(job.get("load_step_enabled"))
    )
    namespace: Dict[str, Any] = {
        "pd": pd,
        "np": np,
        "db_clients": clients,
        "excel_files": excel_files,
        "load_step": load_step,
        "load_entity": load_entity,
        "read_text": build_read_text(excel_files),
    }
    if http is not None:
        namespace["http"] = http

    exec(job["code"], namespace)
    generate_df = namespace.get("generate_df")
    if not generate_df:
        raise RuntimeError("No generate_df function found in code")
    return invoke_generate_df(
        generate_df, clients, excel_files, http,
        load_step=load_step, load_entity=load_entity, params=job.get("params"),
    )


def _run_pptx_job(job: Dict[str, Any], scratch_dir: str):
    from pptx import Presentation
    from pptx.util import Inches, Pt, Emu
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
    from pptx.chart.data import CategoryChartData, ChartData

    images: Dict[str, bytes] = job.get("images") or {}

    def image(file_id: str) -> io.BytesIO:
        raw = images.get(str(file_id))
        if raw is None:
            raise ValueError(
                f"Unknown image id {file_id!r}. Available: {sorted(images) or 'none'}"
            )
        return io.BytesIO(raw)

    output_path = os.path.join(scratch_dir, "output.pptx")
    namespace = {
        "Presentation": Presentation,
        "Inches": Inches, "Pt": Pt, "Emu": Emu, "RGBColor": RGBColor,
        "PP_ALIGN": PP_ALIGN, "MSO_ANCHOR": MSO_ANCHOR, "MSO_SHAPE": MSO_SHAPE,
        "XL_CHART_TYPE": XL_CHART_TYPE, "XL_LEGEND_POSITION": XL_LEGEND_POSITION,
        "CategoryChartData": CategoryChartData, "ChartData": ChartData,
        "visualizations": job.get("visualizations") or [],
        "report": job.get("report") or {},
        "image": image,
        "image_ids": list(images.keys()),
        "_pptx_output_path": output_path,
    }
    exec(job["code"], namespace)
    if not os.path.exists(output_path):
        raise RuntimeError(
            "PPTX code executed but no file was created. "
            "Ensure the code calls prs.save(_pptx_output_path)"
        )
    with open(output_path, "rb") as fh:
        return fh.read()


def _result_frame(value: Any, log: List[str]):
    """Normalize what generate_df returned into (DataFrame|None)."""
    if value is None:
        return None
    if isinstance(value, pd.DataFrame):
        return value
    if isinstance(value, pd.Series):
        return value.to_frame()
    log.append(f"[sandbox] generate_df returned {type(value).__name__}, expected a DataFrame")
    return None


def _enter_scratch_dir(scratch_dir: str) -> None:
    """Make this job's private scratch directory the child's working and
    temp directory. A forked child would otherwise keep the fork server's
    cwd and TMPDIR/HOME/MPLCONFIGDIR, which are shared across jobs and lie
    outside this job's Landlock write allowance."""
    import tempfile

    os.makedirs(os.path.join(scratch_dir, "mpl"), exist_ok=True)
    os.chdir(scratch_dir)
    for var in ("TMPDIR", "TMP", "TEMP", "HOME"):
        os.environ[var] = scratch_dir
    os.environ["MPLCONFIGDIR"] = os.path.join(scratch_dir, "mpl")
    tempfile.tempdir = None  # re-resolve from TMPDIR on next use


def main(argv: Optional[List[str]] = None) -> int:
    """Standalone entrypoint: one fresh interpreter per job (the fallback
    when the fork server is disabled or unavailable)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    in_fd = int(argv[argv.index("--in-fd") + 1])
    out_fd = int(argv[argv.index("--out-fd") + 1])
    return run(in_fd, out_fd)


def run(in_fd: int, out_fd: int) -> int:
    """Serve one job over the given pipe fds. Called by `main` in a spawned
    interpreter, or by the fork server in a freshly forked child."""
    reader = os.fdopen(in_fd, "rb", buffering=0)
    writer = os.fdopen(out_fd, "wb", buffering=0)

    header, payload = read_message(reader)
    if header.get("t") != "job":
        write_message(writer, {"t": "error", "exc_type": "ProtocolError", "message": "expected job"})
        return 2
    job: Dict[str, Any] = pickle.loads(payload)
    limits = job.get("limits") or {}
    scratch_dir = job.get("scratch_dir") or os.getcwd()
    _enter_scratch_dir(scratch_dir)

    applied: Dict[str, Any] = {"pid": os.getpid(), "cwd": os.getcwd()}
    try:
        applied["rlimits"] = _apply_rlimits(limits)
    except Exception as e:  # pragma: no cover - platform dependent
        applied["rlimits_error"] = f"{type(e).__name__}: {e}"
    try:
        landlock.set_no_new_privs()
        applied["no_new_privs"] = True
    except Exception as e:  # pragma: no cover - platform dependent
        applied["no_new_privs"] = f"{type(e).__name__}: {e}"
    ll = _apply_landlock(job)
    applied["landlock"] = ll
    if limits.get("require_landlock") and not ll.get("applied"):
        write_message(writer, {
            "t": "error",
            "exc_type": "SandboxUnavailable",
            "message": f"Landlock required but not available: {ll.get('reason')}",
            "applied": applied,
        })
        return 3
    write_message(writer, {"t": "ready", "applied": applied})

    rpc = _Rpc(reader, writer)
    capture = _CappedBuffer()
    real_stdout = sys.stdout
    sys.stdout = capture
    extra_log: List[str] = []
    try:
        if job.get("mode") == "pptx":
            pptx_bytes = _run_pptx_job(job, scratch_dir)
            sys.stdout = real_stdout
            write_message(writer, {"t": "result", "kind": "pptx", "stdout": capture.getvalue()}, pptx_bytes)
            return 0
        value = _run_data_job(job, rpc)
        df = _result_frame(value, extra_log)
        sys.stdout = real_stdout
        stdout_text = capture.getvalue() + ("\n".join(extra_log) + "\n" if extra_log else "")
        if df is None:
            write_message(writer, {"t": "result", "kind": "none", "stdout": stdout_text})
            return 0
        arrow_bytes, meta = dataframe_to_arrow(df)
        write_message(writer, {"t": "result", "kind": "dataframe", "stdout": stdout_text, "meta": meta}, arrow_bytes)
        return 0
    except BaseException as e:  # noqa: BLE001 - everything is reported to the parent
        sys.stdout = real_stdout
        tb = traceback.format_exc()
        msg = {
            "t": "error",
            "exc_type": type(e).__name__,
            "message": str(e),
            "traceback": tb[-8000:],
            "stdout": capture.getvalue(),
        }
        rpc_id = getattr(e, "_bow_rpc_id", None)
        if rpc_id is not None:
            msg["rpc_id"] = rpc_id
        try:
            write_message(writer, msg)
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
