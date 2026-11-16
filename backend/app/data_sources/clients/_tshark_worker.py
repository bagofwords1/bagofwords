"""Run the offline decoder with an inherited capture fd and an empty environment.

Invoked as an isolated Python subprocess, so changing credentials and process
limits never affects other threads in the application worker.
"""

import ctypes
from glob import glob
import os
from pathlib import Path
import resource
import sys

# Exit status when the decoder cannot be started inside the sandbox (setup
# failed or the exec was refused). Must match _pcap_parquet.SANDBOX_UNAVAILABLE_EXIT.
SANDBOX_UNAVAILABLE_EXIT = 78
CPU_LIMIT_SECONDS = 300


def linux_sandbox(binary):
    sandbox = Path(__file__).resolve().parents[2] / "ai/code_execution/sandbox"
    sys.path.insert(0, str(sandbox))
    import landlock
    import seccomp

    # Refuse internet, raw and Unix sockets, including on older Landlock ABIs.
    lib = ctypes.CDLL("libseccomp.so.2")
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    ctx = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not ctx:
        raise RuntimeError("Cannot initialize decoder syscall restrictions")
    try:
        for name in (b"socket", b"socketpair", b"ptrace", b"process_vm_readv", b"process_vm_writev"):
            nr = lib.seccomp_syscall_resolve_name(name)
            if nr < 0 or lib.seccomp_rule_add(ctx, 0x00050001, nr, 0):  # SCMP_ACT_ERRNO(EPERM)
                raise RuntimeError("Cannot restrict decoder syscalls")
        if lib.seccomp_load(ctx):
            raise RuntimeError("Cannot install decoder syscall restrictions")
    finally:
        lib.seccomp_release(ctx)
    report = landlock.restrict_self(
        read_paths=["/usr/lib", "/lib", "/lib64", "/usr/lib64", "/usr/share/wireshark", "/usr/share/mime"],
        read_files=["/etc/ld.so.cache", "/dev/null", "/dev/urandom", "/etc/localtime"],
        # execve checks both the executable and its ELF interpreter.
        execute_files=[binary, *glob("/lib*/ld-linux*.so.*"), *glob("/lib/*/ld-linux*.so.*")],
    )
    if not report.applied or report.rule_errors:
        raise RuntimeError(f"Decoder filesystem sandbox unavailable: {report.reason or report.rule_errors}")
    seccomp.install_no_process_creation()


def main():
    binary = sys.argv[1]
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_LIMIT_SECONDS, CPU_LIMIT_SECONDS + 5))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    argv = [binary, "-n", "-r", "-", "-T", "pdml"]
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (2 << 30, 2 << 30))
        if os.getuid() == 0:
            import pwd

            nobody = pwd.getpwnam("nobody")
            os.setgroups([])
            os.setgid(nobody.pw_gid)
            os.setuid(nobody.pw_uid)
        linux_sandbox(binary)
    elif sys.platform == "darwin":
        profile = """(version 1)(deny default)(import "system.sb")(deny network*)(deny process-fork)
          (allow process-exec (literal (param "DECODER")))
          (allow sysctl-read mach-lookup file-read-metadata)
          (allow file-read* (subpath "/System") (subpath "/usr/lib") (subpath "/usr/share")
            (subpath "/opt/homebrew/Cellar") (subpath "/opt/homebrew/opt")
            (subpath "/usr/local/Cellar") (subpath "/usr/local/opt")
            (subpath "/Applications/Wireshark.app") (subpath "/Library/Apple")
            (literal "/dev/random") (literal "/dev/urandom") (literal "/dev/null"))"""
        argv = ["/usr/bin/sandbox-exec", "-D", "DECODER=" + binary, "-p", profile, *argv]
    else:
        raise RuntimeError("Unsupported decoder sandbox platform")
    os.execv(argv[0], argv)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # setup or exec failed: nothing was decoded
        print(f"decoder sandbox unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        os._exit(SANDBOX_UNAVAILABLE_EXIT)
