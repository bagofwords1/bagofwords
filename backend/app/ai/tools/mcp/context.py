"""MCP context preparation - shared rich context building for MCP tools.

Provides a single-pass context preparation layer similar to agent_v2.py
but without the feedback/observation loop. This enables MCP tools to have
access to the same rich context (instructions, resources, schemas) that
the main agent uses.
"""

import json
import re
import logging
from typing import Dict, Any, List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.context import ContextHub
from app.ai.llm import LLM
from app.models.user import User
from app.models.organization import Organization
from app.models.llm_model import LLMModel
from app.services.data_source_service import DataSourceService
from app.schemas.mcp import MCPRichContext
from app.ai.tools.schemas.create_widget import TablesBySource
from app.dependencies import async_session_maker

logger = logging.getLogger(__name__)

# Limits for context rendering
DEFAULT_TOP_K_SCHEMA = 10
DEFAULT_TOP_K_RESOURCES = 10
DEFAULT_INDEX_LIMIT = 200
DEFAULT_TOP_K_TABLES_FOR_SELECTION = 30  # Max tables to show LLM for selection


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
    use_llm_selection: bool = True,
) -> MCPRichContext:
    """Build rich context for MCP tool execution.
    
    This is the shared context preparation layer for MCP tools. It:
    1. Creates a ContextHub and loads static context (schemas, instructions, resources)
    2. Builds data source clients
    3. Discovers tables using LLM-powered selection (if not explicitly provided)
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
    use_llm_selection : bool
        Whether to use LLM for intelligent table selection (default True)
        
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
            clients = await ds_service.construct_clients(db, ds, user)
            ds_clients.update(clients)
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
        # Get all available tables first
        all_tables = await _get_all_available_tables(
            context_hub=context_hub,
            top_k=DEFAULT_TOP_K_TABLES_FOR_SELECTION,
        )
        
        # Use LLM to select relevant tables if model is available
        if use_llm_selection and model and all_tables:
            tables_by_source = await _select_tables_with_llm(
                model=model,
                prompt=prompt,
                instructions_text=instructions_text,
                resources_text=resources_text,
                all_tables=all_tables,
            )
        
        # Fall back to keyword-based discovery if LLM selection failed or not available
        if not tables_by_source:
            tables_by_source = await _discover_tables_by_keywords(
                context_hub=context_hub,
                prompt=prompt,
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


async def _get_all_available_tables(
    context_hub: ContextHub,
    top_k: int = 30,
) -> List[Dict[str, Any]]:
    """Get all available tables from all data sources.
    
    Returns a flat list of table info for LLM selection.
    """
    try:
        ctx = await context_hub.schema_builder.build(
            with_stats=True,
            top_k=top_k,
        )
        
        all_tables = []
        for ds in ctx.data_sources:
            ds_id = str(ds.info.id)
            ds_name = ds.info.name
            ds_type = ds.info.type
            
            for table in (ds.tables or []):
                # Build column summary
                columns = []
                for col in (table.columns or [])[:10]:  # Limit columns shown
                    columns.append(f"{col.name} ({col.dtype or 'any'})")
                
                table_info = {
                    "data_source_id": ds_id,
                    "data_source_name": ds_name,
                    "data_source_type": ds_type,
                    "table_name": table.name,
                    "columns": columns,
                    "description": table.metadata_json.get("description") if table.metadata_json else None,
                }
                all_tables.append(table_info)
        
        return all_tables
    except Exception as e:
        logger.warning(f"Failed to get available tables: {e}")
        return []


async def _select_tables_with_llm(
    model: LLMModel,
    prompt: str,
    instructions_text: str,
    resources_text: str,
    all_tables: List[Dict[str, Any]],
    max_selected: int = 10,
) -> List[Dict[str, Any]]:
    """Use LLM to intelligently select relevant tables for the query.
    
    Parameters
    ----------
    model : LLMModel
        The LLM model to use
    prompt : str
        User's query/prompt
    instructions_text : str
        Rendered instructions for context
    resources_text : str
        Rendered resources/metadata for context
    all_tables : List[Dict[str, Any]]
        All available tables with their info
    max_selected : int
        Maximum number of tables to select
        
    Returns
    -------
    List[Dict[str, Any]]
        Selected tables grouped by data source: [{"data_source_id": str, "tables": [str]}]
    """
    if not all_tables:
        return []
    
    # Format tables for the prompt
    tables_text = _format_tables_for_selection(all_tables)
    
    selection_prompt = f"""You are a data analyst assistant. Given a user's query and available database tables, select ONLY the tables needed to answer the query.

