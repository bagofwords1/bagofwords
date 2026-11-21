"""Confluence rendered HTML (``body.export_view``) to compact markdown-like text.

``export_view`` is the page as Confluence renders it for export: macros are
already expanded (includes, excerpts, Jira tables, page properties), links are
absolute and the editor chrome is gone. That makes it the one body format that
reads the same on every Data Center version, so the connector asks for it and
turns it into text here:

  * headings become ``#`` lines, list items ``-`` / ``1.`` lines (nested lists
    are indented), ``<pre>`` blocks become fenced code, ``<code>`` backticks;
  * tables become pipe tables (the first row is the header; cell line breaks
    become spaces and pipes are escaped), so an agent can read them as data;
  * Confluence info / note / warning / tip panels are labelled ``[Warning]``;
  * scripts, styles and images (except their alt text) are dropped.

Pure stdlib (``html.parser``), one linear pass, entities decoded by the parser.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

_BLOCK_TAGS = frozenset((
    "p", "div", "section", "article", "header", "footer", "blockquote", "dl", "dt", "dd", "figure",
    "figcaption", "hr", "address", "details", "summary", "main", "nav", "aside",
))
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_SKIP_TAGS = frozenset(("script", "style", "noscript", "template", "head", "title"))
_VOID_TAGS = frozenset(("br", "hr", "img", "input", "meta", "link", "col", "wbr", "area", "base", "source"))
_PANEL_RE = re.compile(r"confluence-information-macro-(information|info|note|warning|tip)\b")
_PANEL_LABELS = {"information": "Info", "info": "Info", "note": "Note", "warning": "Warning", "tip": "Tip"}
_SPACES_RE = re.compile(r"[ \t\r\f\v ]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_MAX_TABLE_DEPTH = 8


class _Table:
    def __init__(self):
        self.rows: list[list[str]] = []
        self.header_rows: set[int] = set()
        self.cell: list[str] | None = None
        self.row: list[str] | None = None

    def close_cell(self) -> None:
        if self.cell is not None and self.row is not None:
            text = _SPACES_RE.sub(" ", "".join(self.cell)).replace("\n", " ").strip()
            self.row.append(text.replace("|", "\\|"))
        self.cell = None

    def render(self) -> str:
        self.close_cell()
        rows = [r for r in self.rows if any(c.strip() for c in r)]
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + "|".join([" --- "] * width) + "|"]
        lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n".join(lines)


class _Converter(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.pre = 0
        self.lists: list[list] = []          # [kind, counter]
        self.tables: list[_Table] = []
        self.panel_stack: list[bool] = []    # per open div: did it open a panel label

    # -------------------------------------------------------------- output
    def _real_table(self) -> "_Table | None":
        """The innermost real table (nested tables flow into its open cell)."""
        for table in reversed(self.tables):
            if isinstance(table, _Table):
                return table
        return None

    def _emit(self, text: str) -> None:
        table = self._real_table()
        if table is None:
            self.out.append(text)
        elif table.cell is not None:
            table.cell.append(text)
        # else: text between cells (whitespace, stray nodes) is dropped

    def _newline(self, count: int = 1) -> None:
        table = self._real_table()
        if table is None:
            # Top up to `count` trailing newlines rather than stacking them.
            tail = 0
            for chunk in reversed(self.out):
                stripped = chunk.rstrip("\n")
                tail += len(chunk) - len(stripped)
                if stripped:
                    break
            if self.out and tail < count:
                self.out.append("\n" * (count - tail))
        elif table.cell is not None:
            table.cell.append(" ")

    # -------------------------------------------------------------- tags
    def handle_starttag(self, tag, attrs):  # noqa: C901 - one switch over tag names
        if tag in _SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        attr = dict(attrs)
        if tag == "table":
            if len(self.tables) >= _MAX_TABLE_DEPTH:
                return
            outer = self._real_table()
            if outer is not None and outer.cell is not None:
                # A table inside a cell: flatten its text into that cell.
                self.tables.append(_NestedMarker())   # type: ignore[arg-type]
                return
            self._newline(2)
            self.tables.append(_Table())
            return
        if self.tables and isinstance(self.tables[-1], _NestedMarker):
            if tag in ("td", "th", "tr"):
                self._newline()
            return
        if tag == "tr" and self.tables:
            table = self.tables[-1]
            table.close_cell()
            table.row = []
            table.rows.append(table.row)
            return
        if tag in ("td", "th") and self.tables:
            table = self.tables[-1]
            table.close_cell()
            if table.row is None:
                table.row = []
                table.rows.append(table.row)
            table.cell = []
            if tag == "th":
                table.header_rows.add(len(table.rows) - 1)
            return
        if tag in _HEADINGS:
            self._newline(2)
            self._emit("#" * _HEADINGS[tag] + " ")
            return
        if tag in ("ul", "ol"):
            self.lists.append([tag, 0])
            self._newline()
            return
        if tag == "li":
            self._newline()
            depth = max(0, len(self.lists) - 1)
            marker = "-"
            if self.lists and self.lists[-1][0] == "ol":
                self.lists[-1][1] += 1
                marker = f"{self.lists[-1][1]}."
            self._emit("  " * depth + marker + " ")
            return
        if tag == "pre":
            self.pre += 1
            self._newline(2)
            self._emit("```\n")
            return
        if tag == "code" and not self.pre:
            self._emit("`")
            return
        if tag == "br":
            self._newline()
            return
        if tag == "hr":
            self._newline(2)
            self._emit("---")
            self._newline(2)
            return
        if tag == "img":
            alt = (attr.get("alt") or "").strip()
            if alt:
                self._emit(f"[image: {alt}]")
            return
        if tag == "div":
            match = _PANEL_RE.search(attr.get("class") or "")
            self.panel_stack.append(bool(match))
            self._newline()
            if match:
                self._emit(f"[{_PANEL_LABELS[match.group(1)]}] ")
            return
        if tag in _BLOCK_TAGS:
            self._newline()

    def handle_endtag(self, tag):  # noqa: C901
        if tag in _SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "table":
            if not self.tables:
                return
            table = self.tables.pop()
            if isinstance(table, _NestedMarker):
                return
            rendered = table.render()
            if rendered:
                self.out.append(rendered)
                self.out.append("\n\n")
            return
        if self.tables and isinstance(self.tables[-1], _NestedMarker):
            return
        if tag in ("td", "th") and self.tables:
            self.tables[-1].close_cell()
            return
        if tag == "tr" and self.tables:
            self.tables[-1].close_cell()
            self.tables[-1].row = None
            return
        if tag in _HEADINGS:
            self._newline(2)
            return
        if tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            self._newline()
            return
        if tag == "pre":
            self.pre = max(0, self.pre - 1)
            self._emit("\n```")
            self._newline(2)
            return
        if tag == "code" and not self.pre:
            self._emit("`")
            return
        if tag == "div":
            if self.panel_stack:
                self.panel_stack.pop()
            self._newline()
            return
        if tag in _BLOCK_TAGS or tag == "li":
            self._newline()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self.skip or not data:
            return
        if self.pre:
            self._emit(data)
            return
        text = _SPACES_RE.sub(" ", data.replace("\n", " "))
        if text.strip() or (self.out and not self.out[-1].endswith((" ", "\n"))):
            self._emit(text)


class _NestedMarker:
    """Stands on the table stack for a table nested in a cell; its text flows into that cell."""
    cell = None
    row = None


def html_to_text(value) -> str:
    """Markdown-like plain text for one Confluence ``export_view`` body; "" for no content."""
    if not isinstance(value, str) or not value.strip():
        return ""
    parser = _Converter()
    parser.feed(value)
    parser.close()
    # Nested-table text lands in the enclosing cell through _emit; unclosed tables still render.
    while parser.tables:
        table = parser.tables.pop()
        if isinstance(table, _Table):
            parser.out.append("\n\n" + table.render() + "\n\n")
    lines, in_code = [], False
    for line in "".join(parser.out).split("\n"):
        if line.strip() == "```":
            in_code = not in_code
            lines.append("```")
        elif in_code:
            lines.append(line.rstrip())
        else:
            # Keep a list item's leading indent (nesting); collapse runs of blanks elsewhere.
            indent = len(line) - len(line.lstrip(" "))
            body = _SPACES_RE.sub(" ", line).strip()
            is_item = bool(re.match(r"(?:-|\d+\.) ", body))
            lines.append((" " * indent if is_item else "") + body)
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()
