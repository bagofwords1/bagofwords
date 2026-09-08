"""Trusted-side driver for the code sandbox.

`run_job` spawns a fresh interpreter for one execution, feeds it the job,
answers its RPCs (queries, web fetches) using callables the caller provides,
enforces the wall-clock limit, and turns the child's reply into either a
return value or an exception the existing retry loop already understands.

The child is spawned — never forked — so it starts with an empty heap: no
decrypted connection credentials, no ORM session, no encryption key. Its
environment is built from scratch (see `_child_environment`), so nothing the
API process was configured with (`BOW_*`, database URLs, proxies, cloud
credentials) is visible to generated code.
"""
from __future__ import annotations

import logging
import os
import pickle
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

from app.ai.code_execution.sandbox.config import SandboxLimits
from app.ai.code_execution.sandbox.namespace import file_to_attrs
from app.ai.code_execution.sandbox.protocol import (
    arrow_to_dataframe,
    read_message,
    write_message,
)

logger = logging.getLogger(__name__)

_BACKEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
_CHILD_MODULE = "app.ai.code_execution.sandbox.child"
_STDERR_TAIL_BYTES = 16 * 1024
_landlock_warned = False


class SandboxError(Exception):
    """Base for failures of the sandbox machinery itself (not of user code)."""


class SandboxTimeoutError(SandboxError):
    def __init__(self, seconds: int):
        super().__init__(
            f"Code execution exceeded the {seconds}s sandbox time limit and was terminated. "
            "Reduce the amount of data processed or split the work into smaller steps."
        )
        self.seconds = seconds


class SandboxCrashError(SandboxError):
    """The child died without reporting a result (OOM kill, segfault, ...)."""


class SandboxCancelled(SandboxError):
    pass


class SandboxExecutionError(Exception):
    """User code raised inside the child. `str()` is what the retry loop shows
    the coder, so it carries the original type name and message."""

    def __init__(self, exc_type: str, message: str, traceback_text: str = ""):
        self.exc_type = exc_type
        self.message = message
        self.traceback_text = traceback_text
        super().__init__(f"{exc_type}: {message}" if exc_type else message)


@dataclass
class SandboxResult:
    df: Optional[pd.DataFrame] = None
    stdout: str = ""
    pptx_bytes: Optional[bytes] = None
    applied: Dict[str, Any] = field(default_factory=dict)
    spawn_ms: float = 0.0
    total_ms: float = 0.0


@dataclass
class SandboxJob:
    mode: str  # "data" | "pptx"
    code: str
    files: List[Any] = field(default_factory=list)
    loadables: Optional[Dict] = None
    params: Optional[Dict] = None
    client_keys: List[str] = field(default_factory=list)
    http_enabled: bool = False
    load_step_enabled: bool = False
    # pptx mode
    visualizations: Optional[List[Dict]] = None
    report: Optional[Dict] = None
    images: Optional[Dict[str, bytes]] = None
    # extra read-only paths (beyond the uploaded files) the child may see
    extra_read_paths: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Environment and filesystem policy for the child
# ---------------------------------------------------------------------------

def _child_environment(scratch_dir: str) -> Dict[str, str]:
    """Build the child's environment from scratch. Nothing is inherited."""
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": scratch_dir,
        "TMPDIR": scratch_dir,
        "TMP": scratch_dir,
        "TEMP": scratch_dir,
        "MPLCONFIGDIR": os.path.join(scratch_dir, "mpl"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "UTF-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "PYTHONPATH": _BACKEND_DIR,
        "BOW_SANDBOX_CHILD": "1",
    }
    # BLAS/OpenMP thread caps are performance hints, not secrets; carry them
    # over so an operator's tuning applies to generated code too.
    for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_MAX_THREADS"):
        if k in os.environ:
            env[k] = os.environ[k]
    return env


