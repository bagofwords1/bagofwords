"""The themed artifact runtime (v11): theme registry parity between the prompt
and the sandbox globals, the design gate, the runtime stamp, and the assets
every shell must carry so airgapped deployments render the same kit."""
import re
from pathlib import Path

from app.ai.tools.implementations._artifact_refs import design_errors
from app.ai.tools.implementations._sandbox_context import (
    ARTIFACT_RUNTIME_VERSION,
    ARTIFACT_THEMES,
    SANDBOX_RUNTIME_OBSERVATION,
    SANDBOX_RUNTIME_PROMPT,
)

ROOT = Path(__file__).resolve().parents[3]
GLOBALS = ROOT / "frontend" / "public" / "libs" / "artifact-globals.js"
TAILWIND = ROOT / "frontend" / "public" / "libs" / "artifact-tailwind.js"
IFRAME = ROOT / "frontend" / "utils" / "artifactIframe.ts"
VENDOR = ROOT / "scripts" / "download-vendor-libs.sh"

THEMED = {"runtime": {"version": ARTIFACT_RUNTIME_VERSION}, "visualizations": []}
LEGACY = {"runtime": {"version": 0}, "visualizations": []}


def test_theme_registry_matches_sandbox_globals():
    src = GLOBALS.read_text(encoding="utf-8")
    for name in ARTIFACT_THEMES:
        assert re.search(r"^\s+%s: \{" % name, src, re.M), f"theme {name} missing from artifact-globals.js"
        assert name in SANDBOX_RUNTIME_PROMPT, f"theme {name} not taught to the planner"
        assert name in SANDBOX_RUNTIME_OBSERVATION
    # The legacy look is applied automatically, never offered as a choice.
    assert "legacy: {" in src
    assert "ledger" in SANDBOX_RUNTIME_PROMPT and "legacy" not in SANDBOX_RUNTIME_PROMPT.split("Built-in themes")[1].split("Overrides")[0]


def test_runtime_version_matches_globals_and_iframe():
    src = GLOBALS.read_text(encoding="utf-8")
    assert f"var RUNTIME_VERSION = {ARTIFACT_RUNTIME_VERSION};" in src
    # The cache-buster must move with the runtime generation so stale copies never serve the new kit.
    assert f"ARTIFACT_GLOBALS_VERSION = '{ARTIFACT_RUNTIME_VERSION}'" in IFRAME.read_text(encoding="utf-8")


def test_design_gate_requires_set_theme_on_themed_runtime():
    code = '<script type="text/babel">\nfunction App() { return <div className="bg-bg text-ink" />; }\n</script>'
    errs = design_errors(code, THEMED)
    assert len(errs) == 1 and "setTheme" in errs[0]
    assert design_errors("setTheme('ledger');\n" + code, THEMED) == []


def test_design_gate_never_retro_fails_legacy_artifacts():
    code = '<div className="bg-white text-slate-900 border-slate-200 bg-slate-50 text-slate-500 border-slate-100 bg-blue-50" />'
    assert design_errors(code, LEGACY) == []
    assert design_errors(code, {"visualizations": []}) == []


def test_design_gate_flags_raw_palette_soup():
    code = "setTheme('slate');\n" + '<div className="bg-slate-50 text-slate-900 border-slate-200 bg-gray-100 text-gray-500 border-blue-200 bg-blue-500" />'
    errs = design_errors(code, THEMED)
    assert any("raw palette" in e for e in errs)
    # a couple of stray classes are tolerated (an accent border here or there)
    assert design_errors("setTheme('slate');<div className='bg-blue-50 text-slate-500' />", THEMED) == []


def test_design_gate_rejects_positional_access_on_themed_runtime():
    code = "setTheme('slate');\nconst rows = data.visualizations[0].rows;"
    errs = design_errors(code, THEMED)
    assert any("by position" in e for e in errs)
    assert design_errors(code, LEGACY) == []


def test_tailwind_config_maps_every_token():
    src = TAILWIND.read_text(encoding="utf-8")
    for tok in ("bg", "surface", "surface-2", "line", "line-2", "ink", "ink-2", "ink-3", "accent", "accent-2", "accent-ink", "positive", "warning", "negative"):
        assert f"{tok}: token('{tok}')" in src or f"'{tok}': token('{tok}')" in src, tok
    for fam in ("display", "body", "mono", "numeric"):
        assert f"{fam}: 'var(--bow-font-{fam})'" in src
    assert "card: 'var(--bow-radius-lg)'" in src


