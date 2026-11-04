import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest

from app.data_sources.clients.remote_server_client import RemoteServerClient, RemoteSMBClient
from app.schemas.data_source_registry import resolve_client_class

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("bow_reader", ROOT / "tools/remote_server/reader.py")
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


@pytest.mark.parametrize("kind", ["remote_ssh", "remote_smb"])
def test_remote_connectors_resolve_as_tools(kind):
    from app.data_sources.clients.tool_provider_base import ToolProviderClient

    assert issubclass(resolve_client_class(kind), ToolProviderClient)


def ssh_client(**overrides):
    return RemoteServerClient(
        host="server.example",
        host_public_key="ssh-ed25519 YWJj",
        username="reader",
        private_key="test-only-key",
        allowed_paths=["/logs"],
        **overrides,
    )


@pytest.mark.parametrize("path", ["/etc/passwd", "/logs-other/file", "/logs/../secret", "/logs/\x00"])
def test_out_of_scope_requests_never_connect(path):
    with patch("subprocess.Popen", side_effect=AssertionError("Must not connect")):
        result = ssh_client().call_tool("read_file", {"path": path})
    assert not result["success"]


@pytest.mark.parametrize("enabled", [True, False])
def test_unapproved_shell_never_connects(enabled):
    with patch("subprocess.Popen", side_effect=AssertionError("Must not connect")):
        result = ssh_client(enable_shell=enabled).call_tool(
            "execute_shell", {"command": "id", "working_directory": "/"}
        )
    assert not result["success"]


@pytest.mark.parametrize("operation", ["rm", "execute_shell", "system_status"])
def test_disabled_operations_cannot_be_called(operation):
    result = ssh_client(allowed_operations=[]).call_tool(operation, {})
    assert not result["success"]


