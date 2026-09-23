"""create_data over MCP returns an inline preview of the query result.

Contract: the caller chooses how many rows come back through ``limit``
(default and ceiling 1000, always at least one). The preview reports the true
``total_rows`` and says when rows were left out, so a client can tell a
complete result from a cut one without a second call.
"""
import pytest
from pydantic import ValidationError

from app.ai.tools.mcp.create_data import build_data_preview
from app.schemas.mcp import MCP_CREATE_DATA_MAX_PREVIEW_ROWS, MCPCreateDataInput


def _formatted(n_rows: int, total_rows: int | None = None) -> dict:
    return {
        "columns": [{"field": "id"}, {"field": "value"}],
        "rows": [{"id": i, "value": f"v{i}"} for i in range(n_rows)],
        "info": {"total_rows": total_rows if total_rows is not None else n_rows},
    }


# ── input contract ──────────────────────────────────────────────────────────

def test_limit_defaults_to_the_ceiling():
    inp = MCPCreateDataInput(report_id="r", prompt="p")
    assert inp.limit == MCP_CREATE_DATA_MAX_PREVIEW_ROWS == 1000


@pytest.mark.parametrize("limit", [1, 20, 999, 1000])
def test_limit_accepts_values_within_range(limit):
    assert MCPCreateDataInput(report_id="r", prompt="p", limit=limit).limit == limit


@pytest.mark.parametrize("limit", [0, -1, 1001, 50_000])
def test_limit_rejects_values_outside_range_instead_of_clamping(limit):
    with pytest.raises(ValidationError):
        MCPCreateDataInput(report_id="r", prompt="p", limit=limit)


def test_limit_is_advertised_in_the_tool_schema():
    schema = MCPCreateDataInput.model_json_schema()
    prop = schema["properties"]["limit"]
    assert prop["default"] == MCP_CREATE_DATA_MAX_PREVIEW_ROWS
    assert prop["minimum"] == 1
    assert prop["maximum"] == MCP_CREATE_DATA_MAX_PREVIEW_ROWS


# ── preview contract ────────────────────────────────────────────────────────

@pytest.mark.parametrize("n_rows,limit", [(5, 1000), (1000, 1000), (7, 7), (3, 20)])
def test_result_within_limit_is_returned_whole_and_not_marked_truncated(n_rows, limit):
    preview = build_data_preview(_formatted(n_rows), limit=limit)
    assert len(preview["rows"]) == n_rows
    assert preview["total_rows"] == n_rows
    assert preview["truncated"] is False


@pytest.mark.parametrize("n_rows,limit", [(1500, 1000), (21, 20), (2, 1)])
def test_result_over_limit_is_cut_to_limit_and_marked_truncated(n_rows, limit):
    preview = build_data_preview(_formatted(n_rows), limit=limit)
    assert len(preview["rows"]) == limit
    assert preview["rows"] == _formatted(n_rows)["rows"][:limit]
    assert preview["total_rows"] == n_rows
    assert preview["truncated"] is True


def test_total_rows_comes_from_the_source_count_when_rows_were_already_sampled():
    # The executor may hand over fewer rows than the query produced; the true
    # count is what the caller needs to decide whether to fetch the rest.
    preview = build_data_preview(_formatted(50, total_rows=12_345), limit=1000)
    assert preview["total_rows"] == 12_345
    assert preview["truncated"] is True


def test_empty_result_is_well_formed():
    preview = build_data_preview({"columns": [], "rows": [], "info": {}}, limit=1000)
    assert preview == {"columns": [], "rows": [], "total_rows": 0, "truncated": False}
