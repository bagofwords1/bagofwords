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
# Whole-list iteration means the code touches every viz without naming ids —
# coverage can't be judged statically, so it is treated as satisfied.
VIZ_ITERATION_RE = re.compile(r"\b(?:viz|visualizations)\s*\.\s*(?:map|forEach|filter|slice|length)\b")


def migrate_positional_viz_refs(code: str, viz_ids: List[str]) -> Tuple[str, int]:
    """Rewrite positional viz references to id-keyed ``vizById()`` calls.

    ``viz_ids`` must be the artifact's stored ``content.visualization_ids`` —
    the order the positional indexes were written against. Out-of-range
    indexes are left untouched (they become gate errors instead of silently
    guessing). Returns (new_code, replacement_count).
    """
    if not code or not viz_ids:
        return code, 0
    count = 0

    def _repl(m: "re.Match[str]") -> str:
        nonlocal count
        idx = int(m.group(1))
        if idx < len(viz_ids):
            count += 1
            return f'vizById("{viz_ids[idx]}")'
        return m.group(0)

    new_code = VIZ_POSITIONAL_RE.sub(_repl, code)
    new_code = DATA_VIZ_POSITIONAL_RE.sub(_repl, new_code)
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

    # Coverage: every payload viz must be reachable from the code. Only
    # enforceable when the code names vizs (by id or index) rather than
    # iterating the whole list.
    if ids and not VIZ_ITERATION_RE.search(src):
        referenced = set()
        referenced |= id_refs & known
        for idx in positional_idxs:
            if idx < len(ids):
                referenced.add(ids[idx])
        missing = [vid for vid in ids if vid not in referenced]
        if missing and (id_refs or positional_idxs):
            names = ", ".join(f"\"{titles.get(vid, 'Untitled')}\" (vizById(\"{vid}\"))" for vid in missing)
            errors.append(
                "[viz refs] These visualization(s) are in the artifact's data payload but the "
                f"code never references them: {names}. Every viz in the payload must be "
                "rendered somewhere — add a section for each, or it silently disappears "
                "from the dashboard."
            )
    return errors
