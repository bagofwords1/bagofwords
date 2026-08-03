"""Connector files are a report-scoped cache, not a per-read upload.

A connector file has to be materialized to local disk because generated code
runs sandboxed — no network, no credentials, no `open()` — so
`excel_files[N].path` is its only way to the data. What it must NOT be is a new
`File` row and a new copy on disk for every read, unlinked from the report:
that left `session_file_id` dead the moment the turn ended (the model would
hand it back to create_data and get "None of the requested source files
exist"), and leaked a file per read.

These pin the three halves of the fix: the cache key, the prompt-box
classification, and the context tier.
"""
import types
from datetime import datetime

import pytest

from app.ai.context.builders.files_context_builder import decide_file_tiers

_CREATED = datetime(2026, 1, 1)


def _file(fid, *, source_kind="upload", description="x" * 400, created_at=_CREATED):
    return types.SimpleNamespace(
        id=fid, source_kind=source_kind, description=description, created_at=created_at,
    )


# ── context tier ───────────────────────────────────────────────────────────

def test_connector_files_are_index_tier_not_full():
    """Report-linking them would otherwise drop them into the user-file bucket,
    where a small report renders everything full."""
    files = [_file("c1", source_kind="connector"), _file("c2", source_kind="connector")]
    tiers = decide_file_tiers(files, agent_file_ids=set(), forced_rich_ids=set())
    assert tiers == {"c1": "index", "c2": "index"}


def test_user_uploads_still_render_full():
    """The tier change must not touch actual uploads."""
    files = [_file("u1"), _file("u2")]
    tiers = decide_file_tiers(files, agent_file_ids=set(), forced_rich_ids=set())
    assert tiers == {"u1": "full", "u2": "full"}


def test_mention_still_forces_a_connector_file_full():
    """Rule 1 outranks the connector rule — an @-mentioned file is wanted in
    full whatever its origin."""
    files = [_file("c1", source_kind="connector")]
    tiers = decide_file_tiers(files, agent_file_ids=set(), forced_rich_ids={"c1"})
    assert tiers["c1"] == "full"


def test_connector_files_do_not_consume_the_rich_budget():
    """Read-heavy runs accumulate connector files; if they spent budget they
    would starve the user's own uploads of full previews."""
    connectors = [_file(f"c{i}", source_kind="connector") for i in range(20)]
    uploads = [_file(f"u{i}") for i in range(5)]
    tiers = decide_file_tiers(
        connectors + uploads, agent_file_ids=set(), forced_rich_ids=set(),
        rich_budget_tokens=200, small_report_max=3,
    )
    assert all(tiers[f"c{i}"] == "index" for i in range(20))
    assert any(tiers[f"u{i}"] == "full" for i in range(5))


# ── prompt-box classification ──────────────────────────────────────────────

def test_from_data_source_covers_connector_files():
    """PromptBoxV2 hides files where from_data_source is true. Connector files
    have no data_source_file_association row — that table is the agent's
    uploaded knowledge library, and adding them there would list every read in
    the Agents panel — so the flag has to come off source_kind."""
    from app.services.file_service import is_agent_owned_file

    assert is_agent_owned_file(_file("c1", source_kind="connector"), set()) is True


def test_from_data_source_still_covers_library_files():
    from app.services.file_service import is_agent_owned_file

    assert is_agent_owned_file(_file("a1"), {"a1"}) is True


def test_user_uploads_stay_visible_in_the_prompt_box():
    """The whole point of the flag — a file the user actually attached must
    still show as a chip."""
    from app.services.file_service import is_agent_owned_file

    assert is_agent_owned_file(_file("u1"), set()) is False


# ── cache key ──────────────────────────────────────────────────────────────

def test_file_model_carries_the_cache_key():
    """Without (source_connection_id, source_ref) there is nothing to dedupe on
    and every read mints another row."""
    from app.models.file import File

    cols = File.__table__.columns
    assert "source_connection_id" in cols
    assert "source_ref" in cols
    assert cols["source_connection_id"].nullable, "uploads have no remote counterpart"
    assert cols["source_ref"].nullable


