"""Unit tests for CustomApiClient.test_connection status handling.

A reachable host that answers with an error status (404 wrong base URL,
401/403 bad credentials, 5xx) must NOT be reported as a connected/success —
otherwise the UI shows a green "Connected (HTTP 404)" for a broken connection.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.data_sources.clients.custom_api_client import CustomApiClient


@pytest.mark.parametrize(
    "status,expected_success",
    [
        (200, True),
        (204, True),
        (302, True),
        (405, True),   # HEAD not allowed on base path, but host/path is reachable
        (400, False),
        (401, False),
        (403, False),
        (404, False),
        (500, False),
    ],
)
def test_test_connection_status(status, expected_success):
    client = CustomApiClient(base_url="https://api.example.com/v1")
    with patch("httpx.Client") as MockClient:
        inst = MockClient.return_value.__enter__.return_value
        inst.head.return_value = MagicMock(status_code=status)
        result = client.test_connection()
    assert result["success"] is expected_success
    assert str(status) in result["message"]


def test_test_connection_network_error():
    client = CustomApiClient(base_url="https://nope.invalid")
    with patch("httpx.Client") as MockClient:
        inst = MockClient.return_value.__enter__.return_value
        inst.head.side_effect = RuntimeError("name resolution failed")
        result = client.test_connection()
    assert result["success"] is False
    assert "Failed to connect" in result["message"]
