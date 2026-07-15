"""Shared helpers for file-source clients (network_dir, s3, …).

Keeps the two file connectors aligned on the things that would otherwise
drift: glob-scope parsing + matching (used both to FILTER listings and to
ENFORCE access at the resolve chokepoint), and the index-mode enum that
controls how much of a source gets cached.

Design: the include-globs act as an *access boundary*, not just a listing
filter. `path_matches_globs` is called from each client's resolve chokepoint,
so a read/attach of a real-but-out-of-scope file (a `.env` next to the ppts)
is rejected the same way a path escaping the root is.
"""
from __future__ import annotations

import re
from typing import List, Optional

# Index tiers (connection-level). Higher tiers cache more at index time.
INDEX_NONE = "none"          # no catalog; live ls/read; name search only
INDEX_METADATA = "metadata"  # cache file list (name/size/mtime); no content
INDEX_CONTENT = "content"    # cache list + extracted keywords/hash (topic search)
INDEX_MODES = (INDEX_NONE, INDEX_METADATA, INDEX_CONTENT)


def normalize_index_mode(
    index_mode: Optional[str], *, index_content_legacy: Optional[bool] = None
) -> str:
    """Resolve the effective index tier.

    `index_mode` wins when set to a known value. Otherwise fall back to the
    legacy `index_content` boolean: True → content, False → metadata. Default
    is content (the historical behavior — keyword-index everything)."""
    if index_mode:
        m = str(index_mode).strip().lower()
        if m in INDEX_MODES:
            return m
    if index_content_legacy is None:
        return INDEX_CONTENT
    return INDEX_CONTENT if index_content_legacy else INDEX_METADATA


def globs_from_str(value: Optional[str]) -> List[str]:
    """Parse a comma/newline-separated glob list into normalized POSIX patterns.

    Leading slashes are stripped (patterns are relative to the connection root/
    prefix). Blank entries dropped. Returns [] when nothing configured (= match
    everything)."""
    if not value:
        return []
    parts = re.split(r"[,\n]", value)
    out: List[str] = []
    for p in parts:
        p = p.strip().lstrip("/")
        if p:
            out.append(p)
    return out


def _glob_to_regex(pattern: str) -> str:
    """Translate a glob to a regex with correct `/` semantics.

    - `**`  → any chars incl. `/`   (recursive)
    - `*`   → any chars except `/`  (single path segment)
    - `?`   → any single char except `/`
    - everything else is escaped literally.

    Also: a bare `**/x` or trailing `/**` behave intuitively, and a pattern
    with no slash (e.g. `*.ppt`) matches the basename at any depth so that
    `*.ppt` behaves like a filename filter rather than a root-only match.
    """
    # Filename-only patterns (no path separator): match at any depth.
    if "/" not in pattern:
        pattern = "**/" + pattern

    i, n = 0, len(pattern)
    out = ["^"]
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                # `**` — consume it and an optional following slash.
                j = i + 2
                if j < n and pattern[j] == "/":
                    out.append("(?:.*/)?")  # `**/` — zero or more dirs
                    i = j + 1
                else:
                    out.append(".*")        # `**` — anything incl. `/`
                    i = j
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    out.append("$")
    return "".join(out)


_COMPILED_CACHE: dict = {}


def _compiled(patterns: tuple) -> list:
    cached = _COMPILED_CACHE.get(patterns)
    if cached is None:
        cached = [re.compile(_glob_to_regex(p)) for p in patterns]
        _COMPILED_CACHE[patterns] = cached
    return cached


def path_matches_globs(rel_path: str, globs: List[str]) -> bool:
    """True if `rel_path` (POSIX, relative to root/prefix) matches ANY glob.

    Empty `globs` means "no scope restriction" → always True. This is the single
    predicate used for BOTH listing filters and access enforcement, so the two
    can never disagree."""
    if not globs:
        return True
    rp = (rel_path or "").lstrip("/")
    for rx in _compiled(tuple(globs)):
        if rx.match(rp):
            return True
    return False


# ---------------------------------------------------------------------------
# Legacy filename recovery.
#
# Shares written by Windows tools (or zips extracted without a codepage) carry
# filenames in a legacy encoding (cp1255 Hebrew, cp1252 Western). Python's
# os.listdir surrogateescapes those bytes, and the persistence sanitizer then
# (correctly) refuses the lone surrogates — every non-ASCII char degrades to
# '?', names become unreadable AND un-round-trippable. Recover instead:
# display/ids get a best-effort legacy decode; resolution re-derives the
# on-disk byte form from the recovered name.

LEGACY_FILENAME_CHARSETS = ("cp1255", "cp1252")


def has_lone_surrogates(s: str) -> bool:
    return any(0xD800 <= ord(c) <= 0xDFFF for c in s or "")


def recover_filename(s: str) -> str:
    """Best-effort human-readable form of a surrogateescape'd path/name.

    Clean strings pass through untouched. For surrogate-carrying strings the
    original bytes are recovered and tried against the legacy charsets; the
    final fallback replaces rather than crashes. Never raises."""
    if not s or not has_lone_surrogates(s):
        return s
    raw = s.encode("utf-8", "surrogateescape")
    for cs in ("utf-8",) + LEGACY_FILENAME_CHARSETS:
        try:
            decoded = raw.decode(cs)
        except (UnicodeDecodeError, LookupError):
            continue
        if not has_lone_surrogates(decoded):
            return decoded
    return raw.decode("utf-8", "replace")


def legacy_fs_candidates(display: str) -> List[str]:
    """On-disk (fsdecode/surrogateescape) forms a RECOVERED path may have.

    The inverse of recover_filename: re-encode the display form through each
    legacy charset and surrogateescape-decode, yielding strings that map back
    to the original directory-entry bytes. Used by resolve chokepoints when a
    recovered id doesn't exist verbatim on disk."""
    out: List[str] = []
    for cs in LEGACY_FILENAME_CHARSETS:
        try:
            raw = display.encode(cs)
        except (UnicodeEncodeError, LookupError):
            continue
        cand = raw.decode("utf-8", "surrogateescape")
        if cand != display and cand not in out:
            out.append(cand)
    return out


class GlobScopeError(ValueError):
    """Raised when a resolved path is inside the root but outside the configured
    include-globs. A ValueError subclass so existing `except ValueError` paths
    in the tools surface it as a clean error, while callers that care can
    distinguish scope-denials from other resolution failures."""
