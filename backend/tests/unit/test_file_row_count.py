"""render_file_payload must report the file's real row count.

`row_count` used to be `len(df)` AFTER `.head(max_rows)`, so a truncated read
reported the preview size as the file's size. Observed live: a 1,500-row CSV
read with max_rows=1000 produced "row_count: 1000", and the model answered
"the file has 1,000 rows". `truncated: true` sat right beside it and was not
enough to prevent that.
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pandas as pd  # noqa: E402

from app.ai.tools.implementations._file_tool_common import render_file_payload  # noqa: E402


def test_truncated_read_reports_real_row_count():
    df = pd.DataFrame({"a": range(1500), "b": range(1500)})
    out = render_file_payload("big.csv", df, max_rows=1000, max_chars=10**7)
    assert out["row_count"] == 1500, "row_count must be the file's real size"
    assert out["rows_shown"] == 1000, "rows_shown carries the preview size"
    assert out["truncated"] is True
    assert out["csv"].count("\n") <= 1002, "csv itself stays truncated"


def test_untruncated_read_has_matching_counts():
    df = pd.DataFrame({"a": range(10)})
    out = render_file_payload("small.csv", df, max_rows=1000, max_chars=10**7)
    assert out["row_count"] == 10
    assert out["rows_shown"] == 10
    assert out["truncated"] is False


def test_empty_frame():
    out = render_file_payload("empty.csv", pd.DataFrame({"a": []}), max_rows=10, max_chars=1000)
    assert out["row_count"] == 0
    assert out["rows_shown"] == 0
    assert out["truncated"] is False
