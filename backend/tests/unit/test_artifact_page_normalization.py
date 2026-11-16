"""Deterministic repairs applied to page code before it is checked (_artifact_refs).

A page is one classic script whose components and helpers are globals. Models
trained on module-based React write `import { KPICard } from 'bow'`,
`export default App` or `const { KPICard } = React`, and resubmit the same
thing when it fails. The repairs remove only what cannot work in the runtime,
so the first invariant is the one existing dashboards depend on: code that
already runs comes back byte-for-byte.
"""
import re
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.ai.agent_v2 import (  # noqa: E402
    ARTIFACT_BUILD_STOP_AT, ARTIFACT_BUILD_WARN_AT, artifact_build_failure_action,
)
from app.ai.agents.planner.data_app_authoring import DATA_APP_AUTHORING  # noqa: E402
from app.ai.tools.implementations._artifact_refs import (  # noqa: E402
    MOUNT_LINE, RUNTIME_GLOBALS, ensure_app_is_mounted, normalize_page_code, render_error_hint,
    strip_module_syntax,
)

SCRIPT_RE = re.compile(r"<script type=[\"']text/babel[\"']>.*?</script>", re.S)
REWRITTEN = re.compile(r"^[ \t]*(?:import|export)\b|\}\s*=\s*(?:window\.)?React\b", re.M)
MOUNTS = re.compile(r"createRoot|hydrateRoot|\brender\s*\(")


def _pages_already_in_the_repo():
    """Every page-shaped script the repo already ships: test fixtures, the
    skills library and the authoring references."""
    roots = [BACKEND_ROOT / "tests", BACKEND_ROOT / "app" / "ai", REPO_ROOT / "docs" / "feedback-loops"]
    pages = {}
    for root in roots:
        for path in root.rglob("*"):
            if path.suffix not in {".py", ".md", ".jsx", ".mjs"} or path.name == Path(__file__).name:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for i, block in enumerate(SCRIPT_RE.findall(text)):
                pages[f"{path.relative_to(REPO_ROOT)}#{i}"] = block
    return pages


EXISTING_PAGES = _pages_already_in_the_repo()


def test_the_repo_has_existing_pages_to_check():
    assert len(EXISTING_PAGES) >= 10, sorted(EXISTING_PAGES)


@pytest.mark.parametrize("name", sorted(EXISTING_PAGES))
def test_a_page_that_uses_no_module_syntax_is_returned_byte_for_byte(name):
    code = EXISTING_PAGES[name]
    if REWRITTEN.search(code) or not MOUNTS.search(code):
        pytest.skip("this page has something the repairs exist for (module syntax, or it never mounts)")
    assert normalize_page_code(code) == (code, [])


@pytest.mark.parametrize("name", sorted(EXISTING_PAGES))
def test_repairing_twice_changes_nothing_more(name):
    once, _ = normalize_page_code(EXISTING_PAGES[name])
    assert normalize_page_code(once) == (once, [])


WORDS_THAT_ONLY_LOOK_LIKE_SYNTAX = """function App() {
  const important = 1, exported = 2, imports = [important, exported];
  // import nothing here; export nothing either
  const label = "Click import to load, export to save";
  const lazy = () => import("./chunk.js");
  return <p title="import / export">{imports.length} {label} {typeof lazy}</p>;
}
ReactDOM.createRoot(document.getElementById('root')).render(<App />);
"""


def test_words_that_only_look_like_module_syntax_are_left_alone():
    assert normalize_page_code(WORDS_THAT_ONLY_LOOK_LIKE_SYNTAX) == (WORDS_THAT_ONLY_LOOK_LIKE_SYNTAX, [])


@pytest.mark.parametrize("statement", [
    "import React from 'react';",
    "import React, { useState, useEffect } from 'react';",
    "import { useArtifactData, vizById, fmt } from 'bow';",
    'import { KPICard, SectionCard } from "@bow/kit";',
    "import bow from 'bow';",
    "import './styles.css';",
    "import {\n  PageHeader,\n  KPICard,\n} from 'bow';",
])
def test_an_import_of_names_that_are_already_globals_is_removed(statement):
    code = f"{statement}\nfunction App() {{ return <KPICard /> }}\n"
    fixed, changed = strip_module_syntax(code)
    assert changed
    assert "import" not in fixed
    assert "function App() { return <KPICard /> }" in fixed
    # A later error must still point at the line the model wrote.
    assert fixed.count("\n") == code.count("\n")


