"""Id-keyed visualization references for artifact code.

Artifact code historically addressed data positionally (``viz[0]``,
``viz[1]``…, ordered by ``content.visualization_ids``). Positional binding is
the worst regression class: any viz add/remove re-indexes the array and a
single missed reference silently renders the wrong dataset. New code binds by
id via the ``vizById("<uuid>")`` runtime global (artifact-globals.js), and the
helpers here provide:

- ``migrate_positional_viz_refs`` — the deterministic, no-LLM codemod that
  upgrades legacy positional code the first time it is edited (``viz[3]`` ≡
  ``visualization_ids[3]``, so the rewrite is exact).
- ``viz_reference_errors`` — the post-edit/post-create contract check whose
  findings ride the same in-tool repair loop as render and params-wiring
  errors.

Stored artifact versions are never rewritten; the codemod runs on the working
copy at edit time and persists only as the next version.
"""
from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

VIZ_POSITIONAL_RE = re.compile(r"\bviz\[(\d+)\]")
DATA_VIZ_POSITIONAL_RE = re.compile(r"\bdata\.visualizations\[(\d+)\]")
VIZ_BY_ID_RE = re.compile(r"vizById\(\s*['\"]([0-9a-fA-F][0-9a-fA-F-]{10,40})['\"]\s*\)")
# Whole-list rendering (map/forEach over the full list) touches every viz
# without naming ids — coverage can't be judged statically, so it is treated
# as satisfied. Deliberately narrow: `.length`, `.slice`, `.filter` do NOT
# count (viz.slice(0, 1) renders one viz, not all of them).
VIZ_ITERATION_RE = re.compile(r"\b(?:viz|visualizations)\s*\.\s*(?:map|forEach)\b")


def viz_by_id_ref(visualization_id: str) -> str:
    """The one spelling of an id-keyed binding, ``vizById("<id>")``.

    Generated code, the positional codemod and the removed-viz restore all
    write it through here, and ``VIZ_BY_ID_RE`` / the stub helpers parse it
    back — so the writers and the parsers can never drift apart.
    """
    return f'vizById("{visualization_id}")'


def _quoted_id_refs(src: str, ids: List[str]) -> set:
    """Payload ids that appear as a quoted string literal anywhere in the code.

    Coverage asks "does the code bind this viz?", but `vizById(...)` is only
    the RECOMMENDED spelling, not the only correct one — the runtime global is
    literally a lookup by id over the same list, so
    ``visualizations.find(v => v.id === "<uuid>")``, an ``{ "<uuid>": ... }``
    map, or ``byId["<uuid>"]`` bind exactly as well. Matching the helper's
    NAME rejected working dashboards in production (a create that bound all 8
    payload vizs via `.find` was told it "references NONE of the
    visualizations"), so coverage matches the ids themselves.

    Only QUOTED ids count, so prose or a comment merely mentioning a uuid
    binds nothing. The residual false-pass is an id quoted inside a comment
    (`// was vizById("<uuid>")`) — rare, and it costs one unrendered viz,
    where the false FAILURE it replaces discarded an entire valid artifact
    and forced a full regeneration.
    """
    return {vid for vid in ids if re.search(r"['\"]" + re.escape(vid) + r"['\"]", src)}


# The components and helpers the page runtime provides as globals. Mirrors
# BOW_HELPER_NAMES in frontend/public/libs/artifact-globals.js (the unit tests
# compare the two lists).
RUNTIME_GLOBALS = (
    "useArtifactData", "vizById", "useParams", "useParamOptions", "useFilters", "useCurrentUser",
    "useConversation", "usePresence", "useTheme", "setTheme", "fmt", "exportCSV", "share",
    "BOW_FONTS", "BOW_THEMES",
    "useState", "useEffect", "useMemo", "useCallback", "useRef",
    "PageHeader", "KPICard", "SectionCard", "DataTable", "EChart", "TrendChart", "RankChart",
    "FilterBar", "FilterSelect", "FilterSearch", "FilterDateRange", "Segmented", "Tabs",
    "Badge", "Delta", "Sparkline", "ProgressBar", "Icon", "Eyebrow", "Divider", "EmptyState",
    "LoadingSpinner", "Modal", "Drawer", "InfoOverlay", "InfoPopover", "BowFile", "BowPdfViewer",
    "ChatComposer", "ChatMessage", "ChatMessages", "ChatResult", "ChatSuggestions", "ChatThreads",
)
_NOT_DEFINED_RE = re.compile(r"^(?:Uncaught )?(?:ReferenceError: )?([A-Za-z_$][\w$]*) is not defined")
_NOT_A_FUNCTION_RE = re.compile(r"^(?:Uncaught )?(?:TypeError: )?((?:[A-Za-z_$][\w$]*\.)*)([A-Za-z_$][\w$]*) is not a function")


