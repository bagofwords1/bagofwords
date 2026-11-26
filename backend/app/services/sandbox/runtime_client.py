"""Client for ``sandboxd``, the daemon inside every sandbox.

It runs processes (gRPC ``process.v1.ProcessService``) and moves files (REST
``/v1/files``). All it needs is an :class:`~.drivers.base.Endpoint`: an
address, the one CA to trust, the headers that route to the sandbox, and the
sandbox's token. It has no idea what kind of backend the sandbox runs on, and
it is the same client for every backend type.

Streamed runs have no time limit of their own in ``sandboxd``, so this client
enforces the run timeout and cancellation: it signals the process group
(``SIGTERM``, then ``SIGKILL`` after a grace period) and reports how the run
ended.

One client is one gRPC channel and one HTTP connection pool, bound to the
event loop it was created on. Create it per operation and close it:

    async with SandboxRuntimeClient(endpoint) as runtime:
        result = await runtime.run(["python", "main.py"], timeout=600)
"""
from __future__ import annotations

import asyncio
import inspect
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Mapping, Optional, Sequence, Union
from urllib.parse import quote

from app.services.sandbox.drivers.base import Endpoint

# How a run ended.
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"        # the process exited with a non-zero code
RUN_TIMEOUT = "timeout"      # stopped by the run timeout
RUN_CANCELLED = "cancelled"  # stopped because the caller asked

DEFAULT_OUTPUT_TAIL_BYTES = 1024 * 1024
TERMINATE_GRACE_SECONDS = 5.0
KILL_WAIT_SECONDS = 10.0

OutputCallback = Callable[[str, bytes], Union[None, Awaitable[None]]]


