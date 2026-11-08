"""Tests for the tiered <files> context.

The files section renders every report file, but at two detail tiers:
  * full  — the existing rich preview (sample rows / PDF text)
  * index — a one-line structural summary; content stays behind
            read_file / inspect_data

Tier rules (decide_file_tiers): files mentioned or attached this turn are
always full; files snapshotted from an agent's (data source's) file library
are index-only; remaining user uploads stay full while a shared token budget
lasts (all of them, when the report has only a few).
"""
from datetime import datetime, timedelta

import pytest

from app.ai.context.builders.files_context_builder import decide_file_tiers
from app.ai.context.sections.files_schema_section import FilesSchemaContext
from app.services.file_preview import render_file_index_line


class _F:
    def __init__(self, id, description="x" * 400, created_at=None):
        self.id = id
        self.description = description
        self.created_at = created_at or datetime(2026, 1, 1)


def test_agent_files_are_index_only():
    files = [_F("a1"), _F("u1")]
    tiers = decide_file_tiers(files, agent_file_ids={"a1"}, forced_rich_ids=set())
    assert tiers["a1"] == "index"
    assert tiers["u1"] == "full"


def test_mentioned_agent_file_is_promoted_to_full():
    files = [_F("a1")]
    tiers = decide_file_tiers(files, agent_file_ids={"a1"}, forced_rich_ids={"a1"})
    assert tiers["a1"] == "full"


def test_small_report_keeps_all_user_files_full():
    files = [_F(f"u{i}", description="y" * 100_000) for i in range(3)]
    tiers = decide_file_tiers(files, agent_file_ids=set(), forced_rich_ids=set())
    assert all(v == "full" for v in tiers.values())


def test_budget_degrades_oldest_user_files_to_index():
    # 10 files x ~250 tokens each vs a 1000-token budget → newest ~4 stay full.
    files = [
        _F(f"u{i}", description="z" * 1000, created_at=datetime(2026, 1, 1 + i))
        for i in range(10)
    ]
    tiers = decide_file_tiers(
        files, agent_file_ids=set(), forced_rich_ids=set(), rich_budget_tokens=1000
    )
    full = [k for k, v in tiers.items() if v == "full"]
    index = [k for k, v in tiers.items() if v == "index"]
    assert len(full) == 4
    assert "u9" in full and "u8" in full          # newest kept rich
    assert "u0" in index and "u1" in index        # oldest degraded


def test_forced_files_consume_budget_but_never_degrade():
    files = [
        _F("m1", description="z" * 100_000),      # mentioned, huge
        _F("u1", description="z" * 1000, created_at=datetime(2026, 1, 2)),
        _F("u2", description="z" * 1000, created_at=datetime(2026, 1, 3)),
        _F("u3", description="z" * 1000, created_at=datetime(2026, 1, 4)),
        _F("u4", description="z" * 1000, created_at=datetime(2026, 1, 5)),
    ]
    tiers = decide_file_tiers(
        files, agent_file_ids=set(), forced_rich_ids={"m1"},
        rich_budget_tokens=1000, small_report_max=3,
    )
    assert tiers["m1"] == "full"                  # forced stays rich
    assert all(tiers[f"u{i}"] == "index" for i in range(1, 5))  # budget spent


def test_render_index_tier_has_summary_and_note_but_no_schema():
    ctx = FilesSchemaContext(files=[
        FilesSchemaContext.FileItem(
            id="a1", filename="big.xlsx", detail="index", origin="agent",
            index_summary="big.xlsx — Excel, sheets: S1 (100x5)",
            prompt_schema=None,
        ),
        FilesSchemaContext.FileItem(
            id="u1", filename="mine.csv", detail="full",
            prompt_schema="CSV File: mine.csv\nRow 0: {...}",
        ),
    ])
    out = ctx.render()
    assert 'detail="index"' in out
    assert 'source="agent"' in out
    assert "big.xlsx — Excel, sheets" in out
    assert "<note>" in out and "read_file" in out
    # Index entry carries no <schema>; the full entry does.
    assert out.count("<schema>") == 1
    assert "Row 0" in out


