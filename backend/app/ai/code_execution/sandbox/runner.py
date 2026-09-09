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

import json
import logging
import os
import pickle
import select
import shutil
import signal
import socket
import struct
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
    _FRAME,
    MAX_FRAME_BYTES,
    MAX_HEADER_BYTES,
    ProtocolError,
    arrow_to_dataframe,
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
        "/usr", "/lib", "/lib64",
        "/etc/ssl", "/etc/alternatives", "/sys/devices/system/cpu",
        os.path.join(_BACKEND_DIR, "app"),
        "/proc/self",
    ]
    # Plain files go here: a directory-style rule on a file is EINVAL.
    read_files = [
        "/etc/ld.so.cache", "/etc/localtime", "/etc/resolv.conf",
        "/proc/cpuinfo", "/proc/meminfo", "/dev/urandom", "/dev/random",
    ]
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
    """One sandboxed execution process plus the runner's ends of its pipes.

    Created either by the fork server (`_Zygote.fork`, the default: a few
    milliseconds) or by spawning a fresh interpreter (`spawn`, the fallback:
    ~0.7s of imports). The isolation properties are identical — see zygote.py.
    """

    def __init__(self, pid: int, to_child: int, from_child: int, stderr_fd: int,
                 scratch_dir: str, *, zygote: Optional["_Zygote"] = None,
                 proc: Optional[subprocess.Popen] = None):
        self.pid = pid
        self.to_child = os.fdopen(to_child, "wb", buffering=0)
        self.from_child = os.fdopen(from_child, "rb", buffering=0)
        os.set_blocking(self.from_child.fileno(), False)
        self.stderr_fd = stderr_fd
        os.set_blocking(self.stderr_fd, False)
        self.scratch_dir = scratch_dir
        self._zygote = zygote
        self._proc = proc
        self.stderr_tail: deque = deque(maxlen=64)
        self._released = False

    # -- construction ------------------------------------------------------

    @staticmethod
    def _scratch() -> str:
        scratch_dir = tempfile.mkdtemp(prefix="bow-sandbox-")
        os.makedirs(os.path.join(scratch_dir, "mpl"), exist_ok=True)
        return scratch_dir

    @classmethod
    def spawn(cls) -> "_ChildProcess":
        scratch_dir = cls._scratch()
        in_r, in_w = os.pipe()      # parent → child
        out_r, out_w = os.pipe()    # child → parent
        try:
            proc = subprocess.Popen(
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
        assert proc.stderr is not None
        stderr_fd = os.dup(proc.stderr.fileno())
        proc.stderr.close()
        return cls(proc.pid, in_w, out_r, stderr_fd, scratch_dir, proc=proc)

    # -- io ----------------------------------------------------------------

    def drain_stderr(self) -> None:
        try:
            chunk = os.read(self.stderr_fd, 4096)
        except (BlockingIOError, OSError):
            return
        if chunk:
            self.stderr_tail.append(chunk)

    def stderr_text(self) -> str:
        return b"".join(self.stderr_tail).decode("utf-8", "replace")[-_STDERR_TAIL_BYTES:]

    # -- lifecycle ---------------------------------------------------------

    def kill(self) -> None:
        """SIGKILL the child's whole session. Safe against pid reuse: a
        forked child stays a zombie (pid reserved) until `close` reaps it,
        and a spawned one is our own child."""
        if self._released:
            return
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            try:
                os.kill(self.pid, signal.SIGKILL)
            except Exception:
                pass

    def close(self) -> None:
        for f in (self.to_child, self.from_child):
            try:
                f.close()
            except Exception:
                pass
        try:
            os.close(self.stderr_fd)
        except Exception:
            pass
        if not self._released:
            self._released = True
            if self._proc is not None:
                try:
                    self._proc.wait(timeout=2)
                except Exception:
                    self.kill()
                    try:
                        self._proc.wait(timeout=2)
                    except Exception:
                        pass
            elif self._zygote is not None:
                self._zygote.reap(self.pid)
        shutil.rmtree(self.scratch_dir, ignore_errors=True)


class _Zygote:
    """Runner-side handle on the fork server (see zygote.py).

    Spawned lazily on first use, once per API process, with the scrubbed
    environment. `fork` hands it three pipe ends over a Unix socket and gets
    a child pid back; `reap` releases the pid once the runner is done with
    it. A dead zygote is replaced on the next call; if it cannot be started
    at all the runner falls back to spawning a full interpreter per job.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._sock: Optional[socket.socket] = None
        self._scratch: Optional[str] = None
        self.enabled = os.environ.get("BOW_SANDBOX_ZYGOTE", "1").strip() != "0"

    def _ensure(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        self._teardown()
        try:
            ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            scratch = _ChildProcess._scratch()
            proc = subprocess.Popen(
                [sys.executable, "-m", _ZYGOTE_MODULE, "--sock-fd", str(theirs.fileno())],
                cwd=scratch,
                env=_child_environment(scratch),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                pass_fds=(theirs.fileno(),),
                close_fds=True,
                start_new_session=True,
            )
            theirs.close()
            self._proc, self._sock, self._scratch = proc, ours, scratch
            logger.info("code sandbox: fork server started pid=%s", proc.pid)
            return True
        except Exception:
            logger.warning("code sandbox: fork server unavailable, spawning per run", exc_info=True)
            self._teardown()
            return False

    def _teardown(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=2)
            except Exception:
                pass
        if self._scratch:
            shutil.rmtree(self._scratch, ignore_errors=True)
        self._proc, self._sock, self._scratch = None, None, None

    def _call(self, kind: int, pid: int, fds: list) -> int:
        assert self._sock is not None
        socket.send_fds(self._sock, [_ZMSG.pack(kind, pid)], fds)
        buf = b""
        while len(buf) < _ZMSG.size:
            chunk = self._sock.recv(_ZMSG.size - len(buf))
            if not chunk:
                raise EOFError("fork server closed")
            buf += chunk
        _kind, result = _ZMSG.unpack(buf)
        return result

    def fork(self) -> Optional[_ChildProcess]:
        """Return a forked child, or None when the fork server is off/broken."""
        if not self.enabled:
            return None
        with self._lock:
            if not self._ensure():
                return None
            scratch_dir = _ChildProcess._scratch()
            in_r, in_w = os.pipe()
            out_r, out_w = os.pipe()
            err_r, err_w = os.pipe()
            try:
                pid = self._call(_ZKIND_FORK, 0, [in_r, out_w, err_w])
            except Exception:
                logger.warning("code sandbox: fork request failed, restarting fork server", exc_info=True)
                self._teardown()
                for fd in (in_r, in_w, out_r, out_w, err_r, err_w):
                    os.close(fd)
                shutil.rmtree(scratch_dir, ignore_errors=True)
                return None
            finally:
                pass
            for fd in (in_r, out_w, err_w):
                os.close(fd)
            if pid <= 0:
                for fd in (in_w, out_r, err_r):
                    os.close(fd)
                shutil.rmtree(scratch_dir, ignore_errors=True)
                return None
            return _ChildProcess(pid, in_w, out_r, err_r, scratch_dir, zygote=self)

    def reap(self, pid: int) -> None:
        with self._lock:
            if self._sock is None:
                return
            try:
                self._call(_ZKIND_REAP, pid, [])
            except Exception:
                logger.debug("code sandbox: reap failed; restarting fork server", exc_info=True)
                self._teardown()


_ZYGOTE_MODULE = "app.ai.code_execution.sandbox.zygote"
_ZMSG = struct.Struct("<ii")
_ZKIND_FORK = 1
_ZKIND_REAP = 2
_zygote = _Zygote()


def _acquire_child() -> _ChildProcess:
    return _zygote.fork() or _ChildProcess.spawn()


def _wait_for_child(child: _ChildProcess, deadline: float, cancel_event: Optional[threading.Event],
                    limits: SandboxLimits, fds: List[int], timeout: float = 0.25) -> List[int]:
    """One supervision tick: enforce cancel/deadline, drain stderr, and
    return the fds in `fds` that are readable (possibly none)."""
    if cancel_event is not None and cancel_event.is_set():
        child.kill()
        raise SandboxCancelled("code execution cancelled")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        child.kill()
        raise SandboxTimeoutError(limits.timeout_seconds)
    watch = list(fds) + [child.stderr_fd]
    ready, _, _ = select.select(watch, [], [], min(timeout, remaining))
    if child.stderr_fd in ready:
        child.drain_stderr()
    return [fd for fd in ready if fd != child.stderr_fd]


def _read_exact_with_deadline(child: _ChildProcess, n: int, deadline: float,
                              cancel_event: Optional[threading.Event], limits: SandboxLimits) -> bytes:
    """Read exactly `n` bytes from the child's result pipe, never blocking
    past a supervision tick: a child that stalls mid-frame still hits the
    wall clock and the cancel event."""
    fd = child.from_child.fileno()
    chunks: List[bytes] = []
    remaining = n
    while remaining > 0:
        if fd not in _wait_for_child(child, deadline, cancel_event, limits, [fd]):
            continue
        try:
            chunk = os.read(fd, min(remaining, 1 << 20))
        except BlockingIOError:
            continue
        if not chunk:
            raise EOFError("sandbox pipe closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_with_deadline(child: _ChildProcess, deadline: float, cancel_event: Optional[threading.Event], limits: SandboxLimits):
    """Read the next child message under supervision (deadline, cancel,
    stderr draining). A dead child shows up as EOF on its result pipe (the
    kernel closes it)."""
    raw = _read_exact_with_deadline(child, _FRAME.size, deadline, cancel_event, limits)
    hlen, plen = _FRAME.unpack(raw)
    if hlen > MAX_HEADER_BYTES or plen > MAX_FRAME_BYTES:
        raise ProtocolError(f"frame too large: header={hlen} payload={plen}")
    header = json.loads(_read_exact_with_deadline(child, hlen, deadline, cancel_event, limits).decode("utf-8"))
    if not isinstance(header, dict):
        raise ProtocolError("frame header is not an object")
    payload = _read_exact_with_deadline(child, plen, deadline, cancel_event, limits) if plen else b""
    return header, payload


def _call_supervised(fn: Callable[[], Any], child: _ChildProcess, deadline: float,
                     cancel_event: Optional[threading.Event], limits: SandboxLimits) -> Any:
    """Run a trusted-side RPC handler (a data-source query, a web fetch) on
    a worker thread while the runner keeps enforcing the wall clock and the
    cancel event. On either, the child is killed and the handler is
    abandoned — the query wrappers already bound their own calls and cancel
    orphaned queries at the source, so nothing here can outlive them."""
    holder: Dict[str, Any] = {}

    def _target():
        try:
            holder["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - re-raised on the runner thread
            holder["exc"] = e

    t = threading.Thread(target=_target, name="bow_sandbox_rpc", daemon=True)
    t.start()
    while t.is_alive():
        _wait_for_child(child, deadline, cancel_event, limits, [])
        t.join(0.0)
    if "exc" in holder:
        raise holder["exc"]
    return holder.get("value")


def _raise_for_child_death(child: _ChildProcess, limits: SandboxLimits) -> None:
    tail = child.stderr_text().strip()
    raise SandboxCrashError(
        "Code execution process ended unexpectedly before returning a result. "
        f"This usually means it exceeded the {limits.memory_mb} MB sandbox memory limit "
        "or was killed; process less data at once."
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
        child = _acquire_child()
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
                        _client = str(header.get("client"))
                        _args = list(header.get("args") or [])
                        _kwargs = dict(header.get("kwargs") or {})
                        handler = lambda: execute_query(_client, _args, _kwargs)  # noqa: E731
                    elif method == "http_get":
                        if http_get is None:
                            raise RuntimeError("web fetch is not enabled for this organization")
                        kw = {}
                        if header.get("timeout") is not None:
                            kw["timeout"] = header["timeout"]
                        _url = header.get("url")
                        handler = lambda: http_get(_url, **kw)  # noqa: E731
                    elif method == "http_batch_get":
                        if http_batch_get is None:
                            raise RuntimeError("web fetch is not enabled for this organization")
                        kw = {}
                        for k in ("concurrency", "timeout"):
                            if header.get(k) is not None:
                                kw[k] = header[k]
                        _urls = list(header.get("urls") or [])
                        handler = lambda: http_batch_get(_urls, **kw)  # noqa: E731
                    else:
                        raise RuntimeError(f"unknown sandbox rpc {method!r}")
                    value = _call_supervised(handler, child, deadline, cancel_event, limits)
                except (SandboxTimeoutError, SandboxCancelled):
                    raise
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
            child.kill()
            child.close()
