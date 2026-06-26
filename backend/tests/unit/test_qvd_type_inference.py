"""Unit tests for QVD schema-hint type inference.

Pure-function coverage of ``QVDClient._infer_duckdb_type`` /
``_fmt_temporal_type`` — the pre-warmup schema hint that mirrors what the
``qvd2parquet`` converter actually emits. No Rust binary or parquet cache
needed, so these always run (unlike the end-to-end ``test_qvd_client`` suite).

The regression they guard: a Qlik date field that carries no ``$date`` tag and
an ``UNKNOWN`` NumberFormat Type — identifiable only by its display pattern
(``YYYY-MM-DD``) — used to be hinted (and converted) as a raw numeric serial,
which SQL then can't ``CAST(... AS DATE)``.
"""

import pytest

from app.data_sources.clients.qvd_client import QVDClient


class TestFmtTemporalType:
    @pytest.mark.parametrize("fmt,expected", [
        ("YYYY-MM-DD", "DATE"),
        ("DD/MM/YYYY", "DATE"),
        ("M/D/YYYY", "DATE"),
        ("WWW DD MMM", "DATE"),            # weekday/day tokens
        ("YYYY-MM-DD hh:mm:ss", "TIMESTAMP"),
        ("M/D/YYYY h:mm:ss TT", "TIMESTAMP"),
        ("hh:mm:ss", "TIME"),
        ("hh:mm", "TIME"),
    ])
    def test_recognizes_temporal_patterns(self, fmt, expected):
        assert QVDClient._fmt_temporal_type(fmt) == expected

    @pytest.mark.parametrize("fmt", [
        "", "#,##0.00", "###0", "##############", "0.0%", "$#,##0",
        "0.00E+00", "0 Days",   # digit present → numeric, even with a stray 'D'
        "MMMM",                  # bare month name is ambiguous (no Y/D/W/h/s)
    ])
    def test_rejects_numeric_and_ambiguous_patterns(self, fmt):
        assert QVDClient._fmt_temporal_type(fmt) is None


class TestInferDuckdbType:
    def test_explicit_temporal_tags(self):
        assert QVDClient._infer_duckdb_type(["$numeric", "$timestamp"]) == "TIMESTAMP"
        assert QVDClient._infer_duckdb_type(["$date"]) == "DATE"
        assert QVDClient._infer_duckdb_type(["$time"]) == "TIME"

    def test_untagged_date_recognized_by_format(self):
        # The fix: no $date tag, UNKNOWN Type, date only in the Fmt pattern.
        assert QVDClient._infer_duckdb_type(["$numeric"], "UNKNOWN", "YYYY-MM-DD") == "DATE"
        assert QVDClient._infer_duckdb_type(
            ["$numeric"], "UNKNOWN", "DD/MM/YYYY hh:mm:ss"
        ) == "TIMESTAMP"
        assert QVDClient._infer_duckdb_type(["$numeric"], "UNKNOWN", "hh:mm:ss") == "TIME"

    def test_numeric_type_codes(self):
        assert QVDClient._infer_duckdb_type([], "1") == "DATE"
        assert QVDClient._infer_duckdb_type([], "3") == "TIMESTAMP"

    def test_numeric_formats_not_promoted(self):
        assert QVDClient._infer_duckdb_type(["$numeric", "$integer"], "INTEGER", "###0") == "BIGINT"
        assert QVDClient._infer_duckdb_type(["$numeric"], "REAL", "##############") == "DOUBLE"

    def test_text_named_like_a_date_stays_text(self):
        # A $text/$ascii column must not be coerced to a temporal type by a
        # stray date-ish Type/Fmt — matches the converter's all-numeric guard.
        assert QVDClient._infer_duckdb_type(["$ascii", "$text"], "DATE", "YYYY-MM-DD") == "VARCHAR"
        assert QVDClient._infer_duckdb_type(["$ascii", "$text"]) == "VARCHAR"

    def test_plain_numeric_and_default(self):
        assert QVDClient._infer_duckdb_type(["$integer"]) == "BIGINT"
        assert QVDClient._infer_duckdb_type(["$numeric"]) == "DOUBLE"
        assert QVDClient._infer_duckdb_type([]) == "VARCHAR"
