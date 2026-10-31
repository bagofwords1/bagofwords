"""Response schemas whose expiry columns are naive UTC must serialize them
with an explicit UTC offset, the way FastAPI renders a response_model.

The e2e suite covers invites, API keys and service-account keys through their
routes. The two schemas here sit behind flows that need an external platform
or a data-source connection to reach, so the serialization contract is
asserted on the schema directly (validated from ORM-like attributes, dumped
in JSON mode, exactly as the route layer does).
"""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.schemas.external_user_mapping_schema import ExternalUserMappingSchema
from app.schemas.user_data_source_credentials_schema import UserDataSourceCredentialsSchema

NAIVE_UTC = [datetime(2031, 3, 4, 5, 6, 7), datetime(2040, 11, 30, 23, 59, 0)]


def _parse_utc(value):
    assert isinstance(value, str)
    assert value.endswith("Z") or value.endswith("+00:00"), f"{value!r} has no UTC offset"
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _mapping(expiry):
    now = datetime(2030, 1, 1)
    return SimpleNamespace(
        id="m1", organization_id="o1", platform_id="p1", platform_type="slack",
        external_user_id="U1", external_email=None, external_name=None,
        app_user_id=None, is_verified=False, verification_token="t",
        verification_expires_at=expiry, last_verified_at=None,
        created_at=now, updated_at=now,
    )


def _credentials(expiry):
    now = datetime(2030, 1, 1)
    return SimpleNamespace(
        id="c1", data_source_id="d1", user_id="u1", organization_id="o1",
        auth_mode="userpass", is_active=True, is_primary=True,
        last_used_at=None, expires_at=expiry, metadata_json=None,
        created_at=now, updated_at=now,
    )


@pytest.mark.parametrize("expiry", NAIVE_UTC)
def test_external_user_mapping_verification_expiry_has_utc_offset(expiry):
    dumped = ExternalUserMappingSchema.model_validate(_mapping(expiry)).model_dump(mode="json")
    assert _parse_utc(dumped["verification_expires_at"]) == expiry.replace(tzinfo=UTC)


@pytest.mark.parametrize("expiry", NAIVE_UTC)
def test_user_data_source_credentials_expiry_has_utc_offset(expiry):
    dumped = UserDataSourceCredentialsSchema.model_validate(_credentials(expiry)).model_dump(mode="json")
    assert _parse_utc(dumped["expires_at"]) == expiry.replace(tzinfo=UTC)


def test_missing_expiry_stays_null():
    assert ExternalUserMappingSchema.model_validate(_mapping(None)).model_dump(mode="json")["verification_expires_at"] is None
    assert UserDataSourceCredentialsSchema.model_validate(_credentials(None)).model_dump(mode="json")["expires_at"] is None