USER QUERY:
{prompt}

ORGANIZATION INSTRUCTIONS:
{instructions_text[:2000] if instructions_text else "No specific instructions."}

AVAILABLE TABLES:
{tables_text}

TASK:
Select the tables that are needed to answer the user's query. Consider:
1. Which tables contain the data mentioned in the query?
2. Which tables might need to be joined?
3. Don't select tables that are clearly unrelated.

Return your selection as a JSON array. Each element should have:
- "data_source_id": the data source ID
- "tables": array of table names from that data source

Example output:
[{{"data_source_id": "abc-123", "tables": ["orders", "customers"]}}]

Return ONLY the JSON array, no explanation or markdown. If no tables are relevant, return [].
"""

    try:
        llm = LLM(model, usage_session_maker=async_session_maker)
        response = llm.inference(
            selection_prompt,
            usage_scope="mcp_table_selection",
            should_record=True,
        )
        
        # Parse the response
        selected = _parse_table_selection_response(response, all_tables)
        
        if selected:
            logger.info(f"LLM selected {sum(len(s.get('tables', [])) for s in selected)} tables for query")
            return selected
        else:
            logger.warning("LLM table selection returned empty result, falling back to keyword search")
            return []
            
    except Exception as e:
        logger.warning(f"LLM table selection failed: {e}, falling back to keyword search")
        return []


def _format_tables_for_selection(all_tables: List[Dict[str, Any]]) -> str:
    """Format tables into a readable string for LLM selection."""
    lines = []
    
    # Group by data source for clarity
    by_source: Dict[str, List[Dict]] = {}
    for t in all_tables:
        ds_key = f"{t['data_source_name']} ({t['data_source_id'][:8]}...)"
        if ds_key not in by_source:
            by_source[ds_key] = []
        by_source[ds_key].append(t)
    
    for ds_key, tables in by_source.items():
        lines.append(f"\n[Data Source: {ds_key}]")
        for t in tables:
            cols_preview = ", ".join(t["columns"][:5])
            if len(t["columns"]) > 5:
                cols_preview += f", ... (+{len(t['columns']) - 5} more)"
            
            desc = f" - {t['description'][:100]}" if t.get("description") else ""
            lines.append(f"  • {t['table_name']}: {cols_preview}{desc}")
    
    return "\n".join(lines)


def _parse_table_selection_response(
    response: str,
    all_tables: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Parse LLM response and validate selected tables exist."""
    # Clean response - remove markdown code fences if present
    cleaned = response.strip()
    if cleaned.startswith("```"):
        # Remove code fences
        lines = cleaned.split("\n")
        cleaned = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to extract JSON array from response
        match = re.search(r'\[.*\]', cleaned, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
            except json.JSONDecodeError:
                return []
        else:
            return []
    
    if not isinstance(parsed, list):
        return []
    
    # Build a set of valid table names per data source for validation
    valid_tables: Dict[str, set] = {}
    for t in all_tables:
        ds_id = t["data_source_id"]
        if ds_id not in valid_tables:
            valid_tables[ds_id] = set()
        valid_tables[ds_id].add(t["table_name"])
    
    # Validate and normalize the selection
    result = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        
        ds_id = item.get("data_source_id")
        tables = item.get("tables", [])
        
        if not ds_id or not tables:
            continue
        
        # Validate tables exist
        valid_set = valid_tables.get(ds_id, set())
        validated_tables = [t for t in tables if t in valid_set]
        
        if validated_tables:
            result.append({
                "data_source_id": ds_id,
                "tables": validated_tables,
            })
    
    return result


async def _discover_tables_by_keywords(
    context_hub: ContextHub,
    prompt: str,
    top_k: int = 5,
) -> List[Dict[str, Any]]:
    """Fallback: discover tables using keyword extraction from prompt.
    
    This is the original keyword-based discovery logic, used as fallback
    when LLM selection is not available or fails.
    """
    # Extract keywords from prompt
    tokens = [t.lower() for t in re.findall(r"[a-zA-Z0-9_]{3,}", prompt)]
    # Dedupe while preserving order
    keywords = list(dict.fromkeys(tokens))[:5]
    
    # Build name patterns for regex matching
    name_patterns = [f"(?i){re.escape(k)}" for k in keywords] if keywords else None
    
    try:
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
        logger.warning(f"Keyword-based table discovery failed: {e}")
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
