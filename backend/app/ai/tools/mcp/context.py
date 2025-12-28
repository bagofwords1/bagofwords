"""MCP context preparation - shared rich context building for MCP tools.

Provides a single-pass context preparation layer similar to agent_v2.py
but without the feedback/observation loop. This enables MCP tools to have
access to the same rich context (instructions, resources, schemas) that
the main agent uses.
"""

import re
import logging
from typing import Dict, Any, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.context import ContextHub
from app.models.user import User
from app.models.organization import Organization
from app.services.data_source_service import DataSourceService
from app.schemas.mcp import MCPRichContext
from app.ai.tools.schemas.create_widget import TablesBySource

logger = logging.getLogger(__name__)

# Limits for context rendering
DEFAULT_TOP_K_SCHEMA = 10
DEFAULT_TOP_K_RESOURCES = 10
DEFAULT_INDEX_LIMIT = 200


async def build_rich_context(
    *,
    db: AsyncSession,
    user: User,
    organization: Organization,
    report,  # ReportSchema or Report model
    prompt: str,
    explicit_tables: Optional[List[TablesBySource]] = None,
    top_k_schema: int = DEFAULT_TOP_K_SCHEMA,
    top_k_resources: int = DEFAULT_TOP_K_RESOURCES,
) -> MCPRichContext:
    """Build rich context for MCP tool execution.
    
    This is the shared context preparation layer for MCP tools. It:
    1. Creates a ContextHub and loads static context (schemas, instructions, resources)
    2. Builds data source clients
    3. Discovers tables (if not explicitly provided)
    4. Renders context strings ready for prompt inclusion
    
    Parameters
    ----------
    db : AsyncSession
        Database session
    user : User
        Authenticated user
    organization : Organization
        User's organization
    report : ReportSchema or Report
        The report/session being worked on (must have .data_sources)
    prompt : str
        User's prompt - used for intelligent instruction search and table discovery
    explicit_tables : List[TablesBySource], optional
        Explicitly provided tables. If None, tables are auto-discovered.
    top_k_schema : int
        Number of top tables to include per data source
    top_k_resources : int
        Number of top resources to include per repository
        
    Returns
    -------
    MCPRichContext
        Complete context with all sections loaded and rendered
    """
    data_sources = getattr(report, 'data_sources', []) or []
    
    # Get organization settings
    org_settings = await organization.get_settings(db)
    
    # Get default model
    model = await organization.get_default_llm_model(db)
    
    # Build data source clients
    ds_service = DataSourceService()
    ds_clients: Dict[str, Any] = {}
    connected_sources: List[str] = []
    failed_sources: List[str] = []
    
    for ds in data_sources:
        try:
            ds_clients[ds.name] = await ds_service.construct_client(db, ds, user)
            connected_sources.append(ds.name)
        except Exception as e:
            logger.warning(f"Failed to connect to data source {ds.name}: {e}")
            failed_sources.append(ds.name)
    
    # Create ContextHub for context building
    context_hub = ContextHub(
        db=db,
        organization=organization,
        report=report,
        data_sources=data_sources,
        user=user,
    )
    
    # Prime static context with the user's prompt for intelligent instruction search
    # This loads: schemas, instructions, resources, files in parallel
    await context_hub.prime_static(query=prompt)
    
    # Get the context view
    view = context_hub.get_view()
    
    # Render instructions
    instructions_text = ""
    if view.static.instructions:
        try:
            instructions_text = view.static.instructions.render()
        except Exception:
            pass
    
    # Render resources
    resources_text = ""
    if view.static.resources:
        try:
            resources_text = view.static.resources.render_combined(
                top_k_per_repo=top_k_resources, 
                index_limit=DEFAULT_INDEX_LIMIT
            )
        except Exception:
            try:
                resources_text = view.static.resources.render()
            except Exception:
                pass
    
    # Render files context
    files_text = ""
    if view.static.files:
        try:
            files_text = view.static.files.render()
        except Exception:
            pass
    
    # Discover or use explicit tables
    tables_by_source: List[Dict[str, Any]] = []
    
    if explicit_tables:
        # Use explicitly provided tables
        tables_by_source = [
            {"data_source_id": t.data_source_id, "tables": t.tables}
            for t in explicit_tables
        ]
    else:
        # Auto-discover tables using intelligent search
        tables_by_source = await _discover_tables_intelligent(
            context_hub=context_hub,
            prompt=prompt,
            instructions_text=instructions_text,
            top_k=top_k_schema,
        )
    
    # Build schemas excerpt for the discovered/explicit tables
    schemas_excerpt = await _build_schemas_excerpt(
        context_hub=context_hub,
        tables_by_source=tables_by_source,
        top_k=top_k_schema,
    )
    
    return MCPRichContext(
        context_hub=context_hub,
        ds_clients=ds_clients,
        org_settings=org_settings,
        model=model,
        tables_by_source=tables_by_source,
        schemas_excerpt=schemas_excerpt,
        instructions_text=instructions_text,
        resources_text=resources_text,
        files_text=files_text,
        connected_sources=connected_sources,
        failed_sources=failed_sources,
    )


