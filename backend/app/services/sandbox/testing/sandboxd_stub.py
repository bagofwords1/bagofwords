"""An in-process stand-in for ``sandboxd``, for tests and UI development.

It serves the same two APIs as the real daemon at the pinned upstream commit:

* gRPC ``process.v1.ProcessService`` (``Start``, ``Execute``,
  ``ExecuteWithLimits``, ``WriteStdin``, ``SendSignal``);
* REST ``/v1/files/{path}``, ``/v1/health``, ``/v1/metadata``.

One :class:`StubHost` serves many sandboxes on one pair of ports, over TLS
with its own throwaway CA, and picks the sandbox by the ``X-Sandbox-Id``
header: the same shape a real compute's router has. Each sandbox is a
directory on local disk, and "processes" are real local subprocesses.

**There is no isolation here at all.** Commands run as the BoW server's own
user. That is the point (the runtime client and the service are exercised
against real process and file behaviour without a server), and it is why the
``fake`` compute type that uses this is only offered under ``TESTING`` or an
explicit opt-in (see ``drivers/registry.fake_driver_enabled``).

The servers run on their own thread and event loop, so they stay up across
the short-lived event loops tests and requests come and go on.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import os
import shutil
import signal
import socket
import stat as stat_module
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

SANDBOX_HEADER = "x-sandbox-id"

_NET_PROBE = '''#!{python}
"""Simulated egress for the fake compute: `bow-net-probe <domain>`.

