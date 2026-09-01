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

from app.utils.json_sanitize import sanitize_utf8

# Index tiers (connection-level). Higher tiers cache more at index time.
INDEX_NONE = "none"          # no catalog; live ls/read; name search only
INDEX_METADATA = "metadata"  # cache file list (name/size/mtime); no content
INDEX_CONTENT = "content"    # cache list + extracted keywords/hash (topic search)
INDEX_MODES = (INDEX_NONE, INDEX_METADATA, INDEX_CONTENT)


class NamedBytes(bytes):
    """Raw file bytes that remember the source file's name and MIME type.

    `read_file()` returns bare bytes whenever a file can't be turned into text
    (a scanned PDF, a picture, a document whose extraction came up empty). The
    tool layer then has to work out the FORMAT to decide how to render it, and
    its only handle was the caller-supplied file id. That works for the
    path-shaped ids network_dir and s3 hand out, but Graph item ids are opaque
    tokens with no extension — so a scanned PDF or an image from SharePoint
    reached the renderer as an unidentifiable blob and lost its vision
    fallback entirely.

    Subclassing `bytes` carries the name the connector already had in hand
    without disturbing anything: every `isinstance(payload, (bytes, bytearray))`
    check, equality test, and `bytes(payload)` conversion behaves identically.
    (`__slots__` is deliberately absent — CPython rejects non-empty slots on a
    subtype of a variable-length builtin.)
    """

    def __new__(cls, data, name: Optional[str] = None, mime: Optional[str] = None):
        obj = super().__new__(cls, data)
        obj.name = name or ""
        obj.mime = mime or ""
        return obj


class DocumentText(str):
    """Extracted document text that remembers the bytes it came from.

    The sibling of :class:`NamedBytes`, for the opposite outcome: extraction
    SUCCEEDED, so the model gets text — but the UI still needs the original
    file to show the user the actual document (a .pdf in a viewer, opened at
    the page that was read). Without carrying the bytes here, the tool layer
    would have to fetch the same object a SECOND time, a full extra download
    from S3/SharePoint purely to render a preview.

    Only the clients that already hold the bytes in memory when they extract
    (graph_drive, google_drive, s3) wrap their result. network_dir extracts
    straight from a local path and has nothing in hand, so it keeps returning
    a plain str — re-reading a local file costs nothing, and holding every
    document in memory for it would be a pure waste.

    Subclassing ``str`` keeps every consumer working unchanged: isinstance
    checks, ``len``, ``encode``, equality. Slicing returns a plain ``str``, so
    the bytes can never leak into tool output or the context window.
    """

    def __new__(cls, text, raw: Optional[bytes] = None,
                name: Optional[str] = None, mime: Optional[str] = None):
        obj = super().__new__(cls, text)
        obj.raw = raw
        obj.name = name or ""
        obj.mime = mime or ""
        return obj


def payload_name(payload, fallback: str = "") -> str:
    """Best display/dispatch name for a read payload: the connector-supplied
    name when the bytes carry one, else the caller's fallback (usually the
    file id, which IS a path for the path-addressed connectors).

    Only a *string* ``.name`` counts. A tabular read returns a DataFrame, and
    pandas resolves attribute access against the COLUMNS — so a CSV whose header
    contains a column literally called ``name`` made ``getattr(payload, "name")``
    return that column as a Series, and ``Series or ""`` raises "The truth value
    of a Series is ambiguous". read_file then failed outright for any spreadsheet
    with a `name` column, which is one of the most common headers there is.
    """
    name = getattr(payload, "name", "")
    if not isinstance(name, str):
        return fallback
    return name.strip() or fallback


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

# Ordered by likelihood; order also breaks score ties. The DOS codepages
# (cp862) decode ANY byte sequence "successfully", so charset choice cannot be
# first-success — candidates are QUALITY-SCORED and the best decode wins.
LEGACY_FILENAME_CHARSETS = ("cp1255", "iso-8859-8", "cp862", "cp1252")


def has_lone_surrogates(s: str) -> bool:
    return any(0xD800 <= ord(c) <= 0xDFFF for c in s or "")


