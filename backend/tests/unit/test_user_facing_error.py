"""user_facing_error: a failure the admin can act on, with no credential in it.

Used for the connection-status log and test results. The message must name
the error (type + text, plus what it was raised from) and keep what the admin
needs to fix it (the host, the user, the table). It must not carry a password,
token or key, nor the server's directory layout.
"""
import uuid

import pytest

from app.errors.redact import connection_secrets, sanitize_user_message, user_facing_error


def _vals():
    s = uuid.uuid4().hex[:6]
    return {
        "host": f"bi-{s}.intra.example.net",
        "user": f"svc_{s}",
        "password": f"P@ss-{s}!",
        "token": f"tok{s}{uuid.uuid4().hex}",
        "table": f"sales_{s}.uk",
        "path": f"/srv/bow-{s}/certs/client.pem",
    }


def _secrets(v):
    return connection_secrets(
        {"host": v["host"], "port": 4747, "api_token": v["token"]},
        {"user": v["user"], "password": v["password"]},
    )


@pytest.mark.parametrize("exc_type", [KeyError, ValueError, OSError, ConnectionError, RuntimeError])
def test_secrets_never_survive_and_the_diagnosis_does(exc_type):
    v = _vals()
    # The secrets are echoed bare, in no shape a pattern would recognise.
    exc = exc_type(
        f"could not reach {v['host']} as {v['user']} ({v['password']}, {v['token']}): "
        f"relation {v['table']} does not exist, reading {v['path']}"
    )
    out = user_facing_error(exc, _secrets(v))
    for key in ("password", "token"):
        assert v[key] not in out, key
    for key in ("host", "user", "table"):
        assert v[key] in out, key
    # The directory layout goes; the file name an admin can act on stays.
    assert v["path"] not in out and "client.pem" in out
    assert exc_type.__name__ in out


def test_credentials_in_urls_and_pairs_are_masked_without_known_secrets():
    v = _vals()
    out = user_facing_error(RuntimeError(
        f"GET https://{v['user']}:{v['password']}@{v['host']}:4242/qrs/about failed; "
        f"token={v['token']}"
    ))
    assert v["password"] not in out and v["token"] not in out
    assert v["host"] in out and "4242/qrs/about" in out


def test_only_secret_values_of_a_connection_are_collected():
    v = _vals()
    secrets = connection_secrets(
        {"host": v["host"], "account": "acme", "api_token": v["token"]},
        {"user": v["user"], "password": v["password"], "auth_type": "basic",
         "token_type": "Bearer", "client_id": "abc123", "nested": {"private_key": "k" * 20}},
    )
    assert set(secrets) == {v["token"], v["password"], "k" * 20}


def test_terse_errors_get_their_type():
    assert "TimeoutError" in user_facing_error(TimeoutError())
    assert user_facing_error(KeyError("syncing_agents")).startswith("KeyError")


def test_the_underlying_cause_is_included():
    try:
        try:
            raise ConnectionRefusedError("connection refused")
        except ConnectionRefusedError as inner:
            raise RuntimeError("Engine websocket failed") from inner
    except RuntimeError as outer:
        out = user_facing_error(outer)
    assert "Engine websocket failed" in out
    assert "ConnectionRefusedError" in out and "connection refused" in out


@pytest.mark.parametrize("message", [
    'password authentication failed for user "svc_reader"',
    "relation sales.uk does not exist",
    "could not translate host name db.acme.dev to address",
    "Workspace 3f2a9c1e-1111-2222-3333-444455556666 not found (10.1.2.3)",
    "Read timed out after 10:29:54",
    "SSL: CERTIFICATE_VERIFY_FAILED",
])
def test_diagnostic_wording_is_kept(message):
    assert sanitize_user_message(message, ["Bearer-secret-value"]) == message


def test_a_huge_message_is_bounded():
    out = sanitize_user_message("x" * 200_000)
    assert len(out) < 1000
