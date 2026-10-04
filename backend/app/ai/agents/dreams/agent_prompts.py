"""The agent dream's one structured call: group pending AI instruction drafts
into general instructions, propose edits for instructions with repeated
negative feedback, and propose archiving unused AI instructions.

The model only *proposes*. Code recomputes every promotion gate (distinct
users and days per group), restricts edits and archives to the candidates it
supplied, and scopes everything to the one agent being dreamed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.ai.agents.checkins._llm import extract_json

SYSTEM = """You consolidate what an AI data agent learned from yesterday's conversations into its instructions.

Instructions are the agent's semantic layer and rules for EVERYONE who uses it: business definitions, metric logic, required filters, how tables and columns work. They are not personal preferences.

You receive:
- DRAFTS: instruction suggestions the agent wrote during separate conversations (d1, d2, ...). Several usually say the same thing in different words.
- LIVE: the agent's current instructions (L1, L2, ...), so you don't duplicate them.
- FEEDBACK: live instructions that users downvoted, with their comments.
- UNUSED: AI-written live instructions nobody has loaded in a long time.

Do three things:
1. GROUP drafts that express the same rule, and write ONE clear, general instruction per group: declarative, self-contained, specific (tables, columns, values), no conversation-specific details. If a LIVE instruction already covers the rule, set "edits_live" to its id and write the improved full text of that instruction instead of a new one. Put a draft in at most one group. Leave drafts that are one-offs, contradictory, too specific, or wrong out of every group.
2. For FEEDBACK items, propose a corrected full text ONLY when the comments say concretely what is wrong. Otherwise skip it.
3. For UNUSED items, propose archiving only ones that look obsolete or redundant with another live instruction. Skip anything that still looks useful.

Never invent a rule that no draft or comment supports. Prefer doing less.

Reply with JSON only:
{
  "groups": [{"draft_ids": ["d1","d4"], "title": "...", "text": "...", "load_mode": "intelligent"|"always", "edits_live": null|"L3", "why": "one line"}],
  "feedback_edits": [{"live_id": "L2", "text": "...", "why": "one line citing the comments"}],
  "archive": [{"live_id": "L7", "why": "one line"}],
  "summary": "one line"
}"""


@dataclass
class ProposedGroup:
    draft_ids: List[str]
    title: str
    text: str
    load_mode: str = "intelligent"
    edits_live: Optional[str] = None
    why: str = ""


@dataclass
class ProposedEdit:
    live_id: str
    text: str
    why: str = ""


@dataclass
class ProposedArchive:
    live_id: str
    why: str = ""


@dataclass
class AgentDreamProposal:
    groups: List[ProposedGroup] = field(default_factory=list)
    feedback_edits: List[ProposedEdit] = field(default_factory=list)
    archive: List[ProposedArchive] = field(default_factory=list)
    summary: str = ""


def _clip(s: Any, n: int) -> str:
    s = "" if s is None else str(s)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def build_prompt(*, agent_name: str, agent_description: str, drafts: List[dict], live: List[dict],
                 feedback: List[dict], unused: List[dict]) -> str:
    lines = [f"AGENT: {_clip(agent_name, 120)}"]
    if agent_description:
        lines.append(f"DESCRIPTION: {_clip(agent_description, 400)}")
    lines.append("")
    lines.append("DRAFTS:")
    for d in drafts:
        lines.append(f"- {d['key']}: {_clip(d['text'], 600)}")
    if not drafts:
        lines.append("(none)")
    lines.append("")
    lines.append("LIVE:")
    for l in live:
        lines.append(f"- {l['key']}: {_clip(l['title'], 120)} — {_clip(l['text'], 400)}")
    if not live:
        lines.append("(none)")
    lines.append("")
    lines.append("FEEDBACK:")
    for f in feedback:
        comments = "; ".join(_clip(c, 200) for c in (f.get("comments") or [])[:5]) or "(no comments)"
        lines.append(f"- {f['key']}: {f['down']} downvotes — comments: {comments}")
    if not feedback:
        lines.append("(none)")
    lines.append("")
    lines.append("UNUSED:")
    for u in unused:
        lines.append(f"- {u['key']}: last used {u.get('last_used') or 'never'}")
    if not unused:
        lines.append("(none)")
    return "\n".join(lines)


def parse(raw: Any) -> Optional[AgentDreamProposal]:
    obj = extract_json(raw)
    if not isinstance(obj, dict):
        return None
    out = AgentDreamProposal(summary=_clip(obj.get("summary"), 300))
    for g in obj.get("groups") or []:
        if not isinstance(g, dict):
            continue
        ids = [str(x) for x in (g.get("draft_ids") or []) if isinstance(x, (str, int))]
        text = (g.get("text") or "").strip()
        if not ids or not text:
            continue
        mode = g.get("load_mode") if g.get("load_mode") in ("always", "intelligent") else "intelligent"
        edits_live = g.get("edits_live") if isinstance(g.get("edits_live"), str) and g.get("edits_live") else None
        out.groups.append(ProposedGroup(
            draft_ids=ids, title=_clip(g.get("title") or text, 120), text=text[:4000],
            load_mode=mode, edits_live=edits_live, why=_clip(g.get("why"), 300),
        ))
    for e in obj.get("feedback_edits") or []:
        if isinstance(e, dict) and e.get("live_id") and (e.get("text") or "").strip():
            out.feedback_edits.append(ProposedEdit(
                live_id=str(e["live_id"]), text=str(e["text"]).strip()[:4000], why=_clip(e.get("why"), 300),
            ))
    for a in obj.get("archive") or []:
        if isinstance(a, dict) and a.get("live_id"):
            out.archive.append(ProposedArchive(live_id=str(a["live_id"]), why=_clip(a.get("why"), 300)))
    return out


async def run_consolidation(model, prompt: str, *, run_id: str) -> Optional[AgentDreamProposal]:
    from app.ai.agents.checkins._llm import call_small_model

    raw = await call_small_model(
        model, system=SYSTEM, prompt=prompt, usage_scope="dream_agent", usage_scope_ref_id=run_id,
    )
    return parse(raw)


def proposal_to_json(p: AgentDreamProposal) -> Dict[str, Any]:
    return json.loads(json.dumps({
        "groups": [g.__dict__ for g in p.groups],
        "feedback_edits": [e.__dict__ for e in p.feedback_edits],
        "archive": [a.__dict__ for a in p.archive],
        "summary": p.summary,
    }))
