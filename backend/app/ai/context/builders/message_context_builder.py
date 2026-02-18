"""
Message Context Builder - Ports proven logic from agent._build_messages_context()
"""
import json
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy import and_

from app.models.completion import Completion
from app.models.widget import Widget
from app.models.step import Step
from app.models.organization import Organization
from app.ai.context.sections.messages_section import MessagesSection, MessageItem
from app.models.entity import Entity
from app.models.mention import Mention, MentionType
from app.models.file import File
from app.models.data_source import DataSource
from app.models.datasource_table import DataSourceTable


class MessageContextBuilder:
    """
    Builds conversation message context for agent execution.
    
    Ports the proven logic from agent._build_messages_context() with
    completion history, widget associations, and step information.
    """
    
    def __init__(self, db: AsyncSession, organization, report, user=None):
        self.db = db
        self.report = report
        self.organization = organization
        self.organization_settings = organization.settings if organization else None
    
    async def build_context(
        self,
        max_messages: int = 20,
        role_filter: Optional[List[str]] = None
    ) -> str:
        """
        Build clean conversation context showing user prompts and system responses.
        
        Format:
        - User messages: show prompt content
        - System messages: show reasoning + assistant messages from completion blocks
        
        Args:
            max_messages: Maximum number of message pairs to include
            role_filter: Filter by specific roles (e.g., ['user', 'system'])
        
        Returns:
            Formatted conversation context string
        """
        from app.models.completion_block import CompletionBlock
        
        conversation = []
                   # Check organization settings for data visibility
        allow_llm_see_data = True
        if self.organization_settings:
            try:
                # Get the config dictionary from the organization settings
                settings_dict = self.organization_settings.config
                allow_llm_see_data = settings_dict.get("allow_llm_see_data", {}).get("value", True)
            except:
                allow_llm_see_data = False  # Default to True if settings unavailable
                    
        # Get all completions for this report ordered by creation time
        report_completions = await self.db.execute(
            select(Completion)
            .filter(Completion.report_id == self.report.id)
            .order_by(Completion.created_at.asc())
        )
        report_completions = report_completions.scalars().all()
        
        # Skip the last completion if it's from a user (current incomplete conversation)
        completions_to_process = (
            report_completions[:-1] 
            if report_completions and report_completions[-1].role == 'user' 
            else report_completions
        )
        
        # Apply role filter if provided
        if role_filter:
            completions_to_process = [
                c for c in completions_to_process 
                if c.role in role_filter
            ]
        
        # Limit to max_messages (considering pairs)
        completions_to_process = completions_to_process[-max_messages:]
        
        for completion in completions_to_process:
            timestamp = completion.created_at.strftime("%H:%M")
            
            if completion.role == 'user':
                # User message: show prompt content
                content = completion.prompt.get('content', '') if completion.prompt else ''
                if content.strip():
                    conversation.append(f"User ({timestamp}): {content.strip()}")
                    
            elif completion.role == 'system':
                # System message: get reasoning + assistant from completion blocks + tool executions
                blocks_result = await self.db.execute(
                    select(CompletionBlock)
                    .filter(CompletionBlock.completion_id == completion.id)
                    .order_by(CompletionBlock.block_index.asc())
                )
                blocks = blocks_result.scalars().all()
                
                system_parts = []
                
                # Collect reasoning, assistant messages, and tool executions from blocks
                for block in blocks:
                    # Don't truncate reasoning and content - show full text
                    if block.reasoning and block.reasoning.strip():
                        system_parts.append(f"Thinking: {block.reasoning.strip()}")
                    
                    if block.content and block.content.strip():
                        system_parts.append(f"Response: {block.content.strip()}")
                    
                    # Add tool execution details if available
                    if block.tool_execution_id:
                        from app.models.tool_execution import ToolExecution
                        tool_result = await self.db.execute(
                            select(ToolExecution).filter(ToolExecution.id == block.tool_execution_id)
                        )
                        tool_execution = tool_result.scalars().first()
                        
                        if tool_execution:
                            tool_info = f"Tool: {tool_execution.tool_name}"
                            if tool_execution.tool_action:
                                tool_info += f" → {tool_execution.tool_action}"
                            tool_info += f" ({tool_execution.status})"
                            
                
                            # Add widget/step information and data based on tool execution result
                            if tool_execution.status == 'success':
                                # Digest for create_widget results
                                if tool_execution.tool_name == 'create_widget' and tool_execution.result_json:
                                    result_json = tool_execution.result_json or {}
                                    widget_data = result_json.get('widget_data', {}) or {}
                                    columns = widget_data.get('columns', []) or []
                                    rows = widget_data.get('rows', []) or []
                                    col_names = [c.get('field') or c.get('headerName') for c in columns if (c.get('field') or c.get('headerName'))]
                                    row_count = len(rows)
                                    sample_row = None
                                    if allow_llm_see_data:
                                        preview = result_json.get('data_preview', {}) or {}
                                        preview_rows = preview.get('rows') or []
                                        if preview_rows:
                                            sample_row = preview_rows[0]
                                        elif rows:
                                            sample_row = rows[0]
                                    digest_parts = [f"{row_count} rows × {len(col_names)} cols"]
                                    if col_names:
                                        head_cols = ", ".join(col_names[:3])
                                        digest_parts.append(f"cols: {head_cols}{'…' if len(col_names) > 3 else ''}")
                                    if sample_row:
                                        try:
                                            digest_parts.append(f"top row: {json.dumps(sample_row)}")
                                        except Exception:
                                            pass
                                    tool_info += " - " + "; ".join(digest_parts)
                                # Digest for create_data results (same style as create_widget)
                                elif tool_execution.tool_name == 'create_data' and tool_execution.result_json:
                                    rj = tool_execution.result_json or {}
                                    data_obj = rj.get('data') or {}
                                    columns = data_obj.get('columns', []) or []
                                    rows = data_obj.get('rows', []) or []
                                    col_names = [
                                        (c.get('field') or c.get('headerName'))
                                        for c in columns
                                        if isinstance(c, dict) and (c.get('field') or c.get('headerName'))
                                    ]
                                    row_count = len(rows)
                                    sample_row = None
                                    if allow_llm_see_data:
                                        preview = rj.get('data_preview', {}) or {}
                                        preview_rows = preview.get('rows') or []
                                        if preview_rows:
                                            sample_row = preview_rows[0]
                                        elif rows:
                                            sample_row = rows[0]
                                    digest_parts = [f"{row_count} rows × {len(col_names)} cols"]
                                    if col_names:
                                        head_cols = ", ".join(col_names[:3])
                                        digest_parts.append(f"cols: {head_cols}{'…' if len(col_names) > 3 else ''}")
                                    # If a non-table viz was inferred, surface it concisely
                                    try:
                                        dm = rj.get('data_model') or {}
                                        dm_type = str(dm.get('type') or '').strip()
                                        if dm_type and dm_type != 'table':
                                            digest_parts.append(f"chart: {dm_type}")
                                    except Exception:
                                        pass
                                    # Surface visualization_id if available (added by orchestrator)
                                    try:
                                        viz_ids = rj.get('created_visualization_ids') or []
                                        if viz_ids:
                                            digest_parts.append(f"viz_id: {viz_ids[0]}")
                                    except Exception:
                                        pass
                                    if sample_row:
                                        try:
                                            digest_parts.append(f"top row: {json.dumps(sample_row)}")
                                        except Exception:
                                            pass
                                    tool_info += " - " + "; ".join(digest_parts)
                                # Digest for describe_entity results
                                elif tool_execution.tool_name == 'describe_entity' and tool_execution.result_json:
                                    rj = tool_execution.result_json or {}
                                    digest_parts = []
                                    entity_title = rj.get('title')
                                    if entity_title:
                                        digest_parts.append(f"entity: {entity_title}")
                                    # Surface visualization_id if created
                                    try:
                                        viz_ids = rj.get('created_visualization_ids') or []
                                        if viz_ids:
                                            digest_parts.append(f"viz_id: {viz_ids[0]}")
                                    except Exception:
                                        pass
                                    if digest_parts:
                                        tool_info += " - " + "; ".join(digest_parts)
                                elif tool_execution.tool_name == 'describe_tables' and tool_execution.result_json:
                                    # Show table names extracted from schemas excerpt; fallback to query/arguments
                                    rj = tool_execution.result_json or {}
                                    names: list[str] = []
                                    try:
                                        import re
                                        excerpt = rj.get('schemas_excerpt') or ''
                                        names = re.findall(r'<table\s+[^>]*name="([^\"]+)"', excerpt)[:5]
                                    except Exception:
                                        names = []
                                    if not names:
                                        try:
                                            args = getattr(tool_execution, 'arguments_json', None) or {}
                                            q = args.get('query')
                                            if isinstance(q, list):
                                                names = [str(x) for x in q][:5]
                                            elif isinstance(q, str) and q.strip():
                                                names = [q.strip()]
                                        except Exception:
                                            pass
                                    if names:
                                        tool_info += f" - tables: {', '.join(names)}"
                                elif tool_execution.tool_name == 'answer_question' and tool_execution.result_json:
                                    rj = tool_execution.result_json or {}
                                    answer_text = rj.get('answer') or ((rj.get('output') or {}).get('answer') if isinstance(rj.get('output'), dict) else None)
                                    if answer_text:
                                        tool_info += f" - AI answer: {answer_text}"
                                elif tool_execution.tool_name == 'create_artifact' and tool_execution.result_json:
                                    rj = tool_execution.result_json or {}
                                    digest_parts = []
                                    if rj.get('title'):
                                        digest_parts.append(f"artifact: {rj.get('title')}")
                                    if rj.get('mode'):
                                        digest_parts.append(f"mode: {rj.get('mode')}")
                                    if rj.get('artifact_id'):
                                        digest_parts.append(f"artifact_id: {rj.get('artifact_id')}")
                                    # Surface visualization_ids used to build the artifact
                                    viz_ids = rj.get('visualization_ids') or []
                                    if viz_ids:
                                        digest_parts.append(f"viz_ids: {', '.join(viz_ids)}")
                                    if digest_parts:
                                        tool_info += " - " + "; ".join(digest_parts)
                                elif tool_execution.tool_name == 'read_artifact' and tool_execution.result_json:
                                    rj = tool_execution.result_json or {}
                                    digest_parts = []
                                    if rj.get('title'):
                                        digest_parts.append(f"artifact: {rj.get('title')}")
                                    if rj.get('mode'):
                                        digest_parts.append(f"mode: {rj.get('mode')}")
                                    if rj.get('artifact_id'):
                                        digest_parts.append(f"artifact_id: {rj.get('artifact_id')}")
                                    if rj.get('version'):
                                        digest_parts.append(f"v{rj.get('version')}")
                                    if digest_parts:
                                        tool_info += " - " + "; ".join(digest_parts)
                                elif tool_execution.created_widget_id:
                                    # Get widget details for other tools
                                    widget_result = await self.db.execute(
                                        select(Widget).filter(Widget.id == tool_execution.created_widget_id)
                                    )
                                    widget = widget_result.scalars().first()
                                    if widget:
                                        tool_info += f" - Widget: '{widget.title}'"
                                    else:
                                        tool_info += f" - Widget #{tool_execution.created_widget_id}"
                                
                                elif tool_execution.created_step_id:
                                    # Get step details for other tools
                                    step_result = await self.db.execute(
                                        select(Step).filter(Step.id == tool_execution.created_step_id)
                                    )
                                    step = step_result.scalars().first()
                                    if step:
                                        tool_info += f" - Step: '{step.title}'"
                                    else:
                                        tool_info += f" - Step #{tool_execution.created_step_id}"
                                
                                elif tool_execution.result_summary:
                                    # Condense result summary
                                    summary = tool_execution.result_summary
                                    if len(summary) > 60:
                                        summary = summary[:60] + "..."
                                    tool_info += f" - {summary}"
                            
                            elif tool_execution.status == 'error' and tool_execution.error_message:
                                # Show condensed error
                                error = tool_execution.error_message
                                if len(error) > 50:
                                    error = error[:50] + "..."
                                tool_info += f" - Error: {error}"
                            
                            system_parts.append(tool_info)
                
                # If no blocks or content, fall back to completion.completion
                if not system_parts and completion.completion:
                    if isinstance(completion.completion, dict):
                        # Handle JSON completion format
                        content = completion.completion.get('content', '') or completion.completion.get('message', '')
                    else:
                        content = str(completion.completion)
                    
                    if content.strip():
                        system_parts.append(f"Response: {content.strip()}")
                
                if system_parts:
                    conversation.append(f"Assistant ({timestamp}): {' | '.join(system_parts)}")
        
        # Join all conversation parts
        conversation_text = "\n".join(conversation) if conversation else "No conversation history available"
        
        # Only truncate the entire final context if it's too long (like old agent.py approach)
        max_context_length = 8000  # Reasonable limit for LLM context
        if len(conversation_text) > max_context_length:
            conversation_text = conversation_text[:max_context_length] + "...\n[Context truncated due to length]"
        
        return conversation_text

    async def build(
        self,
        max_messages: int = 20,
        role_filter: Optional[List[str]] = None
    ) -> MessagesSection:
        """Build object-based messages section using the same data path as build_context."""
        from app.models.completion_block import CompletionBlock

        items: List[MessageItem] = []

        allow_llm_see_data = True
        if self.organization_settings:
            try:
                settings_dict = self.organization_settings.config
                allow_llm_see_data = settings_dict.get("allow_llm_see_data", {}).get("value", True)
            except Exception:
                allow_llm_see_data = False

        report_completions = await self.db.execute(
            select(Completion)
            .filter(Completion.report_id == self.report.id)
            .order_by(Completion.created_at.asc())
        )
        report_completions = report_completions.scalars().all()

        completions_to_process = (
            report_completions[:-1]
            if report_completions and report_completions[-1].role == 'user'
            else report_completions
        )

        if role_filter:
            completions_to_process = [c for c in completions_to_process if c.role in role_filter]

        completions_to_process = completions_to_process[-max_messages:]

        # =========================
        # Batch-load mentions for all user messages to avoid N+1 queries
        # =========================
        user_completion_ids: List[str] = [str(c.id) for c in completions_to_process if c.role == 'user']
        mentions_by_completion: Dict[str, List[Mention]] = {}
        file_ids: set[str] = set()
        ds_ids: set[str] = set()
        tbl_ids: set[str] = set()
        ent_ids: set[str] = set()

        if user_completion_ids:
            mentions_q = await self.db.execute(
                select(Mention).where(Mention.completion_id.in_(user_completion_ids))
            )
            all_mentions: List[Mention] = mentions_q.scalars().all()
            for m in all_mentions:
                cid = str(getattr(m, 'completion_id', ''))
                mentions_by_completion.setdefault(cid, []).append(m)
                try:
                    if m.type == MentionType.FILE:
                        file_ids.add(str(m.object_id))
                    elif m.type == MentionType.DATA_SOURCE:
                        ds_ids.add(str(m.object_id))
                    elif m.type == MentionType.TABLE:
                        tbl_ids.add(str(m.object_id))
                    elif m.type == MentionType.ENTITY:
                        ent_ids.add(str(m.object_id))
                except Exception:
                    continue

        # Batch-load referenced objects by type (up to 4 queries)
        file_map: Dict[str, Any] = {}
        ds_map: Dict[str, Any] = {}
        tbl_map: Dict[str, Any] = {}
        ent_map: Dict[str, Any] = {}

        if file_ids:
            try:
                rows = await self.db.execute(select(File).where(File.id.in_(list(file_ids))))
                for f in rows.scalars().all():
                    file_map[str(getattr(f, 'id', ''))] = f
            except Exception:
                pass
        if ds_ids:
            try:
                rows = await self.db.execute(select(DataSource).where(DataSource.id.in_(list(ds_ids))))
                for ds in rows.scalars().all():
                    ds_map[str(getattr(ds, 'id', ''))] = ds
            except Exception:
                pass
        if tbl_ids:
            try:
                rows = await self.db.execute(select(DataSourceTable).where(DataSourceTable.id.in_(list(tbl_ids))))
                for t in rows.scalars().all():
                    tbl_map[str(getattr(t, 'id', ''))] = t
                    try:
                        # Opportunistically collect data source ids from tables to show DS name
                        ds_id = str(getattr(t, 'data_source_id', '') or '')
                        if ds_id:
                            ds_ids.add(ds_id)
                    except Exception:
                        pass
                # If we discovered new ds_ids from tables, try to fill missing ones
                missing_ds = [x for x in ds_ids if x not in ds_map]
                if missing_ds:
                    rows2 = await self.db.execute(select(DataSource).where(DataSource.id.in_(missing_ds)))
                    for ds in rows2.scalars().all():
                        ds_map[str(getattr(ds, 'id', ''))] = ds
            except Exception:
                pass
        if ent_ids:
            try:
                rows = await self.db.execute(select(Entity).where(Entity.id.in_(list(ent_ids))))
                for e in rows.scalars().all():
                    ent_map[str(getattr(e, 'id', ''))] = e
            except Exception:
                pass

        for completion in completions_to_process:
            ts = completion.created_at.strftime("%H:%M") if getattr(completion, 'created_at', None) else None
            if completion.role == 'user':
                content = completion.prompt.get('content', '') if completion.prompt else ''
                if content and content.strip():
                    # Prefer persisted mentions over prompt payload for display
                    mentions_str = None
                    try:
                        cid = str(getattr(completion, 'id', ''))
                        mlist = mentions_by_completion.get(cid, [])
                        if mlist:
                            parts: List[str] = []
                            for m in mlist:
                                try:
                                    if m.type == MentionType.DATA_SOURCE:
                                        ds = ds_map.get(str(m.object_id))
                                        name = getattr(ds, 'name', None) or m.mention_content
                                        parts.append(str(name))
                                    elif m.type == MentionType.TABLE:
                                        t = tbl_map.get(str(m.object_id))
                                        if t:
                                            ds_name = None
                                            try:
                                                ds_name = getattr(ds_map.get(str(getattr(t, 'data_source_id', ''))), 'name', None)
                                            except Exception:
                                                ds_name = None
                                            tname = getattr(t, 'name', None) or m.mention_content
                                            if ds_name:
                                                parts.append(f"{tname} (Table in Data Source: {ds_name})")
                                            else:
                                                parts.append(f"{tname} (Table)")
                                        else:
                                            parts.append(m.mention_content)
                                    elif m.type == MentionType.ENTITY:
                                        e = ent_map.get(str(m.object_id))
                                        title = getattr(e, 'title', None) or m.mention_content
                                        cols_preview: List[str] = []
                                        rows_count: Optional[int] = None
                                        try:
                                            data_json = getattr(e, 'data', None) or {}
                                            if isinstance(data_json, dict):
                                                cols = data_json.get('columns')
                                                if isinstance(cols, list):
                                                    for c in cols:
                                                        if isinstance(c, dict):
                                                            n = c.get('field') or c.get('headerName') or c.get('name')
                                                            if n:
                                                                cols_preview.append(str(n))
                                                        else:
                                                            cols_preview.append(str(c))
                                                info = data_json.get('info')
                                                rows = data_json.get('rows')
                                                if isinstance(info, dict) and isinstance(info.get('total_rows'), int):
                                                    rows_count = info.get('total_rows')
                                                elif isinstance(rows, list):
                                                    rows_count = len(rows)
                                        except Exception:
                                            pass
                                        extras: List[str] = ["Entity from Catalog"]
                                        if cols_preview:
                                            extras.append(f"cols: {','.join(cols_preview[:3])}")
                                        if rows_count is not None:
                                            extras.append(f"rows: {rows_count}")
                                        parts.append(f"{title} (" + ", ".join(extras) + ")")
                                    elif m.type == MentionType.FILE:
                                        fobj = file_map.get(str(m.object_id))
                                        fname = getattr(fobj, 'filename', None) or m.mention_content
                                        parts.append(str(fname))
                                except Exception:
                                    continue
                            if parts:
                                mentions_str = ", ".join(parts[:8]) + ("…" if len(parts) > 8 else "")
                    except Exception:
                        mentions_str = None
                    items.append(MessageItem(role="user", timestamp=ts, text=content.strip(), mentions=mentions_str))
            elif completion.role == 'system':
                # Aggregate blocks like build_context
                blocks_result = await self.db.execute(
                    select(CompletionBlock)
                    .filter(CompletionBlock.completion_id == completion.id)
                    .order_by(CompletionBlock.block_index.asc())
                )
                blocks = blocks_result.scalars().all()
                system_parts: List[str] = []
                for block in blocks:
                    if block.reasoning and block.reasoning.strip():
                        system_parts.append(f"Thinking: {block.reasoning.strip()}")
                    if block.content and block.content.strip():
                        system_parts.append(f"Response: {block.content.strip()}")
                    if block.tool_execution_id:
                        from app.models.tool_execution import ToolExecution
                        tool_result = await self.db.execute(
                            select(ToolExecution).filter(ToolExecution.id == block.tool_execution_id)
                        )
                        tool_execution = tool_result.scalars().first()
                        if tool_execution:
                            tool_info = f"Tool: {tool_execution.tool_name}"
                            if tool_execution.tool_action:
                                tool_info += f" → {tool_execution.tool_action}"
                            tool_info += f" ({tool_execution.status})"
                            if tool_execution.status == 'success' and tool_execution.tool_name == 'create_widget' and tool_execution.result_json:
                                result_json = tool_execution.result_json or {}
                                widget_data = result_json.get('widget_data', {}) or {}
                                columns = widget_data.get('columns', []) or []
                                rows = widget_data.get('rows', []) or []
                                col_names = [c.get('field') or c.get('headerName') for c in columns if (c.get('field') or c.get('headerName'))]
                                row_count = len(rows)
                                digest_parts = [f"{row_count} rows × {len(col_names)} cols"]
                                if col_names:
                                    head_cols = ", ".join(col_names[:3])
                                    digest_parts.append(f"cols: {head_cols}{'…' if len(col_names) > 3 else ''}")
                                if allow_llm_see_data:
                                    preview = result_json.get('data_preview', {}) or {}
                                    preview_rows = preview.get('rows') or []
                                    sample_row = preview_rows[0] if preview_rows else (rows[0] if rows else None)
                                    if sample_row:
                                        try:
                                            digest_parts.append(f"top row: {json.dumps(sample_row)}")
                                        except Exception:
                                            pass
                                tool_info += " - " + "; ".join(digest_parts)
                            elif tool_execution.status == 'success' and tool_execution.tool_name == 'create_data' and tool_execution.result_json:
                                rj = tool_execution.result_json or {}
                                data_obj = rj.get('data') or {}
                                columns = data_obj.get('columns', []) or []
                                rows = data_obj.get('rows', []) or []
                                col_names = [
                                    (c.get('field') or c.get('headerName'))
                                    for c in columns
                                    if isinstance(c, dict) and (c.get('field') or c.get('headerName'))
                                ]
                                row_count = len(rows)
                                digest_parts = [f"{row_count} rows × {len(col_names)} cols"]
                                if col_names:
                                    head_cols = ", ".join(col_names[:3])
                                    digest_parts.append(f"cols: {head_cols}{'…' if len(col_names) > 3 else ''}")
                                if allow_llm_see_data:
                                    preview = rj.get('data_preview', {}) or {}
                                    preview_rows = preview.get('rows') or []
                                    sample_row = preview_rows[0] if preview_rows else (rows[0] if rows else None)
                                    if sample_row:
                                        try:
                                            digest_parts.append(f"top row: {json.dumps(sample_row)}")
                                        except Exception:
                                            pass
                                # If a non-table viz was inferred, surface it concisely
                                try:
                                    dm = rj.get('data_model') or {}
                                    dm_type = str(dm.get('type') or '').strip()
                                    if dm_type and dm_type != 'table':
                                        digest_parts.append(f"chart: {dm_type}")
                                except Exception:
                                    pass
                                # Surface visualization_id if available (added by orchestrator)
                                try:
                                    viz_ids = rj.get('created_visualization_ids') or []
                                    if viz_ids:
                                        digest_parts.append(f"viz_id: {viz_ids[0]}")
                                except Exception:
                                    pass
                                tool_info += " - " + "; ".join(digest_parts)
                            elif tool_execution.status == 'success' and tool_execution.tool_name == 'describe_entity' and tool_execution.result_json:
                                # Digest for describe_entity results
                                rj = tool_execution.result_json or {}
                                digest_parts = []
                                entity_title = rj.get('title')
                                if entity_title:
                                    digest_parts.append(f"entity: {entity_title}")
                                # Surface visualization_id if created
                                try:
                                    viz_ids = rj.get('created_visualization_ids') or []
                                    if viz_ids:
                                        digest_parts.append(f"viz_id: {viz_ids[0]}")
                                except Exception:
                                    pass
                                if digest_parts:
                                    tool_info += " - " + "; ".join(digest_parts)
                            elif tool_execution.status == 'success' and tool_execution.tool_name == 'describe_tables' and tool_execution.result_json:
                                # Show table names extracted from schemas excerpt; fallback to query/arguments
                                rj = tool_execution.result_json or {}
                                names: list[str] = []
                                try:
                                    import re
                                    excerpt = rj.get('schemas_excerpt') or ''
                                    names = re.findall(r'<table\s+[^>]*name=\"([^\\\"]+)\"', excerpt)[:5]
                                except Exception:
                                    names = []
                                if not names:
                                    try:
                                        args = getattr(tool_execution, 'arguments_json', None) or {}
                                        q = args.get('query')
                                        if isinstance(q, list):
                                            names = [str(x) for x in q][:5]
                                        elif isinstance(q, str) and q.strip():
                                            names = [q.strip()]
                                    except Exception:
                                        pass
                                if names:
                                    tool_info += f" - tables: {', '.join(names)}"
                            elif tool_execution.status == 'success' and tool_execution.tool_name == 'answer_question' and tool_execution.result_json:
                                rj = tool_execution.result_json or {}
                                answer_text = rj.get('answer') or ((rj.get('output') or {}).get('answer') if isinstance(rj.get('output'), dict) else None)
                                if answer_text:
                                    tool_info += f" - AI answer: {answer_text}"
                            elif tool_execution.status == 'success' and tool_execution.tool_name == 'create_artifact' and tool_execution.result_json:
                                rj = tool_execution.result_json or {}
                                digest_parts = []
                                if rj.get('title'):
                                    digest_parts.append(f"artifact: {rj.get('title')}")
                                if rj.get('mode'):
                                    digest_parts.append(f"mode: {rj.get('mode')}")
                                if rj.get('artifact_id'):
                                    digest_parts.append(f"artifact_id: {rj.get('artifact_id')}")
                                # Surface visualization_ids used to build the artifact
                                viz_ids = rj.get('visualization_ids') or []
                                if viz_ids:
                                    digest_parts.append(f"viz_ids: {', '.join(viz_ids)}")
                                if digest_parts:
                                    tool_info += " - " + "; ".join(digest_parts)
                            elif tool_execution.status == 'success' and tool_execution.tool_name == 'read_artifact' and tool_execution.result_json:
                                rj = tool_execution.result_json or {}
                                digest_parts = []
                                if rj.get('title'):
                                    digest_parts.append(f"artifact: {rj.get('title')}")
                                if rj.get('mode'):
                                    digest_parts.append(f"mode: {rj.get('mode')}")
                                if rj.get('artifact_id'):
                                    digest_parts.append(f"artifact_id: {rj.get('artifact_id')}")
                                if rj.get('version'):
                                    digest_parts.append(f"v{rj.get('version')}")
                                if digest_parts:
                                    tool_info += " - " + "; ".join(digest_parts)
                            elif tool_execution.status == 'error' and tool_execution.error_message:
                                error = tool_execution.error_message
                                if len(error) > 50:
                                    error = error[:50] + "..."
                                tool_info += f" - Error: {error}"
                            system_parts.append(tool_info)
                if not system_parts and completion.completion:
                    if isinstance(completion.completion, dict):
                        content = completion.completion.get('content', '') or completion.completion.get('message', '')
                    else:
                        content = str(completion.completion)
                    if content.strip():
                        system_parts.append(f"Response: {content.strip()}")
                if system_parts:
                    items.append(MessageItem(role="system", timestamp=ts, text=" | ".join(system_parts)))

        return MessagesSection(items=items)
    
    async def get_message_count(self, role_filter: Optional[List[str]] = None) -> int:
        """Get total number of messages for this report."""
        query = select(Completion).filter(Completion.report_id == self.report.id)
        
        if role_filter:
            query = query.filter(Completion.role.in_(role_filter))
            
        result = await self.db.execute(query)
        return len(result.scalars().all())
    
    async def render(self, max_messages: int = 10) -> str:
        """Render a human-readable view of message context."""
        total_count = await self.get_message_count()
        
        parts = [
            f"Message Context: {total_count} total messages",
            "=" * 40
        ]
        
        if total_count == 0:
            parts.append("\nNo messages in conversation")
            return "\n".join(parts)
        
        # Get recent messages
        report_completions = await self.db.execute(
            select(Completion)
            .filter(Completion.report_id == self.report.id)
            .order_by(Completion.created_at.desc())
            .limit(max_messages)
        )
        recent_messages = report_completions.scalars().all()
        
        if recent_messages:
            parts.append(f"\nRecent {len(recent_messages)} messages:")
            for i, msg in enumerate(reversed(recent_messages)):
                timestamp = msg.created_at.strftime("%H:%M:%S")
                content_preview = (
                    msg.prompt['content'][:100] if msg.role == 'user' 
                    else msg.completion['content'][:100]
                )
                parts.append(f"  {i+1}. [{timestamp}] {msg.role}: {content_preview}...")
        
        return "\n".join(parts)