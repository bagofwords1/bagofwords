"""@mention resolution.

A mention is ``@`` followed by the *display name* of a referenced object. Display
names are not identifiers: tables, semantic models and MCP tools routinely carry
spaces, slashes and dots — "Sales Orders", "Microsoft Fabric / dbo.sales".

A regex therefore cannot delimit a mention on its own. In ``@Sales Orders by
region`` nothing in the string says whether the name is "Sales", "Sales Orders"
or "Sales Orders by region" — that depends entirely on which names exist.
Matching ``[A-Za-z0-9_]+`` just takes the first word and silently truncates every
multi-word name.

So mentions are parsed against a dictionary of known names, longest match wins
(maximum munch — the same rule lexers use). The dictionary is held in a trie, so
a lookup costs O(length of the matched name) rather than O(number of names): an
agent with 5k tables parses at the same speed as one with five.

This mirrors ``frontend/utils/mentions.ts``; the two must stay in step, so the
behavioural cases are pinned by tests/unit/test_mentions.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

# Identifier-shaped fallback: what a mention looks like with no dictionary.
_BARE_MENTION_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:[.\-][A-Za-z0-9_]+)*")

# A name needs quoting when its own characters would not survive a bare parse.
_NEEDS_QUOTES_RE = re.compile(r"[\s\-.\"]")

# A dictionary match must end on a word boundary, so "Sales" does not chip the
# head of "@SalesForce".
_WORD_CHAR_RE = re.compile(r"[A-Za-z0-9_]")


@dataclass
class _TrieNode:
    children: dict = field(default_factory=dict)
    name: Optional[str] = None  # canonical display name; set on terminal nodes


class MentionMatcher:
    """Display names indexed for longest-prefix lookup.

    Matching is case-insensitive; the canonical casing from the dictionary is
    returned, so a resolved mention carries the object's real name regardless of
    how it was written.
    """

    def __init__(self, names: Iterable[Optional[str]]):
        self._root = _TrieNode()
        self._size = 0
        for raw in names:
            name = (raw or "").strip()
            if not name:
                continue
            node = self._root
            for ch in name.lower():
                nxt = node.children.get(ch)
                if nxt is None:
                    nxt = _TrieNode()
                    node.children[ch] = nxt
                node = nxt
            # First writer wins, so a stable dictionary order gives stable casing.
            if node.name is None:
                node.name = name
                self._size += 1

    def __len__(self) -> int:
        return self._size

    def match_at(self, text: str, pos: int) -> Optional[Tuple[str, int]]:
        """Longest dictionary name matching ``text`` at ``pos``.

        Returns ``(canonical_name, chars_consumed)`` or None. Walks the trie one
        character at a time, keeping the last terminal seen — so the longest
        candidate wins and the scan stops as soon as no name can extend further.
        """
        node = self._root
        best: Optional[Tuple[str, int]] = None
        for i in range(pos, len(text)):
            nxt = node.children.get(text[i].lower())
            if nxt is None:
                break
            node = nxt
            if node.name is not None:
                after = text[i + 1] if i + 1 < len(text) else ""
                if not after or not _WORD_CHAR_RE.match(after):
                    best = (node.name, i + 1 - pos)
        return best


EMPTY_MATCHER = MentionMatcher([])


@dataclass
class MentionSegment:
    """A run of the source text: literal when ``label`` is None."""

    text: str
    label: Optional[str] = None


def parse_mention_segments(
    text: str, matcher: Optional[MentionMatcher] = None
) -> List[MentionSegment]:
    """Split ``text`` into literal runs and mentions.

    Resolution order at each ``@``:
      1. a quoted label — ``@"Sales Orders"`` — always wins, it is self-delimiting;
      2. the longest name in ``matcher``;
      3. the identifier-shaped fallback, so undictionaried text still resolves.
    """
    src = text or ""
    out: List[MentionSegment] = []
    buffer: List[str] = []

    def flush() -> None:
        if buffer:
            out.append(MentionSegment("".join(buffer)))
            buffer.clear()

    i = 0
    n = len(src)
    while i < n:
        if src[i] == "@":
            rest = src[i + 1 :]

            # 1. Quoted mention.
            if rest[:1] == '"':
                end = rest.find('"', 1)
                if end != -1:
                    label = rest[1:end]
                    flush()
                    out.append(MentionSegment(f'@"{label}"', label))
                    i += 1 + end + 1
                    continue

            # 2. Longest known display name.
            hit = matcher.match_at(src, i + 1) if matcher is not None else None
            if hit is not None:
                name, length = hit
                flush()
                out.append(MentionSegment("@" + src[i + 1 : i + 1 + length], name))
                i += 1 + length
                continue

            # 3. Identifier-shaped fallback.
            word = _BARE_MENTION_RE.match(rest)
            if word:
                flush()
                out.append(MentionSegment("@" + word.group(0), word.group(0)))
                i += 1 + len(word.group(0))
                continue

        buffer.append(src[i])
        i += 1

    flush()
    return out


def extract_mention_labels(
    text: str, matcher: Optional[MentionMatcher] = None
) -> List[str]:
    """Distinct mention labels in ``text``, in order of first appearance."""
    seen = set()
    out: List[str] = []
    for seg in parse_mention_segments(text, matcher):
        if seg.label is None:
            continue
        key = seg.label.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(seg.label)
    return out


def mention_needs_quotes(name: Optional[str]) -> bool:
    """True when ``name`` must be written ``@"name"`` to survive a bare parse."""
    return bool(name) and bool(_NEEDS_QUOTES_RE.search(name))


def format_mention(name: str) -> str:
    """Canonical source form for a mention of ``name``."""
    return f'@"{name.replace(chr(34), "")}"' if mention_needs_quotes(name) else f"@{name}"


def resolve_mentions(
    text: str, candidates: Sequence[dict]
) -> List[dict]:
    """Resolve the @mentions in ``text`` against ``candidates``.

    ``candidates`` are dicts as returned by
    ``InstructionService.get_available_references`` — each carrying at least
    ``id``, ``type`` and ``name``. Returns one entry per distinct resolved
    mention, in order of first appearance, shaped for
    ``InstructionReferenceCreate``.

    Unresolvable mentions are dropped: they name something that does not exist,
    so there is nothing to link them to.
    """
    by_name: dict = {}
    for cand in candidates or []:
        name = (cand.get("name") or "").strip()
        if not name:
            continue
        by_name.setdefault(name.lower(), cand)

    matcher = MentionMatcher(c.get("name") for c in (candidates or []))

    out: List[dict] = []
    for label in extract_mention_labels(text, matcher):
        cand = by_name.get(label.lower())
        if not cand:
            continue
        out.append(
            {
                "object_type": cand.get("type"),
                "object_id": str(cand.get("id")),
                "relation_type": "mention",
                "display_text": cand.get("name"),
            }
        )
    return out


def canonicalize_mentions(text: str, candidates: Sequence[dict]) -> str:
    """Rewrite every resolvable mention in ``text`` to its canonical source form.

    ``@sales orders`` becomes ``@"Sales Orders"`` — right casing, and quoted so
    the text stays unambiguous for any reader that lacks the dictionary. Text
    that resolves to nothing is left exactly as written.
    """
    matcher = MentionMatcher(c.get("name") for c in (candidates or []))
    known = {(c.get("name") or "").lower() for c in (candidates or [])}
    parts: List[str] = []
    for seg in parse_mention_segments(text, matcher):
        if seg.label is not None and seg.label.lower() in known:
            parts.append(format_mention(seg.label))
        else:
            parts.append(seg.text)
    return "".join(parts)
