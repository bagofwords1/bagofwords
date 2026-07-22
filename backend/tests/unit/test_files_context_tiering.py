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
from datetime import datetime

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
