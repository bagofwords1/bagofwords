"""A rejected member credential never comes back in the error, for any connector.

The error reaches the credentials modal for every type. Pydantic's text quotes
the input and cuts it mid-key, which ``redact_error`` cannot mask, so the detail
is built from the error list without the input. Every user-scope variant in the
registry goes through the real save, test and patch paths.
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.schemas.data_source_registry import REGISTRY
from app.services.user_data_source_credentials_service import UserDataSourceCredentialsService

_CANARY = "canary-Zq7Vx9"


def _user_variants():
    for conn_type, entry in REGISTRY.items():
        for mode, variant in (entry.credentials_auth.by_auth or {}).items():
            if "user" in (variant.scopes or []):
                yield conn_type, mode, variant


_MISSING_FIELD_CASES = [
    (conn_type, mode, name)
    for conn_type, mode, variant in _user_variants()
    for name, field in variant.schema.model_fields.items() if field.is_required()
]


def _credentials(conn_type, mode, dropped):
    """Every field of the variant holds the fake secret, except the required one left out.

    One extra key keeps the dict non-empty for a one-field variant, as a mistyped field name would.
    """
    variant = REGISTRY[conn_type].credentials_auth.by_auth[mode]
    fields = {name: f"{_CANARY}-{name}" for name in variant.schema.model_fields if name != dropped}
    return {**fields, "pasted_here": f"{_CANARY}-pasted"}


def _source(conn_type):
    conn = SimpleNamespace(id="CONN-1", type=conn_type, config={}, auth_policy="user_required",
                           allowed_user_auth_modes=None)
    return SimpleNamespace(id="DS-1", organization_id="ORG-1", connections=[conn])


class _Rows:
    def __init__(self, row):
        self._row = row

    def scalars(self):
        return self

    def first(self):
        return self._row


class _Db:
    """The only database answer these paths need before they validate: one stored row."""

    def __init__(self, auth_mode):
        self._row = SimpleNamespace(auth_mode=auth_mode)

    async def execute(self, _stmt):
        return _Rows(self._row)


async def _rejected(call):
    with pytest.raises(HTTPException) as info:
        await call
    assert info.value.status_code == 422, info.value.detail
    return str(info.value.detail)


def _assert_clean(detail, field):
    assert _CANARY.lower() not in detail.lower(), detail
    assert f"{field}: " in detail, detail
    assert "validation error for" not in detail and "errors.pydantic.dev" not in detail, detail


def test_the_registry_has_cases():
    assert len(_MISSING_FIELD_CASES) > 50
    assert {"aws_athena", "sharepoint", "onedrive"} <= {case[0] for case in _MISSING_FIELD_CASES}


@pytest.mark.asyncio
@pytest.mark.parametrize("conn_type,mode,field", _MISSING_FIELD_CASES)
async def test_a_test_with_a_field_missing_never_echoes_the_typed_secret(conn_type, mode, field):
    payload = SimpleNamespace(auth_mode=mode, credentials=_credentials(conn_type, mode, field))
    detail = await _rejected(UserDataSourceCredentialsService().test_my_credentials(
        None, data_source=_source(conn_type), user=SimpleNamespace(id="U-1"), payload=payload))
    _assert_clean(detail, field)


@pytest.mark.asyncio
@pytest.mark.parametrize("conn_type,mode,field", [c for c in _MISSING_FIELD_CASES if c[0] != "kms_lighthouse"])
async def test_a_save_with_a_field_missing_never_echoes_the_typed_secret(conn_type, mode, field):
    payload = SimpleNamespace(auth_mode=mode, credentials=_credentials(conn_type, mode, field), is_primary=True,
                              expires_at=None, metadata_json=None)
    detail = await _rejected(UserDataSourceCredentialsService().upsert_my_credentials(
        None, data_source=_source(conn_type), user=SimpleNamespace(id="U-1"), payload=payload))
    _assert_clean(detail, field)


@pytest.mark.asyncio
@pytest.mark.parametrize("names_the_mode", [True, False], ids=["changes_auth_mode", "keeps_auth_mode"])
@pytest.mark.parametrize("conn_type,mode,field", _MISSING_FIELD_CASES)
async def test_a_patch_with_a_field_missing_never_echoes_the_typed_secret(conn_type, mode, field, names_the_mode):
    payload = SimpleNamespace(auth_mode=mode if names_the_mode else None,
                              credentials=_credentials(conn_type, mode, field))
    detail = await _rejected(UserDataSourceCredentialsService().patch_my_credentials(
        _Db(mode), data_source=_source(conn_type), user=SimpleNamespace(id="U-1"), payload=payload))
    _assert_clean(detail, field)
