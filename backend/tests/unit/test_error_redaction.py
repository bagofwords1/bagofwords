"""redact_error / redact_paths: secrets and server paths never survive into
error text shown to users, while the diagnostic wording an admin needs does."""
import pytest

from app.errors.redact import MASK, redact_error, redact_paths


@pytest.mark.parametrize(
    "message, secret",
    [
        ("fatal: could not read from https://oauth2:ghp_AbC123xyz@github.com/o/r.git", "ghp_AbC123xyz"),
        ("clone failed for https://deploy-token-9f8e7d@gitlab.example.com/g/p", "deploy-token-9f8e7d"),
        ("could not connect: postgresql://bob:Hunter2pw@db.internal:5432/app", "Hunter2pw"),
        ("connection failed: host=db port=5432 user=bob password=Hunter2pw dbname=x", "Hunter2pw"),
        ("DSN 'Driver={ODBC};Server=s;UID=u;PWD=s3cr3tValue;'", "s3cr3tValue"),
        ('upstream 401: {"api_key": "sk-live-42abcdef", "ok": false}', "sk-live-42abcdef"),
        ("request failed with header Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc", "eyJhbGciOiJIUzI1NiJ9.abc"),
        ("GET /token?client_secret=zzTopSecret99&x=1 failed", "zzTopSecret99"),
    ],
)
def test_secret_shapes_are_masked(message, secret):
    out = redact_error(Exception(message))
    assert secret not in out
    assert MASK in out


@pytest.mark.parametrize(
    "message",
    [
        'password authentication failed for user "analytics"',
        "could not translate host name \"db.prod\" to address",
        "Invalid token: expired",
        "Invalid credentials: connection refused",
        "Table 'sales' doesn't exist",
    ],
)
def test_diagnostic_wording_is_kept(message):
    assert redact_error(Exception(message)) == message


def test_known_secret_values_are_masked_wherever_they_appear():
    out = redact_error(Exception("driver echoed Pa55word!x in its dump"), secrets=["Pa55word!x", None, ""])
    assert "Pa55word!x" not in out
    assert "driver echoed" in out


def test_length_is_capped():
    assert len(redact_error(Exception("x" * 10_000))) <= 501


def test_absolute_paths_reduced_to_file_name():
    out = redact_paths("could not load /tmp/tmpq1w2e3/deck_42.pptx; see /opt/app/backend/uploads/x.png")
    assert "/tmp" not in out and "/opt" not in out
    assert "deck_42.pptx" in out and "x.png" in out


def test_non_paths_untouched():
    text = "see https://example.com/a/b, ratio 1/2, ./rel/file"
    assert redact_paths(text) == text
