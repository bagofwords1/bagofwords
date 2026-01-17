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
            required_permissions=["create_instructions"],
            tags=["training", "instruction", "semantic-learning"],
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

        # Validate text ends with period if provided
        if data.text is not None and not data.text.strip().endswith("."):
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": EditInstructionOutput(
                        success=False,
                        instruction_id=data.instruction_id,
                        message="Instruction text must end with a period.",
                        rejected_reason="invalid_format"
                    ).model_dump(),
                    "observation": {
                        "summary": "Edit rejected: text must end with period",
                        "artifacts": [],
                    },
                }
            )
            return

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
            from app.services.instruction_service import InstructionService
            from app.services.build_service import BuildService
            from app.services.instruction_version_service import InstructionVersionService
            from app.schemas.instruction_schema import InstructionUpdate
            from app.schemas.instruction_reference_schema import InstructionReferenceCreate

            instruction_service = InstructionService()
            build_service = BuildService()
            version_service = InstructionVersionService()

            # Fetch the instruction
            stmt = (
                select(Instruction)
                .options(
                    selectinload(Instruction.data_sources),
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

            # Build update data
            update_fields = {}

            if data.text is not None:
                update_fields["text"] = data.text

            if data.title is not None:
                update_fields["title"] = data.title

            if data.category is not None:
                update_fields["category"] = data.category

            if data.load_mode is not None:
                valid_load_modes = {"always", "intelligent"}
                update_fields["load_mode"] = data.load_mode if data.load_mode in valid_load_modes else "intelligent"

            # Handle table_names -> references and data_source_ids
            data_source_ids = None
            references = None
            matched_table_names = []

            if data.table_names is not None:
                data_source_ids = set()
                references = []

                if data.table_names:  # Non-empty list
                    # Build conditions to match table names
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
                                data_source_ids.add(table.datasource_id)
                            references.append(InstructionReferenceCreate(
                                object_type="datasource_table",
                                object_id=str(table.id),
                                relation_type="scope",
                                display_text=table.name,
                            ))
                            matched_table_names.append(table.name)

                update_fields["data_source_ids"] = list(data_source_ids) if data_source_ids else []
                update_fields["references"] = references if references else []

            # Apply updates directly to the instruction model
            for field, value in update_fields.items():
                if field not in ("data_source_ids", "references"):
                    setattr(instruction, field, value)

            # Handle data source associations if provided
            if data_source_ids is not None:
                await instruction_service._update_data_source_associations(db, instruction, list(data_source_ids) if data_source_ids else [])

            # Handle references if provided
            if references is not None:
                ds_ids = list(data_source_ids) if data_source_ids else None
                await instruction_service.reference_service.replace_for_instruction(
                    db, instruction.id, references, organization, ds_ids
                )

            await db.commit()

            # Create new version if content changed
            version_number = None
            try:
                # Re-fetch instruction with relationships for version creation
                fresh_stmt = (
                    select(Instruction)
                    .options(
                        selectinload(Instruction.data_sources),
                        selectinload(Instruction.labels),
                        selectinload(Instruction.references),
                    )
                    .where(Instruction.id == instruction.id)
                )
                fresh_result = await db.execute(fresh_stmt)
                instruction_with_rels = fresh_result.scalar_one()

                # Check if content has changed
                if await version_service.has_content_changed(db, instruction_with_rels):
                    # Create new version
                    version = await version_service.create_version(
                        db, instruction_with_rels, user_id=user.id if user else None
                    )
                    version_number = version.version_number

                    # Update instruction's current version
                    instruction_with_rels.current_version_id = version.id

                    # Add to training build
                    if training_build_id:
                        build = await build_service.get_build(db, training_build_id)
                        if build and build.can_be_edited:
                            await build_service.add_to_build(
                                db, build.id, instruction_with_rels.id, version.id
                            )

                    await db.commit()
                    logger.info(
                        f"Created version {version.id} (v{version_number}) for edited instruction {instruction.id}"
                    )

            except Exception as e:
                logger.warning(f"Failed to create version for edited instruction {instruction.id}: {e}")
                # Don't fail the edit if versioning fails

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
                        version_number=version_number,
                        message=f"Instruction updated successfully{version_str}",
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
