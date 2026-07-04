"""Unit tests for the release-notes (CHANGELOG.md) parser.

Pure logic — no DB, no HTTP. Verifies the contract the /api/changelog route
promises: version headings become sections, bullets become entries, inline
markdown survives, and malformed input degrades gracefully.
"""

from app.routes.changelog import _parse_changelog


SAMPLE = """# Release Notes

## Version 1.2.3 (July 4, 2026)
- **Bold title** — a change with `code` and a detail.
- A second entry
  that wraps across two source lines.

## Version 1.2.2 (July 3, 2026)
- Single entry

## Version 1.2.1
- Entry with no date on the heading
"""


def test_parses_all_version_headings_in_order():
    versions = _parse_changelog(SAMPLE)
    assert [v["version"] for v in versions] == ["1.2.3", "1.2.2", "1.2.1"]


def test_captures_date_when_present_and_none_when_absent():
    versions = _parse_changelog(SAMPLE)
    by_version = {v["version"]: v for v in versions}
    assert by_version["1.2.3"]["date"] == "July 4, 2026"
    assert by_version["1.2.1"]["date"] is None


def test_bullets_become_entries_preserving_inline_markdown():
    versions = _parse_changelog(SAMPLE)
    first = versions[0]
    assert len(first["entries"]) == 2
    # Inline markdown is preserved raw for the frontend to render.
    assert "**Bold title**" in first["entries"][0]
    assert "`code`" in first["entries"][0]


def test_wrapped_continuation_lines_join_into_one_entry():
    versions = _parse_changelog(SAMPLE)
    second_entry = versions[0]["entries"][1]
    assert "wraps across two source lines" in second_entry
    # The wrap collapsed to a single entry, not two.
    assert "\n" not in second_entry


def test_content_before_first_heading_is_ignored():
    versions = _parse_changelog("Preamble text\n- stray bullet\n\n## Version 9.9.9\n- real")
    assert len(versions) == 1
    assert versions[0]["version"] == "9.9.9"
    assert versions[0]["entries"] == ["real"]


def test_empty_input_yields_no_versions():
    assert _parse_changelog("") == []
    assert _parse_changelog("# Just a title, no versions") == []


def test_version_with_no_entries_is_still_returned():
    versions = _parse_changelog("## Version 2.0.0 (date)\n\n## Version 1.0.0\n- x")
    assert [v["version"] for v in versions] == ["2.0.0", "1.0.0"]
    assert versions[0]["entries"] == []
