"""Interactive process execution against sandboxd's gRPC ProcessService.

The Python SDK's `commands.run()` is one-shot (Execute RPC). Prompt/answer flows need
the streaming `Start` RPC with a PTY plus `WriteStdin`, which this module wraps using
the gRPC stubs bundled in the SDK (`pip install 'k8s-agent-sandbox[grpc]'`).
"""
from __future__ import annotations

import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import grpc
from google.protobuf import empty_pb2
from k8s_agent_sandbox.commands._process_stubs import process_pb2, process_pb2_grpc

# CSI (colors, cursor moves) and OSC (hyperlinks, titles) escape sequences Ink emits.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


@dataclass
class SandboxTarget:
    """Where to find sandboxd: a local gRPC address, or a sandbox created from a warm pool."""

    grpc: str = "localhost:9090"
    warmpool: str | None = None
    namespace: str = "default"

    def open(self) -> tuple[grpc.Channel, Callable[[], None]]:
        """Return (gRPC channel, cleanup callable)."""
        if not self.warmpool:
            channel = grpc.insecure_channel(self.grpc)
            return channel, channel.close

        from k8s_agent_sandbox import SandboxClient
        from k8s_agent_sandbox.models import SandboxdPodTunnelConnectionConfig

        client = SandboxClient(connection_config=SandboxdPodTunnelConnectionConfig())
        sandbox = client.create_sandbox(warmpool=self.warmpool, namespace=self.namespace)
        sandbox.connector.connect()
        return sandbox.connector.grpc_channel(), sandbox.terminate


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
