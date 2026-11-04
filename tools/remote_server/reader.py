#!/usr/bin/env python3
"""One-request Linux read-only protocol. Install root-owned; force this command in sshd.
Configuration is local, never supplied by the SSH client. No shell or subprocesses.
"""

import json
import os
import signal
import stat
import sys
from pathlib import PurePosixPath

MAX_BYTES = 1048576
MAX_LINES = 2000


def open_scoped(path, roots, directory=False):
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or "\x00" in path
        or ".." in path.split("/")
    ):
        raise PermissionError("Absolute path within an approved root required")
    path = str(PurePosixPath(path))
    if not any(
        path == r.rstrip("/") or path.startswith(r.rstrip("/") + "/") for r in roots
    ):
        raise PermissionError("Path outside server-approved roots")
    # Walk every component via directory FDs: no symlink traversal or check/open race.
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        parts = [p for p in path.split("/") if p]
        for i, part in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if i < len(parts) - 1 or directory:
                flags |= os.O_DIRECTORY
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        mode = os.fstat(fd).st_mode
        if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
            raise PermissionError("Only regular files or directories are readable")
        return fd
    except BaseException:
        os.close(fd)
        raise


def handle(request, config):
    op = request.get("operation")
    allowed = config.get(
        "allowed_operations",
        ["list_directory", "read_file", "search_file", "system_status"],
    )
    if op == "capabilities":
        return {
            "success": True,
            "content_type": "json",
            "data": {
                "protocol": 1,
                "operations": allowed,
                "roots": config.get("allowed_paths", []),
            },
        }
    if op not in allowed:
        raise PermissionError("Operation disabled by server administrator")
    known = {"operation", "path", "limit", "tail", "text", "max_bytes"}
    if set(request) - known:
        raise ValueError("Unknown request fields")
    limit = min(
        int(request.get("limit", 200)),
        int(config.get("max_lines", MAX_LINES)),
        MAX_LINES,
    )
    budget = min(
        int(request.get("max_bytes", 65536)),
        int(config.get("max_output_bytes", MAX_BYTES)),
        MAX_BYTES,
    )
    if limit < 1 or budget < 1:
        raise ValueError("Limits must be positive")
    if op == "system_status":
        # No Docker socket, process environments or arbitrary command execution.
        data = {
            "platform": os.uname().sysname,
            "load_average": os.getloadavg(),
            "cpu_count": os.cpu_count(),
        }
        with open("/proc/meminfo") as f:
            data["memory"] = "".join(f.readlines()[:5])
        with open("/proc/uptime") as f:
            data["uptime_seconds"] = f.read(128).split()[0]
        disks = {}
        for root in config.get("allowed_paths", [])[:20]:
            fd = open_scoped(root, config["allowed_paths"], directory=True)
            try:
                v = os.fstatvfs(fd)
                disks[root] = {
                    "total_bytes": v.f_blocks * v.f_frsize,
                    "available_bytes": v.f_bavail * v.f_frsize,
                }
            finally:
                os.close(fd)
        data["disks"] = disks
    elif op == "list_directory":
        fd = open_scoped(
            request["path"], config.get("allowed_paths", []), directory=True
        )
        data = []
        used = 0
        try:
            with os.scandir(fd) as entries:
                for entry in entries:
                    item = {
                        "name": entry.name,
                        "directory": entry.is_dir(follow_symlinks=False),
                        "symlink": entry.is_symlink(),
                    }
                    used += len(json.dumps(item).encode())
                    if len(data) >= limit or used > budget:
                        break
                    data.append(item)
        finally:
            os.close(fd)
    else:
        fd = open_scoped(request["path"], config.get("allowed_paths", []))
        with os.fdopen(fd, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            if request.get("tail", op == "read_file"):
                f.seek(max(0, size - budget))
            raw = f.read(budget)
        text = raw.decode("utf-8", "replace")
        lines = text.splitlines()
        if op == "search_file":
            needle = request.get("text")
            if not isinstance(needle, str) or not 1 <= len(needle) <= 512:
                raise ValueError("Search requires bounded literal text")
            lines = [line for line in lines if needle in line][:limit]
        else:
            lines = lines[-limit:] if request.get("tail", True) else lines[:limit]
        data = {
            "text": "\n".join(lines),
            "file_bytes": size,
            "scanned_bytes": len(raw),
            "byte_window_truncated": size > len(raw),
            "note": "Search covers only the bounded byte window. Reads may include partial boundary lines.",
        }
    return {"success": True, "content_type": "json", "data": data}


def main():
    signal.alarm(120)
    try:
        config_path = sys.argv[1] if len(sys.argv) == 2 else "/etc/bow-reader.json"
        with open(config_path) as f:
            config = json.load(f)
        raw = sys.stdin.buffer.readline(32769)
        if len(raw) > 32768:
            raise ValueError("Request too large")
        result = handle(json.loads(raw), config)
    except Exception as exc:
        result = {"success": False, "content_type": "text", "error": str(exc)[:500]}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
