"""payload_name must survive payloads whose ``.name`` is not a string.

A tabular read returns a DataFrame, and pandas resolves attribute access
against the frame's COLUMNS. Any spreadsheet with a column called `name`
therefore made `getattr(payload, "name")` return a Series, and the old
`(... or "")` truthiness test raised "The truth value of a Series is
ambiguous" — read_file failed outright for one of the most common headers
there is.

The contract: return the payload's own name when it really is a string,
otherwise fall back to the caller's fallback. Never raise.
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from app.data_sources.clients._file_source_common import payload_name  # noqa: E402


class _NamedBytes(bytes):
    """Stand-in for the connector wrapper that carries a real filename."""
    name = "report.pdf"


@pytest.mark.parametrize("columns", [
    ["customer_id", "name", "region"],   # `name` column — the reported break
    ["name"],                            # only a name column
    ["Name", "name", "id"],              # case-variant siblings present
])
def test_dataframe_with_name_column_falls_back(columns):
    df = pd.DataFrame({c: [1, 2] for c in columns})
    assert payload_name(df, "customers.csv") == "customers.csv"


def test_dataframe_without_name_column_falls_back():
    df = pd.DataFrame({"order_id": [1], "revenue": [2.0]})
    assert payload_name(df, "sales.csv") == "sales.csv"


def test_series_payload_falls_back():
    assert payload_name(pd.Series([1, 2, 3]), "col.csv") == "col.csv"


def test_string_name_wins_over_fallback():
    assert payload_name(_NamedBytes(b"x"), "opaque-id") == "report.pdf"


def test_blank_name_falls_back():
    class _Blank:
        name = "   "
    assert payload_name(_Blank(), "fallback.txt") == "fallback.txt"


def test_plain_payloads_use_fallback():
    assert payload_name(b"raw bytes", "f.bin") == "f.bin"
    assert payload_name("some text", "f.txt") == "f.txt"
    assert payload_name({"k": "v"}, "f.json") == "f.json"
    assert payload_name(None, "f") == "f"
