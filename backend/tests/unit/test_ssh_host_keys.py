"""SSH discovery is bounded, credential-free, and never implies trust."""

import base64
import hashlib
from ipaddress import ip_address
from unittest.mock import patch

import pytest

from app.errors import AppError
from app.services.ssh_host_keys import SSHHostKeyRequest, discover_host_keys

KEY_BYTES = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"x" * 32
KEY = "ssh-ed25519 " + base64.b64encode(KEY_BYTES).decode()


def test_discovery_returns_fingerprint_without_authentication_or_trust():
    with (
        patch("app.core.egress.check_host", return_value=[ip_address("192.0.2.7")]),
        patch(
            "app.services.ssh_host_keys.run_bounded", return_value=(0, f"# banner\nserver {KEY}\n".encode(), b"")
        ) as scan,
    ):
        result = discover_host_keys(SSHHostKeyRequest(host="server.example"))
    assert result["host"] == "server.example"
    assert result["keys"] == [
        {
            "public_key": KEY,
            "fingerprint": "SHA256:" + base64.b64encode(hashlib.sha256(KEY_BYTES).digest()).decode().rstrip("="),
            "algorithm": "ssh-ed25519",
        }
    ]
    argv = scan.call_args.args[0]
    assert argv[0] == "ssh-keyscan" and argv[-1] == "192.0.2.7"
    assert "server.example" not in argv
    assert scan.call_args.kwargs.get("data", b"") == b""
    assert result["trusted"] is False


@pytest.mark.parametrize("host", ["-oProxyCommand=bad", "user@host", "http://host", "host;id"])
def test_discovery_rejects_non_host_input(host):
    with pytest.raises(ValueError):
        SSHHostKeyRequest(host=host)


def test_discovery_rejects_metadata_before_starting_process():
    with (
        patch("app.services.ssh_host_keys.run_bounded", side_effect=AssertionError("must not run")),
        pytest.raises(AppError),
    ):
        discover_host_keys(SSHHostKeyRequest(host="169.254.169.254"))


@pytest.mark.parametrize("output", [b"", b"host ssh-ed25519 invalid", b"host ssh-ed25519 YWJj"])
def test_discovery_never_offers_invalid_key(output):
    with (
        patch("app.core.egress.check_host", return_value=[ip_address("192.0.2.7")]),
        patch("app.services.ssh_host_keys.run_bounded", return_value=(0, output, b"")),
        pytest.raises(AppError),
    ):
        discover_host_keys(SSHHostKeyRequest(host="server.example"))


def test_missing_host_key_has_actionable_error_without_validator_details():
    from app.data_sources.clients.remote_server_client import RemoteServerClient

    with pytest.raises(ValueError) as error:
        RemoteServerClient(host="server.example", username="reader", private_key="secret")
    assert "Verify" in str(error.value)
    assert "pydantic" not in str(error.value) and "secret" not in str(error.value)