@pytest.mark.asyncio
async def test_cache_lookup_skips_uploads_and_unkeyed_calls():
    """An upload, or a connector call that can't name the remote file, must fall
    through to minting — i.e. exactly the old behaviour, never a wrong hit."""
    from app.ai.tools.implementations._file_tool_common import _find_cached_connector_file

    report = types.SimpleNamespace(id="r1")

    async def _fail(*a, **k):  # any DB touch here is a bug
        raise AssertionError("must not query for an unkeyed lookup")

    db = types.SimpleNamespace(execute=_fail)

    assert await _find_cached_connector_file(
        db, report=report, connection_id="c", source_ref="f", source_kind="upload") is None
    assert await _find_cached_connector_file(
        db, report=report, connection_id=None, source_ref="f", source_kind="connector") is None
    assert await _find_cached_connector_file(
        db, report=report, connection_id="c", source_ref=None, source_kind="connector") is None


@pytest.mark.asyncio
async def test_cache_lookup_failure_falls_back_to_minting():
    """A broken lookup must degrade to the old path, not kill the read."""
    from app.ai.tools.implementations._file_tool_common import _find_cached_connector_file

    async def _boom(*a, **k):
        raise RuntimeError("db down")

    db = types.SimpleNamespace(execute=_boom)
    got = await _find_cached_connector_file(
        db, report=types.SimpleNamespace(id="r1"),
        connection_id="c", source_ref="f", source_kind="connector",
    )
    assert got is None


# ── report-linking side effects ────────────────────────────────────────────
# Connector files are now in report.files so their session_file_id survives the
# turn boundary. Everything that reads report.files therefore sees them, and
# one of those readers must not.

def test_connector_images_are_not_treated_as_user_vision_attachments():
    """_load_images_as_input falls back to "most recent images on the report"
    when a turn uploads none. If a connector-fetched picture were in that set,
    reading it once would attach it to every later turn as though the user had
    uploaded it. Tool images reach the model via _collect_vision_images
    instead, bounded by _VISION_IMAGE_RETENTION_LOOPS.
    """
    import inspect
    from app.ai import agent_v2

    src = inspect.getsource(agent_v2.AgentV2.__init__)
    marker = "self.image_files = ["
    assert marker in src
    block = src.split(marker, 1)[1].split("]", 1)[0]
    assert "connector" in block, (
        "image_files must exclude connector files, or an agent-read picture "
        "becomes a permanent user vision attachment"
    )


def test_connector_files_stay_in_analysis_files():
    """They must remain readable by the code sandbox — that is the entire
    reason they are materialized to local disk."""
    import inspect
    from app.ai import agent_v2

    src = inspect.getsource(agent_v2.AgentV2.__init__)
    marker = "self.analysis_files = ["
    block = src.split(marker, 1)[1].split("]", 1)[0]
    assert "connector" not in block, (
        "analysis_files must NOT filter out connector files — generated code "
        "reads them via excel_files[N].path"
    )


# ── materialized display name ──────────────────────────────────────────────

@pytest.mark.parametrize("file_id,ext,expected", [
    ("sales.csv", "csv", "sales.csv"),          # path-addressed connector id
    ("data/q3/sales.csv", "csv", "sales.csv"),  # nested path
    ("01LZCXOPAQUE", "csv", "01LZCXOPAQUE.csv"),  # opaque provider id
    ("notes", "txt", "notes.txt"),
    ("payload.json", "json", "payload.json"),
    ("SALES.CSV", "csv", "SALES.CSV"),          # extension match is case-insensitive
])
def test_materialized_name_does_not_double_the_extension(file_id, ext, expected):
    """The copy used to be named f"{file_id}.{ext}" — "sales.csv" became
    "sales.csv.csv". Harmless while connector files were orphaned; once they
    are report-linked the name reaches the model's file context, and it reads
    the name back as a file id: "read_file failed: File not found:
    sales.csv.csv". Caught by the live sandbox, not by any unit test.
    """
    from app.ai.tools.implementations.read_file import _materialized_name

    assert _materialized_name(file_id, ext) == expected


def test_both_persist_paths_use_the_naming_helper():
    """There are TWO materialize paths and they must agree.

    The live run took the CACHED-render path (_persist_rendered_session) while
    only the fresh-read path had been fixed, so the bad name survived a "fix"
    that passed its own unit test. Pin both.
    """
    import inspect
    from app.ai.tools.implementations import read_file as rf

    for fn in (rf._persist_session_file, rf.ReadFileTool._persist_rendered_session):
        src = inspect.getsource(fn)
        assert "_materialized_name(file_id," in src, f"{fn.__name__} bypasses the helper"
        assert 'filename=f"{file_id}.' not in src, f"{fn.__name__}: doubling form is back"
