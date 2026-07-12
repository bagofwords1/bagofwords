"""Unit tests for doc-artifact markdown helpers (_doc_markdown.py).

The invariants asserted here are the contract create_doc/edit_doc rely on:
- {{viz:<uuid>}} placeholders are extracted in document order, deduped
- placeholders quoted inside fenced code blocks (``` or ~~~) are NOT embeds
- find/replace edits require exactly-one match and apply atomically
- heading outline skips headings inside code fences
"""
import pytest

from app.ai.tools.implementations._doc_markdown import (
    MAX_DOC_CHARS,
    DocEditError,
    apply_find_replace_edits,
    extract_viz_placeholders,
    heading_outline,
)

V1 = "11111111-1111-1111-1111-111111111111"
V2 = "22222222-2222-2222-2222-222222222222"
V3 = "33333333-3333-3333-3333-333333333333"


class TestExtractVizPlaceholders:
    def test_extracts_in_document_order(self):
        md = f"Intro\n{{{{viz:{V2}}}}}\ntext\n{{{{viz:{V1}}}}}\n"
        assert extract_viz_placeholders(md) == [V2, V1]

    def test_tolerates_whitespace_and_case(self):
        md = f"{{{{ viz: {V1.upper()} }}}}"
        assert extract_viz_placeholders(md) == [V1]

    def test_dedupes_repeated_placeholders(self):
        md = f"{{{{viz:{V1}}}}}\n{{{{viz:{V1}}}}}"
        assert extract_viz_placeholders(md) == [V1]

    def test_skips_placeholders_inside_backtick_fences(self):
        md = f"{{{{viz:{V1}}}}}\n```\nquoted {{{{viz:{V2}}}}}\n```\n{{{{viz:{V3}}}}}"
        assert extract_viz_placeholders(md) == [V1, V3]

    def test_skips_placeholders_inside_tilde_fences(self):
        md = f"~~~\n{{{{viz:{V1}}}}}\n~~~\n{{{{viz:{V2}}}}}"
        assert extract_viz_placeholders(md) == [V2]

    def test_unclosed_fence_quotes_to_end_of_doc(self):
        md = f"```mermaid\ngraph TD\n{{{{viz:{V1}}}}}"
        assert extract_viz_placeholders(md) == []

    def test_empty_and_none_safe(self):
        assert extract_viz_placeholders("") == []
        assert extract_viz_placeholders(None) == []


class TestApplyFindReplaceEdits:
    def test_applies_single_edit(self):
        assert apply_find_replace_edits("a b c", [{"find": "b", "replace": "X"}]) == "a X c"

    def test_applies_sequential_edits(self):
        out = apply_find_replace_edits(
            "alpha beta", [{"find": "alpha", "replace": "A"}, {"find": "beta", "replace": "B"}]
        )
        assert out == "A B"

    def test_replace_may_be_empty_deletion(self):
        assert apply_find_replace_edits("keep DROP keep", [{"find": " DROP", "replace": ""}]) == "keep keep"

    def test_missing_match_raises_and_names_op(self):
        with pytest.raises(DocEditError) as e:
            apply_find_replace_edits("abc", [{"find": "zzz", "replace": "X"}])
        assert "op 1" in str(e.value)
        assert "not found" in str(e.value)

    def test_ambiguous_match_raises(self):
        with pytest.raises(DocEditError) as e:
            apply_find_replace_edits("dup dup", [{"find": "dup", "replace": "X"}])
        assert "2 times" in str(e.value)

    def test_atomic_failure_reports_failing_op_index(self):
        # Second op fails -> exception carries index; caller discards the result,
        # so the document is unchanged by construction.
        with pytest.raises(DocEditError) as e:
            apply_find_replace_edits(
                "one two", [{"find": "one", "replace": "1"}, {"find": "missing", "replace": "X"}]
            )
        assert e.value.op_index == 1

    def test_empty_edits_rejected(self):
        with pytest.raises(DocEditError):
            apply_find_replace_edits("abc", [])

    def test_empty_find_rejected(self):
        with pytest.raises(DocEditError):
            apply_find_replace_edits("abc", [{"find": "", "replace": "X"}])


class TestHeadingOutline:
    def test_collects_heading_levels(self):
        md = "# Title\ntext\n## Findings\n### Detail\n"
        assert heading_outline(md) == ["# Title", "## Findings", "### Detail"]

    def test_skips_headings_in_code_fences(self):
        md = "# Real\n```\n# not a heading\n```\n## Also real\n"
        assert heading_outline(md) == ["# Real", "## Also real"]

    def test_caps_items(self):
        md = "\n".join(f"# H{i}" for i in range(30))
        assert len(heading_outline(md, max_items=20)) == 20


def test_max_doc_chars_is_reasonable():
    # Guard against accidental order-of-magnitude edits to the cap.
    assert 20_000 <= MAX_DOC_CHARS <= 200_000
