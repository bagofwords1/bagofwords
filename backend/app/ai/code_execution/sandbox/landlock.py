"""Minimal ctypes bindings for the Linux Landlock LSM.

Landlock lets an *unprivileged* process irreversibly drop its own filesystem
(and, on newer kernels, TCP) access. That is exactly the shape a code sandbox
needs on a plain Docker host: no capabilities, no seccomp profile changes, no
user namespaces — just a kernel >= 5.13 with the LSM enabled (the default on
every mainstream distro kernel).

ABI levels and what they add (we mask our request to what the kernel offers):
    1  (5.13)  filesystem rights up to MAKE_SYM
    2  (5.19)  FS_REFER (rename/link across directories)
    3  (6.2)   FS_TRUNCATE
    4  (6.7)   TCP bind/connect
    5  (6.10)  FS_IOCTL_DEV
    6  (6.12)  scopes: abstract unix sockets, signals

Fail-open by design: if the syscalls are missing (ENOSYS) or the LSM is not
enabled (EOPNOTSUPP), `restrict_self` reports `applied=False` and the caller
decides whether that is fatal (see `SandboxLimits.require_landlock`).
"""
from __future__ import annotations

import ctypes
import errno
import os
import platform
import struct
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

# Syscall numbers are identical on x86_64 and aarch64 (asm-generic table).
_NR_LANDLOCK_CREATE_RULESET = 444
_NR_LANDLOCK_ADD_RULE = 445
_NR_LANDLOCK_RESTRICT_SELF = 446

_LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
_LANDLOCK_RULE_PATH_BENEATH = 1

# Filesystem access rights
FS_EXECUTE = 1 << 0
FS_WRITE_FILE = 1 << 1
FS_READ_FILE = 1 << 2
FS_READ_DIR = 1 << 3
FS_REMOVE_DIR = 1 << 4
FS_REMOVE_FILE = 1 << 5
FS_MAKE_CHAR = 1 << 6
FS_MAKE_DIR = 1 << 7
FS_MAKE_REG = 1 << 8
FS_MAKE_SOCK = 1 << 9
FS_MAKE_FIFO = 1 << 10
FS_MAKE_BLOCK = 1 << 11
FS_MAKE_SYM = 1 << 12
FS_REFER = 1 << 13        # ABI 2
FS_TRUNCATE = 1 << 14     # ABI 3
FS_IOCTL_DEV = 1 << 15    # ABI 5

# Network access rights (ABI 4)
NET_BIND_TCP = 1 << 0
NET_CONNECT_TCP = 1 << 1

# Scopes (ABI 6)
SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0
SCOPE_SIGNAL = 1 << 1

_FS_RIGHTS_BY_ABI = {
    1: (1 << 13) - 1,
    2: (1 << 14) - 1,
    3: (1 << 15) - 1,
    4: (1 << 15) - 1,
    5: (1 << 16) - 1,
    6: (1 << 16) - 1,
}

READ_RIGHTS = FS_READ_FILE | FS_READ_DIR
RW_RIGHTS = (
    READ_RIGHTS | FS_WRITE_FILE | FS_MAKE_REG | FS_MAKE_DIR
    | FS_REMOVE_FILE | FS_REMOVE_DIR | FS_REFER | FS_TRUNCATE
)

_libc: Optional[ctypes.CDLL] = None


def _get_libc() -> ctypes.CDLL:
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
        _libc.syscall.restype = ctypes.c_long
    return _libc


def _syscall(nr: int, *args) -> int:
    libc = _get_libc()
    res = libc.syscall(ctypes.c_long(nr), *args)
    if res < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return int(res)


def abi_version() -> int:
    """Highest Landlock ABI the running kernel supports; 0 when unavailable."""
    if platform.system() != "Linux":
        return 0
    try:
        return _syscall(
            _NR_LANDLOCK_CREATE_RULESET,
            ctypes.c_void_p(None),
            ctypes.c_size_t(0),
            ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION),
        )
    except OSError as e:
        if e.errno in (errno.ENOSYS, errno.EOPNOTSUPP):
            return 0
        raise


def _ruleset_attr_bytes(abi: int, fs: int, net: int, scoped: int) -> bytes:
    # struct landlock_ruleset_attr grows with the ABI; the kernel rejects a
    # size larger than it knows (E2BIG), so trim to what this ABI defines.
    if abi >= 6:
        return struct.pack("<QQQ", fs, net, scoped)
    if abi >= 4:
        return struct.pack("<QQ", fs, net)
    return struct.pack("<Q", fs)


@dataclass
class LandlockReport:
    applied: bool
    abi: int
    reason: str = ""
    fs_rules: int = 0
    net_blocked: bool = False
    signals_scoped: bool = False
    skipped_paths: List[str] = field(default_factory=list)
    rule_errors: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict:
        return {
            "applied": self.applied,
            "abi": self.abi,
            "reason": self.reason,
            "fs_rules": self.fs_rules,
            "net_blocked": self.net_blocked,
            "signals_scoped": self.signals_scoped,
            "skipped_paths": list(self.skipped_paths),
            "rule_errors": list(self.rule_errors),
        }


