"""Evidence quote verification.

A quote is "verified" when it appears (after normalization) in text the agent
actually read during this report — the persisted outputs of read_file,
read_email, web_fetch, etc. This is source-agnostic: it works the same for
uploads, SharePoint, S3 or Documentum, without re-fetching anything.

Unverified quotes are flagged, never rejected: PDF extraction order and
line-wrapping can defeat an exact match, and a false rejection would block an
otherwise correct record.
"""
import re
import unicodedata
from typing import Any, Dict, List, Optional

from sqlalchemy import select

_READ_TOOLS = ("read_file", "read_email", "web_fetch", "read_note", "read_excel_as_csv", "read_excel_range")
_MAX_SOURCES = 200
_MAX_TEXT = 2_000_000

_DASHES = "‐‑‒–—―−"
_QUOTES = {"‘": "'", "’": "'", "‚": "'", "‛": "'", "“": '"', "”": '"',
           "„": '"', "׳": "'", "״": '"'}


def normalize(text: str) -> str:
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    # Strip bidi / zero-width marks common in RTL PDFs.
    t = re.sub(r"[​-‏‪-‮⁦-⁩﻿]", "", t)
    for d in _DASHES:
        t = t.replace(d, "-")
    for k, v in _QUOTES.items():
        t = t.replace(k, v)
    # Re-join words hyphenated across a line break.
    t = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip().casefold()


def loose(text: str) -> str:
    """Letters/digits only, single-spaced. RTL PDF text layers mirror brackets
    and move sentence punctuation (".2027" for "2027."), so a quote that fails
    the normalized match is retried on this punctuation-free form."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text or "")).strip()


class SourceText:
    __slots__ = ("file_id", "names", "text", "loose")

    def __init__(self, file_id: Optional[str], names: List[str], text: str):
        self.file_id = file_id
        self.names = [n for n in names if n]
        self.text = text
        self.loose = loose(text)


async def collect_report_texts(db, report_id: Optional[str]) -> List[SourceText]:
    """Texts the agent read in this report (newest first)."""
    if not report_id:
        return []
    from app.models.agent_execution import AgentExecution
    from app.models.tool_execution import ToolExecution

    rows = (await db.execute(
        select(ToolExecution)
        .join(AgentExecution, AgentExecution.id == ToolExecution.agent_execution_id)
        .where(
            AgentExecution.report_id == str(report_id),
            ToolExecution.tool_name.in_(_READ_TOOLS),
        )
        .order_by(ToolExecution.created_at.desc())
        .limit(_MAX_SOURCES)
    )).scalars().all()
    out: List[SourceText] = []
    # What the user typed in this report is a source too ("Globex renewed
    # until 2027-12-31 — update the list"): a quote of it is verifiable.
    from app.models.completion import Completion
    prompts = (await db.execute(
        select(Completion.prompt)
        .where(Completion.report_id == str(report_id), Completion.role == "user")
        .order_by(Completion.created_at.desc())
        .limit(50)
    )).scalars().all()
    for p in prompts:
        text = p.get("content") if isinstance(p, dict) else (p if isinstance(p, str) else "")
        if isinstance(text, str) and text.strip():
            out.append(SourceText(None, ["user", "user message", "user instruction"], normalize(text)))
    for te in rows:
        res = te.result_json if isinstance(te.result_json, dict) else {}
        text = res.get("text") or res.get("csv") or res.get("content") or res.get("body") or ""
        if not isinstance(text, str) or not text.strip():
            continue
        args = te.arguments_json if isinstance(te.arguments_json, dict) else {}
        names = [
            str(res.get("file_id") or ""), str(res.get("file_name") or ""), str(res.get("path") or ""),
            str(args.get("file_id") or ""), str(res.get("session_file_id") or ""),
        ]
        out.append(SourceText(str(res.get("file_id") or args.get("file_id") or "") or None, names,
                              normalize(text[:_MAX_TEXT])))
    return out


def _ref_matches(ref: Optional[str], src: SourceText) -> bool:
    if not ref:
        return False
    r = ref.strip().casefold()
    for n in src.names:
        n = n.strip().casefold()
        if not n:
            continue
        if r == n or r.endswith("/" + n) or n.endswith("/" + r) or r in n or n in r:
            return True
    return False


def verify_quote(quote: Optional[str], ref: Optional[str], sources: List[SourceText]) -> bool:
    q = normalize(quote or "")
    if len(q) < 3 or not sources:
        return False
    # Prefer the referenced source, but accept any text the agent read — the
    # model often cites a display name instead of the internal id.
    preferred = [s for s in sources if _ref_matches(ref, s)]
    ordered = preferred + [s for s in sources if s not in preferred]
    for s in ordered:
        if q in s.text:
            return True
    lq = loose(q)
    if len(lq) >= 3:
        for s in ordered:
            if lq in s.loose:
                return True
    # Tolerate an ellipsis-elided quote ("A ... B"): every segment must match in order.
    parts = [p.strip() for p in re.split(r"\.\.\.|…", q) if p.strip()]
    if len(parts) > 1:
        for s in preferred + [s for s in sources if s not in preferred]:
            pos = 0
            ok = True
            for p in parts:
                i = s.text.find(p, pos)
                if i < 0:
                    ok = False
                    break
                pos = i + len(p)
            if ok:
                return True
    return False
