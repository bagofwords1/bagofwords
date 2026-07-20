"""The credential timestamp columns are naive (TIMESTAMP WITHOUT TIME ZONE);
asyncpg rejects a tz-aware datetime for a naive column, so a tz-aware
last_used_at / expires_at only breaks on Postgres (SQLite is lax). The model
coerces aware datetimes to naive UTC so every caller is safe.
"""

from datetime import datetime, timedelta, timezone

from app.models.user_connection_credentials import UserConnectionCredentials


def _cred(**kw):
    return UserConnectionCredentials(
        connection_id="c", user_id="u", organization_id="o",
        auth_mode="oauth", encrypted_credentials="x", **kw,
    )


def test_tz_aware_last_used_at_is_coerced_to_naive_utc():
    cred = _cred(last_used_at=datetime.now(timezone.utc))
    assert cred.last_used_at.tzinfo is None


def test_tz_aware_expires_at_is_coerced_to_naive_utc():
    aware = datetime.now(timezone.utc) + timedelta(hours=1)
    cred = _cred(expires_at=aware)
    assert cred.expires_at.tzinfo is None
    # value preserved (converted to UTC wall-clock), just naive
    assert abs((cred.expires_at - aware.replace(tzinfo=None)).total_seconds()) < 1


def test_naive_and_none_pass_through():
    cred = _cred(last_used_at=datetime.utcnow(), expires_at=None)
    assert cred.last_used_at.tzinfo is None
    assert cred.expires_at is None


def test_non_utc_aware_is_converted_to_utc_naive():
    # A +02:00 time must be shifted to UTC before dropping the tzinfo.
    tz = timezone(timedelta(hours=2))
    cred = _cred(last_used_at=datetime(2026, 1, 1, 12, 0, tzinfo=tz))
    assert cred.last_used_at.tzinfo is None
    assert cred.last_used_at.hour == 10  # 12:00+02:00 -> 10:00 UTC
