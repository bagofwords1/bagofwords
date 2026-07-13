"""Shared line-grep engine for file-source clients (network_dir, s3, …).

Backs the `grep_files` capability: deterministic regex over raw file bytes,
returning matching LINES (with line numbers and context) plus explicit sweep
accounting — files scanned, files skipped and why, why the sweep stopped, and
a cursor to resume. The point is reduction at the source: the agent gets the
hits and a count, never the whole log.

The engine is pure and client-agnostic: each client supplies an ordered
candidate list (already scoped/filtered through its own resolve chokepoint)
and a byte-reader; matching, context, budgets, binary sniffing, and cursor
semantics live here so every file source behaves identically — the same
philosophy as the shared windowed-read contract.

Text detection is content-based (a NUL-byte sniff), NOT an extension
allowlist: any file whose bytes look like text is greppable (.txt, .log,
.csv, .ndjson, extensionless, …). Real binaries are skipped and reported.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.data_sources.clients._file_source_common import GlobScopeError

# Match against at most this many chars of a line — a pathological line (a
# minified-JSON log record) must not stall the sweep or blow up the regex.
MATCH_SCAN_CHARS = 8192
# Cap on stored line content (matched + context lines). Flagged when clipped.
LINE_STORE_CHARS = 500
# A NUL byte in the first block marks the file binary → skipped, not scanned.
BINARY_SNIFF_BYTES = 8192
# How often (in lines) the scan loop re-checks the time budget.
DEADLINE_CHECK_EVERY_LINES = 512

DEFAULT_MAX_BYTES_PER_FILE = 20_000_000

# Reasons a candidate can be excluded from the scan. Everything excluded is
# reported back — silent truncation would read as "covered everything".
SKIP_TOO_LARGE = "too_large"
SKIP_BINARY = "binary"
SKIP_UNREADABLE = "unreadable"
SKIP_ACCESS_DENIED = "access_denied"
SKIP_NOT_FOUND = "not_found"

STOP_COMPLETE = "complete"
STOP_MAX_MATCHES = "max_matches"
STOP_MAX_FILES = "max_files"
STOP_TIME_BUDGET = "time_budget"


def compile_pattern(pattern: str, *, is_regex: bool = True, ignore_case: bool = False):
    """Compile the user pattern (regex or literal). Raises re.error / ValueError
    on a bad pattern — callers surface that as a clean tool error, not a crash."""
    if not (pattern or "").strip():
        raise ValueError("pattern is required")
    flags = re.IGNORECASE if ignore_case else 0
    return re.compile(pattern if is_regex else re.escape(pattern), flags)


def _scope_hash(scope_key: str) -> str:
    return hashlib.sha1((scope_key or "").encode("utf-8")).hexdigest()[:12]


def make_cursor(file_id: str, line_no: int, scope_key: str) -> str:
    """Opaque resume token: which file to resume at, and the last line already
    consumed in it (resume scans lines > line_no). Bound to the sweep's
    pattern+scope via a hash so a cursor can't be replayed against different
    arguments."""
    payload = {"f": file_id, "l": int(line_no), "h": _scope_hash(scope_key)}
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def parse_cursor(cursor: str, scope_key: str) -> Tuple[str, int]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8"))
        file_id, line_no, h = payload["f"], int(payload["l"]), payload["h"]
    except Exception:
        raise ValueError(
            "Invalid grep cursor — pass the exact next_cursor value returned "
            "by the previous grep_files call."
        )
    if h != _scope_hash(scope_key):
        raise ValueError(
            "Grep cursor does not match this pattern/scope. A cursor only "
            "resumes the SAME sweep — repeat the previous pattern and scope, "
            "or drop the cursor to start a new sweep."
        )
    return file_id, line_no


def _clip(line: str) -> Tuple[str, bool]:
    if len(line) > LINE_STORE_CHARS:
        return line[:LINE_STORE_CHARS], True
    return line, False


def run_grep_sweep(
    *,
    candidates: List[Dict[str, Any]],
    read_bytes: Callable[[Dict[str, Any]], bytes],
    pattern: str,
    is_regex: bool = True,
    ignore_case: bool = False,
    before: int = 0,
    after: int = 0,
    max_matches: int = 100,
    max_matches_per_file: int = 50,
    max_files: int = 500,
    max_bytes_per_file: int = DEFAULT_MAX_BYTES_PER_FILE,
    scope_key: str = "",
    cursor: Optional[str] = None,
    time_budget_seconds: float = 60.0,
) -> Dict[str, Any]:
    """Scan candidates in stable (id-sorted) order, line by line.

    candidates: dicts with at least {"id"}; optional "path", "size" (size
        enables skipping oversized files without reading them), and
        "skip_reason" for entries the client pre-flagged (e.g. an explicit
        file_id that resolved off-scope → SKIP_ACCESS_DENIED).
    read_bytes: called only for candidates that passed the gates. A
        GlobScopeError becomes an access_denied skip; any other exception an
        unreadable skip — one bad file never aborts the sweep.

    Returns the sweep dict consumed by the grep_files tool:
        {matches, total_matches, files_scanned, files_with_matches,
         skipped_files, truncated, stop_reason, next_cursor}
    """
    rx = compile_pattern(pattern, is_regex=is_regex, ignore_case=ignore_case)
    deadline = time.monotonic() + max(1.0, float(time_budget_seconds))

    resume_file: Optional[str] = None
    resume_line = 0
    if cursor:
        resume_file, resume_line = parse_cursor(cursor, scope_key)

    ordered = sorted(candidates or [], key=lambda e: str(e.get("id") or ""))

    matches: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []
    total_matches = 0
    files_scanned = 0
    files_with_matches = 0
    truncated = False
    stop_reason = STOP_COMPLETE
    next_cursor: Optional[str] = None

    def _stop_at(file_id: str, line_no: int, reason: str) -> None:
        nonlocal stop_reason, next_cursor
        stop_reason = reason
        next_cursor = make_cursor(file_id, line_no, scope_key)

    for entry in ordered:
        fid = str(entry.get("id") or "")
        if not fid:
            continue
        # Cursor resume: ids before the resume point were fully scanned by the
        # previous call (stable sort order makes the comparison meaningful).
        start_line = 0
        if resume_file is not None:
            if fid < resume_file:
                continue
            if fid == resume_file:
                start_line = resume_line

        reason = entry.get("skip_reason")
        if reason:
            skipped.append({"file_id": fid, "reason": str(reason)})
            continue

        if time.monotonic() > deadline:
            _stop_at(fid, start_line, STOP_TIME_BUDGET)
            break
        if files_scanned >= max_files:
            _stop_at(fid, start_line, STOP_MAX_FILES)
            break

        size = entry.get("size")
        if size is not None and int(size) > max_bytes_per_file:
            skipped.append({"file_id": fid, "reason": SKIP_TOO_LARGE})
            continue

        try:
            data = read_bytes(entry)
        except GlobScopeError:
            skipped.append({"file_id": fid, "reason": SKIP_ACCESS_DENIED})
            continue
        except Exception:
            skipped.append({"file_id": fid, "reason": SKIP_UNREADABLE})
            continue
        if data is None:
            skipped.append({"file_id": fid, "reason": SKIP_UNREADABLE})
            continue
        if len(data) > max_bytes_per_file:
            skipped.append({"file_id": fid, "reason": SKIP_TOO_LARGE})
            continue
        if b"\x00" in data[:BINARY_SNIFF_BYTES]:
            skipped.append({"file_id": fid, "reason": SKIP_BINARY})
            continue

        files_scanned += 1
        lines = data.decode("utf-8", errors="replace").splitlines()
        path = entry.get("path") or fid

        file_emitted = 0
        before_buf: deque = deque(maxlen=before) if before > 0 else deque(maxlen=1)
        # Matches still collecting their trailing context: (match_dict, lines_left).
        open_after: List[List[Any]] = []
        stop_sweep = False

        for idx, raw_line in enumerate(lines):
            line_no = idx + 1  # 1-based, grep convention

            if line_no % DEADLINE_CHECK_EVERY_LINES == 0 and time.monotonic() > deadline:
                _stop_at(fid, line_no - 1, STOP_TIME_BUDGET)
                stop_sweep = True
                break

            clipped, _ = _clip(raw_line)

            # Trailing context for previously emitted matches.
            if open_after:
                still_open = []
                for pair in open_after:
                    pair[0]["after"].append(clipped)
                    pair[1] -= 1
                    if pair[1] > 0:
                        still_open.append(pair)
                open_after = still_open

            # Resume skip: line already consumed by the previous call. Context
            # is still tracked above/below so the first fresh match gets real
            # surroundings.
            if line_no <= start_line:
                if before > 0:
                    before_buf.append(clipped)
                continue

            if rx.search(raw_line[:MATCH_SCAN_CHARS]):
                if len(matches) >= max_matches:
                    # Can't emit — stop the sweep HERE and hand back a cursor
                    # that re-scans this line, so nothing is lost between pages.
                    truncated = True
                    _stop_at(fid, line_no - 1, STOP_MAX_MATCHES)
                    stop_sweep = True
                    break
                if file_emitted >= max_matches_per_file:
                    # Noisy file: count the overflow, flag it, move on — one
                    # file must not exhaust the sweep's whole match budget.
                    total_matches += 1
                    truncated = True
                    break
                line_text, line_trunc = _clip(raw_line)
                m = {
                    "file_id": fid,
                    "path": path,
                    "line_no": line_no,
                    "line": line_text,
                    "line_truncated": line_trunc,
                    "before": list(before_buf) if before > 0 else [],
                    "after": [],
                }
                matches.append(m)
                total_matches += 1
                file_emitted += 1
                if after > 0:
                    open_after.append([m, after])

            if before > 0:
                before_buf.append(clipped)

        if file_emitted:
            files_with_matches += 1
        if stop_sweep:
            break

    return {
        "matches": matches,
        "total_matches": total_matches,
        "files_scanned": files_scanned,
        "files_with_matches": files_with_matches,
        "skipped_files": skipped,
        "truncated": truncated,
        "stop_reason": stop_reason,
        "next_cursor": next_cursor,
    }
