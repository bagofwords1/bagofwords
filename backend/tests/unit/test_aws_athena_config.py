"""The Athena connection form accepts one database or a spec of many, and rejects specs that cannot work."""

import pytest
from pydantic import ValidationError

from app.schemas.data_sources.configs import AWSAthenaConfig

BASE = {"region": "eu-west-1"}


def test_legacy_stored_config_round_trips_without_an_exclude_pattern():
    stored = {"region": "us-east-2", "database": "sales_x", "workgroup": "wg", "data_source": "AwsDataCatalog"}
    config = AWSAthenaConfig(**stored)
    assert config.table_exclude_pattern is None
    assert config.database == "sales_x"
    assert {k: v for k, v in config.model_dump().items() if k in stored} == stored


def test_legacy_single_database_keeps_any_data_source():
    assert AWSAthenaConfig(**BASE, database="x", data_source="OtherCatalog").data_source == "OtherCatalog"


@pytest.mark.parametrize("pattern", ["(unclosed", "[a-", "*bad"])
def test_invalid_exclude_regex_is_rejected(pattern):
    with pytest.raises(ValidationError):
        AWSAthenaConfig(**BASE, database="x", table_exclude_pattern=pattern)


@pytest.mark.parametrize("pattern", [r"^day_\d{2}", "tmp$", ""])
def test_valid_exclude_regex_is_accepted(pattern):
    AWSAthenaConfig(**BASE, database="x", table_exclude_pattern=pattern)


@pytest.mark.parametrize("spec", ["!a_db", "!a_db, !b_*", "a_db,,b_db", "a_db, ", "a_db, !"])
def test_spec_without_a_positive_entry_or_with_empty_entries_is_rejected(spec):
    with pytest.raises(ValidationError):
        AWSAthenaConfig(**BASE, database=spec)


@pytest.mark.parametrize("spec", ["*_db", "a_db, b_db", "a_db, !b_db", "a?_db"])
def test_multi_spec_with_a_non_default_data_source_is_rejected(spec):
    with pytest.raises(ValidationError):
        AWSAthenaConfig(**BASE, database=spec, data_source="OtherCatalog")


@pytest.mark.parametrize("spec", ["*_db", "a_db, b_db", "a_db, !b_db"])
def test_multi_spec_with_the_default_catalog_is_accepted(spec):
    assert AWSAthenaConfig(**BASE, database=spec).database == spec


@pytest.mark.parametrize("pattern", [
    "a" * 201, "x{101}", "x{1,500}",
    "((((a{100}){100}){100}){100})", "(a{50}){50}", "([a-z]{40}){40}", r"(\d{40}){40}",
])
def test_exclude_pattern_that_is_too_costly_is_rejected(pattern):
    with pytest.raises(ValidationError):
        AWSAthenaConfig(**BASE, database="x", table_exclude_pattern=pattern)


@pytest.mark.parametrize("pattern", [
    r"^day_\d{2}", r"^day_\d{2}(_[0-9a-f]{32})?$", "(a{10}){10}", "(ab|cd){3}x{100}", r"[{}]{2}\{5\}",
])
def test_exclude_pattern_with_bounded_repeats_is_accepted(pattern):
    assert AWSAthenaConfig(**BASE, database="x", table_exclude_pattern=pattern).table_exclude_pattern == pattern


@pytest.mark.parametrize("pattern", ["(a+)+$", "(a*)*x", r"(\w+){2,}", r"^(\w+_?)+#", "((ab)+c)+", "(?:x+)+"])
def test_nested_unbounded_repeats_are_refused(pattern):
    from pydantic import ValidationError
    from app.schemas.data_sources.configs import AWSAthenaConfig
    with pytest.raises(ValidationError):
        AWSAthenaConfig(region="eu-west-1", database="acme_db", table_exclude_pattern=pattern)


@pytest.mark.parametrize("pattern", [r"^day_\d{2}", r"^tmp_.*", r"(_bak|_old)+$", r"^(day|week)_\d+$", r"(\d{2}){3}", "[a+]+"])
def test_ordinary_exclude_patterns_are_accepted(pattern):
    from app.schemas.data_sources.configs import AWSAthenaConfig
    AWSAthenaConfig(region="eu-west-1", database="acme_db", table_exclude_pattern=pattern)

