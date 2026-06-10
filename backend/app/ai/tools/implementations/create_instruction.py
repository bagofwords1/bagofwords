"""Create Instruction Tool - Creates instructions during training mode exploration.

This tool allows the training mode agent to create instructions in real-time
as it discovers semantic rules. All instructions are added to a single draft
build that gets finalized when the training session ends.
"""

from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel
import logging

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.create_instruction import CreateInstructionInput, CreateInstructionOutput
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)

logger = logging.getLogger(__name__)

# Minimum confidence to create an instruction
MIN_CONFIDENCE_THRESHOLD = 0.7

# Valid categories
VALID_CATEGORIES = {"general", "code_gen", "visualization", "dashboard", "system"}


class CreateInstructionTool(Tool):
    """Create instruction tool - creates reusable instructions during training mode.

    This tool is available only in training mode. It creates instructions that
    guide AI behavior for future analysis. Instructions are added to a draft
    build that gets finalized when the training session ends.
    """

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="create_instruction",
            description=(
                "ACTION: Create a new instruction that guides AI behavior. "
                "Only use when you have HIGH CONFIDENCE (>= 0.7) based on evidence from exploration. "
                "If confidence is lower, use 'clarify' tool to ask the user first. "
                "Instructions should capture non-obvious semantic rules that prevent mistakes.\n\n"
                "SCOPING — attach to the SPECIFIC agent(s) the rule pertains to, by judgement. "
                "By default an instruction must be scoped, NOT global:\n"
                "- table_names: narrow the rule to specific tables (also drives 'intelligent' loading). "
                "Don't list every table you inspected — that over-scopes the rule.\n"
                "- data_source_names: attach a data-source-wide rule to a specific agent WITHOUT "
                "enumerating tables. Use this when the rule applies across an agent but isn't about "
                "particular tables. Choose the agent(s) the rule is actually about — not all attached agents.\n"
                "- global_scope=True: ONLY when the user explicitly asks to make the rule global / apply "
                "to all agents. Never set it on your own judgement.\n"
                "If you set neither table_names nor data_source_names and the session has a single "
                "attached agent, the instruction is scoped to that agent automatically."
            ),
            category="action",
            version="1.0.0",
            input_schema=CreateInstructionInput.model_json_schema(),
            output_schema=CreateInstructionOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=30,
            idempotent=False,
            required_permissions=["manage_instructions"],
            tags=["training", "instruction", "semantic-learning"],
            allowed_modes=["training", "knowledge"],
            examples=[
                {
                    "input": {
                        "text": "When calculating revenue, always exclude orders with status='cancelled' or status='refunded' to avoid double-counting.",
                        "category": "code_gen",
                        "confidence": 0.9,
                        "evidence": "Observed in inspect_data: orders table has status values including 'cancelled' and 'refunded' which should not count as revenue.",
                        "load_mode": "intelligent",
                        "table_names": ["orders"]
                    },
                    "description": "Scoped: rule is specific to the orders table — list it in table_names so it loads when orders is queried."
                },
                {
                    "input": {
                        "text": "User status values: 1=active, 2=inactive, 3=banned. Always filter status=1 for active user counts.",
                        "category": "general",
                        "confidence": 0.95,
                        "evidence": "Confirmed via clarify tool: user provided status code meanings.",
                        "load_mode": "always",
                        "data_source_names": ["Production Postgres"]
                    },
                    "description": "Agent-wide rule: applies across the agent but not a single table — attach via data_source_names instead of listing tables."
                },
                {
                    "input": {
                        "text": "When summarizing the Music Store dataset at a high level, note that Chinook is the sample database behind it (artists, albums, tracks, customers, invoices, invoice lines).",
                        "category": "general",
                        "confidence": 0.9,
                        "evidence": "Schema inspection identified the Music Store dataset as the Chinook sample.",
                        "load_mode": "intelligent",
                        "data_source_names": ["Music Store"]
                    },
                    "description": "Agent-scoped semantic note: attach to the specific agent via data_source_names rather than listing every table or going global."
                },
                {
                    "input": {
                        "text": "Always format currency values with a thousands separator and 2 decimals across every report.",
                        "category": "visualization",
                        "confidence": 0.9,
                        "evidence": "User explicitly asked to apply this to all agents.",
                        "load_mode": "always",
                        "global_scope": True
                    },
                    "description": "Global: ONLY because the user explicitly asked to apply it to all agents — set global_scope=True."
                },
                {
                    "input": {
                        "text": "The 'amount' column in transactions table is stored in cents. Always divide by 100 when displaying as currency.",
                        "category": "code_gen",
                        "confidence": 0.85,
                        "evidence": "Observed in inspect_data: amount values are large integers (e.g., 9999 for $99.99).",
                        "load_mode": "intelligent",
                        "table_names": ["transactions"]
                    },
                    "description": "Scoped: column-specific transformation — only relevant when transactions is queried."
                }
            ]
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return CreateInstructionInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return CreateInstructionOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        """Execute create_instruction - adds instruction to training session's draft build."""

        try:
            data = CreateInstructionInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={
                    "error": f"Invalid input: {str(e)}",
                    "code": "INVALID_INPUT"
                }
            )
            return

        yield ToolStartEvent(
            type="tool.start",
            payload={
                "text_preview": data.text[:100] + "..." if len(data.text) > 100 else data.text,
                "category": data.category,
                "confidence": data.confidence,
            }
        )

        # Validate confidence threshold
        if data.confidence < MIN_CONFIDENCE_THRESHOLD:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": CreateInstructionOutput(
                        success=False,
                        message=f"Confidence {data.confidence} is below minimum threshold {MIN_CONFIDENCE_THRESHOLD}. Use clarify tool to gather more evidence first.",
                        rejected_reason="low_confidence"
                    ).model_dump(),
                    "observation": {
                        "summary": f"Instruction rejected: confidence {data.confidence} < {MIN_CONFIDENCE_THRESHOLD}",
                        "artifacts": [],
                    },
                }
            )
            return

        # Validate category
        if data.category not in VALID_CATEGORIES:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": CreateInstructionOutput(
                        success=False,
                        message=f"Invalid category '{data.category}'. Must be one of: {', '.join(VALID_CATEGORIES)}",
                        rejected_reason="invalid_category"
                    ).model_dump(),
                    "observation": {
                        "summary": f"Instruction rejected: invalid category '{data.category}'",
                        "artifacts": [],
                    },
                }
            )
            return


        # Get required context from runtime
        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user")
        training_build_id = runtime_ctx.get("training_build_id")
        agent_execution_id = runtime_ctx.get("agent_execution_id")
        report = runtime_ctx.get("report")

        # In knowledge-harness / post-analysis mode we must only attach the
        # instruction to data sources that are actually part of the current
        # report. Training mode is broader (user is intentionally curating the
        # org) so we keep it org-scoped there.
        mode = runtime_ctx.get("mode")
        allowed_data_source_ids = None
        if mode == "knowledge" and report is not None:
            try:
                allowed_data_source_ids = {
                    str(ds.id) for ds in (report.data_sources or [])
                }
            except Exception:
                allowed_data_source_ids = set()

        if not all([db, organization]):
            yield ToolErrorEvent(
                type="tool.error",
                payload={
                    "error": "Missing required runtime context (db, organization)",
                    "code": "MISSING_CONTEXT"
                }
            )
            return

        try:
            from sqlalchemy import select
            from app.services.instruction_service import InstructionService
            from app.services.build_service import BuildService
            from app.schemas.instruction_schema import InstructionCreate
            from app.schemas.instruction_reference_schema import InstructionReferenceCreate
            from app.models.datasource_table import DataSourceTable

            instruction_service = InstructionService()
            build_service = BuildService()

            # Lazy build creation: the harness no longer pre-seeds a draft;
            # the first create/edit in a session creates it and writes the
            # id back into runtime_ctx so subsequent tool calls share it.
            # agent_v2 captures the id back from runtime_ctx after each tool
            # call so the harness can submit the build at the end.
            build = None
            if training_build_id:
                build = await build_service.get_build(db, training_build_id)

            if not build:
                build = await build_service.get_or_create_draft_build(
                    db=db,
                    org_id=str(organization.id),
                    source='ai',
                    user_id=str(user.id) if user else None,
                    agent_execution_id=agent_execution_id,
                )
                runtime_ctx["training_build_id"] = str(build.id)
                logger.info(f"Lazy-created draft build {build.id} on first create_instruction (mode={mode}, agent_execution_id={agent_execution_id})")

            # Generate title if not provided
            title = data.title
            if not title:
                # Auto-generate from first sentence or truncated text
                title = data.text[:100].split('.')[0] + "." if '.' in data.text[:100] else data.text[:100]

            # Validate load_mode
            valid_load_modes = {"always", "intelligent"}
            load_mode = data.load_mode if data.load_mode in valid_load_modes else "intelligent"

            # Look up tables by name to get data_source_ids and create references
            data_source_ids = set()
            references = []
            matched_table_names = []

            if data.table_names:
                # Build conditions to match table names (case-insensitive, with optional schema prefix)
                from sqlalchemy import or_, func
                from app.models.data_source import DataSource

                conditions = []
                for name in data.table_names:
                    # Match exact name or schema.name pattern (case-insensitive)
                    name_lower = name.lower()
                    if '.' in name:
                        # Full qualified name provided - match exactly
                        conditions.append(func.lower(DataSourceTable.name) == name_lower)
                    else:
                        # Simple name - match name directly or as suffix after schema prefix
                        conditions.append(func.lower(DataSourceTable.name) == name_lower)
                        conditions.append(func.lower(DataSourceTable.name).like(f'%.{name_lower}'))

                if conditions:
                    # Join through DataSource to filter by organization. In
                    # knowledge-harness mode, additionally restrict to data
                    # sources attached to the current report so the instruction
                    # cannot be scoped to an unrelated datasource.
                    where_clauses = [
                        DataSource.organization_id == str(organization.id),
                        or_(*conditions),
                    ]
                    if allowed_data_source_ids is not None:
                        if not allowed_data_source_ids:
                            # Report has no datasources — skip table resolution entirely
                            where_clauses.append(DataSource.id.in_([]))
                        else:
                            where_clauses.append(
                                DataSource.id.in_(list(allowed_data_source_ids))
                            )

                    stmt = (
                        select(DataSourceTable)
                        .join(DataSource, DataSourceTable.datasource_id == DataSource.id)
                        .where(*where_clauses)
                    )
                    result = await db.execute(stmt)
                    tables = result.scalars().all()

                    for table in tables:
                        # Collect data source IDs
                        if table.datasource_id:
                            data_source_ids.add(table.datasource_id)

                        # Create reference for intelligent loading
                        references.append(InstructionReferenceCreate(
                            object_type="datasource_table",
                            object_id=str(table.id),
                            relation_type="scope",
                            display_text=table.name,
                        ))
                        matched_table_names.append(table.name)

            # Resolve explicit data_source_names → data source IDs. Lets the
            # model attach an agent-wide rule to a specific agent without having
            # to enumerate its tables.
            matched_data_source_names = []
            if data.data_source_names:
                from sqlalchemy import or_, func
                from app.models.data_source import DataSource

                ds_conditions = [
                    func.lower(DataSource.name) == n.lower()
                    for n in data.data_source_names
                ]
                ds_where = [
                    DataSource.organization_id == str(organization.id),
                    or_(*ds_conditions),
                ]
                # In knowledge-harness mode, restrict to data sources attached to
                # the current report so a rule can't be scoped to an unrelated agent.
                if allowed_data_source_ids is not None:
                    ds_where.append(
                        DataSource.id.in_(list(allowed_data_source_ids) if allowed_data_source_ids else [])
                    )
                ds_rows = (await db.execute(
                    select(DataSource).where(*ds_where)
                )).scalars().all()
                for ds in ds_rows:
                    data_source_ids.add(ds.id)
                    matched_data_source_names.append(ds.name)

            # Scope decision. Default is SPECIFIC, not global — an instruction is
            # attached to the agent(s) it pertains to. global_scope is the only
            # path to an org-wide rule and requires an explicit user request
            # (the model sets the flag).
            if data.global_scope:
                data_source_ids = set()
                logger.info("create_instruction: global_scope=True — instruction will apply to all agents")
            elif not data_source_ids and mode == "training":
                # Model resolved no explicit scope. Fall back to the session's
                # single attached agent if there's exactly one — do NOT blanket-
                # attach to every attached agent. With zero or multiple attached
                # agents we leave it unscoped and log so it surfaces in review.
                session_data_sources = runtime_ctx.get("data_sources") or []
                if len(session_data_sources) == 1:
                    only_ds = session_data_sources[0]
                    data_source_ids.add(only_ds.id)
                    logger.info(
                        f"create_instruction: no explicit scope given; defaulting to the session's "
                        f"single attached agent '{getattr(only_ds, 'name', only_ds.id)}'"
                    )
                else:
                    logger.warning(
                        f"create_instruction: no explicit scope and {len(session_data_sources)} agents "
                        f"attached — leaving instruction unscoped. Model should pass data_source_names "
                        f"or table_names to scope it."
                    )

            # Create the instruction as a draft (pending admin approval) but
            # stage the version with status="published" so promote_build flips
            # the live row when the training build is approved. Planner loaders
            # (legacy status-based fallback) read inst.status, so it must end
            # up "published" once approved.
            instruction_data = InstructionCreate(
                text=data.text,
                title=title,
                category=data.category,
                load_mode=load_mode,
                data_source_ids=list(data_source_ids) if data_source_ids else [],
                references=references,
                status="draft",
            )

            # Create instruction (without auto-finalizing build - we do that at session end)
            instruction = await instruction_service.create_instruction(
                db=db,
                instruction_data=instruction_data,
                current_user=user,
                organization=organization,
                force_global=True,
                build=build,  # Pass build object
                auto_finalize=False,  # Don't finalize yet - wait for session end
                agent_execution_id=agent_execution_id,  # Link to training session for tracking
                version_status_override="published",
            )

            ref_count = len(references)
            tables_str = ", ".join(matched_table_names) if matched_table_names else "none"
            if data.global_scope:
                scope_str = "global (all agents)"
            elif data_source_ids:
                scope_str = f"{len(data_source_ids)} agent(s)"
            else:
                scope_str = "unscoped"
            logger.info(
                f"Created instruction {instruction.id} in training build {build.id}: "
                f"'{title}' (confidence={data.confidence}, category={data.category}, "
                f"load_mode={load_mode}, scope={scope_str}, tables=[{tables_str}])"
            )

            output_dict = CreateInstructionOutput(
                success=True,
                instruction_id=str(instruction.id),
                title=title,
                build_id=str(build.id) if build else None,
                message=f"Instruction created successfully: {title}",
            ).model_dump()
            output_dict["data_source_ids"] = [str(d) for d in data_source_ids] if data_source_ids else []

            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output_dict,
                    "observation": {
                        "summary": f"Created instruction: {title} (confidence={data.confidence}, load_mode={load_mode}, scope={scope_str}, tables={ref_count})",
                        "artifacts": [
                            {
                                "type": "instruction",
                                "id": str(instruction.id),
                                "title": title,
                                "category": data.category,
                                "load_mode": load_mode,
                                "table_count": ref_count,
                                "tables": matched_table_names,
                            }
                        ],
                    },
                }
            )

        except Exception as e:
            logger.exception(f"Failed to create instruction: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={
                    "error": f"Failed to create instruction: {str(e)}",
                    "code": "CREATE_FAILED"
                }
            )
