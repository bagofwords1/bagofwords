"""Persistent SMB process with private auth/cache and bounded framed responses."""

import contextlib
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

# The worker is imported by module name, so run it from the backend root rather
# than whatever cwd the API process happened to start in.
BACKEND_ROOT = str(Path(__file__).resolve().parents[3])


class SMBSession:
    def __init__(self, client):
        self.context = client._auth_workspace()
        self.workspace = self.context.__enter__()
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-u", "-m", "app.data_sources.clients.remote_smb_worker"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self.workspace[1],
                cwd=BACKEND_ROOT,
                start_new_session=True,
            )
        except BaseException:
            self.context.__exit__(None, None, None)
            raise
        self.closed = False

    def healthy(self):
        return not self.closed and self.proc.poll() is None

    def execute(self, payload, timeout, limit):
        if self.proc.poll() is not None:
            raise ConnectionError("SMB session disconnected; request was not sent")
        pending = memoryview(json.dumps(payload).encode() + b"\n")
        received = bytearray()
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            for stream, events in ((self.proc.stdin, selectors.EVENT_WRITE), (self.proc.stdout, selectors.EVENT_READ)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, events)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("SMB operation timed out")
                for key, events in selector.select(remaining):
                    if events & selectors.EVENT_WRITE:
                        count = os.write(key.fd, pending[:8192])
                        pending = pending[count:]
                        if not pending:
                            selector.unregister(self.proc.stdin)
                    else:
                        chunk = os.read(key.fd, 8192)
                        if not chunk:
                            raise ConnectionError("SMB session disconnected")
                        received.extend(chunk)
                        if len(received) > limit:
                            raise ValueError("SMB output exceeded the configured byte limit")
                        if received.endswith(b"\n"):
                            result = json.loads(received)
                            if not result.get("success"):
                                self.close()
                            return result

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc.wait()
        self.proc.stdin.close()
        self.proc.stdout.close()
        self.context.__exit__(None, None, None)
