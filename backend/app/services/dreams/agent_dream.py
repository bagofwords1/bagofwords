"""The nightly agent dream: consolidate one agent's pending AI suggestions.

1. Gather (code): pending AI suggestion drafts touching the agent, the
   agent's live instructions, downvoted instructions with their comments,
   and AI instructions nobody has loaded for a long time. Expired drafts from
   the last ``EXPIRED_EVIDENCE_DAYS`` still count as evidence.
2. Propose (one small-model call): group drafts that say the same thing and
   write one general instruction per group; propose feedback-driven edits
   and archives. Skipped entirely when nothing could clear the gates.
3. Validate (code): every group must clear the promotion gates, recomputed
   here from the drafts it cites (>= MIN_DISTINCT_USERS users across >=
   MIN_DISTINCT_DAYS days); edits/archives are limited to the candidates we
   supplied; everything is scoped to this one agent.
4. Apply: one suggestion build ("Nightly · <agent> · <date>"), submitted for
   review and handed to the agent's Self-Learning policy via
   ``run_for_suggestion(trigger="nightly")``.
5. Close the backlog: suggestion builds fully absorbed by a promoted group
   are closed as merged; AI suggestion builds past the org's expiry are closed
   as expired. Neither records a reviewer verdict, so neither counts as a
   human rejection.
6. Tell the agent's managers once.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Set

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import lazyload

from app.services.dreams import common as C
from app.services.dreams.runtime import DreamResult

logger = logging.getLogger(__name__)

MERGED_MARKER = "[merged]"
EXPIRED_MARKER = "[expired]"
NIGHTLY_AI_SOURCE = "nightly_dream"
# Prompt budget.
MAX_DRAFTS_IN_PROMPT = 60
MAX_LIVE_IN_PROMPT = 60
MAX_FEEDBACK_IN_PROMPT = 15
MAX_UNUSED_IN_PROMPT = 20
# A downvoted instruction is a feedback candidate at this many downvotes.
MIN_DOWNVOTES = 2
# Re-check unused instructions at most this often.
UNUSED_RECHECK_DAYS = 7


@dataclass
class Draft:
    key: str
    build_id: str
    build_number: int
    instruction_id: str
    version_id: str
    text: str
    title: str
    user_id: Optional[str]
    day: str
    created_at: datetime
    expired: bool = False


@dataclass
class Gathered:
    drafts: List[Draft] = field(default_factory=list)
    live: List[dict] = field(default_factory=list)
    feedback: List[dict] = field(default_factory=list)
    unused: List[dict] = field(default_factory=list)
    pending_builds: Dict[str, dict] = field(default_factory=dict)  # build_id -> info


# --------------------------------------------------------------------- queries


async def _agent_instruction_ids(db, ds_id: str) -> Set[str]:
    from app.models.instruction import instruction_data_source_association as A

    return {
        str(r[0])
        for r in (
            await db.execute(select(A.c.instruction_id).where(A.c.data_source_id == str(ds_id)))
        ).all()
    }


async def _ai_builds(db, organization_id: str, *, statuses: Sequence[str], since: Optional[datetime] = None):
    from app.models.instruction_build import InstructionBuild

    q = select(InstructionBuild).options(lazyload("*")).where(
        InstructionBuild.organization_id == str(organization_id),
        InstructionBuild.source == "ai",
        InstructionBuild.is_main.is_(False),
        InstructionBuild.deleted_at.is_(None),
        InstructionBuild.status.in_(list(statuses)),
    )
    if since is not None:
        q = q.where(InstructionBuild.created_at >= since)
    return (await db.execute(q)).scalars().all()


async def _changed_rows(db, build_ids: Sequence[str]) -> Dict[str, List[tuple]]:
    """build_id -> [(instruction_id, version_id)] for rows the build changes."""
    from app.models.build_content import BuildContent

    out: Dict[str, List[tuple]] = defaultdict(list)
    if not build_ids:
        return out
    rows = (
        await db.execute(
            select(BuildContent.build_id, BuildContent.instruction_id, BuildContent.instruction_version_id).where(
                BuildContent.build_id.in_(list(build_ids)), BuildContent.is_change.is_(True),
            )
        )
    ).all()
    for b, i, v in rows:
        out[str(b)].append((str(i), str(v)))
    return out


async def _build_author(db, build) -> Optional[str]:
    if getattr(build, "created_by_user_id", None):
        return str(build.created_by_user_id)
    if getattr(build, "agent_execution_id", None):
        from app.models.agent_execution import AgentExecution

        ae = await db.get(AgentExecution, str(build.agent_execution_id))
        if ae is not None and ae.user_id:
            return str(ae.user_id)
    return None


async def gather(db, organization_id: str, data_source, now: datetime) -> Gathered:
    from app.models.instruction import Instruction
    from app.models.instruction_version import InstructionVersion

    g = Gathered()
    ds_id = str(data_source.id)
    agent_ids = await _agent_instruction_ids(db, ds_id)

    pending = await _ai_builds(db, organization_id, statuses=("pending_approval",))
    expired = [
        b for b in await _ai_builds(
            db, organization_id, statuses=("rejected",), since=now - timedelta(days=C.EXPIRED_EVIDENCE_DAYS)
        )
        if (b.rejection_reason or "").startswith(EXPIRED_MARKER)
    ]
    changed = await _changed_rows(db, [str(b.id) for b in pending + expired])

    n = 0
    for build, is_expired in [(b, False) for b in pending] + [(b, True) for b in expired]:
        rows = changed.get(str(build.id), [])
        in_agent = [(i, v) for i, v in rows if i in agent_ids]
        if not in_agent:
            continue
        author = await _build_author(db, build)
        if not is_expired:
            g.pending_builds[str(build.id)] = {
                "build": build,
                "changed": {i for i, _ in rows},
                "in_agent": {i for i, _ in in_agent},
                "created_at": build.created_at,
            }
        for instr_id, version_id in in_agent:
            v = await db.get(InstructionVersion, version_id)
            if v is None:
                continue
            n += 1
            g.drafts.append(Draft(
                key=f"d{n}", build_id=str(build.id), build_number=int(build.build_number or 0),
                instruction_id=instr_id, version_id=version_id, text=v.text or "", title=v.title or "",
                user_id=author, day=C.day_key(build.created_at) or "", created_at=build.created_at or now,
                expired=is_expired,
            ))

    # Live instructions scoped to the agent.
    live_rows = []
    if agent_ids:
        live_rows = (
            await db.execute(
                select(Instruction).options(lazyload("*")).where(
                    Instruction.id.in_(list(agent_ids)),
                    Instruction.organization_id == str(organization_id),
                    Instruction.status == "published",
                    Instruction.deleted_at.is_(None),
                ).order_by(Instruction.updated_at.desc())
            )
        ).scalars().all()
    for i, inst in enumerate(live_rows[:MAX_LIVE_IN_PROMPT], start=1):
        g.live.append({
            "key": f"L{i}", "id": str(inst.id), "title": inst.title or "", "text": inst.text or "",
            "source_type": inst.source_type or "user", "ai_source": inst.ai_source,
            "created_at": inst.created_at,
        })
    live_by_id = {l["id"]: l for l in g.live}

    # Downvoted live instructions with comments.
    g.feedback = await _feedback_candidates(db, organization_id, live_by_id, data_source, now)
    # Unused AI instructions.
    g.unused = await _unused_candidates(db, live_by_id, data_source, now)
    return g


async def _feedback_candidates(db, organization_id, live_by_id: Dict[str, dict], data_source, now) -> List[dict]:
    from app.models.completion_feedback import CompletionFeedback
    from app.models.instruction_feedback_event import InstructionFeedbackEvent

    if not live_by_id:
        return []
    since = getattr(data_source, "agent_dreamed_at", None) or (now - timedelta(days=30))
    rows = (
        await db.execute(
            select(InstructionFeedbackEvent.instruction_id, InstructionFeedbackEvent.feedback_type, CompletionFeedback.message)
            .join(CompletionFeedback, CompletionFeedback.id == InstructionFeedbackEvent.completion_feedback_id)
            .where(
                InstructionFeedbackEvent.org_id == str(organization_id),
                InstructionFeedbackEvent.instruction_id.in_(list(live_by_id.keys())),
                InstructionFeedbackEvent.created_at_event >= since,
            )
        )
    ).all()
    agg: Dict[str, dict] = {}
    for instr_id, ftype, message in rows:
        a = agg.setdefault(str(instr_id), {"down": 0, "comments": []})
        if str(ftype).lower() in ("negative", "down", "-1", "thumbs_down"):
            a["down"] += 1
            if message and message.strip():
                a["comments"].append(message.strip())
    out = []
    for instr_id, a in agg.items():
        if a["down"] >= MIN_DOWNVOTES:
            l = live_by_id[instr_id]
            out.append({"key": l["key"], "id": instr_id, "down": a["down"], "comments": a["comments"][:5]})
    out.sort(key=lambda x: -x["down"])
    return out[:MAX_FEEDBACK_IN_PROMPT]


async def _unused_candidates(db, live_by_id: Dict[str, dict], data_source, now) -> List[dict]:
    from app.models.instruction_usage_event import InstructionUsageEvent

    watermark = getattr(data_source, "agent_dreamed_at", None)
    if watermark is not None and watermark > now - timedelta(days=UNUSED_RECHECK_DAYS):
        return []
    cutoff = now - timedelta(days=C.UNUSED_ARCHIVE_DAYS)
    ai_old = [
        l for l in live_by_id.values()
        if (l["source_type"] == "ai" or l["ai_source"]) and l["created_at"] and l["created_at"] < cutoff
    ]
    if not ai_old:
        return []
    last = dict(
        (await db.execute(
            select(InstructionUsageEvent.instruction_id, func.max(InstructionUsageEvent.used_at))
            .where(InstructionUsageEvent.instruction_id.in_([l["id"] for l in ai_old]))
            .group_by(InstructionUsageEvent.instruction_id)
        )).all()
    )
    out = []
    for l in ai_old:
        lu = last.get(l["id"])
        if lu is None or lu < cutoff:
            out.append({"key": l["key"], "id": l["id"], "last_used": C.day_key(lu)})
    return out[:MAX_UNUSED_IN_PROMPT]


async def has_new_activity(db, org_settings, data_source, now: datetime) -> bool:
    """Cheap check the sweep uses to decide whether this agent is due."""
    from app.models.instruction_feedback_event import InstructionFeedbackEvent

    watermark = getattr(data_source, "agent_dreamed_at", None)
    org_id = str(data_source.organization_id)
    agent_ids = await _agent_instruction_ids(db, str(data_source.id))
    pending = await _ai_builds(db, org_id, statuses=("pending_approval",))
    if pending and agent_ids:
        changed = await _changed_rows(db, [str(b.id) for b in pending])
        expiry = C.expiry_days(org_settings)
        for b in pending:
            if not any(i in agent_ids for i, _ in changed.get(str(b.id), [])):
                continue
            if watermark is None or (b.created_at and b.created_at > watermark):
                return True
            if expiry and b.created_at and b.created_at < now - timedelta(days=expiry):
                return True
    if agent_ids:
        q = select(InstructionFeedbackEvent.id).where(
            InstructionFeedbackEvent.org_id == org_id,
            InstructionFeedbackEvent.instruction_id.in_(list(agent_ids)),
        )
        if watermark is not None:
            q = q.where(InstructionFeedbackEvent.created_at_event > watermark)
        if (await db.execute(q.limit(1))).first() is not None:
            return True
    if watermark is None or watermark < now - timedelta(days=UNUSED_RECHECK_DAYS):
        from app.models.instruction import Instruction

        if agent_ids:
            old_ai = (
                await db.execute(
                    select(Instruction.id).where(
                        Instruction.id.in_(list(agent_ids)),
                        Instruction.status == "published",
                        Instruction.deleted_at.is_(None),
                        or_(Instruction.source_type == "ai", Instruction.ai_source.isnot(None)),
                        Instruction.created_at < now - timedelta(days=C.UNUSED_ARCHIVE_DAYS),
                    ).limit(1)
                )
            ).first()
            if old_ai is not None:
                return True
    return False


# --------------------------------------------------------------------- gates


def group_gate(drafts: List[Draft]) -> Dict[str, Any]:
    users = {d.user_id for d in drafts if d.user_id}
    days = {d.day for d in drafts if d.day}
    return {
        "users": len(users),
        "days": len(days),
        "count": len(drafts),
        "promotable": len(users) >= C.MIN_DISTINCT_USERS and len(days) >= C.MIN_DISTINCT_DAYS,
    }


def could_promote_anything(g: Gathered) -> bool:
    """Skip the model call when no group could possibly clear the gates and
    there is nothing else to propose."""
    users = {d.user_id for d in g.drafts if d.user_id}
    days = {d.day for d in g.drafts if d.day}
    groups_possible = len(users) >= C.MIN_DISTINCT_USERS and len(days) >= C.MIN_DISTINCT_DAYS
    return groups_possible or bool(g.feedback) or bool(g.unused)


# --------------------------------------------------------------------- apply


async def _resolve_model(db, organization, actor):
    from app.services.llm_service import LLMService

    try:
        return await LLMService().get_default_model(db, organization, actor, is_small=True)
    except Exception:
        logger.exception("agent dream: no small model for org %s", organization.id)
        return None


async def _stage_edit(db, *, build, instruction_id: str, text: str, actor, evidence: str) -> bool:
    from app.models.instruction import Instruction
    from app.services.build_service import BuildService
    from app.services.instruction_version_service import InstructionVersionService

    from sqlalchemy.orm import selectinload

    inst = (
        await db.execute(
            select(Instruction)
            .options(
                selectinload(Instruction.references),
                selectinload(Instruction.data_sources),
                selectinload(Instruction.labels),
            )
            .where(Instruction.id == str(instruction_id))
        )
    ).scalar_one_or_none()
    if inst is None:
        return False
    refs = [
        {"object_type": r.object_type, "object_id": r.object_id, "column_name": r.column_name,
         "display_text": r.display_text}
        for r in (inst.references or [])
    ] or None
    version = await InstructionVersionService().create_version_from_data(
        db=db, instruction_id=str(inst.id), text=text, title=inst.title,
        description=inst.description, structured_data=inst.structured_data,
        formatted_content=inst.formatted_content, status="published",
        load_mode=inst.load_mode or "intelligent", references_json=refs,
        data_source_ids=[str(d.id) for d in (inst.data_sources or [])] or None,
        label_ids=[str(l.id) for l in (inst.labels or [])] or None,
        user_id=str(actor.id) if actor else None, evidence=evidence[:1000],
    )
    await BuildService().add_to_build(db, str(build.id), str(inst.id), str(version.id))
    return True


async def _close_build(db, build, marker: str, reason: str, now: datetime) -> None:
    """Close a suggestion build without a reviewer verdict: it is neither
    accepted nor rejected by a person (build_verdict reads per-hunk actions,
    which stay empty), so it never counts as a human rejection."""
    build.status = "rejected"
    build.rejection_reason = f"{marker} {reason}"[:1000]
    build.approved_at = now
    build.approved_by_user_id = None
    db.add(build)


async def run_agent_dream(
    db,
    *,
    run_id: str,
    organization,
    org_settings,
    target_id: str,
    now: datetime,
    consolidate=None,
    model=None,
) -> DreamResult:
    """Entry point used by the runtime (see runtime.DreamRuntime.run_unit).

    ``consolidate(model, prompt, run_id=...)`` and ``model`` are injectable
    for tests; production uses the org's small default model."""
    from app.ai.agents.dreams.agent_prompts import build_prompt, proposal_to_json
    from app.ai.agents.dreams.agent_prompts import run_consolidation
    from app.models.data_source import DataSource
    from app.services.agent_reliability_service import AgentReliabilityService

    ds = await db.get(DataSource, str(target_id))
    if ds is None:
        return DreamResult(status="skipped", reason="target_missing")

    g = await gather(db, str(organization.id), ds, now)
    summary = {
        "drafts": sum(1 for d in g.drafts if not d.expired),
        "expired_evidence": sum(1 for d in g.drafts if d.expired),
        "pending_builds": len(g.pending_builds),
        "live": len(g.live),
        "feedback": len(g.feedback),
        "unused": len(g.unused),
    }
    outputs: Dict[str, Any] = {"changes": 0}
    tool_calls: List[Dict[str, Any]] = []

    expiry = C.expiry_days(org_settings)
    reliability = AgentReliabilityService()
    actor = await reliability._resolve_actor_user(db, str(organization.id), None)

    proposal = None
    if g.drafts or g.feedback or g.unused:
        if could_promote_anything(g):
            model = model or await _resolve_model(db, organization, actor)
            if model is None and consolidate is None:
                return DreamResult(status="skipped", reason="no_model", inputs_summary=summary)
            prompt = build_prompt(
                agent_name=ds.name or "", agent_description=ds.description or "",
                drafts=[{"key": d.key, "text": d.text} for d in g.drafts[:MAX_DRAFTS_IN_PROMPT]],
                live=g.live, feedback=g.feedback, unused=g.unused,
            )
            await db.commit()  # release the read snapshot for the model call
            proposal = await (consolidate or run_consolidation)(model, prompt, run_id=run_id)
            if proposal is None:
                tool_calls.append({"name": "consolidate", "result": "invalid_output"})
            else:
                tool_calls.append({"name": "consolidate", "proposal": proposal_to_json(proposal)})

    drafts_by_key = {d.key: d for d in g.drafts}
    live_by_key = {l["key"]: l for l in g.live}
    feedback_keys = {f["key"] for f in g.feedback}
    unused_keys = {u["key"] for u in g.unused}

    creates: List[dict] = []
    edits: List[dict] = []
    archives: List[dict] = []
    held: List[dict] = []
    absorbed_instr: Set[str] = set()
    if proposal is not None:
        used_drafts: Set[str] = set()
        for grp in proposal.groups:
            members = [drafts_by_key[k] for k in grp.draft_ids if k in drafts_by_key and k not in used_drafts]
            if not members:
                continue
            gate = group_gate(members)
            entry = {
                "title": grp.title, "text": grp.text, "load_mode": grp.load_mode, "why": grp.why,
                "drafts": [m.key for m in members], **gate,
            }
            if not gate["promotable"]:
                held.append(entry)
                continue
            used_drafts.update(m.key for m in members)
            absorbed_instr.update(m.instruction_id for m in members if not m.expired)
            evidence = (
                f"Nightly learning: {gate['users']} users over {gate['days']} days "
                f"({gate['count']} suggestions, builds " + ", ".join(sorted({f'#{m.build_number}' for m in members})) + ")."
            )
            live = live_by_key.get(grp.edits_live) if grp.edits_live else None
            if live is not None:
                edits.append({"instruction_id": live["id"], "text": grp.text, "evidence": evidence,
                              "title": live["title"], "kind": "group", **entry})
            else:
                creates.append({**entry, "evidence": evidence})
        for e in proposal.feedback_edits:
            if e.live_id not in feedback_keys or e.live_id not in live_by_key:
                continue
            live = live_by_key[e.live_id]
            if any(x["instruction_id"] == live["id"] for x in edits):
                continue
            fb = next(f for f in g.feedback if f["key"] == e.live_id)
            edits.append({
                "instruction_id": live["id"], "text": e.text, "title": live["title"], "kind": "feedback",
                "why": e.why, "evidence": f"Nightly learning: {fb['down']} downvotes — {e.why}"[:1000],
            })
        for a in proposal.archive:
            if a.live_id not in unused_keys or a.live_id not in live_by_key:
                continue
            live = live_by_key[a.live_id]
            if live["source_type"] != "ai" and not live["ai_source"]:
                continue  # never archive human- or git-authored instructions
            archives.append({"instruction_id": live["id"], "title": live["title"], "why": a.why})

    # A master switch may have flipped during the model call: re-check before
    # any write.
    from app.services.dreams.runtime import load_org_settings

    # End the read transaction (fresh snapshot) without expiring loaded
    # objects — expire_on_commit is off, a rollback would expire them all.
    await db.commit()
    fresh = await load_org_settings(db, str(organization.id))
    if not C.agent_dreaming_enabled(fresh):
        return DreamResult(status="cancelled", reason="disabled", inputs_summary=summary, tool_calls=tool_calls)

    build = None
    automation: List[dict] = []
    if creates or edits or archives:
        build, automation = await _apply(
            db, organization=organization, ds=ds, actor=actor, now=now, run_id=run_id,
            creates=creates, edits=edits, archives=archives, org_settings=org_settings,
            summary_line=(proposal.summary if proposal else ""),
        )

    merged, expired_closed = await _close_backlog(
        db, g, absorbed_instr=absorbed_instr, build=build, expiry_days=expiry, now=now,
    )
    await db.commit()

    outputs = {
        "changes": len(creates) + len(edits) + len(archives),
        "created": [{"title": c["title"], "users": c["users"], "days": c["days"]} for c in creates],
        "edited": [{"instruction_id": e["instruction_id"], "title": e["title"], "kind": e["kind"]} for e in edits],
        "archived": [{"instruction_id": a["instruction_id"], "title": a["title"]} for a in archives],
        "held": [{"title": h["title"], "users": h["users"], "days": h["days"], "drafts": h["drafts"]} for h in held],
        "build_id": str(build.id) if build else None,
        "build_number": int(build.build_number) if build else None,
        "merged_builds": merged,
        "expired_builds": expired_closed,
        "automation": automation,
    }
    if outputs["changes"] or merged or expired_closed:
        await _notify_managers(db, organization=organization, ds=ds, outputs=outputs, now=now)

    if not (g.drafts or g.feedback or g.unused) and not expired_closed:
        return DreamResult(status="skipped", reason=C_REASON_NOTHING_NEW, inputs_summary=summary,
                           tool_calls=tool_calls, outputs=outputs)
    return DreamResult(status="done", inputs_summary=summary, tool_calls=tool_calls, outputs=outputs)


