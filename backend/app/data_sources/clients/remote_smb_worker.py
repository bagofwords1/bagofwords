"""Isolated SMB reader: one request, no global shared credentials, no writes."""

import json
import ntpath
import stat
import sys


def execute(payload, connection_cache=None):
    import smbclient
    from smbprotocol.file_info import FileAttributes

    c = payload["config"]
    # Process-local DNS pinning: preserve the hostname/SPN, prevent a second DNS
    # lookup from redirecting this credential to a different endpoint.
    import socket

    original_getaddrinfo = socket.getaddrinfo

    def pinned_getaddrinfo(host, port, *args, **kwargs):
        if str(host).lower() != c["host"].lower():
            raise PermissionError("SMB referrals to other servers are disabled")
        return original_getaddrinfo(payload["connect_ip"], port, *args, **kwargs)

    socket.getaddrinfo = pinned_getaddrinfo
    creds = payload["credentials"]
    # Disable DFS: a log reader must not forward its credential to referral hosts.
    smbclient.ClientConfig(skip_dfs=True)
    cache = connection_cache if connection_cache is not None else {}
    kwargs = {"connection_cache": cache, "port": c["port"]}
    try:
        smbclient.register_session(
            c["host"],
            username=creds.get("username") if payload["auth_type"] == "ntlm" else None,
            password=creds.get("password"),
            auth_protocol=payload["auth_type"],
            encrypt=c["require_encryption"],
            connection_timeout=min(c["timeout_seconds"], 15),
            **kwargs,
        )
        root = "\\\\" + c["host"] + "\\" + c["share"]
        args = payload["arguments"]
        operation = payload["operation"]

        def scoped(relative):
            if ntpath.isabs(relative) or ":" in relative or ".." in relative.replace("\\", "/").split("/"):
                raise PermissionError("Share-relative path required")
            path = root
            for component in relative.replace("/", "\\").split("\\"):
                if component in ("", "."):
                    continue
                path += "\\" + component
                info = smbclient.stat(path, follow_symlinks=False, **kwargs)
                if info.st_file_attributes & FileAttributes.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise PermissionError("Reparse points are not supported")
            return path

        if operation == "capabilities":
            for folder in c["allowed_paths"]:
                path = scoped(folder)
                with smbclient.scandir(path, **kwargs) as entries:
                    next(iter(entries), None)
            data = {"protocol": 1}
        elif operation == "list_directory":
            data = []
            used = 0
            for entry in smbclient.scandir(scoped(args["path"]), **kwargs):
                item = {"name": entry.name, "directory": entry.is_dir(follow_symlinks=False)}
                used += len(json.dumps(item).encode())
                if len(data) >= args.get("limit", c["max_lines"]) or used > c["max_output_bytes"]:
                    break
                data.append(item)
        elif operation in ("read_file", "search_file"):
            path = scoped(args["path"])
            info = smbclient.stat(path, follow_symlinks=False, **kwargs)
            if not stat.S_ISREG(info.st_mode):
                raise PermissionError("Only regular files are supported")
            with smbclient.open_file(path, mode="rb", **kwargs) as f:
                prefix = f.read(3)
                encoding = (
                    "utf-16-le"
                    if prefix.startswith(b"\xff\xfe")
                    else "utf-16-be"
                    if prefix.startswith(b"\xfe\xff")
                    else "utf-8-sig"
                )
                size = f.seek(0, 2)
                start = max(0, size - c["max_output_bytes"]) if args.get("tail", operation == "read_file") else 0
                if encoding.startswith("utf-16") and start % 2:
                    start += 1
                f.seek(start)
                raw = f.read(c["max_output_bytes"])
            lines = raw.decode(encoding, "replace").lstrip("\ufeff").splitlines()
            limit = args.get("limit", c["max_lines"])
            if operation == "search_file":
                lines = [line for line in lines if args["text"] in line][:limit]
            else:
                lines = lines[-limit:] if args.get("tail", True) else lines[:limit]
            data = {
                "text": "\n".join(lines),
                "file_bytes": size,
                "scanned_bytes": len(raw),
                "byte_window_truncated": size > len(raw),
                "note": "Search is bounded to the byte window; use specific rotated files to inspect other periods.",
            }
        else:
            raise PermissionError("Unsupported SMB operation")
        return {"success": True, "content_type": "json", "data": data}
    finally:
        try:
            if connection_cache is None:
                smbclient.reset_connection_cache(connection_cache=cache)
        finally:
            socket.getaddrinfo = original_getaddrinfo


if __name__ == "__main__":
    import smbclient

    cache = {}
    try:
        for line in sys.stdin:
            try:
                result = execute(json.loads(line), cache)
            except Exception:
                smbclient.reset_connection_cache(connection_cache=cache)
                result = {
                    "success": False,
                    "content_type": "text",
                    "error": "SMB request failed. Check authentication, read permissions, path, encryption and network connectivity.",
                }
            print(json.dumps(result), flush=True)
    finally:
        smbclient.reset_connection_cache(connection_cache=cache)