def _fs_policy(job: SandboxJob, scratch_dir: str) -> Dict[str, List[str]]:
    """Landlock view for the child: interpreter + libraries read-only, the
    run's own files read-only, one scratch dir read-write, nothing else.

    `/proc` is deliberately NOT exposed as a hierarchy: `/proc/<pid>/environ`
    of same-uid processes is world-readable there, which would hand the
    child the parent's environment (encryption key included). Only the
    child's own `/proc/self` plus two static info files are allowed.
    """
    read_paths = [
        sys.prefix, sys.base_prefix, sys.exec_prefix,
        "/usr", "/lib", "/lib64", "/etc/ld.so.cache", "/etc/localtime",
        "/etc/ssl", "/etc/alternatives", "/sys/devices/system/cpu",
        os.path.join(_BACKEND_DIR, "app"),
        "/proc/self",
    ]
    read_files = ["/proc/cpuinfo", "/proc/meminfo", "/dev/urandom", "/dev/random"]
    for f in job.files:
        p = getattr(f, "path", None)
        if p:
            read_files.append(os.path.abspath(str(p)))
    read_paths.extend(job.extra_read_paths or [])
    return {
        "read_paths": read_paths,
        "rw_paths": [scratch_dir],
        "read_files": read_files,
        "rw_files": ["/dev/null", "/dev/zero"],
    }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class _ChildProcess:
    def __init__(self, scratch_dir: Optional[str] = None):
        scratch_dir = scratch_dir or tempfile.mkdtemp(prefix="bow-sandbox-")
        os.makedirs(os.path.join(scratch_dir, "mpl"), exist_ok=True)
        self.scratch_dir = scratch_dir
        self.spawned_at = time.monotonic()
        in_r, in_w = os.pipe()      # parent → child
        out_r, out_w = os.pipe()    # child → parent
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", _CHILD_MODULE, "--in-fd", str(in_r), "--out-fd", str(out_w)],
                cwd=scratch_dir,
                env=_child_environment(scratch_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                pass_fds=(in_r, out_w),
                close_fds=True,
                start_new_session=True,
            )
        finally:
            os.close(in_r)
            os.close(out_w)
        self.to_child = os.fdopen(in_w, "wb", buffering=0)
        self.from_child = os.fdopen(out_r, "rb", buffering=0)
        self.stderr_tail: deque = deque(maxlen=64)
        self._stderr_bytes = 0

    def drain_stderr(self) -> None:
        fd = self.proc.stderr
        if fd is None:
            return
        try:
            chunk = os.read(fd.fileno(), 4096)
        except (BlockingIOError, OSError):
            return
        if chunk:
            self._stderr_bytes += len(chunk)
            self.stderr_tail.append(chunk)

    def stderr_text(self) -> str:
        return b"".join(self.stderr_tail).decode("utf-8", "replace")[-_STDERR_TAIL_BYTES:]

    def kill(self) -> None:
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass

    def alive(self) -> bool:
        return self.proc.poll() is None

    def close(self) -> None:
        for f in (self.to_child, self.from_child, self.proc.stderr):
            try:
                if f is not None:
                    f.close()
            except Exception:
                pass
        try:
            self.proc.wait(timeout=2)
        except Exception:
            self.kill()
            try:
                self.proc.wait(timeout=2)
            except Exception:
                pass
        shutil.rmtree(self.scratch_dir, ignore_errors=True)


class _Prewarm:
    """Keep a few idle children per API process so an execution does not pay
    the interpreter + pandas import cost (~0.7s) on the critical path.

    A warm child has read nothing yet: it blocks on its job pipe with an
    empty heap and the scrubbed environment, so pre-spawning changes nothing
    about the isolation. The pool is filled lazily (first execution pays the
    cold start), topped back up after each take, and drained after
    `idle_seconds` without use so a quiet worker gives the memory back
    (an idle child is ~60 MB RSS).

    BOW_SANDBOX_PREWARM = number of warm children to keep (default 2, 0 off).
    """

    def __init__(self, idle_seconds: int = 600):
        self._lock = threading.Lock()
        self._pool: List[_ChildProcess] = []
        self._spawning = 0
        self._last_used = time.monotonic()
        self._idle_seconds = idle_seconds
        self._reaper: Optional[threading.Thread] = None
        raw = os.environ.get("BOW_SANDBOX_PREWARM", "2").strip()
        try:
            self.size = max(0, min(8, int(raw)))
        except ValueError:
            self.size = 2

    def take(self) -> Optional[_ChildProcess]:
        child = None
        with self._lock:
            self._last_used = time.monotonic()
            while self._pool:
                cand = self._pool.pop()
                if cand.alive():
                    child = cand
                    break
                cand.close()
        self._top_up()
        return child

    def _top_up(self) -> None:
        with self._lock:
            missing = self.size - len(self._pool) - self._spawning
            if missing <= 0:
                return
            self._spawning += missing
        for _ in range(missing):
            threading.Thread(target=self._spawn_one, name="bow_sandbox_prewarm", daemon=True).start()

    def _spawn_one(self) -> None:
        fresh: Optional[_ChildProcess] = None
        try:
            fresh = _ChildProcess()
        except Exception:  # pragma: no cover - defensive
            logger.debug("code sandbox: prewarm spawn failed", exc_info=True)
        with self._lock:
            self._spawning -= 1
            if fresh is not None and len(self._pool) < self.size:
                self._pool.append(fresh)
                fresh = None
        if fresh is not None:
            fresh.kill()
            fresh.close()
        self._ensure_reaper()

    def _ensure_reaper(self) -> None:
        with self._lock:
            if self._reaper is not None and self._reaper.is_alive():
                return
            self._reaper = threading.Thread(target=self._reap_loop, name="bow_sandbox_reaper", daemon=True)
            self._reaper.start()

    def _reap_loop(self) -> None:
        while True:
            time.sleep(30)
            with self._lock:
                if not self._pool:
                    return
                if time.monotonic() - self._last_used < self._idle_seconds:
                    continue
                drained, self._pool = self._pool, []
            for child in drained:
                child.kill()
                child.close()
            return