def render_error_hint(error: str) -> Optional[str]:
    """What to change for a render error about a missing name, or None.

    The browser's message names the symptom ("useVizById is not defined",
    "x.useArtifactData is not a function", "Element type is invalid … got:
    undefined"). A model that only sees the symptom rewrites the same call
    another way; this says which name to use and that it is a global.
    """
    text = (error or "").strip()
    if "Element type is invalid" in text and "undefined" in text:
        return (
            "A component used in the JSX is undefined. Every component is a global — write "
            "<KPICard …/>, <SectionCard …/> directly; do not import it or take it from another "
            "object, and check the name against the component list."
        )
    not_fn = _NOT_A_FUNCTION_RE.match(text)
    not_def = _NOT_DEFINED_RE.match(text)
    if not (not_fn or not_def):
        return None
    prefix = (not_fn.group(1) if not_fn else "") or ""
    name = not_fn.group(2) if not_fn else not_def.group(1)
    if name in RUNTIME_GLOBALS:
        where = f"`{prefix}{name}`" if prefix else f"`{name}`"
        return (
            f"{where} is undefined here because `{name}` was taken from an object or redeclared. "
            f"It is a global: use `{name}` directly and delete the import/destructuring that names it."
        )
    import difflib
    close = difflib.get_close_matches(name, RUNTIME_GLOBALS, n=1, cutoff=0.6)
    if close:
        return f"`{name}` does not exist in the page runtime — did you mean the global `{close[0]}`?"
    if not_def and name[:1].isupper():
        return (
            f"`{name}` is not a runtime component. Use one from the component list, or define "
            f"`{name}` yourself in this script."
        )
    return None


# What `React` itself exports (18.x UMD build). Anything else destructured
# from it is undefined.
REACT_EXPORTS = frozenset({
    "Children", "Component", "Fragment", "Profiler", "PureComponent", "StrictMode", "Suspense",
    "cloneElement", "createContext", "createElement", "createFactory", "createRef", "forwardRef",
    "isValidElement", "lazy", "memo", "startTransition", "version",
    "useCallback", "useContext", "useDebugValue", "useDeferredValue", "useEffect", "useId",
    "useImperativeHandle", "useInsertionEffect", "useLayoutEffect", "useMemo", "useReducer",
    "useRef", "useState", "useSyncExternalStore", "useTransition",
})
_REACT_DESTRUCTURE_RE = re.compile(
    r"(?P<indent>^[ \t]*)?\b(?P<kw>const|let|var)\s*\{(?P<body>[^{}]*)\}\s*=\s*(?:window\.)?React(?![\w$.])\s*;?",
    re.MULTILINE,
)
_PLAIN_BINDING_RE = re.compile(r"^[A-Za-z_$][\w$]*(?:\s*:\s*[A-Za-z_$][\w$]*)?$")


def strip_non_react_destructures(code: str) -> Tuple[str, List[str]]:
    """Drop names that `React` does not export from `const { … } = React`.

    The runtime's components and helpers (KPICard, vizById, useArtifactData…)
    are globals. A model that lists them in a `React` destructuring shadows
    each global with `undefined`, and the page dies with "Element type is
    invalid … got: undefined" — or, for a helper, "x is not a function".
    `React.<name>` is undefined for every such name, so removing the binding
    can only replace `undefined` with the global: it never changes code that
    worked. Real React exports are kept as written.

    Returns (code, removed names). A statement with a default value, a rest
    element or a comment inside the braces is left for the render check.
    """
    removed: List[str] = []

    def _fix(m: "re.Match") -> str:
        entries = [e.strip() for e in m.group("body").split(",")]
        entries = [e for e in entries if e]
        if not entries or not all(_PLAIN_BINDING_RE.match(e) for e in entries):
            return m.group(0)
        kept = [e for e in entries if e.split(":")[0].strip() in REACT_EXPORTS]
        dropped = [e.split(":")[-1].strip() for e in entries if e.split(":")[0].strip() not in REACT_EXPORTS]
        if not dropped:
            return m.group(0)
        removed.extend(dropped)
        indent = m.group("indent") or ""
        if not kept:
            return indent
        return f"{indent}{m.group('kw')} {{ {', '.join(kept)} }} = React;"

    src = code or ""
    fixed = _REACT_DESTRUCTURE_RE.sub(_fix, src)
    return (fixed, list(dict.fromkeys(removed))) if removed else (src, [])


# Mirrors IMPORT_GLOBALS / importBindings / stripImports in
# frontend/public/libs/artifact-globals.js: the runtime already removes
# imports when it runs a page, and the saved code must mean the same thing.
_IMPORT_GLOBALS = {"react": "React", "react-dom": "ReactDOM", "react-dom/client": "ReactDOM",
                   "echarts": "echarts", "lucide": "lucide"}
_IMPORT_FROM_RE = re.compile(
    r"^[ \t]*import\s+(?P<clause>[^'\";]+?)\s+from\s*(?P<q>['\"])(?P<mod>[^'\"]+)(?P=q)[ \t]*;?",
    re.MULTILINE,
)
_IMPORT_INLINE_RE = re.compile(r"import\s*\{[^}]*\}\s*from\s*['\"][^'\"]+['\"]\s*;?")
_IMPORT_LINE_RE = re.compile(r"^[ \t]*import\b(?!\s*\().*$", re.MULTILINE)
_IMPORT_PART_RE = re.compile(r"^([\w$]+)(?:\s+as\s+([\w$]+))?$")
_EXPORT_DEFAULT_NAME_RE = re.compile(r"^[ \t]*export\s+default\s+[A-Za-z_$][\w$]*[ \t]*;?[ \t]*$", re.MULTILINE)
# `export default React.memo(Dashboard)` / `withTheme(Dashboard)`: a call
# expression. Kept as a declaration so the page still has its root component.
DEFAULT_EXPORT_NAME = "BowDefaultExport"
_EXPORT_DEFAULT_CALL_RE = re.compile(
    r"^(?P<indent>[ \t]*)export\s+default\s+(?=[A-Za-z_$][\w$.]*\s*\()(?!(?:async\s+)?function\b|class\b)",
    re.MULTILINE,
)
_REACT_MODULES = {"react", "react-dom", "react-dom/client"}
_EXPORT_LIST_RE = re.compile(r"^[ \t]*export\s*\{[^{}]*\}[ \t]*;?[ \t]*$", re.MULTILINE)
_EXPORT_DECL_RE = re.compile(
    r"^(?P<indent>[ \t]*)export\s+(?:default\s+)?(?=(?:async\s+)?function\b|class\b|const\b|let\b|var\b)",
    re.MULTILINE,
)