C_REASON_NOTHING_NEW = "nothing_new"


async def _apply(db, *, organization, ds, actor, now, run_id, creates, edits, archives, org_settings, summary_line):
    from app.models.instruction import Instruction
    from app.models.instruction_build import InstructionBuild
    from app.schemas.instruction_schema import InstructionCreate
    from app.services.agent_reliability_service import AgentReliabilityService
    from app.services.build_service import BuildService
    from app.services.instruction_service import InstructionService

    bs = BuildService()
    build = await bs.create_build(
        db, str(organization.id), source="ai", user_id=str(actor.id) if actor else None,
    )
    isvc = InstructionService()
    lines: List[str] = []
    for c in creates:
        payload = InstructionCreate(
            text=c["text"], title=c["title"], category="general", status="draft",
            load_mode=c.get("load_mode") or "intelligent",
            source_type="ai", ai_source=NIGHTLY_AI_SOURCE,
            data_source_ids=[str(ds.id)],
        )
        created = await isvc.create_instruction(
            db, payload, actor, organization, force_global=True, build=build, auto_finalize=False,
            version_status_override="published", evidence=c["evidence"],
        )
        try:
            inst = await db.get(Instruction, str(created.id))
            if inst is not None:
                inst.trigger_reason = "nightly_learning"
                db.add(inst)
        except Exception:
            pass
        lines.append(f"+ {c['title']} — {c['users']} users, {c['days']} days ({len(c['drafts'])} suggestions)")
    for e in edits:
        ok = await _stage_edit(db, build=build, instruction_id=e["instruction_id"], text=e["text"],
                               actor=actor, evidence=e["evidence"])
        if ok:
            lines.append(f"~ {e['title']} — {e['evidence'].replace('Nightly learning: ', '')}")
    for a in archives:
        removed = await bs.remove_from_build(db, str(build.id), a["instruction_id"])
        if removed:
            lines.append(f"- {a['title']} — unused for {C.UNUSED_ARCHIVE_DAYS}+ days. {a['why']}".strip())

    build_id = str(build.id)
    title = f"Nightly · {ds.name or 'agent'} · {C.local_date(now, C.org_timezone(org_settings))}"[:255]
    description = "\n".join(lines + ([f"\n{summary_line}"] if summary_line else []))
    # Plain UPDATEs: the build's in-session contents may hold rows that
    # remove_from_build just deleted, so re-adding the instance would fail.
    await db.execute(update(InstructionBuild).where(InstructionBuild.id == build_id)
                     .values(title=title, description=description))
    await db.commit()
    await bs.submit_build(db, build_id, user_id=str(actor.id) if actor else None)
    # Keep the nightly title whatever submit does.
    await db.execute(update(InstructionBuild).where(InstructionBuild.id == build_id).values(title=title))
    await db.commit()
    build = (await db.execute(
        select(InstructionBuild).where(InstructionBuild.id == build_id).execution_options(populate_existing=True)
    )).scalar_one()

    automation: List[dict] = []
    try:
        from app.models.agent_automation_run import TRIGGER_NIGHTLY

        records = await AgentReliabilityService().run_for_suggestion(
            db, organization, str(build.id), user=None, trigger=TRIGGER_NIGHTLY,
        )
        for r in records or []:
            automation.append({"data_source_id": str(r.data_source_id), "status": r.status,
                               "reason": (r.detail_json or {}).get("reason") if isinstance(r.detail_json, dict) else None})
    except Exception:
        logger.exception("agent dream: run_for_suggestion failed for build %s", build.id)
    build = await db.get(InstructionBuild, str(build.id))
    return build, automation


