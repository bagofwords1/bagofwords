import re
from typing import AsyncIterator, Dict, Any, Type, List, Optional

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import (
    ReadResourcesInput,
    ReadResourcesOutput,
    ToolEvent,
    ToolStartEvent,
    ToolProgressEvent,
    ToolEndEvent,
)
from app.ai.context.sections.resources_section import ResourcesSection

# ORM models
# NOTE: Import SQLAlchemy and ORM models lazily inside run_stream to avoid
# import-time failures during registry auto-discovery.


TOP_K_PER_REPO_DEFAULT = 20
INDEX_LIMIT_DEFAULT = 200


class ReadResourcesTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="read_resources",
            description=(
                "Describe metadata resources (dbt/LookML/markdown/etc.) by name or regex. "
                "Returns information about the resources and their metadata. Use this when to research or get information about the resources."
            ),
            category="action",
            version="1.0.0",
            input_schema=ReadResourcesInput.model_json_schema(),
            output_schema=ReadResourcesOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=30,
            idempotent=True,
            is_active=True,
            required_permissions=[],
            tags=["resources", "dbt", "lookml", "index", "sample"],
            observation_policy="on_trigger",
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ReadResourcesInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ReadResourcesOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = ReadResourcesInput(**tool_input)

        # Emit start
        yield ToolStartEvent(
            type="tool.start",
            payload={
                "query": data.query,
                "data_source_id": data.data_source_id,
                "git_repository_id": data.git_repository_id,
            },
        )

        # Submit event(s) for UI: "[icon] searching <query>"
        queries = data.query if isinstance(data.query, list) else [data.query]
        for q in queries:
            if isinstance(q, str) and q.strip():
                yield ToolProgressEvent(
                    type="tool.progress",
                    payload={
                        "stage": "submit_search",
                        "query_display": q,
                        "icon": "resource",  # UI can map this to an icon; may switch to dbt/lookml later
                    },
                )

        # Precompute regex patterns (exact vs pattern)
        yield ToolProgressEvent(type="tool.progress", payload={"stage": "resolving_patterns"})
        name_patterns: List[str] = []
        try:
            special = re.compile(r"[\^\$\.\*\+\?\[\]\(\)\{\}\|]")
            for q in queries:
                if not isinstance(q, str):
                    continue
                if special.search(q or ""):
                    name_patterns.append(q)
                else:
                    esc = re.escape(q)
                    name_patterns.append(f"(?i)(?:^|\\.){esc}(?:$|\\b|_)")
        except Exception:
            # Best effort, pass raw tokens
            name_patterns = [q for q in queries if isinstance(q, str)]

        yield ToolProgressEvent(
            type="tool.progress",
            payload={"stage": "collecting_index"},
        )

        db = runtime_ctx.get("db")
        errors: List[str] = []
        repositories: List[ResourcesSection.Repository] = []
        searched_repos = 0
        matched_resources_total = 0

        try:
            from sqlalchemy import select  # lazy import
            from app.models.metadata_resource import MetadataResource
            from app.models.metadata_indexing_job import MetadataIndexingJob
            from app.models.data_source import DataSource
            # Build base query with optional filters
            stmt = select(MetadataResource).where(MetadataResource.is_active == True)
            if data.data_source_id:
                stmt = stmt.where(MetadataResource.data_source_id == data.data_source_id)
            if data.git_repository_id:
                # Join via indexing job to filter by repository
                stmt = stmt.join(MetadataIndexingJob, MetadataIndexingJob.id == MetadataResource.metadata_indexing_job_id)
                stmt = stmt.where(MetadataIndexingJob.git_repository_id == data.git_repository_id)

            rows = (await db.execute(stmt)).scalars().all()

            # Index by data_source for grouping and to compute names
            ds_id_to_resources: Dict[str, List[MetadataResource]] = {}
            for r in rows:
                # Apply name regex patterns in Python
                if name_patterns:
                    name_val = (getattr(r, "name", "") or "")
                    try:
                        if not any(re.search(p, name_val) for p in name_patterns):
                            continue
                    except Exception:
                        # On invalid pattern, skip filtering for that token
                        pass
                ds_id = str(getattr(r, "data_source_id", ""))
                ds_id_to_resources.setdefault(ds_id, []).append(r)

            # Fetch data source names
            ds_names: Dict[str, str] = {}
            if ds_id_to_resources:
                ds_stmt = select(DataSource.id, DataSource.name).where(DataSource.id.in_(list(ds_id_to_resources.keys())))
                for (ds_id, ds_name) in (await db.execute(ds_stmt)).all():
                    ds_names[str(ds_id)] = ds_name

            # Build repositories
            for ds_id, res_list in ds_id_to_resources.items():
                searched_repos += 1
                repo_name = f"{ds_names.get(ds_id, 'Data Source')} Metadata Resources"
                payload: List[dict] = []
                for r in res_list:
                    payload.append({
                        "name": getattr(r, "name", None),
                        "resource_type": getattr(r, "resource_type", None),
                        "path": getattr(r, "path", None),
                        "description": getattr(r, "description", None),
                        "sql_content": getattr(r, "sql_content", None),
                        "source_name": getattr(r, "source_name", None),
                        "database": getattr(r, "database", None),
                        "schema": getattr(r, "schema", None),
                        "columns": getattr(r, "columns", None),
                        "depends_on": getattr(r, "depends_on", None),
                        "raw_data": getattr(r, "raw_data", None),
                    })
                matched_resources_total += len(payload)
                repositories.append(
                    ResourcesSection.Repository(
                        name=repo_name,
                        id=None,
                        data_source_id=ds_id,
                        resources=payload,
                    )
                )
        except Exception as e:
            errors.append(str(e))

        yield ToolProgressEvent(
            type="tool.progress",
            payload={
                "stage": "generating_excerpt",
                "top_k": TOP_K_PER_REPO_DEFAULT,
                "index_limit": INDEX_LIMIT_DEFAULT,
            },
        )

        # Render combined excerpt
        try:
            section = ResourcesSection(repositories=repositories)
            resources_excerpt = section.render_combined(
                top_k_per_repo=TOP_K_PER_REPO_DEFAULT,
                index_limit=INDEX_LIMIT_DEFAULT,
                include_index=True,
            )
            # Determine truncation flag
            truncated = False
            for repo in repositories:
                try:
                    if len(repo.resources or []) > TOP_K_PER_REPO_DEFAULT:
                        truncated = True
                        break
                except Exception:
                    continue
        except Exception as e:
            errors.append(str(e))
            resources_excerpt = ""
            truncated = False

        # Optional found_matches
        try:
            examples = []
            for repo in repositories:
                for r in (repo.resources or [])[:3]:
                    nm = r.get("name") if isinstance(r, dict) else None
                    if nm:
                        examples.append(nm)
                if len(examples) >= 3:
                    break
            yield ToolProgressEvent(
                type="tool.progress",
                payload={"stage": "found_matches", "count": matched_resources_total, "examples": examples},
            )
        except Exception:
            pass

        yield ToolProgressEvent(type="tool.progress", payload={"stage": "finalizing"})

        output = ReadResourcesOutput(
            resources_excerpt=resources_excerpt,
            truncated=truncated,
            searched_repos=searched_repos,
            searched_resources_est=matched_resources_total,
            errors=errors,
            search_query=data.query,
        ).model_dump()

        observation = {
            "summary": f"Described {matched_resources_total} resources across {searched_repos} data sources.",
            "analysis_complete": False,
            "final_answer": None,
            "resources_excerpt": resources_excerpt,
        }

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": output,
                "observation": observation,
            },
        )


