"""Start LibreOffice confined, then exec it. Run as an isolated subprocess.

    python -I -S _soffice_worker.py <workdir> <soffice.bin> [soffice args...]

A document being converted is content we do not control, and a document can
name other files and URLs (linked sections, linked images). The converter
therefore gets: no sockets, read access to the LibreOffice installation and
fonts only, and write access to its own work directory only — which holds the
staged input, the throwaway profile and the output.

Kept free of application imports (it runs before any restriction exists and
must not load settings or credentials); the two sandbox modules are loaded by
path, as `_tshark_worker.py` does.
"""

import ctypes
from glob import glob
import os
from pathlib import Path
import resource
import sys

# Exit status when the confinement could not be applied; nothing was converted.
# Must match _office_convert.CONFINEMENT_UNAVAILABLE_EXIT.
CONFINEMENT_UNAVAILABLE_EXIT = 78
CPU_LIMIT_SECONDS = 300
MAX_OUTPUT_BYTES = 1 << 30


class _ArgCmp(ctypes.Structure):
    # struct scmp_arg_cmp
    _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int),
                ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]


def _deny_network_sockets():
    """Only AF_UNIX sockets can be created: nothing to fetch a remote link
    with. LibreOffice needs a local socket for its own single-instance pipe,
    which lives in the work directory."""
    lib = ctypes.CDLL("libseccomp.so.2")
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(_ArgCmp),
    ]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    deny = 0x00050001  # SCMP_ACT_ERRNO(EPERM)
    ctx = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not ctx:
        raise RuntimeError("cannot initialize syscall restrictions")
    try:
        not_unix = (_ArgCmp * 1)(_ArgCmp(0, 1, 1, 0))  # arg0 != AF_UNIX  (SCMP_CMP_NE)
        for name in (b"socket", b"socketpair"):
            nr = lib.seccomp_syscall_resolve_name(name)
            if nr < 0 or lib.seccomp_rule_add_array(ctx, deny, nr, 1, not_unix):
                raise RuntimeError("cannot restrict sockets")
        for name in (b"ptrace", b"process_vm_readv", b"process_vm_writev"):
            nr = lib.seccomp_syscall_resolve_name(name)
            if nr < 0 or lib.seccomp_rule_add(ctx, deny, nr, 0):
                raise RuntimeError("cannot restrict syscalls")
        if lib.seccomp_load(ctx):
            raise RuntimeError("cannot install syscall restrictions")
    finally:
        lib.seccomp_release(ctx)


def confine(workdir, binary):
    sandbox = Path(__file__).resolve().parents[2] / "ai/code_execution/sandbox"
    sys.path.insert(0, str(sandbox))
    import landlock

    landlock.set_no_new_privs()
    sockets_denied = True
    try:
        _deny_network_sockets()
    except Exception:  # libseccomp missing or refused: Landlock's TCP block must cover it
        sockets_denied = False
    report = landlock.restrict_self(
        read_paths=[
            "/usr", "/lib", "/lib64", "/opt",
            "/etc/libreoffice", "/etc/fonts", "/etc/alternatives", "/etc/ssl",
            "/var/lib/libreoffice", "/var/spool/libreoffice", "/var/cache/fontconfig",
            "/proc/self",
        ],
        rw_paths=[workdir],
        # LibreOffice binds its single-instance pipe in /tmp, whatever TMPDIR says.
        socket_paths=["/tmp"],
        read_files=[
            "/etc/ld.so.cache", "/etc/localtime", "/etc/passwd", "/etc/nsswitch.conf", "/etc/locale.alias",
            "/proc/cpuinfo", "/proc/meminfo", "/proc/version", "/proc/filesystems",
            "/dev/urandom", "/dev/random",
        ],
        rw_files=["/dev/null", "/dev/zero"],
        # execve checks both the executable and its ELF interpreter.
        execute_files=[binary, *glob("/lib*/ld-linux*.so.*"), *glob("/lib/*/ld-linux*.so.*")],
        block_tcp=True,
    )
    # A rule the kernel refused only costs the converter a read (as in the
    # sandbox child); the policy as a whole must be in force.
    if not report.applied:
        raise RuntimeError(f"filesystem confinement unavailable: {report.reason}")
    if not sockets_denied and not report.net_blocked:
        raise RuntimeError("network confinement unavailable (no libseccomp, Landlock ABI < 4)")


def main():
    workdir, binary, args = sys.argv[1], sys.argv[2], sys.argv[3:]
    os.chdir(workdir)
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_LIMIT_SECONDS, CPU_LIMIT_SECONDS + 5))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))
    if sys.platform != "linux":
        raise RuntimeError("unsupported platform")
    confine(workdir, binary)
    os.execv(binary, [binary, *args])


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # setup or exec failed: nothing was converted
        print(f"converter confinement unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        os._exit(CONFINEMENT_UNAVAILABLE_EXIT)
