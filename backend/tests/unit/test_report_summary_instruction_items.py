"""The report summary must survive a tool call that set no optional arguments.

`arguments_json` is a FULL Pydantic dump (ProjectManager.
start_tool_execution_from_models), so every optional field of the tool's input
is present with value None — the key is never absent. `args.get("category",
"general")` therefore returns None rather than its default, and
SummaryInstructionItem.category is a non-optional `str`, so GET
/reports/{id}/summary 500s on any edit that didn't set a category.

Latent until edit_instruction's guidance started telling models to leave
category/load_mode/confidence alone unless the request is about them — at which
point models correctly stopped sending them and the endpoint began failing.

Also pins that the item's title and line count describe the INSTRUCTION, not
the edit's input: for an anchored edit `arguments_json["text"]` is a snippet.
"""
import pytest

from app.schemas.report_summary_schema import SummaryInstructionItem


def _item_from(args: dict, rj: dict, *, is_edit: bool, prev=None):
    """The construction under test, mirroring report_service.get_report_summary."""
    text = rj.get("new_text") or rj.get("text") or args.get("text") or ""
    title = text.split("\n")[0].replace("#", "").strip()[:60] if text else None
    return SummaryInstructionItem(
        instruction_id=rj["instruction_id"],
        title=(prev.title if prev is not None else None) or title or "Instruction",
        category=args.get("category") or (prev.category if prev is not None else None) or "general",
        is_edit=is_edit or (prev.is_edit if prev is not None else False),
        line_count=(
            len([l for l in text.split("\n") if l.strip()]) if text
            else (prev.line_count if prev is not None else 0)
        ),
        message_id="c1",
        build_id=rj.get("build_id"),
    )


# A real edit_instruction dump: every optional field present, all None.
FULL_DUMP_NO_METADATA = {
    "instruction_id": "i1", "old_text": "Use net amounts.",
    "text": "Use net amounts (excluding VAT).", "replace_entire_text": False,
    "title": None, "category": None, "confidence": None, "evidence": "User said so.",
    "load_mode": None, "table_names": None,
}


def test_null_category_does_not_break_the_summary():
    item = _item_from(
        FULL_DUMP_NO_METADATA,
        {"success": True, "instruction_id": "i1",
         "new_text": "Revenue excludes cancelled orders.\nUse net amounts (excluding VAT)."},
        is_edit=True,
    )
    assert item.category == "general"


def test_explicit_category_still_wins():
    args = {**FULL_DUMP_NO_METADATA, "category": "code_gen"}
    item = _item_from(args, {"success": True, "instruction_id": "i1", "new_text": "x\ny"},
                      is_edit=True)
    assert item.category == "code_gen"


def test_title_and_line_count_describe_the_instruction_not_the_snippet():
    """The anchored input is a fragment; the summary must not describe that."""
    rj = {
        "success": True, "instruction_id": "i1",
        "new_text": "Revenue excludes cancelled orders.\nUse net amounts (excluding VAT).",
    }
    item = _item_from(FULL_DUMP_NO_METADATA, rj, is_edit=True)
    assert item.title == "Revenue excludes cancelled orders."
    assert item.line_count == 2
    # The snippet would have given a 1-line item titled after the fragment.
    assert item.title != FULL_DUMP_NO_METADATA["text"]


def test_create_uses_the_stored_text():
    args = {"text": "Exclude test accounts.", "category": None, "confidence": 0.9}
    item = _item_from(args, {"success": True, "instruction_id": "i2",
                             "text": "Exclude test accounts."}, is_edit=False)
    assert item.title == "Exclude test accounts."
    assert item.category == "general"
    assert item.is_edit is False


def test_metadata_only_edit_carries_previous_values_forward():
    """No text at all — the item must inherit the earlier entry rather than
    collapse to an empty title and a zero line count."""
    prev = _item_from(FULL_DUMP_NO_METADATA,
                      {"success": True, "instruction_id": "i1", "new_text": "A\nB\nC"},
                      is_edit=True)
    args = {"instruction_id": "i1", "text": None, "old_text": None,
            "category": None, "confidence": 0.95}
    item = _item_from(args, {"success": True, "instruction_id": "i1"},
                      is_edit=True, prev=prev)
    assert item.title == prev.title
    assert item.line_count == 3
    assert item.category == "general"