def test_every_shell_loads_the_same_kit():
    """Live iframe, MCP app, standalone sandbox and the headless/export inliner
    must all carry the tailwind config, lucide and the fonts — the artifact
    must look identical wherever it renders, including offline."""
    from app.services import artifact_libs

    assert "artifact-tailwind.js" in artifact_libs._PAGE_LIBS
    assert "lucide.min.js" in artifact_libs._PAGE_LIBS
    assert "artifact-tailwind.js" in artifact_libs._EXPORT_LIBS
    assert "lucide.min.js" in artifact_libs._EXPORT_LIBS
    iframe = IFRAME.read_text(encoding="utf-8")
    for asset in ("artifact-tailwind.js", "lucide.min.js", "artifact-fonts.css"):
        assert asset in iframe, asset
    for shell in ("mcp-artifact-app.html", "artifact-sandbox.html"):
        html = (ROOT / "frontend" / "public" / shell).read_text(encoding="utf-8")
        for asset in ("artifact-tailwind.js", "lucide.min.js", "artifact-fonts.css"):
            assert asset in html, f"{shell} lacks {asset}"


def test_vendor_script_downloads_fonts_and_icons_locally():
    """Airgap contract: nothing in the kit reaches a CDN at render time."""
    sh = VENDOR.read_text(encoding="utf-8")
    assert "lucide" in sh and "FONT_SPECS" in sh and "artifact-fonts.css" in sh
    globals_src = GLOBALS.read_text(encoding="utf-8")
    assert "fonts.googleapis.com" not in globals_src
    assert "cdn." not in globals_src


def test_inline_fonts_are_data_uris(tmp_path, monkeypatch):
    from app.services import artifact_libs

    libs = tmp_path / "libs"
    (libs / "fonts").mkdir(parents=True)
    (libs / "artifact-globals.js").write_text("// globals")
    (libs / "fonts" / "x.woff2").write_bytes(b"\x00\x01\x02")
    (libs / "artifact-fonts.css").write_text("@font-face { font-family: 'X'; src: url(/libs/fonts/x.woff2) format('woff2'); }")
    monkeypatch.setattr(artifact_libs, "_CANDIDATE_DIRS", [libs])
    artifact_libs._inline_fonts_css_cached.cache_clear()
    style = artifact_libs.get_inline_fonts_style()
    assert style.startswith("<style>") and "data:font/woff2;base64,AAEC" in style
    assert "/libs/fonts/" not in style


def test_share_helper_is_taught_and_implemented():
    """A proportion printed with fmt's pct reads "0.4%" instead of "37.0%" —
    both models hit it, so the runtime ships share() and the prompt names it."""
    assert "window.share = function(part, whole" in GLOBALS.read_text(encoding="utf-8")
    assert "share(part, whole" in SANDBOX_RUNTIME_PROMPT


def test_pct_reads_a_share_and_says_so():
    """fmt's pct is share-tolerant (a magnitude <= 1 is multiplied by 100) and
    the prompt documents the rule plus the exact escape hatch."""
    src = GLOBALS.read_text(encoding="utf-8")
    assert "!opts.exact && n !== 0 && Math.abs(n) <= 1" in src
    assert "exact: true" in SANDBOX_RUNTIME_PROMPT


def test_table_cells_format_sql_timestamps():
    """A SQL timestamp printed raw ("2021-02-06T00:00:00.000") reads as a
    database dump; midnight renders as the date alone, on the themed runtime only."""
    src = GLOBALS.read_text(encoding="utf-8")
    assert "var _ISO_DT" in src
    assert "if (!LEGACY) {" in src.split("function _infoCell(v)")[1][:600]


def test_prompt_teaches_tokens_not_raw_colors():
    for needle in ("setTheme(", "useTheme()", "bg-surface", "text-ink", "font-display", "MERGES with the defaults", "<Icon name="):
        assert needle in SANDBOX_RUNTIME_PROMPT, needle
