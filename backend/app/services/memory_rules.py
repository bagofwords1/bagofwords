"""Pure rules for user memory entries — no DB, no app imports.

Memory holds FACTS about the user (their work, schedule, projects, the things
they follow, their own shorthand). Rules about how to answer or compute are
instructions — org instructions when they hold for everyone, the user's
personal (custom) instructions otherwise — and never memory.

Shared by every writer (the agent tools, the user API, the migration) and by
the context builder, so the same text normalizes, dedupes, expires and gets
refused the same way wherever it comes from.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta
from typing import Iterable, List, Optional, Sequence

SOURCES: tuple[str, ...] = ("user", "agent", "migration")
STATUSES: tuple[str, ...] = ("active", "superseded", "forgotten")

MAX_TEXT_CHARS = 280
MAX_QUOTE_CHARS = 200
MAX_TAGS = 4
MAX_TAG_CHARS = 64
MAX_ALIASES = 8
MAX_ALIAS_CHARS = 60
MAX_ACTIVE_ENTRIES = 200

EVENT_GRACE = timedelta(days=1)
# An undated fact the agent saved goes stale this long after it was last seen
# unless the user confirmed it (added or edited it themselves).
FACT_TTL = timedelta(days=90)

# Object tags tie an entry to a thing in the turn rather than to a word.
OBJECT_TAG_PREFIXES: tuple[str, ...] = ("agent", "data_source", "report")


class MemoryValidationError(ValueError):
    """A write was refused. ``code`` is machine-readable (and maps to an
    ``errors.memory.*`` catalog key on the API path); ``message`` is written
    for the agent, which handles the error itself."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

_WS = re.compile(r"\s+")
# Punctuation = every unicode "P*" / "S*" char. Letters in any script survive,
# so Hebrew/Spanish text normalizes as well as English.


def normalize_text(text: str) -> str:
    """Dedupe key: lowercase, collapsed whitespace, no punctuation."""
    s = unicodedata.normalize("NFKC", text or "").lower()
    s = "".join(" " if unicodedata.category(ch)[0] in ("P", "S") else ch for ch in s)
    return _WS.sub(" ", s).strip()


def clean_text(text: str) -> str:
    """Display form: trimmed, internal whitespace collapsed to single spaces."""
    return _WS.sub(" ", (text or "").strip())


def normalize_tag(tag: str) -> str:
    """``Board Deck`` / ``board_deck`` / `` board--deck `` → ``board-deck``.

    Object tags keep their prefix and id verbatim apart from case/whitespace:
    ``Agent:ABC-1`` → ``agent:abc-1``.
    """
    raw = unicodedata.normalize("NFKC", str(tag or "")).strip().lower()
    if ":" in raw:
        prefix, _, ident = raw.partition(":")
        prefix = re.sub(r"[\s\-]+", "_", prefix.strip())
        if prefix in OBJECT_TAG_PREFIXES and ident.strip():
            return f"{prefix}:{ident.strip()}"[:MAX_TAG_CHARS]
    slug = re.sub(r"[\s_]+", "-", raw)
    slug = "".join(ch for ch in slug if ch == "-" or ch.isalnum())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug[:MAX_TAG_CHARS]


def normalize_tags(tags: Optional[Iterable[str]]) -> List[str]:
    """Normalize, drop empties, merge exact slug matches, keep first-seen order."""
    out: List[str] = []
    for t in tags or []:
        slug = normalize_tag(t)
        if slug and slug not in out:
            out.append(slug)
    return out