# Rights that make sense on a non-directory. Landlock rejects a rule that
# grants directory-only rights (READ_DIR, MAKE_*, REMOVE_*, REFER) on a file
# with EINVAL, so rules are masked by what the path actually is.
FILE_RIGHTS = FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_TRUNCATE | FS_IOCTL_DEV


def rights_for_path(path: str, rights: int, *, is_dir: Optional[bool] = None) -> int:
    """Mask `rights` to what Landlock accepts for `path` (file vs directory)."""
    if is_dir is None:
        try:
            is_dir = os.path.isdir(path)
        except OSError:
            is_dir = False
    return rights if is_dir else (rights & FILE_RIGHTS)


def _add_path_rule(ruleset_fd: int, path: str, rights: int) -> Optional[str]:
    """Allow `rights` beneath `path`.

    Returns None on success, "absent" when the path does not exist, or the
    OS error text when the kernel refused the rule. A refused rule is
    reported and skipped rather than aborting the whole policy: a missing
    allowance costs the child a read, an abandoned policy costs the
    confinement.
    """
    try:
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except OSError:
        return "absent"
    try:
        rights = rights_for_path(path, rights)
        if rights == 0:
            return "no applicable rights"
        # struct landlock_path_beneath_attr { __u64 allowed_access; __s32 parent_fd; } __packed
        attr = struct.pack("<Qi", rights, fd)
        buf = ctypes.create_string_buffer(attr, len(attr))
        _syscall(
            _NR_LANDLOCK_ADD_RULE,
            ctypes.c_int(ruleset_fd),
            ctypes.c_int(_LANDLOCK_RULE_PATH_BENEATH),
            buf,
            ctypes.c_uint32(0),
        )
        return None
    except OSError as e:
        return f"{e.errno}: {e.strerror}"
    finally:
        os.close(fd)


def set_no_new_privs() -> None:
    """prctl(PR_SET_NO_NEW_PRIVS, 1) — required before landlock_restrict_self
    for an unprivileged process, and a good idea on its own (no setuid gain)."""
    PR_SET_NO_NEW_PRIVS = 38
    libc = _get_libc()
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


def restrict_self(
    *,
    read_paths: Iterable[str],
    rw_paths: Iterable[str] = (),
    read_files: Iterable[str] = (),
    rw_files: Iterable[str] = (),
    block_tcp: bool = True,
    scope_signals: bool = True,
) -> LandlockReport:
    """Confine the calling process to the given filesystem view.

    `read_paths` / `rw_paths` are directory hierarchies; `read_files` /
    `rw_files` are individual files (a rule on a file grants only file-level
    rights, so an uploaded file can be exposed without its siblings).

    Everything not listed is denied for every right the kernel's ABI handles.
    With `block_tcp` (ABI >= 4) the process can neither bind nor connect TCP.
    """
    abi = abi_version()
    if abi <= 0:
        return LandlockReport(applied=False, abi=0, reason="landlock unavailable (kernel < 5.13 or LSM disabled)")

    fs_mask = _FS_RIGHTS_BY_ABI.get(min(abi, 6), _FS_RIGHTS_BY_ABI[1])
    handled_fs = fs_mask
    handled_net = (NET_BIND_TCP | NET_CONNECT_TCP) if (block_tcp and abi >= 4) else 0
    scoped = (SCOPE_SIGNAL | SCOPE_ABSTRACT_UNIX_SOCKET) if (scope_signals and abi >= 6) else 0

    attr = _ruleset_attr_bytes(abi, handled_fs, handled_net, scoped)
    buf = ctypes.create_string_buffer(attr, len(attr))
    try:
        ruleset_fd = _syscall(
            _NR_LANDLOCK_CREATE_RULESET, buf, ctypes.c_size_t(len(attr)), ctypes.c_uint32(0)
        )
    except OSError as e:
        return LandlockReport(applied=False, abi=abi, reason=f"create_ruleset failed: {e}")

    report = LandlockReport(applied=False, abi=abi)
    try:
        # File-level rules can only carry file-level rights.
        file_rights_mask = FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_TRUNCATE | FS_IOCTL_DEV
        specs = [
            (read_paths, READ_RIGHTS & fs_mask),
            (rw_paths, RW_RIGHTS & fs_mask),
            (read_files, FS_READ_FILE),
            (rw_files, (FS_READ_FILE | FS_WRITE_FILE | FS_TRUNCATE) & fs_mask & file_rights_mask),
        ]
        seen = set()
        for paths, rights in specs:
            for p in paths:
                p = os.path.abspath(str(p))
                key = (p, rights)
                if key in seen:
                    continue
                seen.add(key)
                outcome = _add_path_rule(ruleset_fd, p, rights)
                if outcome is None:
                    report.fs_rules += 1
                elif outcome == "absent":
                    report.skipped_paths.append(p)
                else:
                    report.rule_errors.append(f"{p}: {outcome}")

        set_no_new_privs()
        _syscall(_NR_LANDLOCK_RESTRICT_SELF, ctypes.c_int(ruleset_fd), ctypes.c_uint32(0))
        report.applied = True
        report.net_blocked = bool(handled_net)
        report.signals_scoped = bool(scoped & SCOPE_SIGNAL)
        return report
    except OSError as e:
        report.reason = f"restrict_self failed: {e}"
        return report
    finally:
        os.close(ruleset_fd)
