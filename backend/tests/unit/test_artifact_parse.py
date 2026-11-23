"""Unit tests for the fast parse gate helpers (_artifact_parse).

bracket_balance_hint is the heuristic annotation attached to a confirmed Babel
failure; these tests pin its behavior on the incident case (a dropped close
paren inside a useMemo one-liner) and on the string/comment/template awareness
that keeps it from miscounting ordinary code.
"""
import pytest

from app.ai.tools.implementations._artifact_parse import bracket_balance_hint


class TestBracketBalanceHint:
    def test_balanced_code_returns_none(self):
        code = (
            "function App() {\n"
            "  const { visualizations } = useArtifactData();\n"
            "  const rows = vizById(\"abc\").data.rows.map((r) => ({ name: r.Name }));\n"
            "  return <div>{rows.length}</div>;\n"
            "}\n"
        )
        assert bracket_balance_hint(code) is None

    def test_incident_missing_close_paren(self):
        # Reproduction of the production failure: one ')' dropped from a
        # paren-dense useMemo one-liner ("Unexpected token, expected ','").
        code = (
            "function App() {\n"
            "  const d = vizById(\"abc\");\n"
            "  const kpis = useMemo(() => d.data.rows.reduce((a, r) => a + (Number(r.Total) || 0), 0);return kpis;\n"
            "}\n"
        )
        hint = bracket_balance_hint(code)
        assert hint is not None
        assert "unclosed '('" in hint
        assert "line 3" in hint

    def test_extra_closer_reported_with_position(self):
        code = "const x = (1 + 2));\n"
        hint = bracket_balance_hint(code)
        assert hint is not None
        assert "extra ')'" in hint
        assert "line 1" in hint

    def test_brackets_in_strings_ignored(self):
        code = "const s = \"(((\";\nconst t = ')]}';\nconst u = `))`;\n"
        assert bracket_balance_hint(code) is None

    def test_brackets_in_comments_ignored(self):
        code = "// ((( never closed\nconst x = 1; /* ]]] )) */\n"
        assert bracket_balance_hint(code) is None

    def test_template_hole_brackets_count(self):
        # A real unclosed paren inside ${...} must still be caught.
        code = "const s = `total: ${fmt(x};`;\n"
        hint = bracket_balance_hint(code)
        assert hint is not None
        assert "unclosed '('" in hint

    def test_balanced_template_hole(self):
        code = "const s = `total: ${fmt(x)} done`;\n"
        assert bracket_balance_hint(code) is None

    def test_escaped_quote_does_not_end_string(self):
        code = "const s = \"a \\\" ( b\";\nconst x = (1);\n"
        assert bracket_balance_hint(code) is None

    def test_multiple_unclosed_counts(self):
        code = "const x = ((([\n"
        hint = bracket_balance_hint(code)
        assert hint is not None
        assert "4 unclosed" in hint


class TestParseCheckIntegration:
    """Exercise the deployment compiler without a browser."""

    @pytest.mark.asyncio
    async def test_broken_and_clean_and_wrapped(self, monkeypatch):
        monkeypatch.setenv("BOW_CHROMIUM_EXECUTABLE", "/missing/chromium")
        from app.ai.tools.implementations._artifact_parse import parse_check_page_code

        broken = (
            "function App() {\n"
            "  const total = useMemo(() => [1,2].reduce((a, r) => a + r, 0;\n"
            "  return <div>{total}</div>;\n"
            "}\n"
        )
        err = await parse_check_page_code(broken)
        assert err is not None
        assert "Unexpected token" in err or "expected" in err
        assert "bracket-balance hint" in err

        clean = broken.replace(", 0;", ", 0), []);")
        assert await parse_check_page_code(clean) is None

        # Stored wrapper shape must be unwrapped before parsing.
        wrapped = f'<script type="text/babel">\n{clean}</script>'
        assert await parse_check_page_code(wrapped) is None

    @pytest.mark.asyncio
    async def test_unavailable_compiler_cannot_approve_valid_code(self, monkeypatch):
        from app.ai.tools.implementations._artifact_parse import ParseCheckUnavailable, parse_check_page_code

        monkeypatch.setenv("PLAYWRIGHT_NODEJS_PATH", "/missing/compiler")
        with pytest.raises(ParseCheckUnavailable):
            await parse_check_page_code("const page = <div />;")