def _import_bindings(clause: str, mod: str) -> str:
    """The declaration the runtime puts in place of one import statement:
    nothing when every imported name is the global of the same name, else a
    `var` bound to the matching global for each NEW local name."""
    canonical = _IMPORT_GLOBALS.get(mod)
    decls: List[str] = []

    def bind(local: str, name: str) -> None:
        decls.append(f"{local} = window.__bowResolveImport({json.dumps(mod)}, {json.dumps(name)})")

    rest = clause
    braces = re.search(r"\{([\s\S]*)\}", rest)
    if braces:
        for part in braces.group(1).split(","):
            m = _IMPORT_PART_RE.match(part.strip())
            if not m:
                continue
            imported, local = m.group(1), m.group(2) or m.group(1)
            if imported == "default":
                if canonical and local != canonical:
                    bind(local, "default")
            elif local != imported or (mod in _REACT_MODULES and imported not in RUNTIME_GLOBALS):
                # A same-name import binds nothing when the runtime has no
                # global of that name (`import { createRoot } from
                # 'react-dom/client'`): bind it to the module's export.
                bind(local, imported)
        rest = rest.replace(braces.group(0), "")
    namespace = re.search(r"\*\s*as\s+([\w$]+)", rest)
    if namespace:
        if canonical and namespace.group(1) != canonical:
            bind(namespace.group(1), "*")
        rest = rest.replace(namespace.group(0), "")
    default = rest.replace(",", " ").strip()
    if re.match(r"^[\w$]+$", default) and canonical and default != canonical:
        bind(default, "default")
    return ("var " + ", ".join(decls) + ";") if decls else ""


def strip_module_syntax(code: str) -> Tuple[str, List[str]]:
    """Turn `import` / `export` statements into what a plain script allows.

    The page is one classic script: every helper is a global and nothing is
    exported. Models trained on module-based React still write
    `import { KPICard } from 'bow'` and `export default App`. The runtime
    removes imports itself when it runs a page, but the parse check sees the
    code as written and rejects it ("'import' and 'export' may appear only
    with sourceType: module"); `export` never worked anywhere.

    Imports are handled exactly as the runtime handles them, so saved code
    means what it meant before. Line count is preserved, so a later error
    still points at the line the model wrote.

    Returns (code, what was changed). Code without these statements comes
    back untouched.
    """
    src = code or ""
    if not re.search(r"\b(?:import|export)\b", src):
        return src, []
    changed: List[str] = []

    def _keep_lines(original: str, replacement: str) -> str:
        return replacement + "\n" * original.count("\n")

    def _import(m: "re.Match") -> str:
        changed.append(f"import … from '{m.group('mod')}'")
        return _keep_lines(m.group(0), _import_bindings(m.group("clause"), m.group("mod")))

    def _drop(label: str):
        def _sub(m: "re.Match") -> str:
            changed.append(label)
            return _keep_lines(m.group(0), "")
        return _sub

    def _decl(m: "re.Match") -> str:
        changed.append("export")
        return m.group("indent")

    fixed = _IMPORT_FROM_RE.sub(_import, src)
    fixed = _IMPORT_INLINE_RE.sub(_drop("import { … }"), fixed)
    fixed = _IMPORT_LINE_RE.sub(_drop("import"), fixed)
    fixed = _EXPORT_DEFAULT_NAME_RE.sub(_drop("export default"), fixed)
    fixed = _EXPORT_LIST_RE.sub(_drop("export { … }"), fixed)
    fixed = _EXPORT_DECL_RE.sub(_decl, fixed)

    def _default_call(m: "re.Match") -> str:
        changed.append("export default")
        return m.group("indent") + f"var {DEFAULT_EXPORT_NAME} = "

    fixed = _EXPORT_DEFAULT_CALL_RE.sub(_default_call, fixed)
    return (fixed, list(dict.fromkeys(changed))) if fixed != src else (src, [])


# A mount call, not any `render(`: a column's `render(v) { … }` is not one,
# `render(<App />, el)` (React 17 style, however it is bound) is.
_MOUNTS_RE = re.compile(
    r"\bcreateRoot\s*\(|\bhydrateRoot\s*\(|\bReactDOM\s*\.\s*render\s*\(|\brender\s*\(\s*<"
)
MOUNT_LINE = "ReactDOM.createRoot(document.getElementById('root')).render(<App />);"
_EXPORT_DEFAULT_DECL_NAME_RE = re.compile(
    r"^[ \t]*export\s+default\s+(?:(?:async\s+)?function\s*\*?\s*|class\s+)([A-Z][\w$]*)", re.MULTILINE
)
_EXPORT_DEFAULT_BARE_NAME_RE = re.compile(r"^[ \t]*export\s+default\s+([A-Z][\w$]*)[ \t]*;?[ \t]*$", re.MULTILINE)


