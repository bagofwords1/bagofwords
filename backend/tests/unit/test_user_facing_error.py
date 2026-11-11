"""user_facing_error: a failure the admin can act on, with nobody's identity in it.

Used for the connection-status log and test results. The message must name
the error (type + text, plus what it was raised from) and must not carry
credentials, the server's identity (hosts, IPs, paths, internal ids) or a
person's identity (emails, usernames).
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
        "email": f"jane.{s}@example.net",
        "ip": f"172.16.{int(s[:2], 16)}.{int(s[2:4], 16)}",
        "uuid": str(uuid.uuid4()),
        "path": f"/srv/bow-{s}/certs/client.pem",
    }


def _secrets(v):
    return connection_secrets(
        {"host": v["host"], "port": 4747, "user_id": v["user"]},
        {"password": v["password"]},
    )


@pytest.mark.parametrize("exc_type", [KeyError, ValueError, OSError, ConnectionError, RuntimeError])
def test_identity_never_survives(exc_type):
    v = _vals()
    exc = exc_type(
        f"{v['uuid']} at {v['host']} ({v['ip']}) user={v['user']} "
        f"password={v['password']} for {v['email']} reading {v['path']}"
    )
    out = user_facing_error(exc, _secrets(v))
    for key in ("host", "user", "password", "email", "ip", "uuid"):
        assert v[key].lower() not in out.lower(), key
    # The directory layout goes; the file name an admin can act on stays.
    assert v["path"] not in out and "client.pem" in out
    assert exc_type.__name__ in out


def test_url_hosts_are_masked_without_known_secrets():
    v = _vals()
    out = user_facing_error(RuntimeError(f"GET https://{v['host']}:4242/qrs/about failed"))
    assert v["host"] not in out
    assert "4242/qrs/about" in out


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
    'password authentication failed for user "***"',
    "relation public.orders does not exist",
    "Read timed out after 10:29:54",
    "SSL: CERTIFICATE_VERIFY_FAILED",
])
def test_diagnostic_wording_is_kept(message):
    assert sanitize_user_message(message) == message


def test_short_identity_values_mask_whole_words_only():
    out = sanitize_user_message("login as admin failed (administrator role required)", ["admin"])
    assert "administrator" in out and " admin " not in out


def test_bare_hostnames_are_masked_but_dotted_identifiers_are_not():
    host = f"bi-{uuid.uuid4().hex[:6]}.intra.example.net"
    out = sanitize_user_message(f"could not resolve {host} for public.orders.customer_id in etl.py")
    assert host not in out
    assert "public.orders.customer_id" in out and "etl.py" in out