async def _discover_tables_intelligent(
    context_hub: ContextHub,
    prompt: str,
    instructions_text: str,
    top_k: int = 5,
) -> List[Dict[str, Any]]:
    """Auto-discover relevant tables using intelligent search.
    
    Discovery strategy:
    1. Extract keywords from user prompt
    2. Check if instructions mention specific tables (future enhancement)
    3. Use semantic search on schema builder
    4. Fall back to keyword-based pattern matching
    
    Parameters
    ----------
    context_hub : ContextHub
        Context hub with schema builder
    prompt : str
        User's prompt
    instructions_text : str
        Rendered instructions (for future table hint extraction)
    top_k : int
        Maximum tables to discover
        
    Returns
    -------
    List[Dict[str, Any]]
        List of {"data_source_id": str, "tables": List[str]}
    """
    # Extract keywords from prompt (same logic as before, but cleaner)
    tokens = [t.lower() for t in re.findall(r"[a-zA-Z0-9_]{3,}", prompt)]
    # Dedupe while preserving order
    keywords = list(dict.fromkeys(tokens))[:5]
    
    # Build name patterns for regex matching
    name_patterns = [f"(?i){re.escape(k)}" for k in keywords] if keywords else None
    
    try:
        # Use schema builder to find matching tables
        ctx = await context_hub.schema_builder.build(
            with_stats=True,
            name_patterns=name_patterns,
            top_k=top_k,
        )
        
        tables_by_source = []
        for ds in ctx.data_sources:
            if ds.tables:
                tables_by_source.append({
                    "data_source_id": str(ds.info.id),
                    "tables": [t.name for t in ds.tables]
                })
        
        return tables_by_source
    except Exception as e:
        logger.warning(f"Table discovery failed: {e}")
        return []


async def _build_schemas_excerpt(
    context_hub: ContextHub,
    tables_by_source: List[Dict[str, Any]],
    top_k: int = 10,
) -> str:
    """Build a schemas excerpt string for the discovered tables.
    
    Parameters
    ----------
    context_hub : ContextHub
        Context hub with schema builder
    tables_by_source : List[Dict[str, Any]]
        Tables to include, grouped by data source
    top_k : int
        Max tables per data source
        
    Returns
    -------
    str
        Rendered schema excerpt for prompt inclusion
    """
    if not tables_by_source:
        # Fall back to top tables from all sources
        try:
            ctx = await context_hub.schema_builder.build(
                with_stats=True,
                top_k=top_k,
            )
            return ctx.render_combined(
                top_k_per_ds=top_k,
                index_limit=DEFAULT_INDEX_LIMIT,
                include_index=False,
            )
        except Exception:
            return ""
    
    try:
        # Build patterns to match the resolved table names
        all_resolved_names = []
        ds_ids = []
        
        for group in tables_by_source:
            if group.get("data_source_id"):
                ds_ids.append(group["data_source_id"])
            all_resolved_names.extend(group.get("tables", []))
        
        ds_scope = list(set(ds_ids)) if ds_ids else None
        name_patterns = [
            f"(?i)(?:^|\\.){re.escape(n)}$" for n in all_resolved_names
        ] if all_resolved_names else None
        
        ctx = await context_hub.schema_builder.build(
            with_stats=True,
            data_source_ids=ds_scope,
            name_patterns=name_patterns,
        )
        
        return ctx.render_combined(
            top_k_per_ds=top_k,
            index_limit=DEFAULT_INDEX_LIMIT,
            include_index=False,
        )
    except Exception as e:
        logger.warning(f"Failed to build schemas excerpt: {e}")
        return ""

