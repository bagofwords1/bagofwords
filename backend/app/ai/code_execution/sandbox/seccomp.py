"""Linux seccomp rule preventing sandbox code from creating new processes.

Threads remain available to numeric libraries; fork, vfork, clone3, and
process-style clone return EPERM. A child that cannot fork cannot detach from
the process group that the trusted runner kills on cancellation or timeout.
"""
from __future__ import annotations

import ctypes
import os
import platform

_BPF_LD_W_ABS = 0x20
_BPF_ALU_AND_K = 0x54
_BPF_JMP_JEQ_K = 0x15
_BPF_RET_K = 0x06
_SECCOMP_RET_ALLOW = 0x7FFF0000
_SECCOMP_RET_ERRNO = 0x00050000
_CLONE_THREAD = 0x00010000
_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2


class _Filter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]


class _Program(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_Filter))]


def install_no_process_creation() -> None:
    """Install an irreversible filter in this child before running user code."""
    if platform.system() != "Linux":
        raise OSError("seccomp is available only on Linux")
    arch = platform.machine().lower()
    if arch in ("x86_64", "amd64"):
        clone_nr, direct_forks = 56, (57, 58)
    elif arch in ("aarch64", "arm64"):
        clone_nr, direct_forks = 220, ()
    else:
        raise OSError(f"unsupported seccomp architecture: {arch}")

    ins = [(_BPF_LD_W_ABS, 0, 0, 0)]  # seccomp_data.nr
    for nr in direct_forks:
        ins.extend([
            (_BPF_JMP_JEQ_K, 0, 1, nr),
            (_BPF_RET_K, 0, 0, _SECCOMP_RET_ERRNO | 1),  # EPERM
        ])
    # glibc falls back to clone() for threads when clone3 reports ENOSYS.
    # Returning EPERM here would also break legitimate numeric-library threads.
    ins.extend([
        (_BPF_JMP_JEQ_K, 0, 1, 435),
        (_BPF_RET_K, 0, 0, _SECCOMP_RET_ERRNO | 38),  # ENOSYS
    ])
    ins.extend([
        (_BPF_JMP_JEQ_K, 0, 4, clone_nr),
        (_BPF_LD_W_ABS, 0, 0, 16),  # low 32 bits of clone(flags)
        (_BPF_ALU_AND_K, 0, 0, _CLONE_THREAD),
        (_BPF_JMP_JEQ_K, 1, 0, _CLONE_THREAD),
        (_BPF_RET_K, 0, 0, _SECCOMP_RET_ERRNO | 1),
        (_BPF_RET_K, 0, 0, _SECCOMP_RET_ALLOW),
    ])
    filters = (_Filter * len(ins))(*(_Filter(*item) for item in ins))
    program = _Program(len(ins), filters)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    if libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(program), 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