def test_render_all_full_has_no_note():
    ctx = FilesSchemaContext(files=[
        FilesSchemaContext.FileItem(id="u1", filename="a.csv", prompt_schema="s"),
    ])
    out = ctx.render()
    assert "<note>" not in out
    assert 'detail="index"' not in out


def test_render_file_index_line_csv_and_excel():
    csv_line = render_file_index_line(
        {"type": "csv", "filename": "sales.csv", "shape": [5000, 12],
         "columns": ["region", "amount"], "head": [{"region": "EU"}]},
        "/tmp/sales.csv",
    )
    assert "5000 rows x 12 cols" in csv_line
    assert "region" in csv_line
    assert "EU" not in csv_line                  # no sample rows in index tier

    xl_line = render_file_index_line(
        {"type": "excel", "filename": "q1.xlsx", "sheets": ["Rev", "Costs"],
         "sheet_count": 2,
         "sheet_previews": {
             "Rev": {"shape": [900, 8], "raw_cells": [["month", "revenue"], ["Jan", 100]]},
             "Costs": {"shape": [40, 3], "raw_cells": []},
         }},
        "/tmp/q1.xlsx",
    )
    assert "Rev (900x8)" in xl_line
    assert "month" in xl_line                    # header row surfaced
    assert "Jan" not in xl_line                  # data rows are not


def test_render_file_index_line_handles_missing_preview():
    line = render_file_index_line(None, "/tmp/x.bin", filename="x.bin")
    assert "x.bin" in line and "read_file" in line


# ── Agent-produced files are capped in the listing ───────────────────────
# A scheduled report gains one tool-output file per call per run. Listing them
# all made each run's prompt grow with the report's history.

from app.ai.context.builders.files_context_builder import GENERATED_FILES_MAX, limit_generated_files


class _G(_F):
    def __init__(self, id, source_kind="artifact", created_at=None):
        super().__init__(id, created_at=created_at)
        self.source_kind = source_kind


def _history(n, kind="artifact"):
    return [_G(f"g{i}", source_kind=kind, created_at=datetime(2026, 1, 1) + timedelta(hours=i))
            for i in range(n)]


@pytest.mark.parametrize("kind", ["artifact", "connector"])
@pytest.mark.parametrize("n", [GENERATED_FILES_MAX + 1, 57, 1128])
def test_listing_keeps_only_the_newest_generated_files(kind, n):
    files = _history(n, kind)
    kept, omitted = limit_generated_files(files, protected_ids=set())
    assert len(kept) == GENERATED_FILES_MAX
    assert omitted == n - GENERATED_FILES_MAX
    newest = {f"g{i}" for i in range(n - GENERATED_FILES_MAX, n)}
    assert {f.id for f in kept} == newest


def test_listing_size_does_not_grow_with_history():
    sizes = {len(limit_generated_files(_history(n), set())[0]) for n in (25, 100, 1000)}
    assert sizes == {GENERATED_FILES_MAX}


def test_uploads_and_protected_files_are_never_dropped():
    uploads = [_G(f"u{i}", source_kind="upload") for i in range(5)]
    history = _history(GENERATED_FILES_MAX + 30)
    protected = {"g0", "g1"}  # oldest generated, but mentioned / library
    kept, omitted = limit_generated_files(uploads + history, protected_ids=protected)
    ids = {f.id for f in kept}
    assert {f"u{i}" for i in range(5)} <= ids
    assert protected <= ids
    assert omitted == len(history) - len(protected) - GENERATED_FILES_MAX
    assert len([f for f in kept if f.source_kind != "upload" and f.id not in protected]) == GENERATED_FILES_MAX


def test_under_the_cap_nothing_is_dropped():
    files = _history(GENERATED_FILES_MAX)
    kept, omitted = limit_generated_files(files, set())
    assert kept == files and omitted == 0


def test_render_notes_how_many_generated_files_were_left_out():
    ctx = FilesSchemaContext(files=[FilesSchemaContext.FileItem(id="g1", filename="cost_report.json", detail="index",
                                                                index_summary="json", origin="agent")],
                             omitted_generated=42)
    out = ctx.render()
    assert 'omitted="42"' in out
    assert "<file " in out
    assert 'omitted=' not in FilesSchemaContext(files=ctx.files).render()