def test_reader_reads_bounded_logs_and_literal_search(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("ready\nerror [xyz]\nfinished\n")
    config = {"allowed_paths": [str(tmp_path)], "max_lines": 2}
    result = reader.handle({"operation": "read_file", "path": str(log), "limit": 100}, config)
    assert result["success"]
    assert len(result["data"]["text"].splitlines()) <= 2
    result = reader.handle({"operation": "search_file", "path": str(log), "text": "[xyz]"}, config)
    assert result["data"]["text"] == "error [xyz]"


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_reader_rejects_symlink_escapes(tmp_path, kind):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("secret")
    link = allowed / "link"
    link.symlink_to(outside / "secret" if kind == "file" else outside)
    path = link if kind == "file" else link / "secret"
    with pytest.raises((PermissionError, OSError)):
        reader.handle({"operation": "read_file", "path": str(path)}, {"allowed_paths": [str(allowed)]})


def test_reader_rejects_fifo_without_blocking(tmp_path):
    import os

    path = tmp_path / "pipe"
    os.mkfifo(path)
    with pytest.raises(PermissionError):
        reader.handle({"operation": "read_file", "path": str(path)}, {"allowed_paths": [str(tmp_path)]})


def test_reader_never_interprets_shell_metacharacters(tmp_path):
    path = tmp_path / "log; touch marker"
    path.write_text("literal filename")
    result = reader.handle({"operation": "read_file", "path": str(path)}, {"allowed_paths": [str(tmp_path)]})
    assert result["data"]["text"] == "literal filename"
    assert not (tmp_path / "marker").exists()


@pytest.mark.parametrize("path", ["..\\secret", "C:\\secret", "\\\\other\\share\\secret", "AppsLogs\\log:secret"])
def test_smb_rejects_escape_and_alternate_streams(path):
    c = RemoteSMBClient(
        host="files.example", share="Logs", username="DOMAIN\\reader", password="test", allowed_paths=["AppsLogs"]
    )
    with patch("subprocess.Popen", side_effect=AssertionError("Must not connect")):
        result = c.call_tool("read_file", {"path": path})
    assert not result["success"]


def test_subprocess_output_is_bounded():
    import sys

    from app.data_sources.clients.remote_process import run_bounded

    with pytest.raises(ValueError):
        run_bounded([sys.executable, "-c", 'print("x" * 10000)'], limit=100, timeout=3)


def test_subprocess_drains_stdout_and_stderr():
    import sys

    from app.data_sources.clients.remote_process import run_bounded

    code, out, err = run_bounded(
        [sys.executable, "-c", 'import sys; print(sys.stdin.read()); print("error", file=sys.stderr)'],
        data=b"input",
        limit=100,
        timeout=3,
    )
    assert code == 0 and b"input" in out and b"error" in err


@pytest.mark.parametrize("pattern", ["*", "{root}/*.log", "{root}"])
def test_agentless_reads_without_installing_reader(tmp_path, pattern):
    import subprocess
    import sys
    import shlex

    log = tmp_path / "service.log"
    log.write_text("ready\nerror: synthetic\n")
    client = RemoteServerClient(
        host="server.example",
        host_public_key="ssh-ed25519 YWJj",
        username="reader",
        private_key="test-only",
        allowed_paths=[pattern.format(root=tmp_path)],
    )

    def local_transport(command, payload=b""):
        argv = shlex.split(command)
        assert argv[:3] == ["python3", "-I", "-c"]
        result = subprocess.run([sys.executable, *argv[1:]], input=payload, capture_output=True, timeout=5)
        return result.returncode, result.stdout, result.stderr

    with patch.object(client, "_ssh", side_effect=local_transport):
        result = client.call_tool("read_file", {"path": str(log)})
        assert result["success"], result
        assert "synthetic" in result["data"]["text"]
        result = client.call_tool("search_file", {"path": str(log), "text": "error"})
        assert result["success"] and "error" in result["data"]["text"]
    assert client.config.inspection_mode == "agentless"
    shell = next(t for t in client.list_tools() if t["name"] == "execute_shell")
    assert shell["explicit_policy"] == "ask"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["service.log"]


@pytest.mark.parametrize("path", ["/logs/../secret", "relative.log", "/logs/\x00bad"])
def test_all_paths_wildcard_still_rejects_invalid_paths(path):
    client = RemoteServerClient(
        host="server.example",
        host_public_key="ssh-ed25519 YWJj",
        username="reader",
        private_key="test-only",
        allowed_paths=["*"],
    )
    with patch.object(client, "_ssh", side_effect=AssertionError("Must not connect")):
        assert not client.call_tool("read_file", {"path": path})["success"]


def test_wildcard_does_not_allow_nonmatching_file(tmp_path):
    file = tmp_path / "secret.txt"
    file.write_text("private")
    with pytest.raises(PermissionError):
        reader.handle({"operation": "read_file", "path": str(file)}, {"allowed_paths": [str(tmp_path / "*.log")]})


def test_restricted_reader_remains_explicit_option():
    client = ssh_client(inspection_mode="restricted_reader", reader_path="/opt/reader.py")
    with patch.object(client, "_ssh", return_value=(0, b'{"success":true}', b"")) as transport:
        assert client.test_connection()["success"]
    assert transport.call_args.args[0] == "python3 -I /opt/reader.py"


def test_windows_agentless_uses_inline_powershell():
    import base64

    client = ssh_client(operating_system="windows")
    with patch.object(client, "_ssh", return_value=(0, b'{"success":true}', b"")) as transport:
        assert client.test_connection()["success"]
    command = transport.call_args.args[0]
    source = base64.b64decode(command.split()[-1]).decode("utf-16le")
    assert "-EncodedCommand" in command
    assert "Get-Content -LiteralPath" not in source
    import json

    payload = json.loads(transport.call_args.args[1])
    assert "ReadToEnd" in source
    assert "Get-Content -LiteralPath" not in payload["source"]
    assert "$config = $payload.config" in payload["source"]
    assert payload["config"]["inspection_mode"] == "agentless"