_prewarm = _Prewarm()


def _read_with_deadline(child: _ChildProcess, deadline: float, cancel_event: Optional[threading.Event], limits: SandboxLimits):
    """Block for the next child message, servicing stderr, the deadline and
    cooperative cancellation while waiting."""
    out_fd = child.from_child.fileno()
    err_fd = child.proc.stderr.fileno() if child.proc.stderr else None
    if err_fd is not None:
        os.set_blocking(err_fd, False)
    while True:
        if cancel_event is not None and cancel_event.is_set():
            child.kill()
            raise SandboxCancelled("code execution cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            child.kill()
            raise SandboxTimeoutError(limits.timeout_seconds)
        fds = [out_fd] + ([err_fd] if err_fd is not None else [])
        ready, _, _ = select.select(fds, [], [], min(0.25, remaining))
        if err_fd is not None and err_fd in ready:
            child.drain_stderr()
        if out_fd in ready:
            return read_message(child.from_child)
        if child.proc.poll() is not None and out_fd not in ready:
            # Child exited; one last non-blocking check for a final frame.
            r, _, _ = select.select([out_fd], [], [], 0)
            if r:
                return read_message(child.from_child)
            raise EOFError("sandbox pipe closed")


def _raise_for_child_death(child: _ChildProcess, limits: SandboxLimits) -> None:
    code = child.proc.poll()
    tail = child.stderr_text().strip()
    if code is not None and code < 0 and -code == signal.SIGKILL:
        raise SandboxCrashError(
            f"Code execution was killed (exit signal SIGKILL). This usually means it exceeded "
            f"the {limits.memory_mb} MB sandbox memory limit. Process less data at once."
            + (f"\n{tail}" if tail else "")
        )
    raise SandboxCrashError(
        f"Code execution process ended unexpectedly (exit code {code})."
        + (f"\n{tail}" if tail else "")
    )


def run_job(
    job: SandboxJob,
    *,
    execute_query: Optional[Callable[[str, list, dict], Any]] = None,
    http_get: Optional[Callable[..., Any]] = None,
    http_batch_get: Optional[Callable[..., Any]] = None,
    limits: Optional[SandboxLimits] = None,
    cancel_event: Optional[threading.Event] = None,
    log: Optional[logging.Logger] = None,
) -> SandboxResult:
    """Run one job in a fresh sandboxed interpreter. Blocking; call from a
    worker thread (the existing code-exec pool).

    `execute_query(client_key, args, kwargs)` is invoked on the parent side
    for every query the child issues — pass a closure over the wrapped
    clients so capture/timeouts/quotas behave exactly as before.
    """
    global _landlock_warned
    limits = limits or SandboxLimits.from_env()
    log = log or logger
    t0 = time.monotonic()
    rpc_exceptions: Dict[int, BaseException] = {}
    result = SandboxResult()
    child: Optional[_ChildProcess] = None
    try:
        child = _prewarm.take() or _ChildProcess()
        scratch_dir = child.scratch_dir
        payload = {
            "mode": job.mode,
            "code": job.code,
            "files": [file_to_attrs(f) for f in (job.files or [])],
            "loadables": job.loadables,
            "params": dict(job.params or {}),
            "client_keys": list(job.client_keys or []),
            "http_enabled": bool(job.http_enabled and http_get is not None),
            "load_step_enabled": bool(job.load_step_enabled),
            "visualizations": job.visualizations,
            "report": job.report,
            "images": job.images,
            "limits": {**limits.as_dict()},
            "fs": _fs_policy(job, scratch_dir),
            "scratch_dir": scratch_dir,
        }
        write_message(child.to_child, {"t": "job"}, pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL))
        deadline = time.monotonic() + limits.timeout_seconds

        while True:
            try:
                header, body = _read_with_deadline(child, deadline, cancel_event, limits)
            except EOFError:
                child.drain_stderr()
                _raise_for_child_death(child, limits)
                raise  # unreachable

            kind = header.get("t")
            if kind == "ready":
                result.applied = header.get("applied") or {}
                result.spawn_ms = round((time.monotonic() - t0) * 1000.0, 1)
                ll = (result.applied.get("landlock") or {})
                log.info(
                    "code sandbox: child pid=%s mode=%s landlock=%s tcp_blocked=%s rlimits=%s spawn_ms=%s",
                    result.applied.get("pid"), job.mode,
                    "applied(abi=%s)" % ll.get("abi") if ll.get("applied") else "unavailable",
                    ll.get("net_blocked"), result.applied.get("rlimits"), result.spawn_ms,
                )
                if not ll.get("applied") and not _landlock_warned:
                    _landlock_warned = True
                    log.warning(
                        "code sandbox: Landlock not available (%s). Generated code still runs in a "
                        "separate process with a scrubbed environment, rlimits and a kill timeout, "
                        "but filesystem/TCP confinement is off. A kernel >= 5.13 with the Landlock "
                        "LSM enabled adds it; set BOW_SANDBOX_REQUIRE_LANDLOCK=1 to refuse to run without it.",
                        ll.get("reason"),
                    )
                continue

            if kind == "rpc":
                rid = header.get("id")
                method = header.get("method")
                try:
                    if method == "execute_query":
                        if execute_query is None:
                            raise RuntimeError("no data source clients are available in this run")
                        value = execute_query(str(header.get("client")), list(header.get("args") or []), dict(header.get("kwargs") or {}))
                    elif method == "http_get":
                        if http_get is None:
                            raise RuntimeError("web fetch is not enabled for this organization")
                        kw = {}
                        if header.get("timeout") is not None:
                            kw["timeout"] = header["timeout"]
                        value = http_get(header.get("url"), **kw)
                    elif method == "http_batch_get":
                        if http_batch_get is None:
                            raise RuntimeError("web fetch is not enabled for this organization")
                        kw = {}
                        for k in ("concurrency", "timeout"):
                            if header.get(k) is not None:
                                kw[k] = header[k]
                        value = http_batch_get(list(header.get("urls") or []), **kw)
                    else:
                        raise RuntimeError(f"unknown sandbox rpc {method!r}")
                except BaseException as e:  # noqa: BLE001 - forwarded to the child as its exception
                    rpc_exceptions[int(rid)] = e
                    write_message(child.to_child, {
                        "t": "rpc_result", "id": rid, "ok": False,
                        "exc_type": type(e).__name__, "message": str(e),
                    })
                    continue
                try:
                    blob = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
                except Exception as e:
                    rpc_exceptions[int(rid)] = e
                    write_message(child.to_child, {
                        "t": "rpc_result", "id": rid, "ok": False,
                        "exc_type": "TypeError",
                        "message": f"query result of type {type(value).__name__} cannot be passed into the sandbox: {e}",
                    })
                    continue
                write_message(child.to_child, {"t": "rpc_result", "id": rid, "ok": True}, blob)
                continue

            if kind == "result":
                result.stdout = str(header.get("stdout") or "")
                rk = header.get("kind")
                if rk == "dataframe":
                    meta = header.get("meta") or {}
                    result.df = arrow_to_dataframe(body, meta)
                    if meta.get("stringified"):
                        log.info("code sandbox: columns converted to text for transport: %s", meta["stringified"])
                elif rk == "pptx":
                    result.pptx_bytes = body
                else:
                    result.df = None
                result.total_ms = round((time.monotonic() - t0) * 1000.0, 1)
                log.info(
                    "code sandbox: done pid=%s rows=%s total_ms=%s",
                    result.applied.get("pid"),
                    (len(result.df) if result.df is not None else None),
                    result.total_ms,
                )
                return result

            if kind == "error":
                result.stdout = str(header.get("stdout") or "")
                rpc_id = header.get("rpc_id")
                if rpc_id is not None and int(rpc_id) in rpc_exceptions:
                    # The failure originated in the parent (a wrapper raised
                    # UnsafeSQLError / QueryTimeoutError / a quota error) and
                    # propagated unchanged: surface the real object so the
                    # retry loop keeps its type-based decisions.
                    raise rpc_exceptions[int(rpc_id)]
                raise SandboxExecutionError(
                    str(header.get("exc_type") or ""),
                    str(header.get("message") or ""),
                    str(header.get("traceback") or ""),
                )

            raise SandboxError(f"unexpected sandbox message {kind!r}")
    finally:
        if child is not None:
            child.drain_stderr()
            if child.proc.poll() is None:
                child.kill()
            child.close()
