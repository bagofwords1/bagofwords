"""Process execution against sandboxd's gRPC ProcessService inside an existing sandbox.

The Python SDK's `commands.run()` is one-shot (Execute RPC). Prompt/answer flows need
the streaming `Start` RPC with a PTY plus `WriteStdin`, which this module wraps using
the gRPC stubs bundled in the SDK (`pip install 'k8s-agent-sandbox[grpc]'`).

A target is a SandboxClaim name. The claim's bound Sandbox gives the pod IP, and
sandboxd's gRPC listener on that pod is dialled directly (the MCP server runs in the
cluster). Outside the cluster a `kubectl port-forward` is used instead, for local
development only.
"""
from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable

import grpc
from google.protobuf import empty_pb2
from k8s_agent_sandbox.commands._process_stubs import process_pb2, process_pb2_grpc

from . import sandbox_claim

# CSI (colors, cursor moves) and OSC (hyperlinks, titles) escape sequences Ink emits.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

SANDBOXD_GRPC_PORT = int(os.environ.get("SANDBOXD_GRPC_PORT", "9090"))


def _in_cluster() -> bool:
    return os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token")


@dataclass
class SandboxTarget:
    """An existing sandbox, identified by the SandboxClaim that owns it."""

    claim_name: str
    namespace: str = sandbox_claim.SandboxEnv().namespace
    grpc_port: int = SANDBOXD_GRPC_PORT

    def resolve(self) -> dict:
        """Claim status plus the pod IP of the bound Sandbox. Raises if not Ready."""
        st = sandbox_claim.claim_status(self.claim_name, self.namespace)
        if not st.get("exists"):
            raise LookupError(f"no SandboxClaim {self.namespace}/{self.claim_name}; use list_sandboxes")
        if not st.get("ready") or not st.get("sandbox"):
            raise RuntimeError(
                f"sandbox for claim {self.claim_name!r} is not Ready yet "
                f"({st.get('reason')}: {st.get('message')}); try again shortly")
        ip = sandbox_claim.sandbox_pod_ip(st["sandbox"], self.namespace)
        if not ip:
            raise RuntimeError(f"Sandbox {st['sandbox']!r} has no pod IP yet")
        return {**st, "pod_ip": ip}

    def open(self) -> tuple[grpc.Channel, Callable[[], None]]:
        """Return (gRPC channel to sandboxd, cleanup callable)."""
        info = self.resolve()
        if _in_cluster():
            channel = grpc.insecure_channel(f"{info['pod_ip']}:{self.grpc_port}")
            return channel, channel.close
        return _port_forward(info["sandbox"], self.namespace, self.grpc_port)


def _port_forward(pod: str, namespace: str, port: int) -> tuple[grpc.Channel, Callable[[], None]]:
    """Local-development fallback: kubectl port-forward to the sandbox pod."""
    kubectl = shutil.which("kubectl")
    if not kubectl:
        raise RuntimeError("not running in-cluster and kubectl is not on PATH")
    proc = subprocess.Popen(
        [kubectl, "-n", namespace, "port-forward", f"pod/{pod}", f"0:{port}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    deadline = time.monotonic() + 20
    local_port = None
    while time.monotonic() < deadline:
        line = proc.stdout.readline() if proc.stdout else ""
        m = re.search(r"Forwarding from 127\.0\.0\.1:(\d+)", line or "")
        if m:
            local_port = int(m.group(1))
            break
        if proc.poll() is not None:
            break
    if local_port is None:
        proc.kill()
        raise RuntimeError(f"kubectl port-forward to pod/{pod} did not come up")
    channel = grpc.insecure_channel(f"127.0.0.1:{local_port}")

    def cleanup() -> None:
        channel.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    return channel, cleanup


def execute(channel, command: list[str], env: dict | None = None, cwd: str | None = None,
            timeout: float = 30) -> tuple[int, str, str]:
    """One-shot command via the Execute RPC. Returns (exit_code, stdout, stderr)."""
    stub = process_pb2_grpc.ProcessServiceStub(channel)
    resp = stub.Execute(
        process_pb2.ExecuteRequest(config=process_pb2.ProcessConfig(command=command, env_vars=env or {}, cwd=cwd)),
        timeout=timeout,
    )
    return resp.exit_code, resp.stdout.decode(errors="replace"), resp.stderr.decode(errors="replace")


class InteractiveProcess:
    """Start a command under a PTY, read its output as it arrives, and answer prompts."""

    def __init__(self, channel, command, cols=80, rows=24, env=None, cwd=None):
        self.stub = process_pb2_grpc.ProcessServiceStub(channel)
        self._chunks: queue.Queue[bytes | None] = queue.Queue()
        self.output = b""
        self.exit_code: int | None = None
        self.process_id: int | None = None
        started = threading.Event()

        request = process_pb2.StartRequest(
            config=process_pb2.ProcessConfig(command=command, env_vars=env or {}, cwd=cwd),
            pty=process_pb2.PTY(cols=cols, rows=rows),  # a PTY is what makes CLIs prompt
        )
        self._stream = self.stub.Start(request)

        def pump():
            try:
                for ev in self._stream:
                    kind = ev.WhichOneof("event")
                    if kind == "init":
                        self.process_id = ev.init.process_id
                        started.set()
                    elif kind in ("stdout", "stderr"):  # with a PTY everything arrives on stdout
                        self._chunks.put(getattr(ev, kind))
                    elif kind == "exit":
                        self.exit_code = ev.exit.exit_code
            except grpc.RpcError as e:
                # The channel was closed under us (session cancelled); the
                # process is gone either way, so just end the stream.
                if e.code() != grpc.StatusCode.CANCELLED:
                    raise
            finally:
                self._chunks.put(None)  # sentinel: stream closed
                started.set()

        threading.Thread(target=pump, daemon=True).start()
        if not started.wait(10) or self.process_id is None:
            raise TimeoutError("process did not start")

    @property
    def text(self) -> str:
        """Accumulated output with terminal escape sequences removed."""
        return ANSI_RE.sub("", self.output.decode(errors="replace"))

    @property
    def running(self) -> bool:
        return self.exit_code is None

    def expect(self, pattern, timeout=30) -> re.Match:
        """Block until the cleaned output matches `pattern` (regex) or the process exits."""
        deadline = time.monotonic() + timeout
        while True:
            m = re.search(pattern, self.text)
            if m:
                return m
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"no match for {pattern!r}; output so far:\n{self.text}")
            try:
                chunk = self._chunks.get(timeout=remaining)
            except queue.Empty:
                continue
            if chunk is None:
                raise EOFError(
                    f"process exited (code {self.exit_code}) before {pattern!r}; output:\n{self.text}"
                )
            self.output += chunk

    def send_line(self, text: str) -> None:
        """Type a line and press Enter."""
        self.stub.WriteStdin(
            process_pb2.WriteStdinRequest(process_id=self.process_id, input=(text + "\n").encode())
        )

    def close_stdin(self) -> None:
        self.stub.WriteStdin(
            process_pb2.WriteStdinRequest(process_id=self.process_id, eof=empty_pb2.Empty())
        )

    def wait(self, timeout=60) -> int | None:
        """Drain output until the process exits; return its exit code."""
        deadline = time.monotonic() + timeout
        while True:
            chunk = self._chunks.get(timeout=max(0.1, deadline - time.monotonic()))
            if chunk is None:
                return self.exit_code
            self.output += chunk

    def kill(self) -> None:
        if not self.running:
            return
        self.stub.SendSignal(
            process_pb2.SendSignalRequest(process_id=self.process_id, signal=process_pb2.SIGNAL_SIGKILL)
        )
