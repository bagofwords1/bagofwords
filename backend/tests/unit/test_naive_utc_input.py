"""Expiry inputs are stored in naive DateTime columns that hold UTC, and the
services compare them with naive ``datetime.utcnow()``. An offset-aware
``expires_at`` from a client must therefore be normalized at the schema layer
to the same instant as naive UTC; a naive input already means UTC and passes
through unchanged.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.schemas import base
from app.schemas.api_key_schema import ApiKeyCreate
from app.schemas.service_account_schema import ServiceAccountKeyCreate
from app.schemas.user_data_source_credentials_schema import (
    UserDataSourceCredentialsCreate,
    UserDataSourceCredentialsUpdate,
)
from app.ee.scim.schemas import ScimTokenCreate


def test_to_naive_utc_keeps_none():
    assert base.to_naive_utc(None) is None


@pytest.mark.parametrize("naive", [datetime(2030, 1, 1, 12, 0), datetime(2041, 6, 30, 23, 59, 59, 123456)])
def test_to_naive_utc_leaves_naive_input_unchanged(naive):
    out = base.to_naive_utc(naive)
    assert out == naive
    assert out.tzinfo is None


@pytest.mark.parametrize(
    "aware,expected",
    [
        (datetime(2030, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=2))), datetime(2030, 1, 1, 10, 0)),
        (datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc), datetime(2030, 1, 1, 12, 0)),
        # West of UTC, crossing midnight into the next day.
        (datetime(2035, 12, 31, 22, 30, tzinfo=timezone(timedelta(hours=-5))), datetime(2036, 1, 1, 3, 30)),
    ],
)
def test_to_naive_utc_converts_aware_input_to_the_same_instant(aware, expected):
    out = base.to_naive_utc(aware)
    assert out.tzinfo is None
    assert out == expected


INPUT_SCHEMAS = [
    lambda v: ApiKeyCreate(expires_at=v),
    lambda v: ServiceAccountKeyCreate(expires_at=v),
    lambda v: UserDataSourceCredentialsCreate(auth_mode="userpass", credentials={}, expires_at=v),
    lambda v: UserDataSourceCredentialsUpdate(expires_at=v),
    lambda v: ScimTokenCreate(expires_at=v),
]
SCHEMA_IDS = ["api_key", "service_account_key", "user_cred_create", "user_cred_update", "scim_token"]


@pytest.mark.parametrize("build", INPUT_SCHEMAS, ids=SCHEMA_IDS)
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2030-01-01T12:00:00+02:00", datetime(2030, 1, 1, 10, 0)),
        ("2030-01-01T12:00:00Z", datetime(2030, 1, 1, 12, 0)),
        ("2031-03-04T05:06:07", datetime(2031, 3, 4, 5, 6, 7)),
    ],
)
def test_expiry_input_schemas_store_naive_utc(build, raw, expected):
    value = build(raw).expires_at
    assert value.tzinfo is None
    assert value == expected


@pytest.mark.parametrize("build", INPUT_SCHEMAS, ids=SCHEMA_IDS)
def test_expiry_input_schemas_keep_missing_expiry_null(build):
    assert build(None).expires_at is None
