"""Unit tests for the Result Lake query cache.

Exercises the real ResultLake against on-disk Parquet (pandas/pyarrow are
dependencies); no data source or network required.
"""
from __future__ import annotations

import time

import pandas as pd
import pytest

from app.ai.code_execution.result_lake import CacheConfig, ResultLake


def _cfg(tmp_path, **kw):
    base = dict(
        enabled=True,
        root=tmp_path,
        max_bytes=10_000_000,
        ttl_seconds=300.0,
        min_cost_ms=100.0,
    )
    base.update(kw)
    return CacheConfig(**base)


@pytest.fixture
def df():
    return pd.DataFrame({"a": range(5), "b": list("hello")})


def test_miss_then_put_then_hit_roundtrips(tmp_path, df):
    lake = ResultLake(_cfg(tmp_path))
    assert lake.get("scope", "SELECT * FROM t") is None
    lake.put("scope", "SELECT * FROM t", df, cost_ms=500)
    got = lake.get("scope", "SELECT * FROM t")
    assert got is not None and got.equals(df)


def test_normalization_collapses_whitespace_and_case(tmp_path, df):
    lake = ResultLake(_cfg(tmp_path))
    lake.put("scope", "SELECT * FROM t", df, cost_ms=500)
    assert lake.get("scope", "select   *   from   t") is not None


def test_scope_isolation(tmp_path, df):
    lake = ResultLake(_cfg(tmp_path))
    lake.put("scopeA", "SELECT * FROM t", df, cost_ms=500)
    assert lake.get("scopeB", "SELECT * FROM t") is None


def test_cheap_queries_not_cached(tmp_path, df):
    lake = ResultLake(_cfg(tmp_path, min_cost_ms=100.0))
    lake.put("scope", "SELECT cheap", df, cost_ms=10)
    assert lake.get("scope", "SELECT cheap") is None


def test_ttl_expiry(tmp_path, df):
    lake = ResultLake(_cfg(tmp_path, ttl_seconds=0.5))
    lake.put("scope", "SELECT * FROM t", df, cost_ms=500)
    assert lake.get("scope", "SELECT * FROM t") is not None
    time.sleep(0.7)
    assert lake.get("scope", "SELECT * FROM t") is None


def test_cost_aware_eviction_keeps_expensive(tmp_path):
    big = pd.DataFrame({"x": range(2000)})
    lake = ResultLake(_cfg(tmp_path, max_bytes=20_000))
    lake.put("s", "q_cheap", big, cost_ms=300)
    lake.get("s", "q_cheap")  # 1 hit
    lake.put("s", "q_expensive", big, cost_ms=60_000)
    lake.put("s", "q_filler", big, cost_ms=300)  # push over budget
    stats = lake.stats()
    assert stats["total_bytes"] <= stats["max_bytes"]
    assert lake.get("s", "q_expensive") is not None


def test_disabled_cache_always_misses(tmp_path, df):
    lake = ResultLake(_cfg(tmp_path, enabled=False))
    lake.put("s", "q", df, cost_ms=9999)
    assert lake.get("s", "q") is None


def test_non_dataframe_results_are_ignored(tmp_path):
    lake = ResultLake(_cfg(tmp_path))
    lake.put("s", "q", [1, 2, 3], cost_ms=500)  # not a DataFrame
    assert lake.get("s", "q") is None


# --- cache <-> streaming integration (path-based methods) ---


def test_register_path_then_owned_copy_roundtrip(tmp_path, df):
    lake = ResultLake(_cfg(tmp_path / "cache"))
    src = tmp_path / "streamed.parquet"
    df.to_parquet(src, index=False)
    lake.register_path("s", "SELECT 1", str(src), cost_ms=500, source_class="X")
    out = lake.get_owned_copy("s", "SELECT 1", "X", tmp_path / "dest")
    assert out is not None and out.exists()
    assert pd.read_parquet(out).equals(df)


def test_put_df_readable_via_owned_copy_interop(tmp_path, df):
    """A DataFrame cached by the non-lazy path is readable by the lazy path."""
    lake = ResultLake(_cfg(tmp_path / "cache"))
    lake.put("s", "SELECT 2", df, cost_ms=500)
    out = lake.get_owned_copy("s", "SELECT 2", "", tmp_path / "dest")
    assert out is not None
    assert pd.read_parquet(out).equals(df)


def test_owned_copy_survives_eviction(tmp_path):
    big = pd.DataFrame({"x": range(2000)})
    lake = ResultLake(_cfg(tmp_path / "cache", max_bytes=20_000))
    src = tmp_path / "s.parquet"
    big.to_parquet(src, index=False)
    lake.register_path("s", "q1", str(src), cost_ms=500, source_class="X")
    out = lake.get_owned_copy("s", "q1", "X", tmp_path / "dest")
    assert out is not None
    # Force eviction of q1 with more-valuable entries.
    for i in range(3):
        s = tmp_path / f"s{i}.parquet"
        big.to_parquet(s, index=False)
        lake.register_path("s", f"big{i}", str(s), cost_ms=60_000, source_class="X")
    # The private owned copy is still readable even though q1 was evicted.
    assert pd.read_parquet(out).shape[0] == 2000


