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

import re
import uuid
from typing import Any, Dict, List, Tuple

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


def chart_prop_errors(code: str, artifact_data: Dict[str, Any]) -> List[str]:
    """TrendChart/RankChart field props must name columns of their viz.

    Checked only where the chart provably draws the viz's own rows: no ``rows``
    prop, or ``rows={v.rows}`` / ``rows={filterRows(v.rows…)}`` for the same viz.
    Derived rows, expression props and a viz that can't be resolved are skipped,
    so a correct chart is never flagged. Runtime 11+ only, like design_errors.
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
    for m in _CHART_TAG_RE.finditer(src):
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