@pytest.mark.parametrize("statement, binding", [
    ("import { useState as useLocal } from 'react';", 'var useLocal = window.__bowResolveImport("react", "useState");'),
    ("import R from 'react';", 'var R = window.__bowResolveImport("react", "default");'),
    ("import * as E from 'echarts';", 'var E = window.__bowResolveImport("echarts", "*");'),
    ("import { KPICard as Card } from 'bow';", 'var Card = window.__bowResolveImport("bow", "KPICard");'),
])
def test_an_import_under_a_new_local_name_keeps_that_name_bound(statement, binding):
    # Same declaration the runtime writes (importBindings in artifact-globals.js),
    # so the saved code means what the original meant when it ran.
    fixed, _ = strip_module_syntax(statement + "\nfunction App() { return null }\n")
    assert fixed.startswith(binding)


@pytest.mark.parametrize("code, expected", [
    ("function App() { return null }\nexport default App;\n", "function App() { return null }\n\n"),
    ("export default function App() { return null }\n", "function App() { return null }\n"),
    ("export function App() { return null }\n", "function App() { return null }\n"),
    ("export const App = () => null;\n", "const App = () => null;\n"),
    ("function App() { return null }\nexport { App };\n", "function App() { return null }\n\n"),
])
def test_an_export_is_reduced_to_the_declaration_it_carried(code, expected):
    fixed, changed = strip_module_syntax(code)
    assert fixed == expected and changed


def test_the_model_is_told_what_was_removed():
    code = "import { vizById } from 'bow';\nconst { useState, KPICard } = React;\nfunction App() { return <KPICard /> }\nexport default App;\n"
    fixed, notes = normalize_page_code(code)
    assert "import" not in fixed and "export" not in fixed and "KPICard }" not in fixed
    text = " ".join(notes)
    assert "import" in text and "export" in text and "KPICard" in text


def test_the_worked_example_in_the_authoring_reference_needs_no_repair():
    tail = DATA_APP_AUTHORING.split("MINIMAL WORKING PAGE", 1)[1]
    example = SCRIPT_RE.search(tail).group(0)
    assert normalize_page_code(example) == (example, [])
    assert "createRoot" in example and "vizById(" in example


# ------------------------------------------------------------- mounting

@pytest.mark.parametrize("definition", [
    "function App() { return <div /> }",
    "const App = () => <div />;",
    "  function App() {\n    return <div />\n  }",
])
def test_a_page_that_defines_app_but_never_renders_it_gets_the_mount_line(definition):
    fixed, added = ensure_app_is_mounted(definition + "\n")
    assert added and fixed.rstrip().endswith(MOUNT_LINE)
    wrapped, added = ensure_app_is_mounted(f'<script type="text/babel">\n{definition}\n</script>')
    assert added and wrapped.endswith(MOUNT_LINE + "\n</script>")


@pytest.mark.parametrize("mount", [
    "ReactDOM.createRoot(document.getElementById('root')).render(<App />);",
    "const root = ReactDOM.createRoot(document.getElementById('root'));\nroot.render(<App />);",
    "ReactDOM.render(<App />, document.getElementById('root'));",
    "const { render } = ReactDOM;\nrender(<App />, document.getElementById('root'));",
    "ReactDOM.hydrateRoot(document.getElementById('root'), <App />);",
])
def test_a_page_that_mounts_itself_however_it_spells_it_is_untouched(mount):
    code = "function App() { return <div /> }\n" + mount + "\n"
    assert ensure_app_is_mounted(code) == (code, False)
    assert normalize_page_code(code) == (code, [])


def test_a_script_with_no_app_component_is_not_given_a_mount():
    code = "const Card = () => <div />;\nconsole.log('helpers only');\n"
    assert ensure_app_is_mounted(code) == (code, False)


# ------------------------------------------------------------- error hints

@pytest.mark.parametrize("error, must_name", [
    ("useArtifactData is not a function", "useArtifactData"),
    ("bow.useArtifactData is not a function", "useArtifactData"),
    ("TypeError: bow.setTheme is not a function", "setTheme"),
    ("setTheme is not a function", "setTheme"),
    ("Uncaught ReferenceError: KPICard is not defined", "KPICard"),
])
def test_a_missing_runtime_helper_is_named_as_a_global(error, must_name):
    hint = render_error_hint(error)
    assert hint and must_name in hint and "global" in hint


@pytest.mark.parametrize("error, meant", [
    ("useVizById is not defined", "vizById"),
    ("KpiCard is not defined", "KPICard"),
    ("useArtifactsData is not a function", "useArtifactData"),
    ("Datatable is not defined", "DataTable"),
])
def test_a_misspelled_helper_points_at_the_real_one(error, meant):
    hint = render_error_hint(error)
    assert hint and f"`{meant}`" in hint


