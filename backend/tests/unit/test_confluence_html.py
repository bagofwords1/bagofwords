"""Confluence export_view HTML -> text: the shapes the agent reads (tables, code, lists, panels)."""
import pytest

from app.data_sources.clients._confluence_html import html_to_text


@pytest.mark.parametrize("value", [None, "", "   ", 42])
def test_no_content_is_empty_text(value):
    assert html_to_text(value) == ""


def test_tables_become_pipe_tables_with_a_header_row():
    text = html_to_text(
        "<table><tr><th>Region</th><th>Revenue</th></tr><tr><td>EMEA</td><td>1,200</td></tr>"
        "<tr><td>APAC</td><td>900</td></tr></table>")
    assert text.splitlines() == ["| Region | Revenue |", "| --- | --- |", "| EMEA | 1,200 |", "| APAC | 900 |"]


def test_ragged_rows_are_padded_and_pipes_and_breaks_inside_cells_are_safe():
    text = html_to_text("<table><tr><td>a</td><td>b|c</td><td>x</td></tr><tr><td>line<br/>two</td></tr></table>")
    rows = text.splitlines()
    assert rows[0] == "| a | b\\|c | x |"
    assert rows[2].replace(" ", "") == "|linetwo|||"


def test_a_table_nested_in_a_cell_flows_into_that_cell():
    text = html_to_text("<table><tr><td>outer<table><tr><td>in1</td><td>in2</td></tr></table></td>"
                        "<td>right</td></tr></table>")
    assert text.splitlines()[0] == "| outer in1 in2 | right |"


def test_code_blocks_keep_their_lines_and_inline_code_its_backticks():
    text = html_to_text("<p>Run <code>make</code>:</p><pre>line 1\n  line 2</pre>")
    assert "Run `make`:" in text
    assert "```\nline 1\n  line 2\n```" in text


def test_lists_keep_markers_and_nesting():
    text = html_to_text("<ol><li>first</li><li>second<ul><li>child</li></ul></li></ol>")
    assert text.splitlines() == ["1. first", "2. second", "  - child"]


def test_headings_panels_entities_and_scripts():
    text = html_to_text(
        '<h2>Title &amp; more</h2><div class="confluence-information-macro confluence-information-macro-note">'
        "<p>Mind the gap</p></div><script>alert(1)</script><style>p{}</style>"
        '<p>caf&eacute; <img src="x.png" alt="diagram"></p>')
    assert "## Title & more" in text
    assert "[Note]" in text and "Mind the gap" in text
    assert "alert" not in text and "p{}" not in text
    assert "café [image: diagram]" in text


def test_unclosed_markup_still_renders():
    assert "| a | b |" in html_to_text("<table><tr><td>a</td><td>b")