def mount_line(component: str = "App") -> str:
    return MOUNT_LINE.replace("<App />", f"<{component} />")


def exported_root(code: str) -> Optional[str]:
    """The component a module-style page exports as its root, read before
    `export` is stripped: `export default function Dashboard`, `export default
    Dashboard`, or a wrapped root (`export default React.memo(Dashboard)`),
    which strip_module_syntax keeps as DEFAULT_EXPORT_NAME."""
    src = code or ""
    for pattern in (_EXPORT_DEFAULT_DECL_NAME_RE, _EXPORT_DEFAULT_BARE_NAME_RE):
        m = pattern.search(src)
        if m:
            return m.group(1)
    return DEFAULT_EXPORT_NAME if _EXPORT_DEFAULT_CALL_RE.search(src) else None


def _defines(code: str, name: str) -> bool:
    n = re.escape(name)
    return bool(re.search(
        rf"^[ \t]*(?:async\s+)?function\s+{n}\b|^[ \t]*(?:const|let|var)\s+{n}\s*=|^[ \t]*class\s+{n}\b",
        code, re.MULTILINE,
    ))


def ensure_app_is_mounted(code: str, component: str = "App") -> Tuple[str, bool]:
    """Add the mount call to a page that defines its root but never renders it.

    Such a page parses, throws nothing and shows nothing: the dashboard stays
    on "Loading artifact…" while every check passes. ``component`` is `App`
    unless the page exported another root. Applies only when there is no mount
    call of any kind, so a page that mounts itself is returned untouched.
    """
    src = code or ""
    if not _defines(src, component) or _MOUNTS_RE.search(src):
        return src, False
    line = mount_line(component)
    close = src.rfind("</script>")
    if close == -1:
        return src.rstrip("\n") + "\n" + line + "\n", True
    head = src[:close].rstrip("\n")
    return head + "\n" + line + "\n" + src[close:], True


def normalize_page_code(code: str) -> Tuple[str, List[str]]:
    """Every deterministic repair applied to page code before it is checked.

    Each one only removes something that cannot work in the page runtime, so
    code that already runs is returned byte-for-byte. Returns (code, notes for
    the model) — the notes say what was changed so it stops writing it.
    """
    notes: List[str] = []
    fixed, statements = strip_module_syntax(code)
    if statements:
        notes.append(
            "Removed module statements (" + ", ".join(statements) + "): the page is a plain script — "
            "components and helpers are globals, nothing is imported or exported."
        )
    fixed, names = strip_non_react_destructures(fixed)
    if names:
        notes.append(
            "Removed from a `const { … } = React` destructuring, because React does not "
            f"export them (they are runtime globals — use them directly): {', '.join(names)}."
        )
    root = exported_root(code)
    component = root if root and _defines(fixed, root) else "App"
    fixed, mounted = ensure_app_is_mounted(fixed, component)
    if mounted:
        notes.append(
            "Added the missing mount line — a page must end with "
            f"`{mount_line(component)}` or nothing is shown."
        )
    return fixed, notes


def _inside_string_or_comment(line: str, col: int) -> bool:
    """Heuristic: is column ``col`` of ``line`` inside a string literal or
    after a ``//`` line comment? Counts unescaped quote characters before the
    position (JSX string literals don't span lines; template literals rarely
    do in generated artifact code). Errs toward True — a skipped replacement
    degrades to a gate finding, while a wrong replacement inside a string
    breaks the code.
    """
    in_quote: str = ""
    i = 0
    while i < col:
        ch = line[i]
        if in_quote:
            if ch == "\\":
                i += 2
                continue
            if ch == in_quote:
                in_quote = ""
        else:
            if ch in ("'", '"', "`"):
                in_quote = ch
            elif ch == "/" and i + 1 < col and line[i + 1] == "/":
                return True
        i += 1
    return bool(in_quote)


def migrate_positional_viz_refs(code: str, viz_ids: List[str]) -> Tuple[str, int]:
    """Rewrite positional viz references to id-keyed ``vizById()`` calls.

    ``viz_ids`` must be the artifact's stored ``content.visualization_ids`` —
    the order the positional indexes were written against. Out-of-range
    indexes are left untouched (they become gate errors instead of silently
    guessing), and matches inside string literals or line comments are
    skipped (rewriting a quoted "viz[0]" would inject nested quotes and
    break the code). Returns (new_code, replacement_count).
    """
    if not code or not viz_ids:
        return code, 0
    count = 0

    def _sub_line(line: str) -> str:
        nonlocal count

        def _repl(m: "re.Match[str]") -> str:
            nonlocal count
            if _inside_string_or_comment(line, m.start()):
                return m.group(0)
            idx = int(m.group(1))
            if idx < len(viz_ids):
                count += 1
                return viz_by_id_ref(viz_ids[idx])
            return m.group(0)

        out = VIZ_POSITIONAL_RE.sub(_repl, line)
        # Second pattern runs on the ORIGINAL line for position checks only
        # when the first made no changes to it; otherwise re-check on `out`.
        def _repl2(m: "re.Match[str]") -> str:
            nonlocal count
            if _inside_string_or_comment(out, m.start()):
                return m.group(0)
            idx = int(m.group(1))
            if idx < len(viz_ids):
                count += 1
                return viz_by_id_ref(viz_ids[idx])
            return m.group(0)

        return DATA_VIZ_POSITIONAL_RE.sub(_repl2, out)

    new_code = "\n".join(_sub_line(l) for l in code.split("\n"))
    return new_code, count


