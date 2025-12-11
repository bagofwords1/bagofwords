"""
describe_entity tool - Describe and optionally materialize a catalog entity.

Two orthogonal flags:
- should_create: Create a tracked step/visualization from the entity
- should_rerun: Re-execute the entity's code instead of using cached data
"""

import json
from typing import AsyncIterator, Dict, Any, Type, Optional, List

from pydantic import BaseModel
from sqlalchemy import select, or_

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import (
    ToolEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolEndEvent,
)
from app.ai.tools.schemas.describe_entity import DescribeEntityInput, DescribeEntityOutput
from app.models.entity import Entity


class DescribeEntityTool(Tool):
    """Tool to describe a catalog entity and optionally create a visualization from it."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="describe_entity",
            description=(
                "Look up a pre-built entity from the catalog by name or ID.\n\n"
                "**Research mode** (default): Returns SQL code, columns, and sample data. "
                "Use for research, investigation, understanding how something was built, "
                "or as a reference before writing similar code.\n\n"
                "**Create mode** (should_create=True): Creates a visualization from the "
                "entity's stored data. Use when the user wants to display an existing entity "
                "rather than build something new.\n\n"
                "Prefer this over create_data when a relevant entity already exists in the catalog."
            ),
            category="research",
            version="1.0.0",
            input_schema=DescribeEntityInput.model_json_schema(),
            output_schema=DescribeEntityOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=60,
            idempotent=True,
            is_active=True,
            required_permissions=[],
            tags=["entity", "catalog", "research", "reuse", "template"],
            observation_policy="on_trigger",
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return DescribeEntityInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return DescribeEntityOutput

    async def _find_entity(self, db, organization_id: str, name_or_id: str) -> Optional[Entity]:
        """Find entity by ID, title, or slug."""
        from sqlalchemy.orm import selectinload

        # Try exact ID match first
        result = await db.execute(
            select(Entity)
            .options(selectinload(Entity.data_sources))
            .where(
                Entity.id == name_or_id,
                Entity.organization_id == organization_id,
                Entity.status == "published",
                Entity.deleted_at == None,
            )
        )
        entity = result.scalar_one_or_none()
        if entity:
            return entity

        # Try slug exact match (case insensitive)
        result = await db.execute(
            select(Entity)
            .options(selectinload(Entity.data_sources))
            .where(
                Entity.slug.ilike(name_or_id),
                Entity.organization_id == organization_id,
                Entity.status == "published",
                Entity.deleted_at == None,
            )
        )
        entity = result.scalar_one_or_none()
        if entity:
            return entity

        # Try case-insensitive title match
        result = await db.execute(
            select(Entity)
            .options(selectinload(Entity.data_sources))
            .where(
                Entity.title.ilike(name_or_id),
                Entity.organization_id == organization_id,
                Entity.status == "published",
                Entity.deleted_at == None,
            )
        )
        entity = result.scalar_one_or_none()
        if entity:
            return entity

        # Try fuzzy match on title/slug/description
        like_pattern = f"%{name_or_id}%"
        result = await db.execute(
            select(Entity)
            .options(selectinload(Entity.data_sources))
            .where(
                or_(
                    Entity.title.ilike(like_pattern),
                    Entity.slug.ilike(like_pattern),
                    Entity.description.ilike(like_pattern),
                ),
                Entity.organization_id == organization_id,
                Entity.status == "published",
                Entity.deleted_at == None,
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _check_user_access(self, db, entity: Entity, user) -> tuple[bool, str]:
        """Check if user has access to all data sources attached to the entity.
        
        Returns:
            (has_access: bool, error_message: str)
        """
        if not user:
            return False, "No user context available"

        if not entity.data_sources:
            # No data sources attached - access granted
            return True, ""

        from app.models.data_source_membership import DataSourceMembership, PRINCIPAL_TYPE_USER

        inaccessible_ds: List[str] = []

        for ds in entity.data_sources:
            # Check if data source is public
            if ds.is_public:
                continue

            # Check if user has explicit membership
            membership_result = await db.execute(
                select(DataSourceMembership).where(
                    DataSourceMembership.data_source_id == ds.id,
                    DataSourceMembership.principal_type == PRINCIPAL_TYPE_USER,
                    DataSourceMembership.principal_id == user.id,
                )
            )
            if not membership_result.scalar_one_or_none():
                inaccessible_ds.append(ds.name or str(ds.id))

        if inaccessible_ds:
            return False, f"Access denied: no permission for data source(s): {', '.join(inaccessible_ds)}"

        return True, ""

    def _build_data_profile(
        self,
        data: Dict[str, Any],
        allow_llm_see_data: bool,
    ) -> Dict[str, Any]:
        """Build a data profile from entity data (similar to create_data's _build_viz_profile)."""
        if not data or not isinstance(data, dict):
            return {"row_count": 0, "column_count": 0, "columns": []}

        info = data.get("info", {}) if isinstance(data, dict) else {}
        column_info = info.get("column_info") or {}
        rows = data.get("rows", [])
        columns = data.get("columns", [])

        cols: List[Dict[str, Any]] = []
        for name, meta in (column_info.items() if isinstance(column_info, dict) else []):
            cols.append({
                "name": name,
                "dtype": meta.get("dtype"),
                "non_null_count": meta.get("non_null_count"),
                "unique_count": meta.get("unique_count"),
                "null_count": meta.get("null_count"),
                "min": meta.get("min"),
                "max": meta.get("max"),
            })

        # If column_info is empty but we have columns list, build from that
        if not cols and columns:
            for col in columns:
                col_name = col.get("field") if isinstance(col, dict) else str(col)
                cols.append({"name": col_name, "dtype": None})

        profile: Dict[str, Any] = {
            "row_count": info.get("total_rows") or len(rows),
            "column_count": info.get("total_columns") or len(cols),
            "columns": cols,
        }

        if allow_llm_see_data:
            # Add sample rows for better context
            profile["head_rows"] = rows[:5] if rows else []

        return profile

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        data = DescribeEntityInput(**tool_input)

        yield ToolStartEvent(type="tool.start", payload={"name_or_id": data.name_or_id})
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "finding_entity"})

        # Get context
        context_hub = runtime_ctx.get("context_hub")
        db = context_hub.db if context_hub else runtime_ctx.get("db")
        organization = context_hub.organization if context_hub else runtime_ctx.get("organization")
        organization_settings = runtime_ctx.get("settings")

        if not db or not organization:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": DescribeEntityOutput(
                        success=False,
                        errors=["Missing database or organization context"],
                    ).model_dump(),
                    "observation": {
                        "summary": "Failed to describe entity: missing context",
                        "error": {"type": "context_error", "message": "Missing db or organization"},
                    },
                },
            )
            return

        # Find the entity
        entity = await self._find_entity(db, str(organization.id), data.name_or_id)

        if not entity:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": DescribeEntityOutput(
                        success=False,
                        errors=[f"Entity not found: {data.name_or_id}"],
                    ).model_dump(),
                    "observation": {
                        "summary": f"Entity not found: {data.name_or_id}",
                        "error": {"type": "not_found", "message": f"No entity matching '{data.name_or_id}'"},
                    },
                },
            )
            return

        yield ToolProgressEvent(
            type="tool.progress",
            payload={"stage": "entity_found", "entity_id": str(entity.id), "title": entity.title},
        )

        # Check user access to entity's data sources
        user = context_hub.user if context_hub else runtime_ctx.get("user")
        has_access, access_error = await self._check_user_access(db, entity, user)

        if not has_access:
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": DescribeEntityOutput(
                        success=False,
                        entity_id=str(entity.id),
                        title=entity.title,
                        errors=[access_error],
                    ).model_dump(),
                    "observation": {
                        "summary": f"Access denied to entity '{entity.title}'",
                        "error": {"type": "access_denied", "message": access_error},
                    },
                },
            )
            return

        # Determine data source: cached or re-executed
        entity_data = entity.data or {}
        execution_log = None
        errors: List[str] = []

        if data.should_rerun:
            yield ToolProgressEvent(type="tool.progress", payload={"stage": "executing_code"})
            try:
                from app.ai.code_execution.code_execution import StreamingCodeExecutor

                ds_clients = {}
                for ds in (entity.data_sources or []):
                    try:
                        ds_clients[ds.name] = ds.get_client()
                    except Exception as e:
                        errors.append(f"Failed to connect to {ds.name}: {str(e)}")

                executor = StreamingCodeExecutor(organization_settings=organization_settings)
                code_to_run = entity.code or ""

                if not code_to_run.strip():
                    errors.append("Entity has no code to execute")
                else:
                    exec_df, execution_log = executor.execute_code(
                        code=code_to_run,
                        ds_clients=ds_clients,
                        excel_files=[],
                    )
                    entity_data = executor.format_df_for_widget(exec_df)

            except Exception as e:
                errors.append(f"Code execution failed: {str(e)}")
                # Fall back to cached data
                entity_data = entity.data or {}

        # Build data profile
        allow_llm_see_data = True
        if organization_settings:
            try:
                cfg = organization_settings.get_config("allow_llm_see_data")
                allow_llm_see_data = bool(cfg.value) if cfg is not None else True
            except Exception:
                pass

        data_profile = self._build_data_profile(entity_data, allow_llm_see_data)

        # If should_create, create step and visualization
        step_id = None
        data_model = None
        view = None

        if data.should_create:
            yield ToolProgressEvent(type="tool.progress", payload={"stage": "creating_visualization"})

            try:
                # Import here to avoid circular imports
                from app.ai.tools.implementations.create_data import (
                    build_view_from_data_model,
                    _infer_palette_theme,
                )

                # Use entity's stored data_model and view if available
                stored_data_model = entity.original_data_model or {}
                stored_view = entity.view or {}

                # Determine chart type from stored data_model or default to table
                chart_type = stored_data_model.get("type") or stored_view.get("type") or "table"
                data_model = {
                    "type": chart_type,
                    "series": stored_data_model.get("series", []),
                }
                if stored_data_model.get("group_by"):
                    data_model["group_by"] = stored_data_model["group_by"]

                # Build view schema
                palette_theme = _infer_palette_theme(runtime_ctx) or "default"
                available_columns = [c.get("name") for c in data_profile.get("columns", []) if c.get("name")]
                view_schema = build_view_from_data_model(
                    data_model, title=entity.title, palette_theme=palette_theme, available_columns=available_columns
                )
                view = view_schema.model_dump(exclude_none=True) if view_schema else stored_view

                # Emit progress for step creation
                yield ToolProgressEvent(
                    type="tool.progress",
                    payload={
                        "stage": "data_model_type_determined",
                        "data_model_type": chart_type,
                        "query_title": entity.title,
                        "entity_id": str(entity.id),
                    },
                )

                # Get current step ID if available (step creation handled by orchestrator)
                step_id = runtime_ctx.get("current_step_id")

            except Exception as e:
                errors.append(f"Failed to create visualization: {str(e)}")

        # Build output
        # Success only if no errors occurred - having cached data doesn't mean the operation succeeded
        output = DescribeEntityOutput(
            success=len(errors) == 0,
            entity_id=str(entity.id),
            entity_type=entity.type,
            title=entity.title,
            description=entity.description,
            code=entity.code if allow_llm_see_data else "[code hidden]",
            data_profile=data_profile,
            step_id=step_id,
            data_model=data_model,
            view=view,
            execution_log=execution_log,
            errors=errors,
        ).model_dump()

        # Add full data for step creation if should_create
        if data.should_create:
            output["data"] = entity_data

        # Build observation
        summary_parts = [f"Described entity '{entity.title}' (type={entity.type})"]
        if data.should_rerun:
            summary_parts.append("re-executed code")
        if data.should_create:
            summary_parts.append("created visualization")

        observation: Dict[str, Any] = {
            "summary": ", ".join(summary_parts) + ".",
            "entity_id": str(entity.id),
            "entity_type": entity.type,
            "title": entity.title,
            "description": entity.description[:200] if entity.description else None,
            "data_profile": data_profile,
            "analysis_complete": False,
            "final_answer": None,
        }

        if data.should_create:
            observation["data_model"] = data_model
            observation["view"] = view
            if step_id:
                observation["step_id"] = step_id

        if errors:
            observation["errors"] = errors

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": output,
                "observation": observation,
            },
        )