Exits 0 and prints "allowed" when the sandbox's network policy lets it reach
the domain, 1 and "refused" otherwise, and records the attempt in the
sandbox's domain log, as a real compute does.
"""
import json, os, sys, time

domain = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
policy = os.environ.get("BOW_FAKE_NETWORK_POLICY", "none")
listed = json.loads(os.environ.get("BOW_FAKE_ALLOWED_DOMAINS", "[]"))
private = (
    domain in ("localhost", "metadata", "host")
    or domain.startswith(("10.", "127.", "192.168.", "169.254.", "172.16."))
)

def on_list(name):
    for entry in listed:
        entry = entry.lower()
        if name == entry or (entry.startswith("*.") and name.endswith(entry[1:])):
            return True
    return False

if policy == "full":
    allowed = not private
elif policy in ("registries", "allowlist"):
    allowed = on_list(domain) and not private
else:
    allowed = False
with open(os.environ["BOW_FAKE_EGRESS_LOG"], "a") as log:
    log.write(json.dumps({{"domain": domain, "decision": "allowed" if allowed else "refused", "at": time.time()}}) + "\\n")
print("allowed" if allowed else "refused")
sys.exit(0 if allowed else 1)
'''


@dataclass
class StubSandbox:
    sandbox_id: str
    token_sha256: str
    root: Path
    env: Dict[str, str] = field(default_factory=dict)
    running: bool = True
    started_at: float = field(default_factory=time.time)
    processes: Dict[int, "asyncio.subprocess.Process"] = field(default_factory=dict)

    @property
    def egress_log(self) -> Path:
        return self.root.parent / "egress.log"


class _Refused(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _resolve(sandbox: StubSandbox, raw: str) -> Path:
    """A path under the sandbox root; refuses anything that escapes it."""
    root = sandbox.root.resolve()
    target = (root / raw.lstrip("/")).resolve()
    if target != root and root not in target.parents:
        raise _Refused(403, "PERMISSION_DENIED", "path escapes the sandbox root")
    return target


def _entry(path: Path, name: str) -> dict:
    info = path.stat()
    return {
        "name": name,
        "size": info.st_size,
        "type": "directory" if stat_module.S_ISDIR(info.st_mode) else "file",
        "modified_at": dt.datetime.fromtimestamp(info.st_mtime, dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": f"{stat_module.S_IMODE(info.st_mode):04o}",
    }


def _exit_code(returncode: int) -> int:
    return returncode if returncode >= 0 else 128 + (-returncode)


class StubHost:
    """The servers. Use :func:`get_stub_host` for the process-wide instance."""

    def __init__(self) -> None:
        self._sandboxes: Dict[str, StubSandbox] = {}
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready = threading.Event()
        self._startup_error: Optional[BaseException] = None
        self._workdir: Optional[Path] = None
        # Test knob: forward immediate gRPC failures the way a proxy sometimes
        # does, with the status lost ("UNKNOWN: Stream removed").
        self.lose_immediate_status: bool = False
        self.rest_port: int = 0
        self.grpc_port: int = 0
        self.ca_pem: str = ""

    # ── Sandbox registry (thread-safe; called by the fake driver) ──

    @property
    def workdir(self) -> Path:
        self.start()
        assert self._workdir is not None
        return self._workdir

    def add_sandbox(self, sandbox_id: str, token_sha256: str, env: Optional[Dict[str, str]] = None) -> StubSandbox:
        base = self.workdir / "sandboxes" / sandbox_id
        root = base / "workspace"
        root.mkdir(parents=True, exist_ok=True)
        sandbox = StubSandbox(sandbox_id=sandbox_id, token_sha256=token_sha256, root=root, env=dict(env or {}))
        with self._lock:
            self._sandboxes[sandbox_id] = sandbox
        return sandbox

    def get(self, sandbox_id: str) -> Optional[StubSandbox]:
        with self._lock:
            return self._sandboxes.get(sandbox_id)

    def ids(self) -> list:
        with self._lock:
            return list(self._sandboxes)

    def set_running(self, sandbox_id: str, running: bool) -> None:
        sandbox = self.get(sandbox_id)
        if sandbox is None:
            return
        if not running:
            self._kill_all(sandbox)
        sandbox.running = running
        if running:
            sandbox.started_at = time.time()

    def remove_sandbox(self, sandbox_id: str) -> None:
        with self._lock:
            sandbox = self._sandboxes.pop(sandbox_id, None)
        if sandbox is not None:
            self._kill_all(sandbox)
            shutil.rmtree(sandbox.root.parent, ignore_errors=True)

    @staticmethod
    def _kill_all(sandbox: StubSandbox) -> None:
        for pid in list(sandbox.processes):
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    # ── Lifecycle ──

    def start(self) -> "StubHost":
        if self._thread is not None:
            return self
        with self._lock:
            if self._thread is not None:
                return self
            self._workdir = Path(tempfile.mkdtemp(prefix="bow-fake-sandboxes-"))
            bin_dir = self._workdir / "bin"
            bin_dir.mkdir()
            probe = bin_dir / "bow-net-probe"
            probe.write_text(_NET_PROBE.format(python=sys.executable))
            probe.chmod(0o755)
            self._thread = threading.Thread(target=self._run, name="sandboxd-stub", daemon=True)
            self._thread.start()
        self._ready.wait(timeout=30)
        if self._startup_error is not None:
            raise RuntimeError("sandboxd stub failed to start") from self._startup_error
        if not self.rest_port or not self.grpc_port:
            raise RuntimeError("sandboxd stub did not start in time")
        return self

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._serve())
        except BaseException as exc:  # noqa: BLE001 - reported to start()
            self._startup_error = exc
            self._ready.set()

    def _tls_material(self) -> tuple:
        """A throwaway CA and a server certificate for 127.0.0.1 / localhost."""
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID

        now = dt.datetime.now(dt.timezone.utc)

        def _cert(subject, issuer, public_key, signing_key, *, ca: bool):
            builder = (
                x509.CertificateBuilder()
                .subject_name(subject).issuer_name(issuer).public_key(public_key)
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
                # Python 3.13 verifies strictly: both key identifiers are required.
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
                .add_extension(
                    x509.AuthorityKeyIdentifier.from_issuer_public_key(signing_key.public_key()), critical=False,
                )
                .add_extension(
                    x509.KeyUsage(
                        digital_signature=True, key_cert_sign=ca, crl_sign=ca, content_commitment=False,
                        key_encipherment=False, data_encipherment=False, key_agreement=False,
                        encipher_only=False, decipher_only=False,
                    ),
                    critical=True,
                )
            )
            if not ca:
                builder = builder.add_extension(
                    x509.SubjectAlternativeName([
                        x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost"),
                    ]),
                    critical=False,
                )
            return builder.sign(signing_key, hashes.SHA256())

        ca_key = ec.generate_private_key(ec.SECP256R1())
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "bow fake sandbox CA")])
        ca_cert = _cert(ca_name, ca_name, ca_key.public_key(), ca_key, ca=True)
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
        cert = _cert(name, ca_name, key.public_key(), ca_key, ca=False)
        pem = serialization.Encoding.PEM
        key_pem = key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        return ca_cert.public_bytes(pem), cert.public_bytes(pem), key_pem

    async def _serve(self) -> None:
        import grpc
        import uvicorn

        from app.services.sandbox.sandboxd_proto import process_pb2_grpc

        ca_pem, cert_pem, key_pem = self._tls_material()
        tls_dir = self._workdir / "tls"
        tls_dir.mkdir()
        (tls_dir / "cert.pem").write_bytes(cert_pem)
        (tls_dir / "key.pem").write_bytes(key_pem)
        os.chmod(tls_dir / "key.pem", 0o600)

        grpc_server = grpc.aio.server(options=[("grpc.max_receive_message_length", 64 << 20)])
        process_pb2_grpc.add_ProcessServiceServicer_to_server(_make_process_service(self), grpc_server)
        grpc_port = grpc_server.add_secure_port(
            "127.0.0.1:0", grpc.ssl_server_credentials([(key_pem, cert_pem)]),
        )
        await grpc_server.start()

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        rest_port = listener.getsockname()[1]
        config = uvicorn.Config(
            # log_config=None: this server runs inside the application's process (the
            # fake driver), and uvicorn's default would reconfigure the application's
            # own uvicorn loggers.
            _rest_app(self), log_config=None, access_log=False, lifespan="off",
            ssl_certfile=str(tls_dir / "cert.pem"), ssl_keyfile=str(tls_dir / "key.pem"),
        )
        server = uvicorn.Server(config)
        server.install_signal_handlers = lambda: None  # not the main thread
        serving = asyncio.ensure_future(server.serve(sockets=[listener]))
        while not server.started and not serving.done():
            await asyncio.sleep(0.01)

        self.ca_pem = ca_pem.decode("ascii")
        self.grpc_port, self.rest_port = grpc_port, rest_port
        self._ready.set()
        await serving

    # ── Request helpers shared by both APIs ──

    def authorize(self, sandbox_id: Optional[str], bearer: Optional[str], *, need_token: bool = True) -> StubSandbox:
        sandbox = self.get(sandbox_id or "")
        if sandbox is None:
            raise _Refused(404, "NOT_FOUND", "unknown sandbox")
        if not sandbox.running:
            raise _Refused(502, "UNAVAILABLE", "sandbox is not running")
        if need_token:
            digest = hashlib.sha256((bearer or "").encode("utf-8")).hexdigest()
            if not bearer or not hmac.compare_digest(digest, sandbox.token_sha256):
                raise _Refused(401, "UNAUTHENTICATED", "missing or invalid bearer token")
        return sandbox

    def process_env(self, sandbox: StubSandbox, extra: Dict[str, str]) -> Dict[str, str]:
        home = sandbox.root / ".home"
        home.mkdir(exist_ok=True)
        env = {
            "PATH": f"{self.workdir / 'bin'}{os.pathsep}{os.path.dirname(sys.executable)}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(home),
            "LANG": "C.UTF-8",
            "PYTHONUNBUFFERED": "1",
            "SANDBOX_ID": sandbox.sandbox_id,
            "BOW_FAKE_EGRESS_LOG": str(sandbox.egress_log),
        }
        env.update(sandbox.env)
        env.update(extra)
        return env


_HOST: Optional[StubHost] = None
_HOST_LOCK = threading.Lock()


def get_stub_host() -> StubHost:
    """The process-wide stub, started on first use."""
    global _HOST
    with _HOST_LOCK:
        if _HOST is None:
            _HOST = StubHost()
    return _HOST.start()


# ── REST: /v1/files, /v1/health, /v1/metadata ────────────────────────────


def _bearer(headers) -> Optional[str]:
    value = headers.get("authorization") or ""
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return headers.get("x-sandboxd-token")


def _rest_app(host: StubHost):
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import FileResponse, JSONResponse, Response
    from starlette.routing import Route

    def refused(exc: _Refused) -> JSONResponse:
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=exc.status)

    async def health(request: Request):
        try:
            sandbox = host.authorize(request.headers.get(SANDBOX_HEADER), None, need_token=False)
        except _Refused as exc:
            return refused(exc)
        return JSONResponse({"status": "ok", "uptime_seconds": int(time.time() - sandbox.started_at)})

    async def metadata(request: Request):
        try:
            sandbox = host.authorize(request.headers.get(SANDBOX_HEADER), _bearer(request.headers))
        except _Refused as exc:
            return refused(exc)
        return JSONResponse({"env": {"SANDBOX_ID": sandbox.sandbox_id}})

    def listing(sandbox: StubSandbox, directory: Path, raw: str, recursive: bool, max_depth: Optional[int]):
        entries = []

        def walk(current: Path, prefix: str, depth: int):
            for child in sorted(current.iterdir(), key=lambda p: p.name):
                name = f"{prefix}{child.name}"
                entries.append(_entry(child, name))
                if recursive and child.is_dir() and not child.is_symlink() and (max_depth is None or depth < max_depth):
                    walk(child, f"{name}/", depth + 1)

        walk(directory, "", 1)
        return {"path": "/" + raw.strip("/"), "entries": entries}

    async def files(request: Request):
        raw = request.path_params["path"]
        action = None
        for suffix in (":move", ":copy"):
            if raw.endswith(suffix):
                raw, action = raw[: -len(suffix)], suffix[1:]
        query = request.query_params
        try:
            sandbox = host.authorize(request.headers.get(SANDBOX_HEADER), _bearer(request.headers))
            target = _resolve(sandbox, raw)

            if request.method in ("GET", "HEAD"):
                if not target.exists():
                    raise _Refused(404, "NOT_FOUND", "no such file or directory")
                if request.method == "HEAD":
                    return Response(status_code=200)
                if query.get("stat") == "true":
                    return JSONResponse(_entry(target, target.name or "/"))
                if target.is_dir():
                    depth = int(query["max_depth"]) if query.get("max_depth") else None
                    return JSONResponse(listing(sandbox, target, raw, query.get("recursive") == "true", depth))
                return FileResponse(target, media_type="application/octet-stream")

            if request.method == "PUT":
                if query.get("type") == "directory":
                    target.mkdir(parents=True, exist_ok=True)
                    return Response(status_code=204)
                if request.headers.get("if-none-match") == "*" and target.exists():
                    raise _Refused(412, "FAILED_PRECONDITION", "file exists")
                target.parent.mkdir(parents=True, exist_ok=True)
                with open(target, "ab" if query.get("append") == "true" else "wb") as out:
                    async for chunk in request.stream():
                        out.write(chunk)
                if query.get("mode"):
                    os.chmod(target, int(query["mode"], 8))
                return Response(status_code=204)

            if request.method == "DELETE":
                if not target.exists():
                    raise _Refused(404, "NOT_FOUND", "no such file or directory")
                if target.is_dir():
                    if query.get("recursive") != "true" and any(target.iterdir()):
                        raise _Refused(409, "FAILED_PRECONDITION", "directory is not empty")
                    shutil.rmtree(target)
                else:
                    target.unlink()
                return Response(status_code=204)

            if request.method == "POST" and action:
                destination = _resolve(sandbox, query.get("destination", ""))
                if not target.exists():
                    raise _Refused(404, "NOT_FOUND", "no such file or directory")
                destination.parent.mkdir(parents=True, exist_ok=True)
                if action == "move":
                    shutil.move(str(target), str(destination))
                elif target.is_dir():
                    shutil.copytree(target, destination, dirs_exist_ok=True)
                else:
                    shutil.copy2(target, destination)
                return Response(status_code=204)

            raise _Refused(405, "UNIMPLEMENTED", "method not allowed")
        except _Refused as exc:
            return refused(exc)

    return Starlette(routes=[
        Route("/v1/health", health, methods=["GET"]),
        Route("/v1/metadata", metadata, methods=["GET"]),
        Route("/v1/files/{path:path}", files, methods=["GET", "HEAD", "PUT", "DELETE", "POST"]),
        Route("/v1/files", files, methods=["GET"], name="root"),
    ])


# ── gRPC: process.v1.ProcessService ──────────────────────────────────────


def _make_process_service(host: StubHost):
    """The servicer instance. The generated base class is imported here, lazily."""
    from app.services.sandbox.sandboxd_proto import process_pb2_grpc

    class ProcessService(_ProcessService, process_pb2_grpc.ProcessServiceServicer):
        pass

    return ProcessService(host)


class _ProcessService:
    def __init__(self, host: StubHost):
        self.host = host

    async def _refuse(self, context, code, message: str):
        """Fail a call before it produced anything, as sandboxd does (or, with
        the test knob on, as it looks after a proxy dropped the status)."""
        import grpc

        if self.host.lose_immediate_status:
            await context.abort(grpc.StatusCode.UNKNOWN, "Stream removed (Data frame with END_STREAM flag received)")
        await context.abort(code, message)

    async def _sandbox(self, context) -> StubSandbox:
        import grpc

        md = {k.lower(): v for k, v in (context.invocation_metadata() or ())}
        bearer = md.get("authorization", "")
        bearer = bearer[7:].strip() if bearer.lower().startswith("bearer ") else md.get("x-sandboxd-token")
        try:
            return self.host.authorize(md.get(SANDBOX_HEADER), bearer)
        except _Refused as exc:
            code = {
                401: grpc.StatusCode.UNAUTHENTICATED, 404: grpc.StatusCode.NOT_FOUND,
                502: grpc.StatusCode.UNAVAILABLE,
            }.get(exc.status, grpc.StatusCode.PERMISSION_DENIED)
            if exc.status == 401:
                await self._refuse(context, code, exc.message)
            await context.abort(code, exc.message)
            raise  # unreachable; abort raises

    async def _spawn(self, sandbox: StubSandbox, config, context, *, stdin: bool):
        import grpc

        if not config.command:
            await self._refuse(context, grpc.StatusCode.INVALID_ARGUMENT, "command is required")
        try:
            cwd = _resolve(sandbox, config.cwd) if config.HasField("cwd") else sandbox.root
        except _Refused:
            await self._refuse(context, grpc.StatusCode.PERMISSION_DENIED,
                               f'cwd: access denied: path "{config.cwd}": path escapes sandbox root')
        try:
            process = await asyncio.create_subprocess_exec(
                *config.command, cwd=str(cwd), env=self.host.process_env(sandbox, dict(config.env_vars)),
                stdin=asyncio.subprocess.PIPE if stdin else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
            )
        except (FileNotFoundError, PermissionError, NotADirectoryError):
            await self._refuse(
                context, grpc.StatusCode.NOT_FOUND,
                f'failed to start command: command or path not found: exec: "{config.command[0]}": executable file not found in $PATH',
            )
        sandbox.processes[process.pid] = process
        return process

    async def Start(self, request, context):  # noqa: N802 - gRPC method name
        from app.services.sandbox.sandboxd_proto import process_pb2 as pb

        sandbox = await self._sandbox(context)
        process = await self._spawn(sandbox, request.config, context, stdin=True)
        queue: asyncio.Queue = asyncio.Queue()

        async def pump(stream, kind):
            while True:
                chunk = await stream.read(32 * 1024)
                if not chunk:
                    break
                await queue.put((kind, chunk))
            await queue.put((kind, None))

        pumps = [asyncio.ensure_future(pump(process.stdout, "stdout")), asyncio.ensure_future(pump(process.stderr, "stderr"))]
        try:
            yield pb.StartResponse(init=pb.InitEvent(process_id=process.pid))
            open_streams = 2
            while open_streams:
                kind, chunk = await queue.get()
                if chunk is None:
                    open_streams -= 1
                elif kind == "stdout":
                    yield pb.StartResponse(stdout=chunk)
                else:
                    yield pb.StartResponse(stderr=chunk)
            returncode = await process.wait()
            yield pb.StartResponse(exit=pb.ExitEvent(exit_code=_exit_code(returncode)))
        finally:
            for task in pumps:
                task.cancel()
            if process.returncode is None:  # the caller went away: do not leave it running
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            sandbox.processes.pop(process.pid, None)

    async def _run_to_end(self, sandbox, config, context, *, time_limit=None, stdout_limit=None, stderr_limit=None):
        process = await self._spawn(sandbox, config, context, stdin=False)
        started = time.monotonic()
        timed_out = False
        try:
            try:
                out, err = await asyncio.wait_for(process.communicate(), timeout=time_limit)
            except asyncio.TimeoutError:
                timed_out = True
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                out, err = await process.communicate()
        finally:
            sandbox.processes.pop(process.pid, None)
        truncated = [False, False]
        if stdout_limit and len(out) > stdout_limit:
            out, truncated[0] = out[:stdout_limit], True
        if stderr_limit and len(err) > stderr_limit:
            err, truncated[1] = err[:stderr_limit], True
        return _exit_code(process.returncode), out, err, timed_out, time.monotonic() - started, truncated

    async def Execute(self, request, context):  # noqa: N802
        from app.services.sandbox.sandboxd_proto import process_pb2 as pb

        sandbox = await self._sandbox(context)
        code, out, err, _, _, _ = await self._run_to_end(sandbox, request.config, context)
        return pb.ExecuteResponse(exit_code=code, stdout=out, stderr=err)

    async def ExecuteWithLimits(self, request, context):  # noqa: N802
        from google.protobuf import duration_pb2

        from app.services.sandbox.sandboxd_proto import process_pb2 as pb

        sandbox = await self._sandbox(context)
        limit = request.time_limit.ToTimedelta().total_seconds() if request.HasField("time_limit") else None
        code, out, err, timed_out, elapsed, truncated = await self._run_to_end(
            sandbox, request.config, context, time_limit=limit or None,
            stdout_limit=request.stdout_limit_bytes if request.HasField("stdout_limit_bytes") else None,
            stderr_limit=request.stderr_limit_bytes if request.HasField("stderr_limit_bytes") else None,
        )
        duration = duration_pb2.Duration()
        duration.FromTimedelta(dt.timedelta(seconds=elapsed))
        return pb.ExecuteWithLimitsResponse(
            exit_code=code, stdout=out, stderr=err, elapsed_duration=duration,
            termination_reason=(
                pb.EXECUTE_TERMINATION_REASON_TIME_LIMIT_EXCEEDED if timed_out
                else pb.EXECUTE_TERMINATION_REASON_EXITED
            ),
            stdout_truncated=truncated[0], stderr_truncated=truncated[1],
        )

    async def _process(self, sandbox: StubSandbox, process_id: int, context):
        import grpc

        process = sandbox.processes.get(process_id)
        if process is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, "unknown process id")
        return process

    async def SendSignal(self, request, context):  # noqa: N802
        from app.services.sandbox.sandboxd_proto import process_pb2 as pb

        sandbox = await self._sandbox(context)
        process = await self._process(sandbox, request.process_id, context)
        number = {pb.SIGNAL_SIGINT: signal.SIGINT, pb.SIGNAL_SIGKILL: signal.SIGKILL}.get(request.signal, signal.SIGTERM)
        try:
            os.killpg(process.pid, number)
        except (ProcessLookupError, PermissionError):
            pass
        return pb.SendSignalResponse()

    async def WriteStdin(self, request, context):  # noqa: N802
        from app.services.sandbox.sandboxd_proto import process_pb2 as pb

        sandbox = await self._sandbox(context)
        process = await self._process(sandbox, request.process_id, context)
        if process.stdin is not None:
            if request.WhichOneof("payload") == "eof":
                process.stdin.close()
            else:
                process.stdin.write(request.input)
                await process.stdin.drain()
        return pb.WriteStdinResponse()

    async def ResizeTTY(self, request, context):  # noqa: N802
        import grpc

        await context.abort(grpc.StatusCode.FAILED_PRECONDITION, "process has no PTY")
