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
from typing import Any, Dict, List, Tuple

VIZ_POSITIONAL_RE = re.compile(r"\bviz\[(\d+)\]")
DATA_VIZ_POSITIONAL_RE = re.compile(r"\bdata\.visualizations\[(\d+)\]")
VIZ_BY_ID_RE = re.compile(r"vizById\(\s*['\"]([0-9a-fA-F][0-9a-fA-F-]{10,40})['\"]\s*\)")
# Whole-list rendering (map/forEach over the full list) touches every viz
# without naming ids — coverage can't be judged statically, so it is treated
# as satisfied. Deliberately narrow: `.length`, `.slice`, `.filter` do NOT
# count (viz.slice(0, 1) renders one viz, not all of them).
VIZ_ITERATION_RE = re.compile(r"\b(?:viz|visualizations)\s*\.\s*(?:map|forEach)\b")


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
                return f'vizById("{viz_ids[idx]}")'
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
                return f'vizById("{viz_ids[idx]}")'
            return m.group(0)

        return DATA_VIZ_POSITIONAL_RE.sub(_repl2, out)

    new_code = "\n".join(_sub_line(l) for l in code.split("\n"))
    return new_code, count


def viz_reference_errors(code: str, artifact_data: Dict[str, Any]) -> List[str]:
    """Contract check: viz references in code must match the data payload.

    Returns synthetic repair errors (empty when satisfied):
    - a ``vizById()`` id that is not in the payload (e.g. a removed viz whose
      section survived the edit) — renders null/crashes or, worse, nothing;
    - a positional index outside the payload — silently undefined;
    - a payload viz that the code never references at all (dropped chart) —
      unless the code iterates the whole list, which satisfies coverage.
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
    if ids and src.strip() and not VIZ_ITERATION_RE.search(src):
        referenced = set()
        referenced |= id_refs & known
        for idx in positional_idxs:
            if idx < len(ids):
                referenced.add(ids[idx])
        missing = [vid for vid in ids if vid not in referenced]
        if missing:
            names = ", ".join(f"\"{titles.get(vid, 'Untitled')}\" (vizById(\"{vid}\"))" for vid in missing)
            if not referenced:
                errors.append(
                    "[viz refs] The code references NONE of the visualizations in this "
                    f"artifact's data payload ({names}) — the page renders no data. Bind "
                    "each viz with vizById(\"<uuid>\") or render the whole list."
                )
            else:
                errors.append(
                    "[viz refs] These visualization(s) are in the artifact's data payload but the "
                    f"code never references them: {names}. Every viz in the payload must be "
                    "rendered somewhere — add a section for each, or it silently disappears "
                    "from the dashboard."
                )
    return errors
