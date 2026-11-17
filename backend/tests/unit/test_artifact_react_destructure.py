"""Names a page takes from `React` that React does not export (_artifact_refs).

The runtime's components and helpers are globals. A model that lists them in
`const { … } = React` shadows each global with `undefined`, and the page dies
with "Element type is invalid … got: undefined". The names are dropped from
the destructuring so the globals are used; what React really exports stays.
"""
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.ai.tools.implementations._artifact_refs import (  # noqa: E402
    REACT_EXPORTS, strip_non_react_destructures,
)

RUNTIME_GLOBALS = ["KPICard", "SectionCard", "DataTable", "EChart", "vizById", "useArtifactData", "useParams", "fmt"]


@pytest.mark.parametrize("name", RUNTIME_GLOBALS)
@pytest.mark.parametrize("keyword", ["const", "let", "var"])
def test_a_runtime_global_is_dropped_and_react_exports_are_kept(name, keyword):
    code = f"{keyword} {{ useState, {name}, useEffect }} = React;\nfunction App() {{ return <{name} /> }}"
    fixed, removed = strip_non_react_destructures(code)
    assert removed == [name]
    assert fixed == f"{keyword} {{ useState, useEffect }} = React;\nfunction App() {{ return <{name} /> }}"


def test_a_destructuring_of_only_runtime_globals_is_removed_whole():
    code = "const { KPICard, vizById } = React;\nfunction App() { return null }"
    fixed, removed = strip_non_react_destructures(code)
    assert removed == ["KPICard", "vizById"]
    assert "React" not in fixed and "function App() { return null }" in fixed


def test_a_multi_line_destructuring_and_window_react_are_handled():
    code = (
        "  const {\n    useState,\n    KPICard,\n    DataTable,\n  } = window.React\n"
        "  function App() { return <KPICard /> }"
    )
    fixed, removed = strip_non_react_destructures(code)
    assert removed == ["KPICard", "DataTable"]
    assert fixed.startswith("  const { useState } = React;")


def test_an_alias_of_a_missing_export_is_dropped_under_its_local_name():
    fixed, removed = strip_non_react_destructures("const { KPICard: Card, useMemo: memo } = React;")
    assert removed == ["Card"]
    assert fixed == "const { useMemo: memo } = React;"


@pytest.mark.parametrize("code", [
    "const { useState, useEffect, useMemo, useRef, Fragment, createContext } = React;",
    "const { createRoot } = ReactDOM;",
    "const { KPICard } = window.BowKit;",
    "const { rows } = sales;",
    "function App() { const [a, setA] = React.useState(0); return null }",
    "",
])
def test_code_that_takes_nothing_missing_from_react_is_untouched(code):
    assert strip_non_react_destructures(code) == (code, [])


@pytest.mark.parametrize("code", [
    "const { KPICard = Fallback, useState } = React;",
    "const { useState, ...rest } = React;",
    "const { useState, /* kit */ KPICard } = React;",
])
def test_a_destructuring_that_is_not_a_plain_list_is_left_for_the_render_check(code):
    assert strip_non_react_destructures(code) == (code, [])


def test_every_hook_react_18_ships_counts_as_an_export():
    hooks = ["useState", "useEffect", "useMemo", "useCallback", "useRef", "useContext", "useReducer",
             "useLayoutEffect", "useId", "useTransition", "useDeferredValue", "useSyncExternalStore"]
    assert set(hooks) <= REACT_EXPORTS
    code = "const { " + ", ".join(hooks) + " } = React;"
    assert strip_non_react_destructures(code) == (code, [])
