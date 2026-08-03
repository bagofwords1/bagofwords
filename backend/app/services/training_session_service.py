"""Embedded agent-training sessions (Create Data Agent wizard, step 3).

The wizard's "Set Context" step can start a short, guided training session for
the agent the user just created. The session is a regular training-mode report
(titled ``Training "<agent name>"``) driven by a *hidden* kickoff prompt — the
``trigger_source`` idiom from ``machine_turn.py``: the ``role='user'`` brief is
filtered out of the timeline by ``get_completions_v2``, a compact
``role='external'`` event strip shows instead, and the agent's ``role='system'``
reply streams normally. The wizard embeds ``/reports/{id}?embed=1`` in an
iframe, so the whole tool surface (describe/inspect, notes, clarify,
instructions) renders with the exact same components as the full report page.
"""
from __future__ import annotations

import asyncio
import logging
import re

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.data_source import DataSource
from app.models.datasource_table import DataSourceTable
from app.models.organization import Organization
from app.models.report import Report
from app.models.user import User

logger = logging.getLogger(__name__)

TRIGGER_SOURCE = "training_session"

# How many tables the kickoff brief asks the agent to actually review.
MAIN_TABLES_LIMIT = 10


class TrainingSessionService:

    # ------------------------------------------------------------------
    # Preview — powers the wizard CTA copy ("32 tables … looks like a
    # music store") before any session exists.
    # ------------------------------------------------------------------
    async def get_training_preview(
        self,
        db: AsyncSession,
        data_source_id: str,
        organization: Organization,
        current_user: User,
    ) -> dict:
        data_source = await self._get_data_source(db, data_source_id, organization)

        names_res = await db.execute(
            select(DataSourceTable.name)
            .filter(
                DataSourceTable.datasource_id == str(data_source.id),
                DataSourceTable.is_active == True,  # noqa: E712
            )
            .order_by(DataSourceTable.name)
        )
        table_names = [n for (n,) in names_res.all() if n]
        table_count = len(table_names)

        model = await organization.get_default_llm_model(db)
        llm_available = bool(model)
        training_enabled = self._training_mode_enabled(organization)

        domain_hint = None
        if llm_available and table_count > 0 and training_enabled:
            domain_hint = await self._guess_domain(model, table_names)

        return {
            "agent_name": data_source.name,
            "table_count": table_count,
            "domain_hint": domain_hint,
            "llm_available": llm_available,
            "use_llm_sync": bool(getattr(data_source, "use_llm_sync", True)),
            "training_enabled": training_enabled,
        }

    # ------------------------------------------------------------------
    # Start — create the report and kick off the hidden brief.
    # ------------------------------------------------------------------
    async def start_training_session(
        self,
        db: AsyncSession,
        data_source_id: str,
        organization: Organization,
        current_user: User,
    ) -> dict:
        from app.schemas.completion_schema import PromptSchema
        from app.schemas.completion_v2_schema import CompletionCreate
        from app.schemas.report_schema import ReportCreate
        from app.services.completion_service import CompletionService
        from app.services.report_service import ReportService

        data_source = await self._get_data_source(db, data_source_id, organization)

        if not self._training_mode_enabled(organization):
            raise HTTPException(
                status_code=400,
                detail="Training mode is not enabled for this organization",
            )

        default_model = await organization.get_default_llm_model(db)
        if not default_model:
            raise HTTPException(
                status_code=400,
                detail="No LLM model configured for this organization.",
            )
        # Short guided sessions run on the small default (e.g. Haiku) when the
        # org has one — cheap and fast; falls back to the org default.
        from app.services.llm_service import LLMService
        model = await LLMService().get_default_model(
            db, organization, current_user, is_small=True
        ) or default_model

        count_res = await db.execute(
            select(func.count(DataSourceTable.id)).filter(
                DataSourceTable.datasource_id == str(data_source.id),
                DataSourceTable.is_active == True,  # noqa: E712
            )
        )
        table_count = int(count_res.scalar() or 0)

        title = f'Training "{data_source.name}"'
        report_schema = await ReportService().create_report(
            db,
            ReportCreate(title=title, data_sources=[str(data_source.id)]),
            current_user,
            organization,
        )

        # Training mode + model pin are set directly on the row: the HTTP
        # update path re-gates on org flag + per-agent manage_instructions,
        # which the route decorator for this endpoint already enforces
        # (data_source `manage` implies manage_instructions).
        report_res = await db.execute(select(Report).filter(Report.id == str(report_schema.id)))
        report = report_res.scalar_one()
        report.mode = "training"
        report.model_id = str(model.id)
        await db.commit()
        await db.refresh(report)

        # Visible event strip (same shape machine_turn.py writes). The kickoff
        # brief itself stays hidden from the timeline.
        from app.models.completion import Completion
        event = Completion(
            prompt={
                "content": "Training session started",
                "summary": "Training session started",
                "meta": {"agent_name": data_source.name},
            },
            completion={"content": ""},
            model=TRIGGER_SOURCE,
            report_id=str(report.id),
            turn_index=0,
            message_type="training_session_started",
            role="external",
            status="success",
            user_id=str(current_user.id),
            external_platform=TRIGGER_SOURCE,
            trigger_source=TRIGGER_SOURCE,
        )
        db.add(event)
        await db.commit()

        brief = self._build_kickoff_brief(
            agent_name=data_source.name,
            table_count=table_count,
        )
        # background=True persists the hidden user turn + in-progress system
        # turn now (so the embedded page attaches to the live stream on load)
        # and runs the agent on its own session.
        await CompletionService().create_completion(
            db=db,
            report_id=str(report.id),
            completion_data=CompletionCreate(
                prompt=PromptSchema(content=brief, mode="training")
            ),
            current_user=current_user,
            organization=organization,
            background=True,
            trigger_source=TRIGGER_SOURCE,
        )

        return {"report_id": str(report.id), "title": title}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _get_data_source(
        self, db: AsyncSession, data_source_id: str, organization: Organization
    ) -> DataSource:
        res = await db.execute(
            select(DataSource).filter(
                DataSource.id == data_source_id,
                DataSource.organization_id == organization.id,
            )
        )
        data_source = res.scalar_one_or_none()
        if not data_source:
            raise HTTPException(status_code=404, detail="Data source not found")
        return data_source

    def _training_mode_enabled(self, organization: Organization) -> bool:
        # Same interpretation as report_service.update_report's training gate.
        org_settings = organization.settings
        if not org_settings:
            return False
        cfg = org_settings.get_config("enable_training_mode")
        if cfg is None:
            return False
        if hasattr(cfg, "value"):
            return cfg.value is not False
        if isinstance(cfg, dict):
            return cfg.get("value") is not False
        return True

    async def _guess_domain(self, model, table_names: list[str]) -> str | None:
        """One tiny LLM call: guess the business domain from table names.

        Best-effort — any failure or an unusable answer degrades to None and
        the CTA copy simply drops the domain sentence.
        """
        from app.ai.llm import LLM
        from app.dependencies import async_session_maker

        names = ", ".join(table_names[:60])
        prompt = (
            "Here are the table names of a database:\n"
            f"{names}\n\n"
            "In 2-5 plain-English words, what kind of business data is this? "
            "Answer with just the guess, lowercase, no punctuation, no quotes — "
            "for example: music store data, e-commerce orders, hospital records, "
            "financial trading data. If you genuinely cannot tell, answer: unknown"
        )
        try:
            llm = LLM(model, usage_session_maker=async_session_maker)
            raw = await asyncio.wait_for(
                asyncio.to_thread(llm.inference, prompt, usage_scope="data_source.training_preview"),
                timeout=15,
            )
            hint = re.sub(r"[\s\.\"'`]+", " ", str(raw or "")).strip().strip(".").strip()
            if not hint or hint.lower().startswith("unknown"):
                return None
            if len(hint) > 48 or len(hint.split()) > 7:
                return None
            return hint
        except Exception:
            logger.info("training preview domain guess failed", exc_info=True)
            return None

    def _build_kickoff_brief(self, agent_name: str, table_count: int) -> str:
        over_limit = table_count > MAIN_TABLES_LIMIT
        review_scope = (
            f"only the MAIN tables — at most {MAIN_TABLES_LIMIT} of the {table_count} in scope; "
            "pick the most central, business-relevant ones by name and relationships"
            if over_limit
            else f"the {table_count} tables in scope"
        )
        over_limit_note = (
            f'   - Since only ~{MAIN_TABLES_LIMIT} of {table_count} tables get reviewed now, end the note '
            'with a short "To review later" section listing the table names you skipped, and include one '
            "clarify question (step 4) asking which of those actually matter.\n"
            if over_limit
            else ""
        )
        return f"""[Guided training session — kickoff brief. The user does not see this message; your replies and tool calls are what they see.]

You are running a quick, guided TRAINING SESSION for the agent "{agent_name}", which the user JUST created in the setup wizard. The session is embedded in the wizard as a small preview window. Be warm, delightful, and CONCISE — this is a first taste of training, not the full thing. Aim to finish within a couple of minutes and a handful of turns; the session is saved as 'Training "{agent_name}"' and the user can reopen it anytime from Reports to continue — mention that when you wrap up.

Follow this plan, in order:

1. FIRST, before any tool call, write a short friendly intro (2-3 sentences max): what you can see at a glance and what you are about to do — take a quick look at the data, jot down an overview note, then ask a few questions to set this agent up well.
2. In the SAME turn, review the data with TWO calls batched together: `describe_tables` and `inspect_data`, covering {review_scope}. Skip obvious lookup/junk tables. Keep inspect queries tiny (LIMIT 3 peeks at nulls, formats, join keys).
3. Then create ONE note titled "Data overview" with what you learned: the domain, each main table and what it holds, key relationships/join keys, and data-quality quirks you noticed (missing values, odd types, empty tables, suspicious columns). Tight and scannable — bullets, not prose.
{over_limit_note}4. Then call `clarify` ONCE with 2-4 sharp questions that would most improve this agent — ambiguous business terms or metrics you actually saw in the schema, which areas matter most to the user, definitions worth pinning down (e.g. what counts as "revenue" or "active"). Give clickable options where enumerable, plus "Other…". Your turn ends there; the user answers in the embedded window.
5. When answers come back: thank the user in ONE short sentence, save the durable learnings with `create_instruction` (concise, GENERAL rules — never record-level facts), update the note if useful, then EITHER ask one final clarify round (only if genuinely high-value questions remain) OR wrap up.
6. Wrap-up: a 3-5 bullet summary of what you learned and created (the note + instructions), then one warm closing sentence: the agent is ready to use, and they can continue training right here anytime — this session stays available.

Hard rules:
- No dashboards, artifacts, docs, or heavy analyses in this session.
- At most 2 clarify rounds total. Short turns. No walls of text.
- If note tools are unavailable, put the overview in your reply text instead.
- If the data is empty or unreadable, say so kindly and ask how the user would like to proceed."""


training_session_service = TrainingSessionService()
