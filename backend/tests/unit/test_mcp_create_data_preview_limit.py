"""create_data over MCP returns an inline preview of the query result.

Contract: the organization sets how many rows come back
(``mcp_create_data_preview_rows``, default 1000, clamped 1-10000). A caller may
ask for fewer through ``limit``, never more. The preview reports the true
``total_rows`` and says when rows were left out, so a client can tell a
complete result from a cut one without a second call.
"""
import pytest
from pydantic import ValidationError

from app.ai.tools.mcp.create_data import build_data_preview, resolve_preview_limit
from app.models.organization_settings import OrganizationSettings
from app.schemas.mcp import (
    MCP_CREATE_DATA_DEFAULT_PREVIEW_ROWS,
    MCP_CREATE_DATA_MAX_PREVIEW_ROWS,
    MCPCreateDataInput,
)


def _formatted(n_rows: int, total_rows: int | None = None) -> dict:
    return {
        "columns": [{"field": "id"}, {"field": "value"}],
        "rows": [{"id": i, "value": f"v{i}"} for i in range(n_rows)],
        "info": {"total_rows": total_rows if total_rows is not None else n_rows},
    }


def _settings(value=None) -> OrganizationSettings:
    """Real settings model; ``value=None`` leaves the key unset (schema default)."""
    config = {}
    if value is not None:
        config["mcp_create_data_preview_rows"] = {
            "value": value, "name": "MCP data preview rows", "description": "",
        }
    return OrganizationSettings(config=config)


# ── input contract ──────────────────────────────────────────────────────────

def test_limit_is_optional_so_the_org_default_applies():
    assert MCPCreateDataInput(report_id="r", prompt="p").limit is None


@pytest.mark.parametrize("limit", [1, 20, 1000, MCP_CREATE_DATA_MAX_PREVIEW_ROWS])
def test_limit_accepts_values_within_range(limit):
    assert MCPCreateDataInput(report_id="r", prompt="p", limit=limit).limit == limit


@pytest.mark.parametrize("limit", [0, -1, MCP_CREATE_DATA_MAX_PREVIEW_ROWS + 1])
def test_limit_rejects_values_outside_range(limit):
    with pytest.raises(ValidationError):
        MCPCreateDataInput(report_id="r", prompt="p", limit=limit)


def test_limit_is_advertised_in_the_tool_schema():
    prop = MCPCreateDataInput.model_json_schema()["properties"]["limit"]
    assert "default" not in prop or prop["default"] is None
    assert "organization" in prop["description"]


# ── org setting ─────────────────────────────────────────────────────────────

def test_unset_org_setting_falls_back_to_schema_default():
    assert resolve_preview_limit(_settings(), None) == MCP_CREATE_DATA_DEFAULT_PREVIEW_ROWS == 1000


def test_no_settings_at_all_uses_default():
    assert resolve_preview_limit(None, None) == MCP_CREATE_DATA_DEFAULT_PREVIEW_ROWS


@pytest.mark.parametrize("value", [20, 664, 5000])
def test_org_value_is_the_default(value):
    assert resolve_preview_limit(_settings(value), None) == value


@pytest.mark.parametrize("value,expected", [(0, 1), (-5, 1), (50_000, MCP_CREATE_DATA_MAX_PREVIEW_ROWS), ("junk", 1000)])
def test_bad_org_value_is_clamped(value, expected):
    assert resolve_preview_limit(_settings(value), None) == expected


@pytest.mark.parametrize("org,requested,expected", [(1000, 20, 20), (1000, 5000, 1000), (50, 100, 50), (5000, 5000, 5000)])
def test_caller_can_lower_but_not_exceed_org_value(org, requested, expected):
    assert resolve_preview_limit(_settings(org), requested) == expected


def test_setting_is_registered_in_org_settings_schema():
    from app.schemas.organization_settings_schema import OrganizationSettingsConfig
    cfg = OrganizationSettingsConfig().mcp_create_data_preview_rows
    assert cfg.value == 1000 and cfg.editable


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
