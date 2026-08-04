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
from sqlalchemy import select
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

        items, catalog_names = await self._catalog_breakdown(db, str(data_source.id))
        # Backwards-friendly total of catalog entries (tables/objects/files all
        # live in datasource_tables — only the noun differs; see useCatalogCount).
        table_count = sum(i["count"] for i in items if i["shape"] != "tools")

        model = await organization.get_default_llm_model(db)
        llm_available = bool(model)
        training_enabled = self._training_mode_enabled(organization)

        domain_hint = None
        if llm_available and catalog_names and training_enabled:
            domain_hint = await self._guess_domain(model, catalog_names)

        return {
            "agent_name": data_source.name,
            "table_count": table_count,
            # Shape-aware counts for the CTA copy: the noun follows each
            # connection's registry data_shape — "11 tables", "9 files",
            # "3 tools", or a combination. Order: tables, objects, files, tools.
            "items": items,
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

        items, _names = await self._catalog_breakdown(db, str(data_source.id))

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
            items=items,
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
    async def _catalog_breakdown(
        self, db: AsyncSession, data_source_id: str
    ) -> tuple[list[dict], list[str]]:
        """Active catalog entries grouped by their connection's data_shape,
        plus connection tools.

        The noun follows each connection's registry ``data_shape`` (mirrors
        ``frontend/composables/useCatalogCount.ts``). Table/object selections
        are the agent's ``datasource_tables`` rows; file directories don't
        materialize per-agent rows (their scope is connection-level), so files
        count from ``connection_tables``; tools from ``connection_tools``.
        Returns ``([{shape, count}, ...], names)`` with shapes ordered
        tables → objects → files → tools and zero-count shapes dropped;
        ``names`` feed the domain guess.
        """
        from app.models.connection import Connection
        from app.models.connection_table import ConnectionTable
        from app.models.connection_tool import ConnectionTool
        from app.models.domain_connection import domain_connection
        from app.schemas.data_source_registry import data_shape_for

        def _shape_of(conn_type: str | None) -> str:
            # Legacy rows without a connection link read as plain tables.
            try:
                shape = data_shape_for(conn_type) if conn_type else "tables"
            except Exception:
                shape = "tables"
            return shape if shape in ("tables", "objects", "files", "tools") else "tables"

        counts: dict[str, int] = {}
        names: list[str] = []

        rows = await db.execute(
            select(Connection.id, Connection.type, DataSourceTable.name)
            .select_from(DataSourceTable)
            .outerjoin(ConnectionTable, DataSourceTable.connection_table_id == ConnectionTable.id)
            .outerjoin(Connection, ConnectionTable.connection_id == Connection.id)
            .filter(
                DataSourceTable.datasource_id == data_source_id,
                DataSourceTable.is_active == True,  # noqa: E712
            )
        )
        seen_file_connections: set[str] = set()
        for conn_id, conn_type, name in rows.all():
            shape = _shape_of(conn_type)
            if shape == "tools":
                shape = "tables"
            if shape == "files" and conn_id:
                seen_file_connections.add(str(conn_id))
            counts[shape] = counts.get(shape, 0) + 1
            if name:
                names.append(name)

        # Files-shaped connections keep their catalog at the connection level
        # (the wizard's "N files match" scope card) — count those directly,
        # unless the agent somehow has explicit per-agent file rows already.
        conn_rows = await db.execute(
            select(Connection.id, Connection.type)
            .select_from(Connection)
            .join(domain_connection, domain_connection.c.connection_id == Connection.id)
            .filter(domain_connection.c.data_source_id == data_source_id)
        )
        for conn_id, conn_type in conn_rows.all():
            if _shape_of(conn_type) != "files":
                continue
            file_rows = await db.execute(
                select(ConnectionTable.name).filter(ConnectionTable.connection_id == str(conn_id))
            )
            file_names = [n for (n,) in file_rows.all() if n]
            if file_names and str(conn_id) not in seen_file_connections:
                counts["files"] = counts.get("files", 0) + len(file_names)
                names.extend(file_names)

        tool_rows = await db.execute(
            select(ConnectionTool.name)
            .select_from(ConnectionTool)
            .join(Connection, ConnectionTool.connection_id == Connection.id)
            .join(domain_connection, domain_connection.c.connection_id == Connection.id)
            .filter(domain_connection.c.data_source_id == data_source_id)
        )
        tool_names = [n for (n,) in tool_rows.all() if n]
        if tool_names:
            counts["tools"] = len(tool_names)
            names.extend(tool_names)

        items = [
            {"shape": shape, "count": counts[shape]}
            for shape in ("tables", "objects", "files", "tools")
            if counts.get(shape)
        ]
        return items, names

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
            "Here are the catalog item names (tables, files, or tools) of a data source:\n"
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

    def _build_kickoff_brief(self, agent_name: str, items: list[dict]) -> str:
        counts = {i["shape"]: i["count"] for i in items}
        noun = {"tables": "tables", "objects": "collections", "files": "files", "tools": "tools"}
        catalog_desc = ", ".join(f'{i["count"]} {noun[i["shape"]]}' for i in items) or "no catalog entries yet"

        # Tables/objects/files are all datasource_tables entries — the review
        # cap spans them; tools are reviewed from context, no calls needed.
        entry_count = sum(c for s, c in counts.items() if s != "tools")
        entry_nouns = " / ".join(noun[s] for s in ("tables", "objects", "files") if counts.get(s)) or "tables"
        over_limit = entry_count > MAIN_TABLES_LIMIT

        review_bits = []
        if counts.get("tables") or counts.get("objects"):
            scope = (
                f"only the MAIN {entry_nouns} — at most {MAIN_TABLES_LIMIT} of the {entry_count} in scope; "
                "pick the most central, business-relevant ones by name and relationships"
                if over_limit
                else f"the {entry_count} {entry_nouns} in scope"
            )
            review_bits.append(
                f"review the data with TWO calls batched together: `describe_tables` and `inspect_data`, covering {scope}. "
                "Skip obvious lookup/junk tables. Keep inspect queries tiny (LIMIT 3 peeks at nulls, formats, join keys)"
            )
        if counts.get("files"):
            review_bits.append(
                f"skim the file catalog: `list_files`, then `read_file` on 2-3 representative files (of the {counts['files']}) to learn what they contain — never read everything"
            )
        if counts.get("tools"):
            review_bits.append(
                f"review the {counts['tools']} connection tools already listed in your context (no calls needed) and note what each one is for"
            )
        review_step = "; also ".join(review_bits) if review_bits else (
            "note that the catalog looks empty — say so kindly and ask the user what they expected to see"
        )

        over_limit_note = (
            f'   - Since only ~{MAIN_TABLES_LIMIT} of {entry_count} {entry_nouns} get reviewed now, end the note '
            'with a short "To review later" section listing the names you skipped, and include one '
            "clarify question (step 4) asking which of those actually matter.\n"
            if over_limit
            else ""
        )
        return f"""[Guided training session — kickoff brief. The user does not see this message; your replies and tool calls are what they see.]

You are running a quick, guided TRAINING SESSION for the agent "{agent_name}", which the user JUST created in the setup wizard. Its catalog: {catalog_desc}. The session is embedded in the wizard as a small preview window. Be warm, delightful, and CONCISE — this is a first taste of training, not the full thing. Aim to finish within a couple of minutes and a handful of turns; the session is saved as 'Training "{agent_name}"' and the user can reopen it anytime from Reports to continue — mention that when you wrap up.

Follow this plan, in order:

1. FIRST, before any tool call, write a short friendly intro (2-3 sentences max): what you can see at a glance and what you are about to do — take a quick look at the data, jot down an overview note, then ask a few questions to set this agent up well.
2. In the SAME turn, {review_step}.
3. Then create ONE note titled "Data overview" with what you learned: the domain, each main item and what it holds, key relationships/join keys, and quality quirks you noticed (missing values, odd types, empty tables, suspicious columns).{" For tools, note the purpose of each." if counts.get("tools") else ""} Tight and scannable — bullets, not prose.
{over_limit_note}4. Then call `clarify` ONCE with 2-4 sharp questions that would most improve this agent — ambiguous business terms or metrics you actually saw, which areas matter most to the user, definitions worth pinning down (e.g. what counts as "revenue" or "active"). Give clickable options where enumerable, plus "Other…". Your turn ends there; the user answers in the embedded window.
5. When answers come back: thank the user in ONE short sentence, save the durable learnings with `create_instruction` (concise, GENERAL rules — never record-level facts), update the note if useful, then EITHER ask one final clarify round (only if genuinely high-value questions remain) OR wrap up.
6. Wrap-up: a 3-5 bullet summary of what you learned and created (the note + instructions), then one warm closing sentence: the agent is ready to use, and they can continue training right here anytime — this session stays available.

Hard rules:
- No dashboards, artifacts, docs, or heavy analyses in this session.
- At most 2 clarify rounds total. Short turns. No walls of text.
- If note tools are unavailable, put the overview in your reply text instead.
- If the data is empty or unreadable, say so kindly and ask how the user would like to proceed."""


training_session_service = TrainingSessionService()
