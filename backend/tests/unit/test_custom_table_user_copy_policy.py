"""When a user's own copy of a custom table may be served, and what ties it
to the login it was built with (app/data_sources/fast/user_copies.py)."""
import base64
import json
from datetime import datetime, timedelta

import pytest

from app.data_sources.fast import user_copies
from app.data_sources.fast.user_copies import credential_fingerprint, evaluate, schedule_due_at

NOW = datetime(2026, 11, 27, 12, 0, 0)
DAY = 86400


def _copy(**over):
    base = dict(
        status="ok", has_artifact=True, refreshed_at=NOW - timedelta(minutes=5),
        invalidated_at=None, copy_definition_hash="d1", definition_hash="d1",
        copy_fingerprint="f1", fingerprint="f1", now=NOW, max_age_seconds=DAY,
    )
    base.update(over)
    return evaluate(**base)


def _jwt(**claims) -> str:
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


def test_a_recent_copy_built_from_the_same_query_and_login_is_served_as_is():
    state = _copy()
    assert state.servable and not state.rebuild


@pytest.mark.parametrize("change", [
    {"has_artifact": False},
    {"refreshed_at": None},
    {"status": "evicted"},
    {"copy_definition_hash": "an older query"},
    {"copy_fingerprint": "another login"},
    {"invalidated_at": NOW - timedelta(minutes=1)},
    {"refreshed_at": NOW - timedelta(seconds=DAY + 1)},
])
def test_a_copy_that_is_missing_or_built_from_something_else_is_never_served(change):
    state = _copy(**change)
    assert not state.servable and state.rebuild and state.reason


def test_an_invalidation_older_than_the_build_no_longer_applies():
    built = NOW - timedelta(minutes=5)
    assert _copy(refreshed_at=built, invalidated_at=built - timedelta(seconds=1)).fresh
    assert not _copy(refreshed_at=built, invalidated_at=built).servable


@pytest.mark.parametrize("stale", [
    {"stale_before": NOW - timedelta(minutes=1)},
    {"refresh_requested_at": NOW - timedelta(minutes=1)},
])
def test_a_copy_past_its_schedule_or_asked_to_refresh_is_served_and_rebuilt(stale):
    state = _copy(**stale)
    assert state.servable and state.rebuild


def test_the_maximum_age_wins_over_a_longer_schedule():
    # The table says "weekly"; the organization says a copy lives a day.
    state = _copy(refreshed_at=NOW - timedelta(days=2), stale_before=NOW - timedelta(days=7))
    assert not state.servable


def test_a_reader_that_cannot_resolve_credentials_skips_only_that_check():
    assert _copy(fingerprint=None, copy_fingerprint="whatever").fresh
    assert not _copy(fingerprint=None, copy_definition_hash="other").servable


def test_a_failed_rebuild_does_not_take_a_still_valid_copy_away():
    assert _copy(status="error").servable
    assert _copy(status="building").servable


# -- which login a copy belongs to --------------------------------------------

def test_the_same_login_fingerprints_the_same_and_another_login_does_not():
    alice = {"user": "alice", "password": "pw-a"}
    assert credential_fingerprint(alice, "k") == credential_fingerprint(dict(alice), "k")
    assert credential_fingerprint(alice, "k") != credential_fingerprint({"user": "bob", "password": "pw-a"}, "k")
    assert credential_fingerprint(alice, "k") != credential_fingerprint({"user": "alice", "password": "new"}, "k")


def test_the_fingerprint_is_keyed_and_does_not_contain_the_secret():
    creds = {"user": "alice", "password": "correct horse"}
    one, other = credential_fingerprint(creds, "key-1"), credential_fingerprint(creds, "key-2")
    assert one != other
    assert "correct horse" not in one and "alice" not in one


def test_refreshing_a_token_keeps_the_identity_and_another_account_changes_it():
    first = {"access_token": _jwt(tid="t", oid="user-1", exp=1), "refresh_token": "r1", "expires_at": 1}
    renewed = {"access_token": _jwt(tid="t", oid="user-1", exp=2), "refresh_token": "r2", "expires_at": 2}
    someone_else = {"access_token": _jwt(tid="t", oid="user-2", exp=2), "refresh_token": "r3"}
    assert credential_fingerprint(first, "k") == credential_fingerprint(renewed, "k")
    assert credential_fingerprint(first, "k") != credential_fingerprint(someone_else, "k")


def test_a_token_that_names_no_account_is_its_own_identity():
    assert credential_fingerprint({"access_token": "opaque-1"}, "k") != \
        credential_fingerprint({"access_token": "opaque-2"}, "k")


def test_missing_credentials_still_fingerprint():
    assert credential_fingerprint(None, "k") == credential_fingerprint({}, "k")


# -- the table's own schedule ---------------------------------------------------

def test_an_interval_schedule_makes_anything_older_than_the_interval_stale():
    assert schedule_due_at(NOW, "interval", 60, None) == NOW - timedelta(minutes=60)
    assert schedule_due_at(NOW, "interval", None, None) is None


def test_a_time_of_day_schedule_is_the_last_time_that_hour_passed():
    assert schedule_due_at(NOW, "time", None, "09:00") == NOW.replace(hour=9)
    # 15:00 has not come yet today: the last one was yesterday.
    assert schedule_due_at(NOW, "time", None, "15:00") == NOW.replace(hour=15) - timedelta(days=1)
    assert schedule_due_at(NOW, "time", None, "not a time") is None


def test_a_time_of_day_schedule_is_read_in_the_organizations_zone():
    from zoneinfo import ZoneInfo

    # 12:00 UTC is 14:00 in Jerusalem (UTC+2 in November): 13:00 there has passed.
    due = schedule_due_at(NOW, "time", None, "13:00", ZoneInfo("Asia/Jerusalem"))
    assert due == NOW.replace(hour=11)


def test_only_user_required_connections_cache_per_user():
    class Conn:
        def __init__(self, policy):
            self.auth_policy = policy

    assert user_copies.is_per_user_connection(Conn("user_required"))
    assert not user_copies.is_per_user_connection(Conn("system_only"))
    assert not user_copies.is_per_user_connection(Conn(None))
