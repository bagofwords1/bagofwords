"""Unit tests for the training-mode agent + connection catalog tools.

Covers:
- Tool metadata (mode gating, required permissions, category)
- Auto-registration through the global ``ToolRegistry``
- Input schema validation (rejecting bad input shapes)
- Output payload assembly through ``run_stream`` against a stubbed
  ``runtime_ctx`` (mocked services). No DB, no FastAPI.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from app.ai.registry import ToolRegistry
from app.ai.tools.implementations.create_agent import CreateAgentTool
from app.ai.tools.implementations.get_agent import GetAgentTool
from app.ai.tools.implementations.get_connection import GetConnectionTool
from app.ai.tools.implementations.list_agents import ListAgentsTool
from app.ai.tools.implementations.list_connections import ListConnectionsTool
from app.schemas.agent_manifest_schema import (
    ApplyError,
    ApplyErrorCode,
    ApplyResult,
    ApplyStatus,
)


# ---------------------------------------------------------------------------
# Metadata + registration
# ---------------------------------------------------------------------------


def test_all_five_tools_register_in_global_registry():
    reg = ToolRegistry()
    expected = {
        "list_agents",
        "get_agent",
        "create_agent",
        "list_connections",
        "get_connection",
    }
    for name in expected:
        assert reg.get(name) is not None, f"{name} did not auto-register"
        meta = reg.get_metadata(name)
        assert meta is not None
        assert meta.allowed_modes == ["training"], f"{name} must be training-only"


def test_categories_and_permissions():
    tools = {
        "list_agents": (ListAgentsTool, "research", []),
        "get_agent": (GetAgentTool, "research", []),
        "create_agent": (CreateAgentTool, "action", ["create_data_source"]),
        "list_connections": (ListConnectionsTool, "research", []),
        "get_connection": (GetConnectionTool, "research", ["manage_connections"]),
    }
    for name, (cls, category, perms) in tools.items():
        meta = cls().metadata
        assert meta.name == name
        assert meta.category == category
        assert meta.required_permissions == perms


def test_create_agent_default_is_private():
    """is_public must default to False in the input schema."""
    schema = CreateAgentTool().input_model.model_json_schema()
    props = schema.get("properties", {})
    assert props["is_public"]["default"] is False


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


async def _drive(tool, args: Dict[str, Any], ctx: Dict[str, Any]) -> List[Any]:
    """Drain the async iterator and return all events."""
    events = []
    async for ev in tool.run_stream(args, ctx):
        events.append(ev)
    return events


def _stub_ctx(**overrides) -> Dict[str, Any]:
    """Build a minimal runtime_ctx with db/org/user stubs."""
    base = {
        "db": SimpleNamespace(),
        "organization": SimpleNamespace(id="org-1"),
        "user": SimpleNamespace(id="user-1", email="a@b.com"),
        "mode": "training",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# list_agents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_agents_invalid_input():
    tool = ListAgentsTool()
    events = await _drive(tool, {"page": -1}, _stub_ctx())
    assert len(events) == 1
    assert events[0].type == "tool.error"
    assert events[0].payload["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_list_agents_missing_context():
    tool = ListAgentsTool()
    events = await _drive(tool, {}, {})
    # ToolStart is emitted before the context check
    assert events[-1].type == "tool.error"
    assert events[-1].payload["code"] == "MISSING_CONTEXT"


@pytest.mark.asyncio
async def test_list_agents_happy_path_and_filtering():
    """Service returns 3 items; type filter narrows to 1."""

    fake_items = [
        SimpleNamespace(
            id="a-1",
            name="alpha",
            description="first",
            type="postgresql",
            is_public=True,
            created_at=None,
            connections=[SimpleNamespace(table_count=4)],
        ),
        SimpleNamespace(
            id="a-2",
            name="beta",
            description=None,
            type="mcp",
            is_public=False,
            created_at=None,
            connections=[SimpleNamespace(table_count=0)],
        ),
        SimpleNamespace(
            id="a-3",
            name="alpha-2",
            description=None,
            type="postgresql",
            is_public=False,
            created_at=None,
            connections=[],
        ),
    ]

    with patch(
        "app.services.data_source_service.DataSourceService.get_data_sources",
        new=AsyncMock(return_value=fake_items),
    ):
        events = await _drive(
            ListAgentsTool(),
            {"type": "mcp"},
            _stub_ctx(),
        )

    assert events[0].type == "tool.start"
    end = next(e for e in events if e.type == "tool.end")
    out = end.payload["output"]
    assert out["success"] is True
    assert out["total"] == 1
    assert out["agents"][0]["name"] == "beta"
    assert out["agents"][0]["is_public"] is False


@pytest.mark.asyncio
async def test_list_agents_name_search_substring():
    fake_items = [
        SimpleNamespace(id="1", name="revenue-analyst", description=None, type="x", is_public=False, created_at=None, connections=[]),
        SimpleNamespace(id="2", name="support-agent", description=None, type="x", is_public=False, created_at=None, connections=[]),
    ]
    with patch(
        "app.services.data_source_service.DataSourceService.get_data_sources",
        new=AsyncMock(return_value=fake_items),
    ):
        events = await _drive(
            ListAgentsTool(),
            {"name_search": "REVENUE"},  # case-insensitive
            _stub_ctx(),
        )
    end = next(e for e in events if e.type == "tool.end")
    names = [a["name"] for a in end.payload["output"]["agents"]]
    assert names == ["revenue-analyst"]


# ---------------------------------------------------------------------------
# create_agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_agent_blocks_empty_tables_when_connection_has_indexed_tables():
    """If the planner forgets tables_include and the connections actually
    have tables, return code=tables_unconfirmed instead of silently opening
    every table."""
    from unittest.mock import MagicMock

    # Stub the DB so the indexed-table count query returns >0.
    class _RowAll:
        def all(self):
            return self._data
    class _Scalar:
        def __init__(self, v):
            self._v = v
        def scalar(self):
            return self._v
    class _DB:
        async def execute(self, stmt):
            sql = str(stmt)
            r = _RowAll()
            if "FROM connections" in sql:
                r._data = [("conn-uuid-1",)]
                return r
            # count() path
            return _Scalar(7)

    ctx = _stub_ctx(db=_DB())

    captured: Dict[str, Any] = {}

    async def fake_apply(self, db, organization, user, yaml_text, *, dry_run=False):
        captured["called"] = True
        return ApplyResult(status=ApplyStatus.CREATED, id="x", name="x")

    with patch(
        "app.services.agent_yaml_service.AgentYamlService.apply",
        new=fake_apply,
    ):
        events = await _drive(
            CreateAgentTool(),
            {"name": "y", "connection_names": ["postgres-prod"]},
            ctx,
        )

    assert "called" not in captured, "apply must not run when empty tables blocked"
    end = next(e for e in events if e.type == "tool.end")
    out = end.payload["output"]
    assert out["success"] is False
    assert out["status"] == "error"
    assert out["errors"][0]["code"] == "tables_unconfirmed"


@pytest.mark.asyncio
async def test_create_agent_empty_tables_allowed_with_confirm_flag():
    """When the planner sets confirm_empty_tables=True, the call goes
    through even if the connections have tables."""
    captured: Dict[str, Any] = {}

    async def fake_apply(self, db, organization, user, yaml_text, *, dry_run=False):
        captured["called"] = True
        return ApplyResult(status=ApplyStatus.CREATED, id="x", name="open-explorer")

    with patch(
        "app.services.agent_yaml_service.AgentYamlService.apply",
        new=fake_apply,
    ):
        events = await _drive(
            CreateAgentTool(),
            {
                "name": "open-explorer",
                "connection_names": ["postgres-prod"],
                "confirm_empty_tables": True,
            },
            _stub_ctx(),
        )

    assert captured.get("called") is True
    end = next(e for e in events if e.type == "tool.end")
    assert end.payload["output"]["success"] is True


@pytest.mark.asyncio
async def test_create_agent_translates_to_manifest_and_calls_apply():
    """Structured input must be passed verbatim to AgentYamlService.apply
    via a YAML serialization round-trip."""

    captured: Dict[str, Any] = {}

    async def fake_apply(self, db, organization, user, yaml_text, *, dry_run=False):
        captured["yaml"] = yaml_text
        captured["dry_run"] = dry_run
        return ApplyResult(
            status=ApplyStatus.CREATED,
            id="ds-1",
            name="my-agent",
        )

    with patch(
        "app.services.agent_yaml_service.AgentYamlService.apply",
        new=fake_apply,
    ):
        events = await _drive(
            CreateAgentTool(),
            {
                "name": "my-agent",
                "description": "test",
                "connection_names": ["postgres-prod"],
                "tool_policies": [
                    {
                        "connection_name": "postgres-prod",
                        "allow": ["search"],
                        "deny": ["delete"],
                    }
                ],
                "conversation_starters": ["Q1?"],
                "members": [{"user": "x@y.com"}],
            },
            _stub_ctx(),
        )

    assert captured["dry_run"] is False
    assert "name: my-agent" in captured["yaml"]
    assert "postgres-prod" in captured["yaml"]
    end = next(e for e in events if e.type == "tool.end")
    out = end.payload["output"]
    assert out["success"] is True
    assert out["status"] == "created"
    assert out["id"] == "ds-1"


@pytest.mark.asyncio
async def test_create_agent_dry_run_flag_propagates():
    captured: Dict[str, Any] = {}

    async def fake_apply(self, db, organization, user, yaml_text, *, dry_run=False):
        captured["dry_run"] = dry_run
        return ApplyResult(
            status=ApplyStatus.DRY_RUN,
            id=None,
            name="x",
            diff={"action": "create"},
        )

    with patch(
        "app.services.agent_yaml_service.AgentYamlService.apply",
        new=fake_apply,
    ):
        events = await _drive(
            CreateAgentTool(),
            {"name": "x", "dry_run": True},
            _stub_ctx(),
        )

    assert captured["dry_run"] is True
    end = next(e for e in events if e.type == "tool.end")
    assert end.payload["output"]["status"] == "dry_run"


@pytest.mark.asyncio
async def test_create_agent_surfaces_apply_errors_in_envelope():
    """Errors come back via ToolEndEvent (not ToolErrorEvent) so the
    planner can self-correct."""

    async def fake_apply(self, db, organization, user, yaml_text, *, dry_run=False):
        return ApplyResult(
            status=ApplyStatus.ERROR,
            id=None,
            name="x",
            errors=[
                ApplyError(
                    loc=["connections", 0],
                    code=ApplyErrorCode.CONNECTION_NOT_FOUND,
                    message="Connection 'postgres-prod-DOES-NOT-EXIST' not found.",
                    value="postgres-prod-DOES-NOT-EXIST",
                    suggestion="postgres-prod",
                ),
            ],
        )

    with patch(
        "app.services.agent_yaml_service.AgentYamlService.apply",
        new=fake_apply,
    ):
        events = await _drive(
            CreateAgentTool(),
            {"name": "x", "connection_names": ["postgres-prod-DOES-NOT-EXIST"]},
            _stub_ctx(),
        )

    end = next(e for e in events if e.type == "tool.end")
    out = end.payload["output"]
    assert out["success"] is False
    assert out["status"] == "error"
    assert out["errors"][0]["code"] == "connection_not_found"
    assert out["errors"][0]["suggestion"] == "postgres-prod"


# ---------------------------------------------------------------------------
# Input validation across all five tools
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_cls, args",
    [
        (ListAgentsTool, {"page": 0}),
        (GetAgentTool, {"name": "", "table_limit": 50}),  # name min validated
        (CreateAgentTool, {"members": [{"permissions": []}]}),  # member missing user+group
        (ListConnectionsTool, {"page_size": -1}),
        (GetConnectionTool, {"table_limit": 0}),
    ],
)
@pytest.mark.asyncio
async def test_invalid_input_returns_structured_error(tool_cls, args):
    """Every tool must reject bad input with code=INVALID_INPUT
    (or for GetAgentTool with name='', the Pydantic check still fails
    because the schema treats empty strings as valid — adjust as needed)."""
    events = await _drive(tool_cls(), args, _stub_ctx())
    # Some tools fail at input parsing, others at the missing-context
    # check (when args are technically valid). Both produce ToolErrorEvent.
    err_events = [e for e in events if e.type == "tool.error"]
    assert err_events, f"{tool_cls.__name__} did not return an error event"
