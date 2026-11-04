"""Opt-in live SSH verification. Uses an existing trusted alias; never changes sshd.
Creates and removes a uniquely named fixture under /tmp. No production files read.
Run from repo root with backend/.venv/bin/python tools/remote_server/verify_ssh.py.
"""

import io
import json
import os
import pathlib
import subprocess
import tarfile
import uuid
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.data_sources.clients.remote_server_client import RemoteServerClient  # noqa: E402

alias = os.environ.get("BOW_VERIFY_SSH_ALIAS", "bow")
ssh = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "--", alias]
settings = {}
for line in subprocess.check_output(
    ["ssh", "-G", "--", alias], stderr=subprocess.DEVNULL, text=True
).splitlines():
    k, _, v = line.partition(" ")
    settings.setdefault(k, v)
host = settings["hostname"]
port = int(settings.get("port", 22))
lookup = host if port == 22 else f"[{host}]:{port}"
trusted = subprocess.check_output(["ssh-keygen", "-F", lookup], text=True)
keys = [
    line.split() for line in trusted.splitlines() if line and not line.startswith("#")
]
assert keys, "Existing trusted host key required"
public = " ".join(keys[0][1:3])
remote = "/tmp/bow-connector-verify-" + uuid.uuid4().hex
# Only this uniquely named fixture directory is written on the host.
subprocess.run(ssh + ["mkdir -m 700 " + remote], check=True)
try:
    files = {
        "reader.py": (ROOT / "tools/remote_server/reader.py").read_bytes(),
        "reader.json": json.dumps(
            {
                "allowed_paths": [remote + "/logs"],
                "allowed_operations": [
                    "list_directory",
                    "read_file",
                    "search_file",
                    "system_status",
                ],
                "max_lines": 200,
                "max_output_bytes": 65536,
            }
        ).encode(),
        "entry.py": f'import runpy,sys\nsys.argv=["reader.py", "{remote}/reader.json"]\nrunpy.run_path("{remote}/reader.py",run_name="__main__")\n'.encode(),
        "logs/app.log": b"ready\nerror: verification fixture\nfinished\n",
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(data))
    subprocess.run(ssh + ["tar -xf - -C " + remote], input=buf.getvalue(), check=True)
    kwargs = dict(
        host=host,
        port=port,
        username=settings["user"],
        private_key=pathlib.Path(
            os.path.expanduser(settings["identityfile"])
        ).read_text(),
        host_public_key=public,
        reader_path=remote + "/entry.py",
        allowed_paths=[remote + "/logs"],
        allowed_operations=[
            "list_directory",
            "read_file",
            "search_file",
            "system_status",
        ],
        enable_shell=True,
    )
    c = RemoteServerClient(**kwargs)
    results = {}
    results["connection"] = c.test_connection()["success"]
    with tempfile.TemporaryDirectory(prefix="bow-auth-verify-") as tmp:
        key_copy = pathlib.Path(tmp, "encrypted_key")
        key_copy.write_text(kwargs["private_key"])
        key_copy.chmod(0o600)
        subprocess.run(
            [
                "ssh-keygen",
                "-p",
                "-P",
                "",
                "-N",
                "fixture-only-passphrase",
                "-f",
                str(key_copy),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        encrypted = RemoteServerClient(
            **{
                **kwargs,
                "private_key": key_copy.read_text(),
                "passphrase": "fixture-only-passphrase",
            }
        )
        results["encrypted_key"] = encrypted.test_connection()["success"]
        wrong_key = pathlib.Path(tmp, "wrong_host")
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(wrong_key)],
            check=True,
        )
        wrong = RemoteServerClient(
            **{**kwargs, "host_public_key": wrong_key.with_suffix(".pub").read_text()}
        )
        results["wrong_host_key_denied"] = not wrong.test_connection()["success"]
    for name, args in [
        ("list_directory", {"path": remote + "/logs"}),
        ("read_file", {"path": remote + "/logs/app.log", "limit": 2}),
        ("search_file", {"path": remote + "/logs/app.log", "text": "verification"}),
        ("system_status", {}),
    ]:
        r = c.call_tool(name, args)
        results[name] = r["success"]
        if not r["success"]:
            print(name, r.get("error"))
    results["outside_denied"] = not c.call_tool("read_file", {"path": "/etc/passwd"})[
        "success"
    ]
    args = {"command": "printf bow-verification-ok", "working_directory": "/tmp"}
    results["unapproved_shell_denied"] = not c.call_tool("execute_shell", args)[
        "success"
    ]
    import asyncio

    r = asyncio.run(c.acall_human_approved("execute_shell", args))
    results["approved_shell"] = (
        r["success"] and r["data"]["stdout"] == "bow-verification-ok"
    )
    results["approval_single_use"] = not c.call_tool("execute_shell", args)["success"]
    transport_args = {
        "command": 'printf "%s" "$SSH_CONNECTION"',
        "working_directory": "/tmp",
    }
    first = asyncio.run(c.acall_authorized("execute_shell", transport_args))
    next_client = RemoteServerClient(**kwargs)
    next_client.pool_scope = c.pool_scope
    second = asyncio.run(next_client.acall_authorized("execute_shell", transport_args))
    results["transport_reused"] = (
        first["success"]
        and second["success"]
        and bool(first["data"]["stdout"])
        and first["data"]["stdout"] == second["data"]["stdout"]
    )
    asyncio.run(
        c.acall_authorized(
            "execute_shell",
            {
                "command": "export BOW_FIXTURE_STATE=present",
                "working_directory": "/tmp",
            },
        )
    )
    fresh = asyncio.run(
        c.acall_authorized(
            "execute_shell",
            {
                "command": 'printf "%s" "${BOW_FIXTURE_STATE-unset}"',
                "working_directory": "/tmp",
            },
        )
    )
    results["fresh_shell_state"] = (
        fresh["success"] and fresh["data"]["stdout"] == "unset"
    )
    sh_client = RemoteServerClient(**{**kwargs, "linux_shell": "sh"})
    sh_result = asyncio.run(
        sh_client.acall_authorized(
            "execute_shell",
            {"command": "printf sh-verification", "working_directory": "/tmp"},
        )
    )
    results["posix_shell"] = (
        sh_result["success"] and sh_result["data"]["stdout"] == "sh-verification"
    )
    print(json.dumps(results))
    assert all(results.values())
finally:
    subprocess.run(ssh + ["rm -rf -- " + remote], check=True)