def normalize_aliases(aliases: Optional[Iterable[str]]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for a in aliases or []:
        disp = clean_text(str(a or ""))[:MAX_ALIAS_CHARS]
        key = normalize_text(disp)
        if key and key not in seen:
            seen.add(key)
            out.append(disp)
    return out[:MAX_ALIASES]


def is_object_tag(tag: str) -> bool:
    prefix, sep, ident = (tag or "").partition(":")
    return bool(sep) and prefix in OBJECT_TAG_PREFIXES and bool(ident)


# ---------------------------------------------------------------------------
# Dates / expiry (computed on read — there is no background job)
# ---------------------------------------------------------------------------

def parse_when(value) -> Optional[datetime]:
    """Accept a datetime, a ``YYYY-MM-DD`` date or an ISO timestamp; return a
    naive UTC datetime (the column convention in this codebase)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            raise MemoryValidationError(
                "memory.invalid_date",
                f"'{value}' is not a valid ISO date. Use YYYY-MM-DD (resolve relative dates like "
                "'next Thursday' to an absolute date first).",
            )
    if dt.tzinfo is not None:
        from datetime import timezone
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def effective_expiry(
    *,
    expires_at: Optional[datetime],
    event_start: Optional[datetime],
    event_end: Optional[datetime],
    last_seen_at: Optional[datetime],
    source: Optional[str] = None,
) -> Optional[datetime]:
    """When an entry stops being injected/searchable. None = never.

    One rule: nothing the agent wrote is permanent.
    - an explicit ``expires_at`` always wins;
    - a dated fact ends one day after its date (or end date), where a
      date-only value means the end of that day;
    - an undated fact the agent saved goes stale FACT_TTL after it was last
      seen; one the user added or edited never expires.
    """
    if expires_at is not None:
        return expires_at
    anchor = event_end or event_start
    if anchor is not None:
        if anchor.hour == 0 and anchor.minute == 0 and anchor.second == 0 and anchor.microsecond == 0:
            anchor = anchor + timedelta(days=1)
        return anchor + EVENT_GRACE
    if source == "user":
        return None
    if last_seen_at is not None:
        return last_seen_at + FACT_TTL
    return None


def is_dated(entry) -> bool:
    return getattr(entry, "event_start", None) is not None


def is_expired(entry, now: datetime) -> bool:
    exp = effective_expiry(
        expires_at=getattr(entry, "expires_at", None),
        event_start=getattr(entry, "event_start", None),
        event_end=getattr(entry, "event_end", None),
        last_seen_at=getattr(entry, "last_seen_at", None),
        source=getattr(entry, "source", None),
    )
    return exp is not None and now >= exp


# ---------------------------------------------------------------------------
# Content filters
# ---------------------------------------------------------------------------

# Credentials / secrets. Conservative: well-known token shapes plus explicit
# "password: …" style assignments. Applied to EVERY write (agent and user).
_SECRET_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_\-]{16,}"),          # OpenAI / Anthropic keys
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                               # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),                     # GitHub tokens
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),                    # Slack tokens
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),                          # Google API keys
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{5,}"),  # JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:password|passwd|pwd|passcode|api[_\s-]?key|secret|token|pin)\s*(?:is|=|:)\s*\S{4,}", re.I),
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),                             # card-number-like digit runs
)

# Health and similarly sensitive personal details. Events are limited to work
# availability, so "out of office" is fine but the medical reason is not.
_SENSITIVE_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(
        r"\b(?:diagnos\w*|surgery|chemo\w*|therapy|therapist|pregnan\w*|illness|disease|cancer|"
        r"medication|prescription|depress\w*|anxiety|disorder|rehab|hospital\w*|sick leave|"
        r"religio\w*|sexual orientation|political (?:party|views)|ethnicity)\b",
        re.I,
    ),
)


def contains_secret(text: str) -> bool:
    return any(p.search(text or "") for p in _SECRET_PATTERNS)


def contains_sensitive(text: str) -> bool:
    return any(p.search(text or "") for p in _SENSITIVE_PATTERNS)


# Rule vs fact (agent writes only). One test: is the text a rule about how to
# answer or compute — a definition, a convention, a required filter, a way to
# format or phrase answers? Then it is an instruction, not memory. The prompt
# rule is the primary guard; this is a lexical backstop, tuned from the
# refusal log in the trace.
#
# The user's own shorthand maps THEIR words to a value ("when I say my region
# I mean EMEA") — a fact about how they talk — and stays memory.
_PERSONAL_SHORTHAND = re.compile(
    r"(?:\bwhen i (?:say|write|ask for|mention|refer to)\b|\bby [\"'“‘]?[^\"'”’]{1,40}[\"'”’]? i mean\b|"
    r"\bi (?:call|refer to)\b|\bmy (?:shorthand|term|name) for\b|"
    r"\b(?:user|they|he|she)\s+(?:says|means|calls|refers to|uses)\s+[\"'“‘]|"
    r"^\s*(?:uses\s+)?[\"'“‘]?(?:my|our)\s+\w[\w\s-]{0,30}[\"'”’]?\s*(?:=|→|->|means\b|to mean\b)|"
    r"^\s*uses\s+[\"'“‘][^\"'”’]{1,40}[\"'”’]\s+(?:to mean|for)\b|"
    # a quoted phrase mapped to a thing is the user's own label ("'the board deck' = Q3 Board Pack")
    r"^\s*[\"'“‘][^\"'”’]{1,40}[\"'”’]\s*(?:=|→|->))",
    re.I,
)
_RULE_PATTERNS: tuple[re.Pattern, ...] = (
    # Imperatives: a rule addressed to whoever answers.
    re.compile(
        r"^\s*(?:please\s+)?(?:always|never|don'?t|do not|use|show|display|format|round|exclude|include|filter|"
        r"count|treat|report|give|keep|put|avoid|make sure|lead with|start with|answer|respond|write|convert|"
        r"express|present)\b", re.I),
    # Modal rules anywhere in the text.
    re.compile(r"\b(?:must|should|always|never)\s+(?:be\s+)?\w+", re.I),
    # Definitions and metric logic.
    re.compile(r"\b(?:is|are) (?:defined|calculated|computed|measured|counted) (?:as|by|from|using)\b", re.I),
    re.compile(r"\bdefinition of\b", re.I),
    re.compile(
        r"^\s*[\"'“‘]?(?:an?\s+|the\s+)?(?!(?:my|i|our|their|his|her|they|he|she|user'?s?)\b)"
        r"[\w\s\-]{1,40}?[\"'”’]?\s*(?:\bmeans?\b|\brefers? to\b|=)", re.I),
    re.compile(
        r"^\s*[\"'“‘]?(?:an?\s+|the\s+)?(?!(?:my|i|our|their|his|her|they|he|she|user'?s?)\b)[\w\s\-]{0,40}?\b"
        r"(?:customers?|users?|accounts?|revenue|churn|arr|mrr|gmv|margin|retention|conversion|orders?|sales|"
        r"bookings|pipeline|leads?|subscriptions?|region|segment|cohort|metric|kpi|track|tracks)"
        r"[\"'”’]?\s+(?:are|is|includes?|excludes?)\b(?!\s+(?:due|on|at|in \d))", re.I),
    re.compile(r"\b(?:column|table|field)\s+[\w.`\"]+\s+(?:means|is|stores|holds|contains|represents)\b", re.I),
    re.compile(r"^\s*(?!my\b|i\b)[A-Z][A-Za-z]{1,10}\s+(?:includes?|excludes?|covers?)\s+\w+", re.I),
    # How-to-answer preferences phrased as facts ("Prefers numbers first").
    re.compile(
        r"\b(?:prefers?|wants?|likes?|expects?|needs?|asks? for)\b[^.;]{0,50}?\b(?:answers?|responses?|replies|amounts?|"
        r"format(?:ted|ting)?|numbers?|figures?|decimals?|currency|dollars?|euros?|pounds?|thousands|millions|"
        r"percent(?:ages?)?|charts?|tables?|bullets?|summar(?:y|ies)|short(?:er)?|concise|brief|length|detail(?:ed)?|"
        r"sql|code|emojis?|tone|language|units?|rounded|rounding|usd|eur|gbp|ils|nis|jpy|chf|cad|aud)\b"
        r"|\b(?:prefers?|wants?|likes?)\b[^.;]{0,50}?[$€£¥₪]", re.I),
    re.compile(r"[$€£]\s?\d+(?:\.\d)?\s?[kKmMbB]\b"),  # "$2.3K"-style formatting examples
    # Format specs as noun phrases ("Amounts in €M, one decimal").
    re.compile(
        r"^\s*(?:all\s+)?(?:amounts?|numbers?|figures?|values?|money|currency|prices?|dates?|percent\w*|"
        r"answers?|responses?|charts?|tables?|totals?)\b[^.;]{0,30}?\b(?:in|as|with|to|rounded|formatted)\b", re.I),
)


def looks_like_rule(text: str) -> Optional[str]:
    """Return the matched fragment when ``text`` reads like a rule about how
    to answer or compute (an instruction — org or personal), else None. The
    user's own shorthand is a fact and is exempt."""
    s = clean_text(text)
    if not s:
        return None
    if _PERSONAL_SHORTHAND.search(s):
        return None
    for pat in _RULE_PATTERNS:
        m = pat.search(s)
        if m:
            return m.group(0).strip()
    return None


# ---------------------------------------------------------------------------
# Legacy migration helpers
# ---------------------------------------------------------------------------

_BULLET = re.compile(r"^\s*(?:[-*•·]|\d+[.)])\s+")


def legacy_lines(memory_text: Optional[str]) -> List[str]:
    """Split a legacy ``Membership.memory`` document into one fact per line
    or bullet. Headings (``#``) and blanks are dropped; each fact is trimmed
    to the entry cap."""
    out: List[str] = []
    for raw in (memory_text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = _BULLET.sub("", line).strip()
        line = clean_text(line)
        if not line:
            continue
        if len(line) > MAX_TEXT_CHARS:
            line = line[: MAX_TEXT_CHARS - 1].rstrip() + "…"
        if normalize_text(line) not in {normalize_text(x) for x in out}:
            out.append(line)
    return out


def best_quote(message: Optional[str], text: str, keywords_fn=None) -> Optional[str]:
    """Pick the user's own sentence that best supports ``text`` (≤200 chars).

    Chosen by code, never by the model: the sentence of the user's message
    with the highest word overlap with the entry, falling back to the start of
    the message.
    """
    msg = clean_text(message or "")
    if not msg:
        return None
    sentences = [s.strip() for s in re.split(r"(?<=[.!?\n])\s+", msg) if s.strip()] or [msg]
    target = set(normalize_text(text).split())
    best, best_score = sentences[0], -1
    for s in sentences:
        score = len(target & set(normalize_text(s).split()))
        if score > best_score:
            best, best_score = s, score
    if len(best) > MAX_QUOTE_CHARS:
        best = best[: MAX_QUOTE_CHARS - 1].rstrip() + "…"
    return best


# Direct memory-edit requests in the user's own message — the one case where
# the agent may change an entry the user typed themselves.
_DIRECT_EDIT = re.compile(
    r"\b(?:forget|remove|delete|erase|drop|change|update|edit|correct|fix|replace|"
    r"no longer|not anymore|isn'?t true|stop remembering|don'?t remember)\b",
    re.I,
)


def is_direct_edit_request(message: Optional[str], entry_text: str, entry_tags: Sequence[str] = ()) -> bool:
    """True only when the user's message both asks for a change AND points at
    this entry (shares a content word with it). Ambiguous → False."""
    msg = message or ""
    if not _DIRECT_EDIT.search(msg):
        return False
    stop = {"the", "a", "an", "i", "my", "me", "to", "of", "and", "is", "in", "on", "that", "for", "it", "with"}
    msg_words = {w for w in normalize_text(msg).split() if len(w) > 2 and w not in stop}
    entry_words = {w for w in normalize_text(entry_text).split() if len(w) > 2 and w not in stop}
    for t in entry_tags or ():
        entry_words.update(w for w in normalize_text(t.replace("-", " ")).split() if len(w) > 2)
    return bool({_light_stem(w) for w in msg_words} & {_light_stem(w) for w in entry_words})


def _light_stem(word: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if word.endswith(suf) and len(word) - len(suf) >= 3:
            return word[: -len(suf)]
    return word


# ---------------------------------------------------------------------------
# Noticing: lexical signals that a user message carries durable personal
# context. Only used to put a one-line hint next to the ask — the model still
# decides whether anything is worth saving (and the tools still validate).
# ---------------------------------------------------------------------------

_DATE_WORD = (
    r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|tonight|next (?:week|month|quarter)|"
    r"this (?:week|month|friday|monday)|the week after|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}|"
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
# Facts about the user → memory.
_FACT_SIGNALS: tuple[tuple[str, re.Pattern], ...] = (
    ("role", re.compile(
        r"\b(?:i(?:'m| am) (?:the|a|an|in charge|responsible)|my (?:role|job|title|team) is|"
        r"i (?:own|run|manage|lead|report to|present to|work (?:in|for|on the))\b)", re.I)),
    ("their own shorthand", re.compile(
        r"\b(?:when i say|by [\"'“‘]?[^\"'”’]{1,40}[\"'”’]? i mean|i call (?:it|them)|i refer to|my shorthand)\b", re.I)),
    ("a date in their work life", re.compile(
        r"\b(?:meeting|deadline|due|board|review|offsite|off-site|trip|travel(?:ling|ing)?|vacation|holiday|launch|"
        r"out of (?:the )?office|ooo|i(?:'m| am) off|time off|on leave|presentation|demo day)\b"
        r"[^.?!]{0,60}\b" + _DATE_WORD + r"\b|\b" + _DATE_WORD + r"\b[^.?!]{0,60}\b(?:meeting|deadline|due|board|"
        r"review|offsite|vacation|holiday|launch|out of (?:the )?office|i(?:'m| am) off|time off|presentation)\b", re.I)),
    ("what they're working on", re.compile(
        r"\b(?:i(?:'m| am) (?:working on|investigating|looking into|focused on|focusing on|digging into|preparing)|"
        r"my (?:focus|priority|project) (?:is|this)|i(?:'m| am) (?:on|leading) the\b)", re.I)),
    ("what they follow", re.compile(
        r"\b(?:i (?:track|follow|watch|monitor|report on|look at)\b|the (?:metrics?|kpis?|numbers) i (?:track|follow|watch))",
        re.I)),
    ("explicit request", re.compile(r"\b(?:remember (?:that|this|i|my)|keep in mind)\b", re.I)),
)
# Rules about how to answer → instructions (the user's personal ones by default).
_RULE_SIGNALS: tuple[tuple[str, re.Pattern], ...] = (
    ("how they want answers", re.compile(
        r"\b(?:shorter|longer|too (?:long|short|verbose|wordy|detailed)|more concise|less detail|more detail|"
        r"bullet(?:s| points)?|number first|numbers first|headline first|one decimal|two decimals|no decimals|"
        r"in (?:thousands|millions|billions)|no emojis?|as a table|as a chart|plain text|tl;?dr|"
        r"from now on|going forward|in (?:the )?future|always (?:use|show|give|put|start)|"
        r"(?:use|show|give me)\b[^.?!]{0,30}\b(?:format|decimals?|currency|percent(?:age)?s?|thousands|millions|\$|€|£|k\b|m\b))",
        re.I)),
)


def fact_signals(message: Optional[str]) -> List[str]:
    """Kinds of durable facts about the user a message may carry."""
    msg = message or ""
    return [kind for kind, pat in _FACT_SIGNALS if pat.search(msg)]


def rule_signals(message: Optional[str]) -> List[str]:
    """Kinds of how-to-answer rules a message may state."""
    msg = message or ""
    return [kind for kind, pat in _RULE_SIGNALS if pat.search(msg)]


def appends_new_fact(old_text: str, new_text: str) -> bool:
    """True when an update keeps the old fact verbatim and bolts another one
    on ("Prefers X" → "Prefers X; formats money as $K"). One fact per entry:
    the addition should be its own entry."""
    old, new = normalize_text(old_text), normalize_text(new_text)
    if not old or not new or old == new or not new.startswith(old):
        return False
    return len(new.split()) - len(old.split()) >= 3


def note_rules(note: Optional[str]) -> List[str]:
    """The lines of a user's Custom instructions, without bullet markers."""
    return [_BULLET.sub("", l).strip() for l in (note or "").splitlines() if l.strip()]


def note_has_rule(note: Optional[str], rule: str) -> bool:
    key = normalize_text(rule)
    return bool(key) and any(normalize_text(l) == key for l in note_rules(note))


def append_rule_to_note(note: Optional[str], rule: str, max_chars: int) -> Optional[str]:
    """``note`` with ``rule`` appended as a bullet, unchanged if already there,
    or None when it would not fit ``max_chars``."""
    current = (note or "").strip()
    if note_has_rule(current, rule):
        return current
    candidate = f"{current}\n- {clean_text(rule)}" if current else f"- {clean_text(rule)}"
    return candidate if len(candidate) <= max_chars else None