class SandboxRuntimeError(Exception):
    """The sandbox could not be reached or refused the request.

    ``code``: ``unreachable`` | ``unauthenticated`` | ``not_found`` |
    ``invalid`` | ``too_large`` | ``runtime_error``.
    """

    def __init__(self, message: str, *, code: str = "runtime_error", status: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


class _Tail:
    """Keeps the last ``limit`` bytes of a stream and counts all of them."""

    def __init__(self, limit: int):
        self.limit = limit
        self.total = 0
        self._chunks: List[bytes] = []
        self._size = 0

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        self._chunks.append(chunk)
        self._size += len(chunk)
        while self._size - len(self._chunks[0]) >= self.limit and len(self._chunks) > 1:
            self._size -= len(self._chunks.pop(0))

    def value(self) -> bytes:
        return b"".join(self._chunks)[-self.limit:] if self.limit else b""

    @property
    def truncated(self) -> bool:
        return self.total > self.limit


@dataclass
class RunResult:
    status: str
    exit_code: Optional[int]
    # The last bytes of each stream (see ``output_tail_bytes``); the callback saw all of it.
    stdout: bytes = b""
    stderr: bytes = b""
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    duration_seconds: float = 0.0
    process_id: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.status == RUN_SUCCEEDED


@dataclass
class FileEntry:
    # Path relative to the listed directory ("a/b/c.txt" in a recursive listing).
    name: str
    size: int
    type: str  # "file" | "directory"
    modified_at: Optional[str] = None
    mode: Optional[str] = None

    @property
    def is_file(self) -> bool:
        return self.type == "file"


def _unreachable(exc: Exception) -> str:
    reason = str(exc).strip().splitlines()[0][:200] if str(exc).strip() else ""
    return f"The sandbox could not be reached ({type(exc).__name__}{': ' + reason if reason else ''})"


def _quote_path(path: str) -> str:
    return quote(path.strip("/"), safe="/")


class SandboxRuntimeClient:
    def __init__(self, endpoint: Endpoint, *, connect_timeout: float = 10.0, request_timeout: float = 300.0):
        self.endpoint = endpoint
        self._connect_timeout = connect_timeout
        self._request_timeout = request_timeout
        self._channel = None
        self._stub = None
        self._http = None

    async def __aenter__(self) -> "SandboxRuntimeClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None

    # ── connections (opened on first use) ──

    def _auth_headers(self) -> Dict[str, str]:
        headers = dict(self.endpoint.headers)
        if self.endpoint.token:
            headers["Authorization"] = f"Bearer {self.endpoint.token}"
        return headers

    def _metadata(self) -> tuple:
        return tuple((k.lower(), v) for k, v in self._auth_headers().items())

    def _http_client(self):
        if self._http is None:
            import httpx

            verify: Any = True
            if self.endpoint.secure:
                if not self.endpoint.ca_cert:
                    raise SandboxRuntimeError("The endpoint has no CA certificate to verify it against", code="invalid")
                # Trust the endpoint's CA and nothing else: no system roots.
                verify = ssl.create_default_context(cadata=self.endpoint.ca_cert)
                if self.endpoint.tls_server_name:
                    verify.check_hostname = True
            self._http = httpx.AsyncClient(
                base_url=self.endpoint.base_url, verify=verify, headers=self._auth_headers(),
                timeout=httpx.Timeout(self._request_timeout, connect=self._connect_timeout),
                trust_env=False,  # never through a proxy from the server's environment
            )
        return self._http

    def _process_stub(self):
        if self._stub is None:
            import grpc

            from app.services.sandbox.sandboxd_proto import process_pb2_grpc

            options = [
                ("grpc.keepalive_time_ms", 30_000),
                ("grpc.keepalive_timeout_ms", 10_000),
                ("grpc.max_receive_message_length", 64 << 20),
                ("grpc.enable_http_proxy", 0),
            ]
            if self.endpoint.secure:
                if not self.endpoint.ca_cert:
                    raise SandboxRuntimeError("The endpoint has no CA certificate to verify it against", code="invalid")
                if self.endpoint.tls_server_name:
                    options.append(("grpc.ssl_target_name_override", self.endpoint.tls_server_name))
                credentials = grpc.ssl_channel_credentials(root_certificates=self.endpoint.ca_cert.encode("ascii"))
                self._channel = grpc.aio.secure_channel(self.endpoint.grpc_target, credentials, options=options)
            else:
                self._channel = grpc.aio.insecure_channel(self.endpoint.grpc_target, options=options)
            self._stub = process_pb2_grpc.ProcessServiceStub(self._channel)
        return self._stub

    @staticmethod
    def _grpc_error(exc) -> SandboxRuntimeError:
        import grpc

        code = exc.code() if hasattr(exc, "code") else None
        mapped = {
            grpc.StatusCode.UNAUTHENTICATED: "unauthenticated",
            grpc.StatusCode.PERMISSION_DENIED: "unauthenticated",
            grpc.StatusCode.NOT_FOUND: "not_found",
            grpc.StatusCode.INVALID_ARGUMENT: "invalid",
            grpc.StatusCode.UNAVAILABLE: "unreachable",
            grpc.StatusCode.DEADLINE_EXCEEDED: "unreachable",
        }.get(code, "runtime_error")
        detail = exc.details() if hasattr(exc, "details") else str(exc)
        return SandboxRuntimeError(detail or "The sandbox refused the request", code=mapped)

    @staticmethod
    def _http_error(response) -> SandboxRuntimeError:
        code = {401: "unauthenticated", 403: "unauthenticated", 404: "not_found", 413: "too_large",
                502: "unreachable", 503: "unreachable", 504: "unreachable"}.get(response.status_code, "runtime_error")
        try:
            message = response.json().get("message") or response.text
        except Exception:
            message = response.text
        return SandboxRuntimeError((message or f"HTTP {response.status_code}")[:500], code=code, status=response.status_code)

    async def _request(self, method: str, url: str, **kwargs):
        import httpx

        try:
            return await self._http_client().request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise SandboxRuntimeError(_unreachable(exc), code="unreachable") from exc

    # ── health ──

    async def health(self, timeout: float = 5.0) -> bool:
        """True when ``sandboxd`` answers. Never raises."""
        try:
            response = await self._request("GET", "/v1/health", timeout=timeout)
        except SandboxRuntimeError:
            return False
        return response.status_code == 200

    async def wait_healthy(self, deadline_seconds: float = 30.0, interval: float = 0.1) -> None:
        """Wait until ``sandboxd`` answers; raises ``unreachable`` at the deadline."""
        started = time.monotonic()
        while True:
            if await self.health(timeout=min(2.0, max(0.2, deadline_seconds))):
                return
            if time.monotonic() - started >= deadline_seconds:
                raise SandboxRuntimeError("The sandbox did not become healthy in time", code="unreachable")
            await asyncio.sleep(interval)

    # ── processes ──

    def _config(self, command: Sequence[str], env: Optional[Mapping[str, str]], cwd: Optional[str]):
        from app.services.sandbox.sandboxd_proto import process_pb2 as pb

        if not command:
            raise SandboxRuntimeError("The command is empty", code="invalid")
        config = pb.ProcessConfig(command=[str(part) for part in command], env_vars=dict(env or {}))
        if cwd:
            config.cwd = cwd
        return config

    async def send_signal(self, process_id: int, signal_name: str = "SIGTERM") -> None:
        """Signal a running process group. Unknown process ids are ignored (it already exited)."""
        import grpc

        from app.services.sandbox.sandboxd_proto import process_pb2 as pb

        number = {"SIGINT": pb.SIGNAL_SIGINT, "SIGKILL": pb.SIGNAL_SIGKILL}.get(signal_name, pb.SIGNAL_SIGTERM)
        try:
            await self._process_stub().SendSignal(
                pb.SendSignalRequest(process_id=process_id, signal=number), metadata=self._metadata(), timeout=10,
            )
        except grpc.aio.AioRpcError as exc:
            if exc.code() != grpc.StatusCode.NOT_FOUND:
                raise self._grpc_error(exc) from exc

    async def run(
        self,
        command: Sequence[str],
        *,
        env: Optional[Mapping[str, str]] = None,
        cwd: Optional[str] = None,
        timeout: Optional[float] = None,
        on_output: Optional[OutputCallback] = None,
        cancel: Optional[asyncio.Event] = None,
        on_started: Optional[Callable[[int], Union[None, Awaitable[None]]]] = None,
        output_tail_bytes: int = DEFAULT_OUTPUT_TAIL_BYTES,
    ) -> RunResult:
        """Run a command with streamed output.

        ``on_output(kind, chunk)`` is called for every chunk (``kind`` is
        ``"stdout"`` or ``"stderr"``) as it arrives. The run is stopped at
        ``timeout`` seconds, or when ``cancel`` is set; the result says which.
        """
        import grpc

        from app.services.sandbox.sandboxd_proto import process_pb2 as pb

        request = pb.StartRequest(config=self._config(command, env, cwd))
        started = time.monotonic()
        tails = {"stdout": _Tail(output_tail_bytes), "stderr": _Tail(output_tail_bytes)}
        state: Dict[str, Any] = {"pid": None, "exit_code": None}
        call = self._process_stub().Start(request, metadata=self._metadata())

        async def consume() -> None:
            async for event in call:
                kind = event.WhichOneof("event")
                if kind == "init":
                    state["pid"] = event.init.process_id
                    if on_started is not None:
                        outcome = on_started(state["pid"])
                        if inspect.isawaitable(outcome):
                            await outcome
                elif kind in ("stdout", "stderr"):
                    chunk = getattr(event, kind)
                    tails[kind].add(chunk)
                    if on_output is not None:
                        outcome = on_output(kind, chunk)
                        if inspect.isawaitable(outcome):
                            await outcome
                elif kind == "exit":
                    state["exit_code"] = event.exit.exit_code

        consumer = asyncio.ensure_future(consume())
        cancel_waiter = asyncio.ensure_future(cancel.wait()) if cancel is not None else None
        stopped_by: Optional[str] = None
        try:
            waiting = {consumer} | ({cancel_waiter} if cancel_waiter else set())
            done, _ = await asyncio.wait(waiting, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if consumer not in done:
                stopped_by = RUN_CANCELLED if (cancel_waiter is not None and cancel_waiter in done) else RUN_TIMEOUT
                await self._terminate(state, consumer)
            await consumer  # re-raises a stream error
        except grpc.aio.AioRpcError as exc:
            if stopped_by is None or exc.code() != grpc.StatusCode.CANCELLED:
                raise self._grpc_error(exc) from exc
        except asyncio.CancelledError:
            # The caller's own task was cancelled: do not leave the process running.
            call.cancel()
            consumer.cancel()
            raise
        finally:
            if cancel_waiter is not None:
                cancel_waiter.cancel()

        exit_code = state["exit_code"]
        if stopped_by is not None:
            status = stopped_by
        elif exit_code == 0:
            status = RUN_SUCCEEDED
        else:
            status = RUN_FAILED
        return RunResult(
            status=status, exit_code=exit_code,
            stdout=tails["stdout"].value(), stderr=tails["stderr"].value(),
            stdout_bytes=tails["stdout"].total, stderr_bytes=tails["stderr"].total,
            stdout_truncated=tails["stdout"].truncated, stderr_truncated=tails["stderr"].truncated,
            duration_seconds=time.monotonic() - started, process_id=state["pid"],
        )

    async def _terminate(self, state: Dict[str, Any], consumer: "asyncio.Future") -> None:
        """SIGTERM, a grace period, SIGKILL, then give up on the stream."""
        for signal_name, wait in (("SIGTERM", TERMINATE_GRACE_SECONDS), ("SIGKILL", KILL_WAIT_SECONDS)):
            if state["pid"] is not None:
                try:
                    await self.send_signal(state["pid"], signal_name)
                except SandboxRuntimeError:
                    break
            done, _ = await asyncio.wait({consumer}, timeout=wait)
            if done:
                return
        consumer.cancel()
        try:
            await consumer
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - the run is over either way
            pass

    async def execute(
        self,
        command: Sequence[str],
        *,
        env: Optional[Mapping[str, str]] = None,
        cwd: Optional[str] = None,
        time_limit: Optional[float] = None,
        stdout_limit_bytes: Optional[int] = None,
        stderr_limit_bytes: Optional[int] = None,
    ) -> RunResult:
        """Run a short command to completion (no streaming), with the daemon's own limits."""
        import grpc
        from google.protobuf import duration_pb2

        from app.services.sandbox.sandboxd_proto import process_pb2 as pb

        request = pb.ExecuteWithLimitsRequest(config=self._config(command, env, cwd))
        if stdout_limit_bytes:
            request.stdout_limit_bytes = stdout_limit_bytes
        if stderr_limit_bytes:
            request.stderr_limit_bytes = stderr_limit_bytes
        if time_limit:
            duration = duration_pb2.Duration()
            duration.FromNanoseconds(int(time_limit * 1e9))
            request.time_limit.CopyFrom(duration)
        started = time.monotonic()
        try:
            response = await self._process_stub().ExecuteWithLimits(
                request, metadata=self._metadata(), timeout=(time_limit + 30) if time_limit else None,
            )
        except grpc.aio.AioRpcError as exc:
            raise self._grpc_error(exc) from exc
        timed_out = response.termination_reason == pb.EXECUTE_TERMINATION_REASON_TIME_LIMIT_EXCEEDED
        status = RUN_TIMEOUT if timed_out else (RUN_SUCCEEDED if response.exit_code == 0 else RUN_FAILED)
        return RunResult(
            status=status, exit_code=response.exit_code, stdout=response.stdout, stderr=response.stderr,
            stdout_bytes=len(response.stdout), stderr_bytes=len(response.stderr),
            stdout_truncated=response.stdout_truncated, stderr_truncated=response.stderr_truncated,
            duration_seconds=time.monotonic() - started,
        )

    # ── files ──

    async def write_file(self, path: str, data: Union[bytes, AsyncIterator[bytes]], *, mode: Optional[str] = None) -> None:
        """Write a file under the sandbox root; missing parent directories are created."""
        params = {"mode": mode} if mode else None
        response = await self._request(
            "PUT", f"/v1/files/{_quote_path(path)}", content=data, params=params,
            headers={"Content-Type": "application/octet-stream"},
        )
        if response.status_code not in (200, 201, 204):
            raise self._http_error(response)

    async def make_dir(self, path: str) -> None:
        response = await self._request(
            "PUT", f"/v1/files/{_quote_path(path)}", params={"type": "directory", "parents": "true"},
        )
        if response.status_code not in (200, 201, 204):
            raise self._http_error(response)

    async def read_file(self, path: str, *, max_bytes: Optional[int] = None) -> bytes:
        """Read a whole file. With ``max_bytes``, a larger file raises ``too_large``."""
        chunks: List[bytes] = []
        size = 0
        async for chunk in self.iter_file(path):
            size += len(chunk)
            if max_bytes is not None and size > max_bytes:
                raise SandboxRuntimeError(f"The file is larger than {max_bytes} bytes", code="too_large")
            chunks.append(chunk)
        return b"".join(chunks)

    async def iter_file(self, path: str, chunk_size: int = 1 << 20) -> AsyncIterator[bytes]:
        """Stream a file's bytes without holding it in memory."""
        import httpx

        try:
            async with self._http_client().stream("GET", f"/v1/files/{_quote_path(path)}") as response:
                if response.status_code != 200:
                    await response.aread()
                    raise self._http_error(response)
                async for chunk in response.aiter_bytes(chunk_size):
                    yield chunk
        except httpx.HTTPError as exc:
            raise SandboxRuntimeError(_unreachable(exc), code="unreachable") from exc

    async def list_dir(self, path: str, *, recursive: bool = False) -> List[FileEntry]:
        """Entries under a directory; an absent directory is an empty list."""
        params = {"recursive": "true"} if recursive else None
        response = await self._request("GET", f"/v1/files/{_quote_path(path)}", params=params)
        if response.status_code == 404:
            return []
        if response.status_code != 200:
            raise self._http_error(response)
        return [
            FileEntry(name=e["name"], size=int(e.get("size") or 0), type=e.get("type", "file"),
                      modified_at=e.get("modified_at"), mode=e.get("mode"))
            for e in (response.json().get("entries") or [])
        ]

    async def exists(self, path: str) -> bool:
        response = await self._request("HEAD", f"/v1/files/{_quote_path(path)}")
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        raise self._http_error(response)

    async def delete(self, path: str, *, recursive: bool = True) -> None:
        """Delete a file or directory. Absent paths are fine."""
        response = await self._request(
            "DELETE", f"/v1/files/{_quote_path(path)}", params={"recursive": "true"} if recursive else None,
        )
        if response.status_code not in (200, 204, 404):
            raise self._http_error(response)