def _decode_quality(s: str) -> float:
    """How much a candidate decode looks like a real filename.

    Letters/digits good; control chars, box-drawing/symbol soup, and
    replacement chars bad; a single word mixing Latin and Hebrew letters is a
    strong misdecode signal (cp1255 happily turns cp1252's 'é' into a Hebrew
    letter mid-word)."""
    import unicodedata

    letters = digits = bad = 0
    for c in s:
        if c.isalpha():
            letters += 1
        elif c.isdigit():
            digits += 1
        else:
            cat = unicodedata.category(c)
            if cat in ("Cc", "Co", "Cn") or c == "�" or 0x2500 <= ord(c) <= 0x25FF or cat.startswith("S"):
                bad += 1
    # A single word mixing ASCII-Latin letters with letters from ANY other
    # script (Hebrew ט, Greek Θ, box-adjacent glyphs…) is the fingerprint of
    # a misdecode — real filenames don't write 'cafΘ'.
    mixed = 0
    for word in re.split(r"[\W\d_]+", s):
        has_ascii = any("a" <= ch.lower() <= "z" for ch in word)
        has_other = any(ch.isalpha() and not (
            "a" <= ch.lower() <= "z" or "À" <= ch <= "ÿ" or "Ā" <= ch <= "ɏ"
        ) for ch in word)
        if has_ascii and has_other:
            mixed += 1
    return letters + 0.5 * digits - 2.0 * bad - 3.0 * mixed


def recover_filename(s: str) -> str:
    """Best-effort human-readable form of a surrogateescape'd path/name.

    Clean strings pass through untouched. For surrogate-carrying strings the
    original bytes are recovered and every legacy charset that decodes them
    cleanly becomes a candidate; the highest-QUALITY decode wins (list order
    breaks ties). The final fallback replaces rather than crashes — and logs
    the raw bytes so an unknown encoding is diagnosable from server logs
    without host access. Never raises.

    A PATH is recovered one segment at a time. Its segments were written at
    different times by different tools and can cross encodings — a cp862
    directory holding a cp1255 file is ordinary on a share that outlived a
    migration — and scoring the joined string picks ONE charset for all of it,
    so whichever segment has more characters decides, and the rest comes out as
    mojibake ('בקשה/πστ ε∙δ≡·α 2024.pdf'). Per-segment recovery also matches how
    `_scan_resolve` compares display forms, so ids stay round-trippable.
    Splitting before decoding is safe while every candidate charset is
    single-byte: no legacy character can produce a spurious 0x2F."""
    if not s or not has_lone_surrogates(s):
        return s
    if "/" in s:
        return "/".join(recover_filename(part) for part in s.split("/"))
    raw = s.encode("utf-8", "surrogateescape")
    best: Optional[str] = None
    best_score = float("-inf")
    for cs in LEGACY_FILENAME_CHARSETS:
        try:
            decoded = raw.decode(cs)
        except (UnicodeDecodeError, LookupError):
            continue
        if has_lone_surrogates(decoded):
            continue
        score = _decode_quality(decoded)
        if score > best_score:
            best, best_score = decoded, score
    if best is not None and best_score > 0:
        return best
    import logging
    logging.getLogger(__name__).warning(
        "filename recovery: no charset in %s decoded %r acceptably — "
        "falling back to replacement. Add the right charset to "
        "LEGACY_FILENAME_CHARSETS to recover these names.",
        LEGACY_FILENAME_CHARSETS, raw,
    )
    return best if best is not None else raw.decode("utf-8", "replace")


def storage_safe_name(s: str) -> str:
    """UTF-8-encodable form of a name headed for the database / uploads store.

    The last line of defence at the persistence boundary. Postgres columns are
    UTF-8 and asyncpg refuses a lone surrogate outright — and it fails during
    *flush*, so the whole `AsyncSession` is left needing a rollback and every
    later query in the same agent turn dies with "This Session's transaction
    has been rolled back". One un-recovered filename therefore takes down the
    rest of the run, not just its own INSERT.

    So: recover the legacy encoding when one explains the bytes (a real Hebrew
    name beats '??????'), then hard-scrub anything still unencodable. Callers
    must apply this BEFORE deriving the on-disk path too, so what's written to
    disk and what's written to the DB stay the same string. Never raises."""
    if not s:
        return s
    return sanitize_utf8(recover_filename(s))


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
