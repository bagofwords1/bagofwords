"""Connection tools expose a model-authored `title` for human-readable UI labels.

New agentic UIs (Claude Code, Codex, …) render "Searching Notion for customer"
instead of the raw `notion_search`. In BOW that label is a `title` argument the
model fills on every connection/external tool; the frontend (MCPTool.vue,
GenericTool.vue, the file-tool components) renders `arguments_json.title` as the
live status line when present.

The contract this pins:

1. Every connection tool advertises `title` in the JSON schema the planner
   sees (so the model knows to produce it), and it is OPTIONAL — a missing
   title must never break a call.
2. A title the model emits survives into the persisted `arguments_json` that
   the UI reads (the planner stores the raw tool arguments verbatim).

These are invariants over the whole connection-tool family, not one tool — a
new connection tool that forgets `title` should fail this test.
"""
import pytest

from app.ai.tools.implementations.execute_mcp import ExecuteMCPTool
from app.ai.tools.implementations.search_mcps import SearchMCPsTool
from app.ai.tools.implementations.list_files import ListFilesTool
from app.ai.tools.implementations.read_file import ReadFileTool
from app.ai.tools.implementations.search_files import SearchFilesTool
from app.ai.tools.implementations.write_file import WriteFileTool
from app.ai.tools.implementations.attach_file import AttachFileTool
from app.ai.tools.implementations.web_fetch import WebFetchTool

# The tools that operate against an attached connection or an external target,
# and therefore benefit from a plain-language label in the chat UI.
CONNECTION_TOOLS = [
    ExecuteMCPTool,
    SearchMCPsTool,
    ListFilesTool,
    ReadFileTool,
    SearchFilesTool,
    WriteFileTool,
    AttachFileTool,
    WebFetchTool,
]


def _title_prop(schema: dict) -> dict:
    props = schema.get("properties") or {}
    assert "title" in props, "connection tool schema is missing the `title` property"
    return props["title"]


def _accepts_string(prop: dict) -> bool:
    """A field the planner may fill with a string (directly or via anyOf/null)."""
    if prop.get("type") == "string":
        return True
    for variant in prop.get("anyOf", []):
        if variant.get("type") == "string":
            return True
    return False


@pytest.mark.parametrize("tool_cls", CONNECTION_TOOLS, ids=lambda c: c.__name__)
def test_connection_tool_advertises_optional_title(tool_cls):
    """The planner-facing schema exposes `title` as an optional string field."""
    schema = tool_cls().metadata.input_schema
    prop = _title_prop(schema)

    assert _accepts_string(prop), f"{tool_cls.__name__}.title is not string-typed: {prop}"

    # Optional: never in `required`, so a call without a title still validates.
    assert "title" not in (schema.get("required") or []), (
        f"{tool_cls.__name__}.title must be optional so a missing label never breaks a call"
    )

    # The description must actually guide the model to write a human label,
    # not leave it as an opaque field.
    desc = (prop.get("description") or "").lower()
    assert desc, f"{tool_cls.__name__}.title has no description to guide the model"


@pytest.mark.parametrize("tool_cls", CONNECTION_TOOLS, ids=lambda c: c.__name__)
def test_title_survives_into_persisted_arguments(tool_cls):
    """A model-authored title round-trips through the input model unchanged.

    The planner persists the tool arguments as `arguments_json` (via
    `model_dump()` on the validated input, or the raw dict). Either way the
    title the model produced must be exactly what the UI later reads.
    """
    label = "Searching Acme for signed 2025 contracts"
    input_model = tool_cls().input_model
    assert input_model is not None, f"{tool_cls.__name__} has no input_model"

    # Minimal required args per tool, plus the title.
    base = {
        "ExecuteMCPTool": {"connection_id": "c1", "tool_name": "notion_search"},
        "SearchMCPsTool": {},
        "ListFilesTool": {"connection_id": "c1"},
        "ReadFileTool": {"connection_id": "c1", "file_id": "f1"},
        "SearchFilesTool": {"connection_id": "c1", "query": "contract"},
        "WriteFileTool": {"connection_id": "c1", "filename": "out.md"},
        "AttachFileTool": {"connection_id": "c1", "file_ids": ["f1"]},
        "WebFetchTool": {"url": "https://example.com"},
    }[tool_cls.__name__]

    model = input_model(**base, title=label)
    dumped = model.model_dump()
    assert dumped["title"] == label, f"{tool_cls.__name__}: title lost in model_dump()"


@pytest.mark.parametrize("tool_cls", CONNECTION_TOOLS, ids=lambda c: c.__name__)
def test_title_is_optional_at_construction(tool_cls):
    """Omitting the title is valid and yields a null title (never an error)."""
    input_model = tool_cls().input_model
    base = {
        "ExecuteMCPTool": {"connection_id": "c1", "tool_name": "notion_search"},
        "SearchMCPsTool": {},
        "ListFilesTool": {"connection_id": "c1"},
        "ReadFileTool": {"connection_id": "c1", "file_id": "f1"},
        "SearchFilesTool": {"connection_id": "c1", "query": "contract"},
        "WriteFileTool": {"connection_id": "c1", "filename": "out.md"},
        "AttachFileTool": {"connection_id": "c1", "file_ids": ["f1"]},
        "WebFetchTool": {"url": "https://example.com"},
    }[tool_cls.__name__]

    model = input_model(**base)  # no title
    assert model.model_dump().get("title") is None