def referenced_viz_ids(code: str, viz_ids: List[str]) -> List[str]:
    """Subset of ``viz_ids`` the code actually binds (renders), in list order.

    The coverage rule shared with ``viz_reference_errors``: whole-list
    iteration binds every viz; otherwise a viz is bound when its id appears
    quoted (``vizById()`` or any other id-keyed lookup) or an in-range
    positional ``viz[N]`` points at it.
    """
    src = code or ""
    ids = [str(v) for v in viz_ids or []]
    if not ids or not src.strip():
        return []
    if VIZ_ITERATION_RE.search(src):
        return ids
    referenced = set(VIZ_BY_ID_RE.findall(src)) & set(ids)
    referenced |= _quoted_id_refs(src, ids)
    positional = {int(m) for m in VIZ_POSITIONAL_RE.findall(src)}
    positional |= {int(m) for m in DATA_VIZ_POSITIONAL_RE.findall(src)}
    for idx in positional:
        if 0 <= idx < len(ids):
            referenced.add(ids[idx])
    return [vid for vid in ids if vid in referenced]


def option_source_viz_ids(vizs: List[Dict[str, Any]]) -> set:
    """Payload vizs whose query only feeds a filter's choices: another viz's
    parameter names it as ``options_source``. The page never renders them --
    useParamOptions() gets the choices, resolved server-side -- so they need
    no reference in the code."""
    sources = set()
    for v in vizs:
        for spec in v.get("parameters") or []:
            src = spec.get("options_source") if isinstance(spec, dict) else getattr(spec, "options_source", None)
            qid = src.get("query_id") if isinstance(src, dict) else getattr(src, "query_id", None)
            if qid:
                sources.add(str(qid))
    return {str(v.get("id")) for v in vizs if v.get("id") and str(v.get("query_id") or "") in sources}


def viz_reference_errors(code: str, artifact_data: Dict[str, Any]) -> List[str]:
    """Contract check: viz references in code must match the data payload.

    Returns synthetic repair errors (empty when satisfied):
    - a ``vizById()`` id that is not in the payload (e.g. a removed viz whose
      section survived the edit) — renders null/crashes or, worse, nothing;
    - a positional index outside the payload — silently undefined;
    - a payload viz that the code never references at all (dropped chart) —
      unless the code iterates the whole list, which satisfies coverage, or
      the viz only feeds a filter's choices (``option_source_viz_ids``).
    """
    src = code or ""
    vizs = [v for v in (artifact_data or {}).get("visualizations") or [] if isinstance(v, dict)]
    ids = [str(v.get("id")) for v in vizs if v.get("id")]
    titles = {str(v.get("id")): (v.get("title") or "Untitled") for v in vizs if v.get("id")}
    errors: List[str] = []

    id_refs = set(VIZ_BY_ID_RE.findall(src))
    known = set(ids)

    for ref in sorted(id_refs - known):
        errors.append(
            f"[viz refs] The code references vizById(\"{ref}\") but no visualization with "
            "that id is in this artifact's data payload — it renders as null. If this viz "
            "was removed, delete the section that references it; if it's a typo, use one "
            "of the payload ids."
        )

    positional_idxs = {int(m) for m in VIZ_POSITIONAL_RE.findall(src)}
    positional_idxs |= {int(m) for m in DATA_VIZ_POSITIONAL_RE.findall(src)}
    for idx in sorted(i for i in positional_idxs if i >= len(ids)):
        errors.append(
            f"[viz refs] The code references viz[{idx}] but the data payload only has "
            f"{len(ids)} visualization(s) (valid indexes 0–{len(ids) - 1}) — it renders as "
            "undefined. Remove the stale reference or point it at an existing viz "
            "(prefer vizById(\"<uuid>\"))."
        )

    # Coverage: every payload viz must be reachable from the code. Whole-list
    # map/forEach satisfies it; otherwise every id must be named (by id or
    # in-range index). A non-empty payload with NO references at all is the
    # worst case — a chartless page — and is always an error.
    if ids and src.strip():
        referenced = set(referenced_viz_ids(src, ids))
        option_sources = option_source_viz_ids(vizs)
        missing = [vid for vid in ids if vid not in referenced and vid not in option_sources]
        if missing:
            names = ", ".join(f"\"{titles.get(vid, 'Untitled')}\" (vizById(\"{vid}\"))" for vid in missing)
            if not referenced:
                errors.append(
                    "[viz refs] The code references NONE of the visualizations in this "
                    f"artifact's data payload ({names}) — the page renders no data. Bind "
                    "each viz by id (vizById(\"<uuid>\") is the recommended form) or render "
                    "the whole list."
                )
            else:
                errors.append(
                    "[viz refs] These visualization(s) are in the artifact's data payload but the "
                    f"code never references them: {names}. Every viz in the payload must be "
                    "rendered somewhere — add a section for each, or it silently disappears "
                    "from the dashboard."
                )
    return errors


