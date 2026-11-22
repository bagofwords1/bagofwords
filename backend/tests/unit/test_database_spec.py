from app.data_sources.clients.database_spec import (
    is_glob,
    parse_database_spec,
    select_databases,
    spec_matches,
)


def test_case_sensitive_match_keeps_case_distinct():
    assert spec_matches("Tenant_A", ["Tenant_*"], [], case_sensitive=True)
    assert not spec_matches("tenant_a", ["Tenant_*"], [], case_sensitive=True)


def test_case_insensitive_match_for_athena():
    assert spec_matches("tenant_a", ["Tenant_*"], [], case_sensitive=False)


def test_exclusion_wins_over_include():
    assert not spec_matches("tenant_test", ["tenant_*"], ["tenant_test"], case_sensitive=True)


def test_exclusion_glob_applies():
    assert not spec_matches("tenant_test", ["tenant_*"], ["*_test"], case_sensitive=True)


def test_literal_include_is_exact_not_prefix():
    assert not spec_matches("tenant_ab", ["tenant_a"], [], case_sensitive=True)


def test_select_databases_filters_and_dedupes():
    got = select_databases(["tenant_a", "tenant_b", "tenant_test", "postgres"],
                           ["tenant_*", "tenant_a"], ["tenant_test"], case_sensitive=True)
    assert got == ["tenant_a", "tenant_b"]


def test_select_databases_drops_literal_not_on_server():
    assert select_databases(["tenant_a"], ["tenant_a", "ghost"], [], case_sensitive=True) == ["tenant_a"]


def test_is_glob_detects_wildcards():
    assert is_glob("a*") and is_glob("a?") and is_glob("a[0-9]")
    assert not is_glob("plain_name")


def test_parse_database_spec_splits_includes_and_excludes():
    assert parse_database_spec("a_*, !a_test, b") == (["a_*", "b"], ["a_test"], True)
