"""write_csv display-filename derivation.

The saved CSV's display name is derived from the LLM-provided ``title`` so
files read as e.g. ``Software_Houses_Income_Tax_2025.csv`` instead of the
generic ``write_csv_output.csv``. Derivation must stay filename-safe (no path
separators leaking through) and fall back to the default when the title is
missing or slugs to nothing.
"""
from __future__ import annotations

import pytest

from app.ai.tools.implementations.write_csv import (
    _DEFAULT_CSV_FILENAME,
    _derive_csv_filename,
)


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Software Houses Income Tax 2025", "Software_Houses_Income_Tax_2025.csv"),
        ("Sales — Q1/Q2 (2025)!!!", "Sales_Q1_Q2_2025.csv"),
        ("report.csv", "report.csv"),  # existing extension is normalized, not doubled
    ],
)
def test_derives_readable_name(title, expected):
    assert _derive_csv_filename(title) == expected


@pytest.mark.parametrize("title", [None, "", "   ", "....", "///"])
def test_falls_back_to_default(title):
    assert _derive_csv_filename(title) == _DEFAULT_CSV_FILENAME


def test_strips_path_separators():
    result = _derive_csv_filename("../../etc/passwd")
    assert "/" not in result and "\\" not in result
    assert ".." not in result
    assert result == "etc_passwd.csv"


def test_caps_length():
    result = _derive_csv_filename("x" * 500)
    # 100-char slug budget + ".csv"
    assert len(result) <= 104
    assert result.endswith(".csv")
