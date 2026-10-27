"""Bound artifact reads before the transcript's head-only 100K cut.

Sizes use the same JSON encoding as result_parts.split_views. Source excerpts
stay verbatim; continuation arguments describe exactly where to resume.
"""

import json

from app.ai.context.result_parts import METADATA_KEYS
from app.ai.context.transcript import MAX_RESULT_CHARS

# Headroom under the transport cut for the notes model_text() appends to a
# result (failure marker, released-images note) and for JSON punctuation.
MODEL_READ_HEADROOM = 10_000
MODEL_READ_MAX_CHARS = MAX_RESULT_CHARS - MODEL_READ_HEADROOM
SOURCE_MAX_CHARS = 65_000
# Room one context-stripped grep match needs, because a page always keeps its
# first one. Worst case is read_artifact's GREP_LINE_CLIP (500) chars of
# astral-plane text, which escape to 12 each ("\udbff\udfff") = 6,000, plus
# ~200 for the line_no/col/context keys.
GREP_MATCH_MAX_CHARS = 8_192
SECONDARY_KEYS = ("render_errors", "runtime_environment", "resources", "visualization_profiles")


def _size(value):
    return len(json.dumps(value, default=str))


def _visible_size(observation):
    return _size({k: v for k, v in observation.items() if k not in METADATA_KEYS})


def _prefix(text, budget):
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _size(text[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def _append_note(summary, note):
    """Keep the read's own description; add the continuation note to it.

    The summary is what survives tier decay for this tool, so replacing it
    would drop the window note, the match count and the quote-safety line on
    exactly the turns that need them.
    """
    summary = (summary or "").rstrip()
    return f"{summary} {note}".strip()


def budget_artifact_read(observation, request):
    """Return a complete JSON observation with bounded source and optional context."""
    result = dict(observation)
    secondary = {k: result.pop(k) for k in SECONDARY_KEYS if k in result}
    artifact_id = result["artifact_id"]
    # Reserve space for identity, counts, and continuation metadata before
    # choosing a source window. The source ceiling is in escaped JSON chars.
    identity = {k: v for k, v in result.items() if k not in {"code", "matches", "outline"}}
    source_budget = min(SOURCE_MAX_CHARS, MODEL_READ_MAX_CHARS - _visible_size(identity) - 2000)
    # A source window narrower than this says nothing useful. Grep needs the
    # wider floor so its first match always fits (see the match loop); a code
    # or outline read is prefix-trimmed and safe at any width.
    if source_budget < 4096 or ("matches" in result and source_budget < GREP_MATCH_MAX_CHARS):
        return _metadata_error(result)
    code = result.get("code")
    if code is not None:
        start = result.get("start_line") or 1
        column = request.character_offset
        shown = _prefix(code, source_budget)
        if len(shown) < len(code):
            # Prefer whole lines. A single oversized line must still make
            # progress, using a character offset within that line.
            boundary = shown.rfind("\n")
            if boundary >= 0:
                shown = shown[: boundary + 1]
            newlines = shown.count("\n")
            next_column = len(shown.rsplit("\n", 1)[-1]) if newlines else column + len(shown)
            result.update(
                code=shown,
                read_mode="range",
                start_line=start,
                start_character=column,
                end_line=start + newlines - int(shown.endswith("\n")),
                truncated=True,
                next_read={
                    "artifact_id": artifact_id,
                    "offset": start + newlines,
                    "character_offset": next_column,
                    "limit": min(
                        2000, max(1, (result.get("end_line") or result["total_lines"]) - start - newlines + 1)
                    ),
                },
            )
            result["summary"] = _append_note(
                result.get("summary"),
                "Partial verbatim source; continue with next_read. end_line may be partial when character_offset is nonzero.",
            )
        elif result.get("truncated"):
            result["next_read"] = {
                "artifact_id": artifact_id,
                "offset": result["end_line"] + 1,
                "limit": request.limit or 400,
            }
        if column:
            result["start_character"] = column

    if "matches" in result:
        kept = []
        context_omitted = False
        for original in result["matches"]:
            match = dict(original)
            if _size(match) > source_budget:
                match.update(before=[], after=[], context_start_line=match["line_no"], context_omitted=True)
                context_omitted = True
            if kept and _size(kept + [match]) > source_budget:
                break
            # The first match rides even if it alone overshoots: an empty page
            # whose next_read repeats the incoming match_offset would never
            # advance. One overshoot stays inside MODEL_READ_HEADROOM.
            kept.append(match)
        result["matches"] = kept
        consumed = request.match_offset + len(kept)
        result["grep_truncated"] = consumed < result.get("grep_total_matches", 0)
        if result["grep_truncated"]:
            result["next_read"] = {
                "artifact_id": artifact_id,
                "grep_pattern": request.grep_pattern,
                "ignore_case": request.ignore_case,
                "before": request.before,
                "after": request.after,
                "max_matches": request.max_matches,
                "match_offset": consumed,
            }
            result["summary"] = _append_note(
                result.get("summary"),
                "Partial grep results; continue with next_read. Match text is verbatim.",
            )
        if context_omitted:
            result["context_note"] = (
                "Oversized before/after context omitted; read offset=line_no with limit and character_offset for source."
            )

    if result.get("outline") and _size(result["outline"]) > source_budget:
        result["outline"] = _prefix(result["outline"], source_budget)
        result["outline_truncated"] = True
        result["next_read"] = {"artifact_id": artifact_id, "offset": 1, "limit": 400}
        result["summary"] = _append_note(
            result.get("summary"),
            "Partial structural outline; read source with next_read or grep_pattern.",
        )

    # Add optional fields only when they fit whole. Profiles can be paged by
    # visualization id with read_query; never silently clip a profile's rows.
    omitted = []
    for key, value in secondary.items():
        if _visible_size({**result, key: value}) <= MODEL_READ_MAX_CHARS - 1000:
            result[key] = value
        else:
            omitted.append(key)
    if omitted:
        result["omitted_context"] = omitted
        result["context_note"] = (
            result.get("context_note", "")
            + " Optional context exceeded the budget. Use a smaller source range with include_visualization_profiles=true; "
            "use read_query for individual data profiles and manage_artifact_resources for resources."
        )
    # Defensive explicit failure for pathological identity/error metadata.
    # Never let the transport silently turn a successful read into broken JSON.
    if _visible_size(result) > MODEL_READ_MAX_CHARS:
        return _metadata_error(result)
    return result


def _metadata_error(result):
    return {
        "artifact_id": result["artifact_id"],
        "version": result.get("version"),
        "success": False,
        "error": {"type": "artifact_read_metadata_too_large", "message": "Artifact metadata exceeds the read budget."},
    }
