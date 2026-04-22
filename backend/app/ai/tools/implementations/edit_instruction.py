"""Edit Instruction Tool - Edits instructions during training mode exploration.

This tool allows the training mode agent to edit instructions that were created
in the current training session. All edits create new versions that are added
to the same draft build.
"""

from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel
import logging

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.edit_instruction import EditInstructionInput, EditInstructionOutput
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)

logger = logging.getLogger(__name__)

# Minimum confidence to maintain for an instruction
MIN_CONFIDENCE_THRESHOLD = 0.7

# Valid categories
VALID_CATEGORIES = {"general", "code_gen", "visualization", "dashboard", "system"}


class EditInstructionTool(Tool):
    """Edit instruction tool - edits existing instructions during training mode.

    This tool is available only in training mode. It edits instructions that
    belong to the organization. Edits create new versions that are tracked
    in the training draft build.
    """

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="edit_instruction",
            description=(
                "ACTION: Edit an existing instruction. "
                "Use when you need to correct mistakes, improve clarity, update confidence after "
                "user confirmation, or refine table associations."
            ),
            category="action",
            version="1.0.0",
            input_schema=EditInstructionInput.model_json_schema(),
            output_schema=EditInstructionOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=30,
            idempotent=False,
            required_permissions=["manage_instructions"],
            tags=["training", "instruction", "semantic-learning"],
            allowed_modes=["training", "knowledge"],
            examples=[
                {
                    "input": {
                        "instruction_id": "inst_abc123",
                        "confidence": 0.95,
                        "evidence": "User confirmed via clarify: status 1=active, 2=inactive, 3=banned"
                    },
                    "description": "Update confidence after user confirmation"
                },
                {
                    "input": {
                        "instruction_id": "inst_abc123",
                        "text": "When calculating revenue, always exclude orders with status='cancelled', status='refunded', or status='pending' to avoid double-counting.",
                        "table_names": ["orders", "order_items"]
                    },
                    "description": "Correct instruction text and add table association"
                },
                {
                    "input": {
                        "instruction_id": "inst_abc123",
                        "category": "code_gen",
                        "load_mode": "always"
                    },
                    "description": "Change category and load mode for a critical rule"
                }
            ]
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return EditInstructionInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return EditInstructionOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        """Execute edit_instruction - updates instruction in training session's draft build."""

        try:
            data = EditInstructionInput(**tool_input)
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
                "instruction_id": data.instruction_id,
                "updating_fields": [k for k, v in tool_input.items() if v is not None and k != "instruction_id"],
            }
        )


        # Validate confidence threshold if provided
        if data.confidence is not None and data.confidence < MIN_CONFIDENCE_THRESHOLD:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": EditInstructionOutput(
                        success=False,
                        instruction_id=data.instruction_id,
                        message=f"Confidence {data.confidence} is below minimum threshold {MIN_CONFIDENCE_THRESHOLD}.",
                        rejected_reason="low_confidence"
                    ).model_dump(),
                    "observation": {
                        "summary": f"Edit rejected: confidence {data.confidence} < {MIN_CONFIDENCE_THRESHOLD}",
                        "artifacts": [],
                    },
                }
            )
            return

        # Validate category if provided
        if data.category is not None and data.category not in VALID_CATEGORIES:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": EditInstructionOutput(
                        success=False,
                        instruction_id=data.instruction_id,
                        message=f"Invalid category '{data.category}'. Must be one of: {', '.join(VALID_CATEGORIES)}",
                        rejected_reason="invalid_category"
                    ).model_dump(),
                    "observation": {
                        "summary": f"Edit rejected: invalid category '{data.category}'",
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
        mode = runtime_ctx.get("mode")

        # Harness contract: in knowledge mode, agent_v2 seeds
        # runtime_ctx["training_build_id"] before the harness sub-loop runs.
        # Fail loudly rather than silently skipping add_to_build and leaving
        # the edit unattached to any build. In training mode, the first
        # create seeds the build, so edit-before-create is not expected.
        if mode == "knowledge" and not training_build_id:
            yield ToolErrorEvent(
                type="tool.error",
                payload={
                    "error": (
                        "Missing training_build_id in runtime context. "
                        "Knowledge/training mode requires the harness to seed "
                        "a draft build before edit_instruction runs."
                    ),
                    "code": "MISSING_TRAINING_BUILD",
                }
            )
            return

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
            from sqlalchemy import select, or_, func
            from sqlalchemy.orm import selectinload
            from app.models.instruction import Instruction
            from app.models.datasource_table import DataSourceTable
            from app.models.data_source import DataSource
            from app.services.build_service import BuildService
            from app.services.instruction_version_service import InstructionVersionService
            from app.schemas.instruction_reference_schema import InstructionReferenceCreate

            build_service = BuildService()
            version_service = InstructionVersionService()

            # Fetch the instruction (read-only — we do NOT mutate the live row).
            # Edits are staged as a new InstructionVersion attached to the draft
            # build. Live row is updated only when the build is promoted.
            stmt = (
                select(Instruction)
                .options(
                    selectinload(Instruction.data_sources),
                    selectinload(Instruction.labels),
                    selectinload(Instruction.references),
                )
                .where(
                    Instruction.id == data.instruction_id,
                    Instruction.organization_id == str(organization.id)
                )
            )
            result = await db.execute(stmt)
            instruction = result.scalar_one_or_none()

            if not instruction:
                yield ToolEndEvent(
                    type="tool.end",
                    payload={
                        "output": EditInstructionOutput(
                            success=False,
                            instruction_id=data.instruction_id,
                            message=f"Instruction '{data.instruction_id}' not found",
                            rejected_reason="not_found"
                        ).model_dump(),
                        "observation": {
                            "summary": f"Edit failed: instruction '{data.instruction_id}' not found",
                            "artifacts": [],
                        },
                    }
                )
                return

            previous_text = instruction.text if data.text is not None else None

            # Start from the current live row state, overlay only the fields
            # the caller wants to change. These values become the new version.
            new_text = data.text if data.text is not None else instruction.text
            new_title = data.title if data.title is not None else instruction.title
            new_category = data.category if data.category is not None else instruction.category
            if data.load_mode is not None:
                valid_load_modes = {"always", "intelligent"}
                new_load_mode = data.load_mode if data.load_mode in valid_load_modes else "intelligent"
            else:
                new_load_mode = instruction.load_mode or "always"

            matched_table_names = []
            if data.table_names is not None:
                # Resolve the requested table list to ds_ids + references.
                resolved_ds_ids = set()
                resolved_refs = []
                if data.table_names:
                    conditions = []
                    for name in data.table_names:
                        name_lower = name.lower()
                        if '.' in name:
                            conditions.append(func.lower(DataSourceTable.name) == name_lower)
                        else:
                            conditions.append(func.lower(DataSourceTable.name) == name_lower)
                            conditions.append(func.lower(DataSourceTable.name).like(f'%.{name_lower}'))
                    if conditions:
                        table_stmt = (
                            select(DataSourceTable)
                            .join(DataSource, DataSourceTable.datasource_id == DataSource.id)
                            .where(
                                DataSource.organization_id == str(organization.id),
                                or_(*conditions)
                            )
                        )
                        table_result = await db.execute(table_stmt)
                        tables = table_result.scalars().all()
                        for table in tables:
                            if table.datasource_id:
                                resolved_ds_ids.add(table.datasource_id)
                            resolved_refs.append({
                                "object_type": "datasource_table",
                                "object_id": str(table.id),
                                "column_name": None,
                                "display_text": table.name,
                            })
                            matched_table_names.append(table.name)
                new_data_source_ids = list(resolved_ds_ids)
                new_references_json = resolved_refs
            else:
                new_data_source_ids = [ds.id for ds in (instruction.data_sources or [])] or None
                new_references_json = [
                    {
                        "object_type": ref.object_type,
                        "object_id": ref.object_id,
                        "column_name": ref.column_name,
                        "display_text": ref.display_text,
                    }
                    for ref in (instruction.references or [])
                ] or None

            label_ids = [label.id for label in (instruction.labels or [])] or None
            category_ids = None
            if new_category:
                category_ids = new_category if isinstance(new_category, list) else [new_category]

            version_number = None
            try:
                version = await version_service.create_version_from_data(
                    db=db,
                    instruction_id=str(instruction.id),
                    text=new_text,
                    title=new_title,
                    structured_data=instruction.structured_data,
                    formatted_content=instruction.formatted_content,
                    status=instruction.status or "published",
                    load_mode=new_load_mode,
                    references_json=new_references_json,
                    data_source_ids=new_data_source_ids,
                    label_ids=label_ids,
                    category_ids=category_ids,
                    user_id=user.id if user else None,
                )
                version_number = version.version_number

                if training_build_id:
                    build = await build_service.get_build(db, training_build_id)
                    if build and build.can_be_edited:
                        await build_service.add_to_build(
                            db, build.id, str(instruction.id), version.id
                        )

                await db.commit()
                logger.info(
                    f"Staged edit as version {version.id} (v{version_number}) for instruction {instruction.id} in build {training_build_id}"
                )
            except Exception as e:
                logger.warning(f"Failed to stage edit for instruction {instruction.id}: {e}")

            # Build summary of changes
            changes = []
            if data.text is not None:
                changes.append("text")
            if data.title is not None:
                changes.append("title")
            if data.category is not None:
                changes.append(f"category={data.category}")
            if data.confidence is not None:
                changes.append(f"confidence={data.confidence}")
            if data.load_mode is not None:
                changes.append(f"load_mode={data.load_mode}")
            if data.table_names is not None:
                changes.append(f"tables={matched_table_names}")

            changes_str = ", ".join(changes) if changes else "no changes"
            version_str = f" (v{version_number})" if version_number else ""

            logger.info(f"Edited instruction {instruction.id}{version_str}: {changes_str}")

            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": EditInstructionOutput(
                        success=True,
                        instruction_id=str(instruction.id),
                        title=getattr(instruction, "title", None),
                        version_number=version_number,
                        build_id=str(training_build_id) if training_build_id else None,
                        message=f"Instruction updated successfully{version_str}",
                        previous_text=previous_text,
                        new_text=(data.text if data.text is not None else None),
                    ).model_dump(),
                    "observation": {
                        "summary": f"Edited instruction{version_str}: {changes_str}",
                        "artifacts": [
                            {
                                "type": "instruction_edit",
                                "id": str(instruction.id),
                                "version_number": version_number,
                                "changes": changes,
                                "tables": matched_table_names,
                            }
                        ],
                    },
                }
            )

        except Exception as e:
            logger.exception(f"Failed to edit instruction: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={
                    "error": f"Failed to edit instruction: {str(e)}",
                    "code": "EDIT_FAILED"
                }
            )
