"""Bounded subprocess I/O; isolated credentials and no shell on the BOW host."""

import os
import selectors
import subprocess
import time
from contextlib import suppress


def run_bounded(argv, *, data=b"", env=None, timeout=30, limit=1048576):
    # Payload is bounded and stdin is written concurrently with stdout draining.
    proc = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, start_new_session=True
    )
    output = bytearray()
    errors = bytearray()
    pending = memoryview(data)
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as sel:
            for stream, target in ((proc.stdout, output), (proc.stderr, errors)):
                os.set_blocking(stream.fileno(), False)
                sel.register(stream, selectors.EVENT_READ, target)
            if pending:
                os.set_blocking(proc.stdin.fileno(), False)
                sel.register(proc.stdin, selectors.EVENT_WRITE, None)
            else:
                proc.stdin.close()
            while sel.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        "Remote operation timed out; remote shell work may still be running. Check its state before retrying."
                    )
                for key, _ in sel.select(min(remaining, 0.2)):
                    if key.fileobj is proc.stdin:
                        try:
                            n = os.write(proc.stdin.fileno(), pending[:8192])
                            pending = pending[n:]
                        except BrokenPipeError:
                            pending = memoryview(b"")
                        if not pending:
                            sel.unregister(proc.stdin)
                            proc.stdin.close()
                        continue
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        sel.unregister(key.fileobj)
                    else:
                        key.data.extend(chunk)
                        if len(output) + len(errors) > limit:
                            raise ValueError(
                                "Remote output exceeded the configured byte limit; narrow the request. Remote shell work may still be running."
                            )
            proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        return proc.returncode, bytes(output), bytes(errors)
    finally:
        if proc.poll() is None:
            import signal

            with suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()
