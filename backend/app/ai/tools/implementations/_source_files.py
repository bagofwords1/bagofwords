"""Bind a tool call to the files it is meant to read.

`create_data` and `write_csv` both generate code against an `excel_files` list.
Without an explicit binding the generated code has to work out *which* attached
file the prompt meant, from a one-line index entry — and when the file it wants
isn't there at all (a tool result that never got materialized, say) the model
goes looking for the data elsewhere, which is how generated code ends up trying
to call a connection directly.

`source_file_ids` closes that gap: the caller names the files, this module
resolves them, and the codegen prompt is told the path, the reader and the
column shape for each one.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Reader per extension, mirroring coder._file_access_rules. Kept here so the
# per-file directive names one concrete call instead of restating every rule.
_READERS = {
    "csv": "pd.read_csv(excel_files[{i}].path)",
    "tsv": "pd.read_csv(excel_files[{i}].path, sep='\\t')",
    "json": "pd.read_json(excel_files[{i}].path)",
    "ndjson": "pd.read_json(excel_files[{i}].path, lines=True)",
    "jsonl": "pd.read_json(excel_files[{i}].path, lines=True)",
    "xlsx": "pd.read_excel(excel_files[{i}].path, sheet_name=0)",
    "xls": "pd.read_excel(excel_files[{i}].path, sheet_name=0)",
    "txt": "read_text(excel_files[{i}])",
    "log": "read_text(excel_files[{i}])",
    "md": "read_text(excel_files[{i}])",
}

# Extensions codegen has no reader for at all. Naming them keeps the model from
# reaching for pd.read_csv on a PDF and getting an error — or worse, on prose
# and getting a plausible-looking frame of nonsense.
_NOT_LOADABLE = {"pdf", "docx", "pptx", "png", "jpg", "jpeg", "gif", "webp"}


def _json_reader(index: int, tabular_path: str, shape_known: bool = True) -> str:
    """The reader for a JSON artifact, given where the records sit.

    A bare `pd.read_json` only works when the file IS a list of records. The
    common MCP shape is an envelope — `{"WorkOrdersMFG": [...], "warnings":
    [...]}` — where read_json raises `All arrays must be of the same length`.
    So there are three cases, and conflating the last two costs an attempt:

    * we know the records are nested → hand over the two-step form,
    * we know the file is a bare array → the plain reader is right,
    * we never inspected the file (too large to parse) → we must NOT imply it
      is a bare array. Give the discovery form instead.
    """
    if tabular_path:
        lookup = "".join(f"[{part!r}]" for part in tabular_path.split("."))
        return (
            f"payload = pd.read_json(excel_files[{index}].path, typ='series').to_dict(); "
            f"df = pd.json_normalize(payload{lookup})"
        )
    if shape_known:
        return _READERS["json"].format(i=index)
    return (
        f"payload = pd.read_json(excel_files[{index}].path, typ='series').to_dict(); "
        "print(list(payload.keys()))  # find the key holding the records, then: "
        "df = pd.json_normalize(payload['<that key>'])  "
        "— do NOT call pd.read_json() on its own here, it raises "
        "'All arrays must be of the same length' on an enveloped payload"
    )


def _extension(filename: str) -> str:
    return (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""


def _candidates(runtime_ctx: Dict[str, Any]) -> List[Any]:
    """Every file this run could read, newest sources included.

    `excel_files` is the live list — execute_mcp appends to it so a file
    materialized earlier in the same turn is visible — with the report's own
    files as a fallback for callers that never populated it.
    """
    files = list(runtime_ctx.get("excel_files") or [])
    seen = {str(getattr(f, "id", "")) for f in files}
    report = runtime_ctx.get("report")
    for f in (getattr(report, "files", None) or []):
        if str(getattr(f, "id", "")) not in seen:
            files.append(f)
    return files


def resolve_source_files(
    runtime_ctx: Dict[str, Any],
    source_file_ids: Optional[List[str]],
) -> Tuple[Optional[List[Any]], str, List[str]]:
    """Resolve `source_file_ids` to file objects plus a codegen directive.

    Returns ``(scoped_files, directive, missing_ids)``:

    * ``scoped_files`` — what to hand the executor as `excel_files`, in the
      order the caller listed them, so `excel_files[0]` is unambiguous. None
      when no ids were given (leave the caller's own list alone).
    * ``directive`` — prose appended to the codegen prompt naming each file's
      path and reader. Empty when nothing resolved.
    * ``missing_ids`` — ids that matched no file. The caller surfaces these
      rather than generating code against a file that isn't there.
    """
    if not source_file_ids:
        return None, "", []

    by_id = {str(getattr(f, "id", "")): f for f in _candidates(runtime_ctx)}
    scoped: List[Any] = []
    missing: List[str] = []
    for fid in source_file_ids:
        found = by_id.get(str(fid))
        if found is None:
            missing.append(str(fid))
        elif found not in scoped:
            scoped.append(found)

    if not scoped:
        return [], "", missing

    lines = [
        "",
        "SOURCE FILES — read the data from these, and only these. They are "
        "already in `excel_files` at the indices below; do not search for "
        "another file and do not fetch the data from anywhere else.",
    ]
    for i, f in enumerate(scoped):
        name = getattr(f, "filename", "") or ""
        ext = _extension(name)
        hint = _observation_hint(runtime_ctx, f)
        line = f"  - excel_files[{i}]: {name} (path: {getattr(f, 'path', '')})"
        if ext in _NOT_LOADABLE:
            line += (
                " → NOT readable from generated code. Use "
                f"read_file(file_id='{getattr(f, 'id', '')}') instead."
            )
        elif ext == "json":
            line += " → read with " + _json_reader(
                i,
                hint.get("tabular_path", ""),
                shape_known=bool(hint) and not hint.get("parse_skipped"),
            )
        elif ext in _READERS:
            line += f" → read with {_READERS[ext].format(i=i)}"
        lines.append(line)
        for extra in _describe(hint):
            lines.append(f"      {extra}")
    if missing:
        lines.append(
            f"  - NOTE: no file matched id(s) {', '.join(missing)} — do not "
            "invent a substitute; work with the files listed above."
        )
    return scoped, "\n".join(lines), missing


def _observation_hint(runtime_ctx: Dict[str, Any], file_obj: Any) -> Dict[str, Any]:
    """What the tool that produced this file said about it.

    execute_mcp records `tabular_path`, `candidate_paths` and `record_shape`
    alongside the artifact. Threading them here is what lets the directive name
    the exact key the records live under and the columns they carry, instead of
    leaving the model to rediscover both from a three-row preview.
    """
    try:
        hub = runtime_ctx.get("context_hub")
        observations = getattr(getattr(hub, "observation_builder", None), "tool_observations", None) or []
        for obs in reversed(observations):
            data = obs if isinstance(obs, dict) else getattr(obs, "__dict__", {})
            if str(data.get("file_id") or "") != str(getattr(file_obj, "id", "")):
                continue
            return {
                "tabular_path": data.get("tabular_path") or "",
                "candidate_paths": data.get("candidate_paths") or [],
                "record_shape": data.get("record_shape") or {},
                # True when the producer never inspected the payload's shape,
                # so absence of a tabular_path means "unknown", not "flat".
                "parse_skipped": bool(data.get("parse_skipped")),
            }
    except Exception as e:  # never block codegen on a missing hint
        logger.debug(f"source file hint unavailable: {e}")
    return {}


def _describe(hint: Dict[str, Any]) -> List[str]:
    """Human-readable notes for one source file, from its observation hint."""
    out: List[str] = []
    shape = hint.get("record_shape") or {}
    columns = shape.get("columns") or {}
    if columns:
        named = ", ".join(f"{k}:{v}" for k, v in list(columns.items())[:25])
        more = " …" if shape.get("columns_truncated") else ""
        out.append(f"columns ({shape.get('row_count', '?')} records): {named}{more}")
    if hint.get("tabular_path"):
        out.append(
            f"the records are under the key '{hint['tabular_path']}' — everything "
            "else in the file is envelope metadata, not rows."
        )
    if hint.get("parse_skipped"):
        out.append(
            "this file was too large to inspect — its structure is UNKNOWN. "
            "Print the top-level keys first, then normalize the one holding records."
        )
    if hint.get("candidate_paths"):
        out.append(
            "other record lists in the same file (ignore unless asked): "
            + ", ".join(hint["candidate_paths"])
        )
    return out
