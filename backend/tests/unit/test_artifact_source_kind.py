"""Tool-produced files are artifacts, never pending attachments.

execute_mcp and write_csv link their output to the report with no
completion_id, and a File's source_kind defaulted to "upload" — so a saved
MCP response was indistinguishable from a screenshot the user had just
dropped in, and popped up as an attachment chip in the prompt box.
"""
import inspect
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.file_service import is_agent_owned_file  # noqa: E402


def _file(fid, **kw):
    return SimpleNamespace(id=fid, **kw)


def test_artifacts_are_agent_owned():
    assert is_agent_owned_file(_file("a1", source_kind="artifact"), set()) is True


def test_any_future_non_upload_kind_is_agent_owned():
    """The rule is "not an upload", so a new tool origin can never fall
    through to looking like something the user attached."""
    assert is_agent_owned_file(_file("x1", source_kind="something_new"), set()) is True


def test_missing_kind_reads_as_upload():
    assert is_agent_owned_file(_file("u1", source_kind=None), set()) is False
    assert is_agent_owned_file(_file("u2"), set()) is False


class _Db:
    """Just enough of AsyncSession for the materializers: records adds."""

    def __init__(self):
        self.added = []

    @asynccontextmanager
    async def begin_nested(self):
        yield

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = "file-1"

    async def execute(self, *_a, **_k):
        return None


@pytest.mark.asyncio
async def test_execute_mcp_saves_its_json_as_an_artifact(tmp_path, monkeypatch):
    from app.ai.tools.implementations import execute_mcp as mod

    monkeypatch.setattr(mod, "_upload_path", lambda name: str(tmp_path / name))
    db = _Db()
    ctx = {
        "db": db, "report": None,
        "organization": SimpleNamespace(id="org"), "user": SimpleNamespace(id="usr"),
    }
    tool = mod.ExecuteMCPTool()
    saved = await tool._materialize_to_json({"orders": [{"id": 1}]}, "list_orders", ctx)
    assert saved.source_kind == "artifact"
    assert db.added and db.added[0] is saved


@pytest.mark.asyncio
async def test_execute_mcp_saves_text_as_an_artifact(tmp_path, monkeypatch):
    from app.ai.tools.implementations import execute_mcp as mod

    monkeypatch.setattr(mod, "_upload_path", lambda name: str(tmp_path / name))
    ctx = {
        "db": _Db(), "report": None,
        "organization": SimpleNamespace(id="org"), "user": SimpleNamespace(id="usr"),
    }
    saved = await mod.ExecuteMCPTool()._materialize_to_text("a long log", "tail_log", ctx)
    assert saved.source_kind == "artifact"


def test_write_csv_and_read_excel_as_csv_tag_their_output():
    from app.ai.tools.implementations import read_excel_as_csv, write_csv

    assert 'source_kind="artifact"' in inspect.getsource(write_csv.WriteCsvTool.run_stream)
    assert 'source_kind="artifact"' in inspect.getsource(read_excel_as_csv)


def test_user_attachments_are_still_uploads():
    """attach_file deliberately attaches durable, user-visible files."""
    from app.ai.tools.implementations import attach_file

    assert 'source_kind="upload"' in inspect.getsource(attach_file)
