"""The connection service validates stored config for types that carry a validating config schema."""

import pytest

from app.errors import AppError
from app.services.connection_service import _validate_connection_config

VALID = {
    "aws_athena": {"region": "eu-west-1", "database": "acme_db"},
    "postgresql_multi": {"host": "h", "databases": "tenant_*"},
}
INVALID = {
    "aws_athena": {"region": "eu-west-1", "database": "!acme_db"},
    "postgresql_multi": {"host": "h", "databases": "tenant_*", "table_exclude_pattern": "(unclosed"},
}


@pytest.mark.parametrize("connection_type", list(VALID))
def test_valid_config_passes(connection_type):
    _validate_connection_config(connection_type, VALID[connection_type])


@pytest.mark.parametrize("connection_type", list(INVALID))
def test_invalid_config_is_rejected_as_a_bad_request(connection_type):
    with pytest.raises(AppError) as raised:
        _validate_connection_config(connection_type, INVALID[connection_type])
    assert raised.value.status_code == 400


def test_blank_optional_values_are_dropped_before_validation():
    _validate_connection_config("postgresql_multi", {**VALID["postgresql_multi"], "table_exclude_pattern": "", "schema": None})


def test_other_types_and_missing_config_are_not_validated():
    _validate_connection_config("postgresql", {"anything": 1})
    _validate_connection_config("postgresql_multi", None)
