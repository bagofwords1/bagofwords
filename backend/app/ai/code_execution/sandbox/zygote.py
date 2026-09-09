"""Fork server for the code sandbox (``python -m …sandbox.zygote``).

One zygote per API process. It is *spawned* by the runner with the scrubbed
environment, imports the heavy libraries once (numpy, pandas, pyarrow — the
~0.7s that dominates a cold child), and then only ever does two things:

* ``fork``: receive three pipe fds (job in, result out, stderr) over the
  Unix socket, fork, and in the child hand those fds to `child.run`.
* ``reap``: waitpid a finished child so its pid can be recycled.

The zygote never receives a job, a credential or a DataFrame, and it holds
no thread; a forked child therefore starts from the same clean state a
freshly spawned interpreter would, minus the import cost. Every child still
applies its own rlimits / no_new_privs / Landlock in `child.run`.

Zombies are kept until the runner sends ``reap`` — a pid that is still a
zombie cannot be reused, so the runner's SIGKILL can never hit an unrelated
process that happened to inherit the number.
"""
from __future__ import annotations

import os
import signal
import socket
import struct
import sys

# Import before serving so forked children inherit the loaded modules.
import numpy  # noqa: F401
import pandas  # noqa: F401
import pyarrow  # noqa: F401

from app.ai.code_execution.sandbox import child as _child  # noqa: F401  (pre-import)
from app.ai.code_execution.sandbox import landlock

_MSG = struct.Struct("<ii")  # (kind, pid)   kind: 1 = fork, 2 = reap
KIND_FORK = 1
KIND_REAP = 2


def _serve(sock: socket.socket) -> None:
    while True:
        try:
            msg, fds, _flags, _addr = socket.recv_fds(sock, _MSG.size, 3)
        except (OSError, EOFError):
            return
        if len(msg) < _MSG.size:
            # Parent closed the socket: exit, taking no children with us
            # (they are in their own sessions and the runner owns them).
            return
        kind, pid_arg = _MSG.unpack(msg[: _MSG.size])

        if kind == KIND_REAP:
            try:
                os.waitpid(pid_arg, 0)
            except ChildProcessError:
                pass
            sock.sendall(_MSG.pack(KIND_REAP, pid_arg))
            continue

        if kind != KIND_FORK or len(fds) != 3:
            for fd in fds:
                os.close(fd)
            sock.sendall(_MSG.pack(KIND_FORK, -1))
            continue

        in_r, out_w, err_w = fds
        pid = os.fork()
        if pid == 0:
            # Child: its own session so the runner can SIGKILL the group;
            # stderr onto the runner's pipe; drop the zygote's socket.
            code = 70
            try:
                os.setsid()
                sock.close()
                os.dup2(err_w, 2)
                os.close(err_w)
                signal.signal(signal.SIGCHLD, signal.SIG_DFL)
                code = _child.run(in_r, out_w)
            except BaseException:  # noqa: BLE001 - never return into the zygote loop
                try:
                    import traceback
                    traceback.print_exc()
                except Exception:
                    pass
            finally:
                os._exit(code)
        # Zygote: hand the fds back, report the pid.
        for fd in (in_r, out_w, err_w):
            os.close(fd)
        sock.sendall(_MSG.pack(KIND_FORK, pid))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    sock_fd = int(argv[argv.index("--sock-fd") + 1])
    try:
        landlock.set_no_new_privs()
    except Exception:
        pass
    sock = socket.socket(fileno=sock_fd)
    try:
        _serve(sock)
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
