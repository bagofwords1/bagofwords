"""Search the messages of one report (the ⌘K "In this report" group).

The transcript is paginated, so the client only has the pages it has loaded;
this searches every message the transcript would show, newest first.

What counts as a message's text is what the transcript renders for it:

- user / external rows: ``prompt.content`` (external rows also show
  ``completion.content``)
- system rows: the answer blocks' ``content`` and ``completion.content``

Tool output and reasoning are not searched (v1).

Matching is a case-insensitive substring over that text with whitespace
collapsed, done in Python: the JSON columns may hold ciphertext envelopes
(enterprise data_encryption) that SQL cannot look inside, and matching the
extracted text instead of the serialized JSON keeps keys such as "content"
from matching every row.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.completion import Completion
from app.models.completion_block import CompletionBlock

SNIPPET_BEFORE = 60
SNIPPET_AFTER = 100
MAX_LIMIT = 50


def visible_in_timeline() -> list:
    """WHERE clauses for the completions the report transcript shows.

    Hides internal machine triggers (the synthetic prompt the agent answers):
    (webhook_id OR trigger_source set) AND role='user'. The visible event entry
    (role='external') and the agent reply (role='system') still show.

    Sub-agent children (mode=subagent) are hidden too: the parent's delegate
    card is their only surface. NULL-safe: a plain `trigger_source ==
    'sub_agent'` is NULL for every ordinary row, and NOT NULL would filter
    those rows out as well.
    """
    return [
        ~(
            (Completion.webhook_id.isnot(None) | Completion.trigger_source.isnot(None))
            & (Completion.role == 'user')
        ),
        (Completion.trigger_source.is_(None))
        | (Completion.trigger_source != 'sub_agent')
        | (Completion.role != 'system'),
    ]


def completion_cursor(created_at, completion_id: str) -> str:
    """The transcript's opaque position format ("<ISO8601>~<id>")."""
    return f"{created_at.isoformat()}~{completion_id}"


def normalize_space(text: str) -> str:
    return " ".join(text.split())


def _content(value: Any) -> str | None:
    if isinstance(value, dict):
        content = value.get("content")
        return content if isinstance(content, str) and content.strip() else None
    return None


def message_texts(role: str, prompt: Any, completion: Any, block_contents: Iterable[str]) -> list[str]:
    """The texts the transcript renders for one completion, in display order."""
    if role == 'system':
        texts = [b for b in block_contents if isinstance(b, str) and b.strip()]
        answer = _content(completion)
        if answer:
            texts.append(answer)
        return texts
    texts = []
    asked = _content(prompt)
    if asked:
        texts.append(asked)
    if role == 'external':
        answer = _content(completion)
        if answer:
            texts.append(answer)
    return texts


def find_snippet(text: str, query: str) -> str | None:
    """A short window of ``text`` around the first match of ``query``, or None.

    Both are compared with whitespace collapsed and case ignored.
    """
    text = normalize_space(text)
    match = re.search(re.escape(query), text, re.IGNORECASE)
    if not match:
        return None
    start = max(0, match.start() - SNIPPET_BEFORE)
    end = min(len(text), match.end() + SNIPPET_AFTER)
    # Don't cut a word in half at either edge.
    if start > 0:
        space = text.find(" ", start, match.start())
        start = space + 1 if space != -1 else start
    if end < len(text):
        space = text.rfind(" ", match.end(), end)
        end = space if space != -1 else end
    snippet = text[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


async def search_report_messages(
    db: AsyncSession,
    report_id: str,
    query: str,
    limit: int = 20,
) -> list[dict]:
    """Hits ``{completion_id, role, created_at, snippet, cursor}``, newest first.

    Call inside ``display_redaction`` so the snippet — and the match itself —
    use the text the viewer sees: a term that only appears in a redacted value
    must not be findable (that would confirm the hidden value).
    """
    from app.ai.llm.pii.display import current_display_redactor, redact_text_display

    query = normalize_space(query or "")
    if not query:
        return []
    limit = max(1, min(int(limit or 20), MAX_LIMIT))

    rows = (await db.execute(
        select(
            Completion.id, Completion.role, Completion.created_at,
            Completion.prompt, Completion.completion,
        )
        .where(Completion.report_id == report_id, *visible_in_timeline())
        .order_by(Completion.created_at.desc(), Completion.id.desc())
    )).all()
    if not rows:
        return []

    # Block text is a plain String column: prefilter in SQL, confirm in Python.
    system_ids = [r.id for r in rows if r.role == 'system']
    blocks_by_completion: dict[str, list[str]] = {}
    if system_ids:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        block_rows = (await db.execute(
            select(CompletionBlock.completion_id, CompletionBlock.content)
            .where(
                CompletionBlock.completion_id.in_(
                    select(Completion.id).where(
                        Completion.report_id == report_id,
                        Completion.role == 'system',
                        *visible_in_timeline(),
                    )
                ),
                CompletionBlock.content.ilike(f"%{escaped}%", escape="\\"),
            )
            .order_by(CompletionBlock.completion_id, CompletionBlock.block_index)
        )).all()
        for completion_id, content in block_rows:
            blocks_by_completion.setdefault(completion_id, []).append(content)

    redacting = current_display_redactor() is not None
    hits: list[dict] = []
    for row in rows:
        for text in message_texts(row.role, row.prompt, row.completion, blocks_by_completion.get(row.id, ())):
            # Cheap raw check first so only candidate texts pay for redaction.
            # (A term that only exists in a redaction placeholder, like the
            # mask label itself, is not a hit — nobody searches for those.)
            if find_snippet(text, query) is None:
                continue
            snippet = find_snippet(redact_text_display(text), query) if redacting else find_snippet(text, query)
            if snippet is None:
                continue
            hits.append({
                "completion_id": row.id,
                "role": row.role,
                "created_at": row.created_at,
                "snippet": snippet,
                "cursor": completion_cursor(row.created_at, row.id),
            })
            break
        if len(hits) >= limit:
            break
    return hits