def design_errors(code: str, artifact_data: Dict[str, Any]) -> List[str]:
    """Protect stable data bindings on new runtimes; aesthetics are optional.

    Legacy positional references remain valid. New apps bind by ID so adding
    or removing a dataset cannot silently repoint an existing view.
    """
    runtime = (artifact_data or {}).get("runtime") or {}
    try:
        version = int(runtime.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version < 11:
        return []
    src = code or ""
    if not src.strip():
        return []
    errors: List[str] = []
    positional = VIZ_POSITIONAL_RE.findall(src) + DATA_VIZ_POSITIONAL_RE.findall(src)
    if positional:
        errors.append(
            "[design] The code reads visualizations by position (viz[N] / data.visualizations[N]) — on the "
            "themed runtime every viz must be bound by id: const x = vizById(\"<uuid>\") (copy the uuid from "
            "YOUR VISUALIZATIONS). Positional access silently repoints at the wrong dataset when the viz set changes."
        )
    return errors


_CHART_TAG_RE = re.compile(r"<(TrendChart|RankChart)\b")
_CHART_FIELD_PROPS = {"TrendChart": ("x", "y", "series"), "RankChart": ("label", "value")}
_VIZ_ASSIGN_RE = re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*vizById\(\s*['\"]([0-9a-fA-F][0-9a-fA-F-]{10,40})['\"]\s*\)")


def _jsx_tag_body(src: str, start: int) -> str:
    """The attribute text of the JSX tag opening at ``start`` (``<Name …>``).

    Walks braces and quotes so an ``onClick={p => …}`` arrow does not end the tag.
    """
    depth, quote, i = 0, None, start + 1
    while i < len(src):
        ch = src[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == ">" and depth == 0:
            return src[start:i]
        i += 1
    return src[start:]


def _jsx_prop(body: str, name: str):
    """(kind, value) for prop ``name``: ("str", text) for name="…", ("expr", text) for name={…}.

    Only a top-level attribute counts: text inside another attribute's quoted
    value or {expression} (``alt='id="x"'``) is skipped.
    """
    attr = re.compile(r"\s" + re.escape(name) + r"\s*=\s*")
    depth, quote, i, m = 0, None, 0, None
    while i < len(body):
        ch = body[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif depth == 0 and ch.isspace():
            m = attr.match(body, i)
            if m:
                break
        i += 1
    if not m:
        return None
    rest = body[m.end():]
    if rest[:1] in "\"'":
        end = rest.find(rest[0], 1)
        return ("str", rest[1:end]) if end > 0 else None
    if rest[:1] == "{":
        depth = 0
        for j, ch in enumerate(rest):
            depth += ch == "{"
            depth -= ch == "}"
            if depth == 0:
                return ("expr", rest[1:j].strip())
    return None


_BOW_FILE_TAG_RE = re.compile(r"<BowFile\b")
# One string literal and nothing else: "x", 'x' or `x` without ${...}.
_STRING_LITERAL_RE = re.compile(r"""^(?:"([^"\\]*)"|'([^'\\]*)'|`([^`\\$]*)`)$""")


def normalize_file_id(value: Any) -> str:
    """One spelling per file id: the canonical UUID form, else trimmed lower case."""
    text = str(value if value is not None else "").strip()
    try:
        return str(uuid.UUID(text))
    except ValueError:
        return text.lower()


def bow_file_literal_ids(code: str) -> set:
    """Ids that ``<BowFile id=…>`` tags name literally, exactly as written.

    Not normalised: the runtime (``_bowFindFile``) matches them against
    ``files[].id`` with ``===``, so another spelling shows "File not found".

    The tag is read with the JSX-aware helpers, so a ``>`` inside an earlier
    attribute (``onLoad={() => …}``, ``alt="a > b"``) doesn't end it. Only an id
    that is one string literal counts; a computed id (``{"img-" + theme}``,
    a variable) can't be checked here and is left to the runtime.
    """
    src = code or ""
    found: set = set()
    for m in _BOW_FILE_TAG_RE.finditer(src):
        prop = _jsx_prop(_jsx_tag_body(src, m.start()), "id")
        if not prop:
            continue
        kind, value = prop
        if kind == "str":
            literal = value
        else:
            lit = _STRING_LITERAL_RE.match(value or "")
            if not lit:
                continue
            literal = next(g for g in lit.groups() if g is not None)
        if literal.strip():
            found.add(literal)
    return found


def file_reference_errors(code: str, artifact_data: Dict[str, Any], ignore_ids: Any = ()) -> List[str]:
    """A ``<BowFile>`` naming a file that isn't attached renders "File not found".

    ``artifact_data["files"]`` holds the attached files. ``ignore_ids`` lets an
    edit leave references the page already had alone, so an unrelated edit
    isn't blocked by them.
    """
    attached = {str(f.get("id")) for f in (artifact_data or {}).get("files") or [] if isinstance(f, dict)}
    spelled = {normalize_file_id(a): a for a in attached}
    ignored = {str(i) for i in ignore_ids or ()}
    missing = sorted(bow_file_literal_ids(code) - attached - ignored)
    return [
        f'<BowFile id="{fid}" /> spells the attached file\'s id differently; ids must match exactly, so it would show '
        f'"File not found". Write id="{spelled[normalize_file_id(fid)]}".'
        if normalize_file_id(fid) in spelled else
        f'<BowFile id="{fid}" /> names a file that is not attached to the artifact, so it would show "File not found". '
        f"Pass its id in `file_ids`, or remove the tag."
        for fid in missing
    ]


_PARAM_LIST_RES = (
    re.compile(r"\bfunction\b\s*[\w$]*\s*\(([^()]*)\)"),   # function Card({ viz }) / function (viz)
    re.compile(r"\(([^()]*)\)\s*=>"),                         # ({ viz, title }) => / (viz) =>
)
_BARE_ARROW_PARAM_RE = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*=>")  # viz => ...
_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")


def _function_param_names(src: str) -> set:
    """Every identifier that appears in a function's parameter list (destructured or not)."""
    found: set = set()
    for rx in _PARAM_LIST_RES:
        for m in rx.finditer(src):
            found.update(_IDENT_RE.findall(m.group(1)))
    found.update(m.group(1) for m in _BARE_ARROW_PARAM_RE.finditer(src))
    return found


_SCRIPT_OPEN_RE = re.compile(r"^\s*<script\b[^>]*>", re.I)
# Where an expression may start, so a `<` opens JSX and a `/` a regex: after
# one of these characters or keywords, not after a value (`i < n`, `a / b`).
_EXPR_START_CHARS = frozenset("([{,;:=!&|?+-*/%~^<>")
_EXPR_START_WORDS = frozenset((
    "return", "case", "typeof", "void", "delete", "in", "of", "await", "yield",
    "throw", "else", "do", "new", "instanceof", "default", "export",
))


def _expression_may_start(src: str, i: int) -> bool:
    k = i - 1
    while k >= 0 and src[k].isspace():
        k -= 1
    if k < 0:
        return True
    if src[k] in _EXPR_START_CHARS:
        return True
    if src[k].isalnum() or src[k] in "_$":
        end = k + 1
        while k >= 0 and (src[k].isalnum() or src[k] in "_$"):
            k -= 1
        return src[k + 1:end] in _EXPR_START_WORDS
    return False


def _skip_regex(src: str, i: int, end: int) -> int:
    """End of the regex literal starting at ``src[i] == "/"``; ``i + 1`` if it isn't one."""
    j, in_class = i + 1, False
    while j < end:
        ch = src[j]
        if ch == "\n":
            return i + 1
        if ch == "\\":
            j += 2
            continue
        if in_class:
            in_class = ch != "]"
        elif ch == "[":
            in_class = True
        elif ch == "/":
            return j + 1
        j += 1
    return i + 1


def _jsx_element_starts(src: str) -> set:
    """Offsets of the ``<`` of every JSX element the page's code opens.

    A small JS/JSX scanner. String, template and regex literals and comments
    are skipped, so a tag named inside one is not an element; in JSX text
    quotes are plain characters (``<p>Don't</p>``). The stored
    ``<script type="text/babel">`` wrapper is not part of the code.
    """
    start, end = 0, len(src)
    wrapper = _SCRIPT_OPEN_RE.match(src)
    if wrapper:
        start = wrapper.end()
        close = src.rfind("</script>")
        end = close if close >= start else end
    starts: set = set()
    stack = ["code"]  # code | template | tag (inside <Name …>) | text (an element's children)
    i = start
    while i < end:
        mode, ch = stack[-1], src[i]
        if mode == "code":
            if src.startswith("//", i):
                nl = src.find("\n", i, end)
                i = end if nl < 0 else nl
            elif src.startswith("/*", i):
                close = src.find("*/", i + 2, end)
                i = end if close < 0 else close + 2
            elif ch in "'\"":
                j = i + 1
                while j < end and src[j] != ch and src[j] != "\n":
                    j += 2 if src[j] == "\\" else 1
                i = j + 1
            elif ch == "`":
                stack.append("template")
                i += 1
            elif ch == "/" and _expression_may_start(src, i):
                i = _skip_regex(src, i, end)
            elif ch == "{":
                stack.append("code")
                i += 1
            elif ch == "}":
                if len(stack) > 1:
                    stack.pop()
                i += 1
            elif ch == "<" and i + 1 < end and (src[i + 1].isalpha() or src[i + 1] == ">") and _expression_may_start(src, i):
                starts.add(i)
                stack.append("tag")
                i += 1
            else:
                i += 1
        elif mode == "template":
            if ch == "\\":
                i += 2
            elif ch == "`":
                stack.pop()
                i += 1
            elif src.startswith("${", i):
                stack.append("code")
                i += 2
            else:
                i += 1
        elif mode == "tag":
            if ch in "'\"":  # JSX attribute strings have no escapes
                close = src.find(ch, i + 1, end)
                i = end if close < 0 else close + 1
            elif ch == "{":
                stack.append("code")
                i += 1
            elif src.startswith("/>", i):
                stack.pop()
                i += 2
            elif ch == ">":
                stack[-1] = "text"
                i += 1
            else:
                i += 1
        else:  # text
            if ch == "{":
                stack.append("code")
                i += 1
            elif src.startswith("</", i):
                close = src.find(">", i, end)
                i = end if close < 0 else close + 1
                stack.pop()
            elif ch == "<":
                starts.add(i)
                stack.append("tag")
                i += 1
            else:
                i += 1
    return starts


def _has_spread_attr(body: str) -> bool:
    """Whether a JSX tag body has a top-level ``{...props}`` attribute."""
    depth, quote, i = 0, None, 0
    while i < len(body):
        ch = body[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch == "{":
            if depth == 0 and body[i + 1:].lstrip().startswith("..."):
                return True
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    return False


def _charts_without_viz(src: str) -> List[Tuple[str, str]]:
    """(component, whole tag with whitespace collapsed) of each chart with no ``viz`` prop.

    Tags inside a comment or a string, and tags with a ``{...spread}`` that may
    carry the viz, are left out.
    """
    elements = _jsx_element_starts(src)
    found: List[Tuple[str, str]] = []
    for m in _CHART_TAG_RE.finditer(src):
        start = m.start()
        if start not in elements:
            continue
        body = _jsx_tag_body(src, start)
        if _jsx_prop(body, "viz") is not None or _has_spread_attr(body):
            continue
        tag = body + ">" if src[start + len(body):start + len(body) + 1] == ">" else body
        found.append((m.group(1), " ".join(tag.split())))
    return found


def chart_prop_errors(code: str, artifact_data: Dict[str, Any], *, previous_code: Optional[str] = None) -> List[str]:
    """TrendChart/RankChart must name their viz, and field props its columns.

    A chart without a ``viz`` prop has no ⓘ of its own, and without ``rows`` it
    draws an error box. ``previous_code`` (an edit) leaves the viz-less charts
    the page already had alone, so only the ones an edit adds are flagged. A
    page with no visualizations has no viz to pass, so it isn't asked for one.

    Field props are checked only where the chart provably draws the viz's own
    rows: no ``rows`` prop, or ``rows={v.rows}`` / ``rows={filterRows(v.rows…)}``
    for the same viz. Derived rows, expression props and a viz that can't be
    resolved are skipped, so a correct chart is never flagged. Runtime 11+ only,
    like design_errors.
    """
    runtime = (artifact_data or {}).get("runtime") or {}
    try:
        version = int(runtime.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version < 11:
        return []
    src = code or ""
    if "TrendChart" not in src and "RankChart" not in src:
        return []
    vizs = {str(v.get("id")): v for v in (artifact_data or {}).get("visualizations") or [] if isinstance(v, dict) and v.get("id")}
    # One id per identifier. Two components may each declare `const viz =
    # vizById(...)` for different vizzes; such an identifier is ambiguous here
    # (no scope analysis), so charts that use it are skipped, never misjudged.
    seen: Dict[str, set] = {}
    for m in _VIZ_ASSIGN_RE.finditer(src):
        seen.setdefault(m.group(1), set()).add(m.group(2))
    # An identifier that is also a function parameter (a component taking its
    # own `viz`) may hold any viz there, so it is ambiguous too.
    params = _function_param_names(src)
    names = {k: next(iter(v)) for k, v in seen.items() if len(v) == 1 and k not in params}
    errors: List[str] = []
    # A page with no visualizations (static, resource-backed) has no viz to
    # pass: its charts draw from their own rows.
    existing = Counter(tag for _, tag in _charts_without_viz(previous_code or "")) if vizs else Counter()
    for comp, tag in _charts_without_viz(src) if vizs else []:
        if existing[tag] > 0:
            existing[tag] -= 1
            continue
        shown = tag if len(tag) <= 120 else tag[:119] + "…"
        drawn = "" if _jsx_prop(tag, "rows") is not None else " and draws an error box instead of a chart"
        errors.append(
            f"[chart] <{comp}> has no viz prop ({shown}): it has no ⓘ of its own{drawn}. "
            f"Pass viz={{vizById(\"<uuid>\")}} for the visualization its rows come from, inside a SectionCard too "
            "(a card with the same viz keeps the only ⓘ); keep rows= for filtered or derived rows. "
            "Only when its rows come from no visualization (a resource or constants) pass viz={null}."
        )
    elements = _jsx_element_starts(src)
    for m in _CHART_TAG_RE.finditer(src):
        if m.start() not in elements:
            continue
        comp = m.group(1)
        body = _jsx_tag_body(src, m.start())
        viz_prop = _jsx_prop(body, "viz")
        if not viz_prop or viz_prop[0] != "expr":
            continue
        expr = viz_prop[1]
        inline = VIZ_BY_ID_RE.fullmatch(expr)
        viz_id = inline.group(1) if inline else names.get(expr)
        ident = None if inline else expr
        viz = vizs.get(str(viz_id)) if viz_id else None
        if not viz:
            continue
        rows_prop = _jsx_prop(body, "rows")
        if rows_prop is not None:
            v = rows_prop[1] if rows_prop[0] == "expr" else ""
            own = bool(ident) and (
                re.fullmatch(re.escape(ident) + r"\.rows", v) is not None
                or re.fullmatch(r"filterRows\(\s*" + re.escape(ident) + r"\.rows\s*(?:,[^()]*)?\)", v) is not None
            )
            if not own:
                continue
        cols = [str(c.get("field") or c.get("name")) for c in viz.get("columns") or [] if isinstance(c, dict) and (c.get("field") or c.get("name"))]
        if not cols:
            continue
        for prop in _CHART_FIELD_PROPS[comp]:
            p = _jsx_prop(body, prop)
            if not p or p[0] != "str" or p[1] in cols:
                continue
            title = viz.get("title") or "Untitled"
            errors.append(
                f"[chart] <{comp}> {prop}=\"{p[1]}\" is not a column of viz \"{title}\" ({viz_id}). "
                f"Columns: {', '.join(cols)}. Use one of them, or pass rows= with the derived rows that have that field."
            )
    return errors
