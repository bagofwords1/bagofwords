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
                "Instructions should capture non-obvious semantic rules that prevent mistakes."
            ),
            category="action",
            version="1.0.0",
            input_schema=CreateInstructionInput.model_json_schema(),
            output_schema=CreateInstructionOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=30,
            idempotent=False,
            required_permissions=["create_instructions"],
            tags=["training", "instruction", "semantic-learning"],
            allowed_modes=["training"],
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
                    "description": "Create instruction for revenue calculation rule scoped to orders table"
                },
                {
                    "input": {
                        "text": "User status values: 1=active, 2=inactive, 3=banned. Always filter status=1 for active user counts.",
                        "category": "general",
                        "confidence": 0.95,
                        "evidence": "Confirmed via clarify tool: user provided status code meanings.",
                        "load_mode": "always",
                    },
                    "description": "Create critical business rule instruction (always loaded)"
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
                    "description": "Create instruction for column-specific data transformation"
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

        # Validate text ends with period
        if not data.text.strip().endswith("."):
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": CreateInstructionOutput(
                        success=False,
                        message="Instruction text must end with a period.",
                        rejected_reason="invalid_format"
                    ).model_dump(),
                    "observation": {
                        "summary": "Instruction rejected: text must end with period",
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

            # Get or create the training session's draft build
            build = None
            if training_build_id:
                # Fetch existing build
                build = await build_service.get_build(db, training_build_id)

            if not build:
                # Create a new draft build for this training session
                # Pass agent_execution_id to scope build to this specific session
                build = await build_service.get_or_create_draft_build(
                    db=db,
                    org_id=str(organization.id),
                    source='ai',
                    user_id=str(user.id) if user else None,
                    agent_execution_id=agent_execution_id,
                )
                # Store in runtime context for subsequent calls
                runtime_ctx["training_build_id"] = str(build.id)
                logger.info(f"Created training build {build.id} for training session (agent_execution_id={agent_execution_id})")

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
                    # Join through DataSource to filter by organization
                    stmt = (
                        select(DataSourceTable)
                        .join(DataSource, DataSourceTable.datasource_id == DataSource.id)
                        .where(
                            DataSource.organization_id == str(organization.id),
                            or_(*conditions)
                        )
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

            # Create the instruction
            instruction_data = InstructionCreate(
                text=data.text,
                title=title,
                category=data.category,
                load_mode=load_mode,
                data_source_ids=list(data_source_ids) if data_source_ids else [],
                references=references,
                status="published",
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
            )

            ref_count = len(references)
            tables_str = ", ".join(matched_table_names) if matched_table_names else "none"
            logger.info(
                f"Created instruction {instruction.id} in training build {build.id}: "
                f"'{title}' (confidence={data.confidence}, category={data.category}, "
                f"load_mode={load_mode}, tables=[{tables_str}])"
            )

            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": CreateInstructionOutput(
                        success=True,
                        instruction_id=str(instruction.id),
                        message=f"Instruction created successfully: {title}",
                    ).model_dump(),
                    "observation": {
                        "summary": f"Created instruction: {title} (confidence={data.confidence}, load_mode={load_mode}, tables={ref_count})",
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