async def _close_backlog(db, g: Gathered, *, absorbed_instr: Set[str], build, expiry_days: int, now: datetime):
    merged: List[dict] = []
    expired: List[dict] = []
    for build_id, info in g.pending_builds.items():
        b = info["build"]
        if b.status != "pending_approval":
            continue
        if build is not None and absorbed_instr and info["changed"] and info["changed"] <= absorbed_instr:
            await _close_build(db, b, MERGED_MARKER, f"Merged into nightly build #{build.build_number}.", now)
            merged.append({"build_id": build_id, "build_number": int(b.build_number or 0)})
            continue
        if expiry_days and info["created_at"] and info["created_at"] < now - timedelta(days=expiry_days):
            await _close_build(db, b, EXPIRED_MARKER, f"No review after {expiry_days} days.", now)
            expired.append({"build_id": build_id, "build_number": int(b.build_number or 0)})
    return merged, expired


async def _notify_managers(db, *, organization, ds, outputs: Dict[str, Any], now: datetime) -> None:
    from app.services.inbox_service import InboxService

    changes = outputs["changes"]
    parts: List[str] = []
    for c in outputs["created"][:3]:
        parts.append(f"{c['title']} ({c['users']} users)")
    for e in outputs["edited"][:3]:
        parts.append(f"updated: {e['title']}")
    if outputs["archived"]:
        parts.append(f"{len(outputs['archived'])} unused instructions proposed for archive")
    if outputs["merged_builds"]:
        parts.append(f"{len(outputs['merged_builds'])} pending suggestions merged")
    if outputs["expired_builds"]:
        parts.append(f"{len(outputs['expired_builds'])} stale suggestions expired")
    status_line = ""
    for a in outputs.get("automation") or []:
        if str(a.get("data_source_id")) == str(ds.id):
            status_line = {
                "passed": "Evals passed — promoted.",
                "passed_pending": "Waiting for your review.",
                "gave_up": "Evals failed — left in review, nothing changed.",
                "no_evals": "No evals to run — waiting for your review.",
            }.get(a.get("status"), "")
    if changes and not status_line:
        status_line = "Waiting for your review."
    title = (
        f"{ds.name or 'Agent'} learned {changes} thing{'s' if changes != 1 else ''} overnight"
        if changes else f"{ds.name or 'Agent'}: overnight suggestion cleanup"
    )
    body = " · ".join(parts) + (f". {status_line}" if parts and status_line else status_line)
    # The agent page lists the agent's pending suggestions (the nightly one
    # included), like other agent notices.
    link = f"/agents/{ds.id}"
    try:
        await InboxService().notify_agent_managers(
            db, organization_id=str(organization.id), data_source_id=str(ds.id),
            type="nightly_learning", title=title[:200], body=body[:1000], link=link,
            subject={"data_source_id": str(ds.id), "build_id": outputs.get("build_id")},
            group_key=f"nightly_learning:{ds.id}:{C.day_key(now)}",
        )
    except Exception:
        logger.exception("agent dream: manager notification failed for %s", ds.id)
