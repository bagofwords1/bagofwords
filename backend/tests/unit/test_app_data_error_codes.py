"""Invariant: every ``app_data.*`` ErrorCode has a localized message in every catalog.

The frontend (``composables/useErrorMessage.ts``) resolves ``t('errors.<code>')``
with vue-i18n, which walks the dotted path segment by segment. A flat key such
as ``errors["app_data.conflict"]`` is therefore NOT found; the message must be
nested (``errors.app_data.conflict``). The lookup below mirrors that walk.
"""
import json
import re
from pathlib import Path

import pytest

from app.errors import ErrorCode

LOCALES_DIR = Path(__file__).resolve().parents[3] / "locales"
CATALOGS = sorted(LOCALES_DIR.glob("*.json"))

EXPECTED_APP_DATA_CODES = {
    "APP_DATA_UNAUTHENTICATED": "app_data.unauthenticated",
    "APP_DATA_FORBIDDEN": "app_data.forbidden",
    "APP_DATA_COLLECTION_NOT_DECLARED": "app_data.collection_not_declared",
    "APP_DATA_RECORD_NOT_FOUND": "app_data.record_not_found",
    "APP_DATA_VALIDATION": "app_data.validation",
    "APP_DATA_TOO_LARGE": "app_data.too_large",
    "APP_DATA_LIMIT_REACHED": "app_data.limit_reached",
    "APP_DATA_CONFLICT": "app_data.conflict",
}

# The server also sends `reason` (a machine code such as "expected_number"),
# but no message may interpolate it.
ALLOWED_PARAMS = {"field", "current_version", "limit"}
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _resolve(errors: dict, code: str):
    node = errors
    for segment in code.split("."):
        if not isinstance(node, dict) or segment not in node:
            return None
        node = node[segment]
    return node


def _app_data_codes():
    return [c for c in ErrorCode if c.value.startswith("app_data.")]


def test_app_data_error_codes_are_exactly_the_contract():
    actual = {c.name: c.value for c in _app_data_codes()}
    assert actual == EXPECTED_APP_DATA_CODES


def test_all_ten_catalogs_present():
    assert {p.stem for p in CATALOGS} == {
        "en", "es", "he", "fr", "sv", "ar", "ru", "de", "pt", "it"
    }


@pytest.mark.parametrize("catalog", CATALOGS, ids=lambda p: p.stem)
def test_every_app_data_code_has_a_message(catalog):
    errors = json.loads(catalog.read_text(encoding="utf-8"))["errors"]
    en_errors = json.loads((LOCALES_DIR / "en.json").read_text(encoding="utf-8"))["errors"]
    codes = _app_data_codes()
    assert len(codes) == 8
    for code in codes:
        message = _resolve(errors, code.value)
        assert isinstance(message, str) and message.strip(), (
            f"{catalog.name}: missing errors.{code.value}"
        )
        params = set(PLACEHOLDER.findall(message))
        assert params <= ALLOWED_PARAMS, f"{catalog.name}: {code.value} uses {params}"
        # Named params must survive translation unchanged.
        en_params = set(PLACEHOLDER.findall(_resolve(en_errors, code.value) or ""))
        assert params == en_params, f"{catalog.name}: {code.value} params {params} != en {en_params}"


@pytest.mark.parametrize("catalog", CATALOGS, ids=lambda p: p.stem)
def test_validation_message_names_the_field_but_not_the_machine_reason(catalog):
    # `reason` is a machine code (e.g. "expected_number"); it must never be
    # interpolated into a user-facing sentence.
    errors = json.loads(catalog.read_text(encoding="utf-8"))["errors"]
    message = _resolve(errors, "app_data.validation")
    assert set(PLACEHOLDER.findall(message)) == {"field"}, f"{catalog.name}: {message!r}"