def test_an_undefined_component_error_says_components_are_globals():
    hint = render_error_hint(
        "Element type is invalid: expected a string (for built-in components) or a class/function "
        "(for composite components) but got: undefined."
    )
    assert hint and "global" in hint


@pytest.mark.parametrize("error", [
    "Cannot read properties of undefined (reading 'rows')",
    "rows.map is not a function",
    "x is not defined",
    "",
])
def test_an_error_about_the_pages_own_code_gets_no_hint(error):
    assert render_error_hint(error) is None


def test_the_backend_list_of_runtime_globals_matches_the_runtime():
    js = (REPO_ROOT / "frontend" / "public" / "libs" / "artifact-globals.js").read_text(encoding="utf-8")
    block = re.search(r"var BOW_HELPER_NAMES = \[(.*?)\];", js, re.S).group(1)
    assert sorted(re.findall(r"'([A-Za-z_$][\w$]*)'", block)) == sorted(RUNTIME_GLOBALS)
    # Every listed name is really defined by the runtime (or is a React hook it re-exports).
    for name in RUNTIME_GLOBALS:
        assert re.search(rf"window\.{re.escape(name)}\s*=", js), name


# ---------------------------------------------- consecutive failed builds

def _run(outcomes):
    streak = {"n": 0}
    return [artifact_build_failure_action(streak, failed) for failed in outcomes]


def test_failed_builds_warn_once_then_stop_and_keep_stopping():
    actions = _run([True] * (ARTIFACT_BUILD_STOP_AT + 2))
    assert actions.count("warn") == 1 and actions.index("warn") == ARTIFACT_BUILD_WARN_AT - 1
    assert actions.index("stop") == ARTIFACT_BUILD_STOP_AT - 1
    assert all(a == "stop" for a in actions[ARTIFACT_BUILD_STOP_AT - 1:])
    assert ARTIFACT_BUILD_WARN_AT < ARTIFACT_BUILD_STOP_AT


def test_a_successful_build_resets_the_count():
    almost = [True] * (ARTIFACT_BUILD_STOP_AT - 1)
    actions = _run(almost + [False] + almost)
    assert "stop" not in actions
    assert actions.count("warn") == 2


def test_builds_that_succeed_never_warn():
    assert _run([False] * 10) == [None] * 10


BARE_CREATE_ROOT = re.compile(r"(?<![\w$.])createRoot\s*\(")


@pytest.mark.parametrize("root, definition, export", [
    ("ArtistDashboard", "const ArtistDashboard = () => <div>Artists</div>;", "export default ArtistDashboard;"),
    ("SalesOverview", "function SalesOverview() { return <div/> }", "export default SalesOverview;"),
    ("Board", "", "export default function Board() { return <div/> }"),
])
def test_a_page_whose_root_is_not_named_app_is_mounted_by_that_name(root, definition, export):
    code = f"import React from 'react';\n{definition}\n{export}\n"
    fixed, notes = normalize_page_code(code)

    assert re.search(rf"\.render\(<{root} />\)", fixed)
    assert "export" not in fixed and "<App />" not in fixed
    assert any("mount" in n for n in notes)


def test_a_page_that_defines_app_mounts_app_even_if_it_exports_another_name():
    code = "function Card() { return <div/> }\nfunction App() { return <Card/> }\nexport default Card;\n"
    fixed, _ = normalize_page_code(code)
    assert ".render(<App />)" in fixed and ".render(<Card />)" not in fixed


@pytest.mark.parametrize("imports", [
    "import { createRoot } from 'react-dom/client';",
    "import React from 'react';\nimport { createRoot } from \"react-dom/client\";",
])
def test_a_bare_create_root_call_uses_the_reactdom_global(imports):
    code = f"{imports}\nfunction App() {{ return <div/> }}\ncreateRoot(document.getElementById('root')).render(<App />);\n"
    fixed, notes = normalize_page_code(code)

    assert not BARE_CREATE_ROOT.search(fixed)
    assert fixed.count("ReactDOM.createRoot(") == 1
    assert any("createRoot" in n for n in notes)


@pytest.mark.parametrize("binding", [
    "const { createRoot } = ReactDOM;",
    "const createRoot = (el) => ReactDOM.createRoot(el);",
])
def test_a_create_root_the_page_binds_itself_is_left_alone(binding):
    code = f"{binding}\nfunction App() {{ return <div/> }}\ncreateRoot(document.getElementById('root')).render(<App />);\n"
    assert normalize_page_code(code) == (code, [])