def test_cache_serves_second_query_without_resourcing(tmp_path, df):
    """End-to-end: model the wrapper's get-or-source-then-put flow and assert the
    source is hit exactly once across two identical queries (DataFrame path)."""
    lake = ResultLake(_cfg(tmp_path))
    calls = {"n": 0}

    def source(_sql):
        calls["n"] += 1
        return df

    def cached_query(scope, sql):
        hit = lake.get(scope, sql)
        if hit is not None:
            return hit
        result = source(sql)
        lake.put(scope, sql, result, cost_ms=500)
        return result

    a = cached_query("s", "SELECT * FROM t")
    b = cached_query("s", "SELECT * FROM t")
    assert a.equals(df) and b.equals(df)
    assert calls["n"] == 1  # second query served from cache, source not re-hit


def test_lazy_cache_serves_second_query_without_resourcing(tmp_path, df):
    """Same, for the lazy path: register a streamed Parquet, then a second call is
    served from cache (get_owned_copy) without touching the source."""
    lake = ResultLake(_cfg(tmp_path / "cache"))
    calls = {"n": 0}

    def source_streams_parquet(sql):
        calls["n"] += 1
        p = tmp_path / f"streamed_{calls['n']}.parquet"
        df.to_parquet(p, index=False)
        return p

    def cached_lazy(scope, sql):
        hit = lake.get_owned_copy(scope, sql, "X", tmp_path / "dest")
        if hit is not None:
            return hit
        path = source_streams_parquet(sql)
        lake.register_path(scope, sql, str(path), cost_ms=500, source_class="X")
        return path

    cached_lazy("s", "SELECT 1")
    out = cached_lazy("s", "SELECT 1")
    assert pd.read_parquet(out).equals(df)
    assert calls["n"] == 1  # second call served from cache, source not re-streamed


def test_register_path_skips_cheap(tmp_path, df):
    lake = ResultLake(_cfg(tmp_path / "cache", min_cost_ms=100.0))
    src = tmp_path / "s.parquet"
    df.to_parquet(src, index=False)
    lake.register_path("s", "q", str(src), cost_ms=10, source_class="X")
    assert lake.get_owned_copy("s", "q", "X", tmp_path / "dest") is None


# --- subsumption: serve a narrower query from a cached full table scan ---


def _orders(tmp_path):
    full = pd.DataFrame({"region": ["EU", "US", "EU"], "amt": [10, 20, 30]})
    src = tmp_path / "full.parquet"
    full.to_parquet(src, index=False)
    return src


def test_subsumption_serves_narrower_df(tmp_path):
    pytest.importorskip("sqlglot")
    lake = ResultLake(_cfg(tmp_path / "cache", subsumption_enabled=True))
    lake.register_path("s", "SELECT * FROM orders", str(_orders(tmp_path)), cost_ms=5000, source_class="X")
    out = lake.get_subsuming_df(
        "s", "SELECT region, SUM(amt) AS t FROM orders GROUP BY region ORDER BY region"
    )
    assert out is not None
    assert dict(zip(out.region, out.t)) == {"EU": 40, "US": 20}


def test_subsumption_serves_narrower_path(tmp_path):
    pytest.importorskip("sqlglot")
    lake = ResultLake(_cfg(tmp_path / "cache", subsumption_enabled=True))
    lake.register_path("s", "SELECT * FROM orders", str(_orders(tmp_path)), cost_ms=5000, source_class="X")
    out = lake.get_subsuming_path("s", "SELECT region FROM orders WHERE amt > 15", "X", tmp_path / "dest")
    assert out is not None
    res = pd.read_parquet(out)
    assert sorted(res.region) == ["EU", "US"]  # amt 20 (US) and 30 (EU)


def test_subsumption_rejects_other_table(tmp_path):
    pytest.importorskip("sqlglot")
    lake = ResultLake(_cfg(tmp_path / "cache", subsumption_enabled=True))
    lake.register_path("s", "SELECT * FROM orders", str(_orders(tmp_path)), cost_ms=5000, source_class="X")
    assert lake.get_subsuming_df("s", "SELECT * FROM customers") is None


def test_subsumption_disabled_returns_none(tmp_path):
    lake = ResultLake(_cfg(tmp_path / "cache", subsumption_enabled=False))
    lake.register_path("s", "SELECT * FROM orders", str(_orders(tmp_path)), cost_ms=5000, source_class="X")
    assert lake.get_subsuming_df("s", "SELECT region FROM orders WHERE amt > 1") is None
