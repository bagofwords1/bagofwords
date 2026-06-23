"""Unit tests for query subsumption analysis and rewrite."""
from __future__ import annotations

import pytest

pytest.importorskip("sqlglot")

from app.ai.code_execution.subsumption import (
    STAR,
    analyze,
    can_subsume,
    rewrite_onto_parquet,
)


def test_analyze_full_scan_star():
    s = analyze("SELECT * FROM orders")
    assert s.is_full_scan and s.columns == STAR
    assert s.base_table == "orders" and s.single_table


def test_analyze_full_scan_explicit_columns():
    s = analyze("SELECT a, b FROM orders")
    assert s.is_full_scan and s.columns == frozenset({"a", "b"})


def test_analyze_where_is_not_full_scan():
    s = analyze("SELECT * FROM orders WHERE x > 5")
    assert s.analyzable and s.single_table and not s.is_full_scan


def test_analyze_groupby_is_not_full_scan():
    s = analyze("SELECT a, SUM(b) FROM orders GROUP BY a")
    assert not s.is_full_scan
    assert {"a", "b"} <= s.referenced_columns


def test_analyze_join_is_not_single_table():
    s = analyze("SELECT * FROM a JOIN b ON a.id = b.id")
    assert not s.single_table


def test_can_subsume_star_covers_narrower():
    a = analyze("SELECT * FROM orders")
    b = analyze("SELECT region, SUM(amt) FROM orders WHERE country = 'US' GROUP BY region")
    assert can_subsume(a, b)


def test_can_subsume_explicit_column_subset():
    a = analyze("SELECT region, amt FROM orders")
    b = analyze("SELECT region FROM orders WHERE amt > 5")
    assert can_subsume(a, b)


def test_cannot_subsume_missing_column():
    a = analyze("SELECT region FROM orders")
    b = analyze("SELECT region, amt FROM orders")
    assert not can_subsume(a, b)


def test_cannot_subsume_different_table():
    a = analyze("SELECT * FROM orders")
    b = analyze("SELECT * FROM customers")
    assert not can_subsume(a, b)


def test_cannot_subsume_when_cached_not_full_scan():
    a = analyze("SELECT * FROM orders WHERE x > 5")
    b = analyze("SELECT * FROM orders WHERE x > 5 AND y < 3")
    assert not can_subsume(a, b)


def test_rewrite_onto_parquet_preserves_alias():
    out = rewrite_onto_parquet("SELECT region FROM orders WHERE amt > 5", "/x/y.parquet")
    assert out is not None
    assert "read_parquet('/x/y.parquet')" in out.lower()
    assert "orders" in out  # table name preserved as the subquery alias
