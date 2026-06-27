"""
Message Context Builder - Ports proven logic from agent._build_messages_context()
"""
import json
from types import SimpleNamespace
from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text
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
from app.models.tool_execution import ToolExecution


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _is_postgres_session(db: AsyncSession) -> bool:
    try:
        return db.get_bind().dialect.name == "postgresql"
    except Exception:
        return False


def _digest_knowledge_tool(tool_execution) -> str:
    """Build a tight one-line digest for knowledge-harness instruction tools.

    Returns an empty string when the tool isn't one of the knowledge tools so
    callers can fall through to the next elif.
    """
    name = tool_execution.tool_name
    rj = tool_execution.result_json or {}
    if name == 'search_instructions':
        # Never dump full text — ids + titles only, capped at 5.
        try:
            obs = rj.get('observation') or {}
            arts = (obs.get('artifacts') or [])
            items = arts[0].get('items', []) if arts else []
        except Exception:
            items = []
        total = rj.get('total') if isinstance(rj.get('total'), int) else len(items)
        hits = []
        for it in items[:5]:
            hits.append(f"{it.get('id','?')}:{(it.get('title') or '').strip()[:60]}")
        more = f" (+{len(items)-5} more)" if len(items) > 5 else ""
        return f"found {total} — [{'; '.join(hits)}]{more}" if hits else f"found {total}"
    if name == 'create_instruction':
        parts = []
        if rj.get('title'):
            parts.append(f"title: {rj.get('title')}")
        if rj.get('instruction_id'):
            parts.append(f"instruction_id: {rj.get('instruction_id')}")
        if rj.get('build_id'):
            parts.append(f"build_id: {rj.get('build_id')}")
        if rj.get('rejected_reason'):
            parts.append(f"rejected: {rj.get('rejected_reason')}")
        return "; ".join(parts)
    if name == 'edit_instruction':
        parts = []
        if rj.get('title'):
            parts.append(f"title: {rj.get('title')}")
        if rj.get('instruction_id'):
            parts.append(f"instruction_id: {rj.get('instruction_id')}")
        if rj.get('version_number'):
            parts.append(f"v{rj.get('version_number')}")
        if rj.get('build_id'):
            parts.append(f"build_id: {rj.get('build_id')}")
        return "; ".join(parts)
    return ""


def _digest_eval_tool(tool_execution) -> str:
    """Digest for eval tools (search_evals, create_eval, run_eval).

    Returns empty string when the tool isn't one of these so callers can
    fall through to the next elif.
    """
    name = tool_execution.tool_name
    if name not in ('search_evals', 'create_eval', 'run_eval'):
        return ""
    rj = tool_execution.result_json or {}
    output = rj.get('output') or {}

    if name == 'search_evals':
        items = output.get('items') or []
        total = output.get('total') if isinstance(output.get('total'), int) else len(items)
        hits = []
        for it in items[:5]:
            hits.append(f"{it.get('id','?')}:{(it.get('name') or '').strip()[:60]}({it.get('status','?')})")
        more = f" (+{len(items)-5} more)" if len(items) > 5 else ""
        return f"found {total} — [{'; '.join(hits)}]{more}" if hits else f"found {total}"

    if name == 'create_eval':
        if output.get('success') is False:
            reason = output.get('rejected_reason') or output.get('message') or 'unknown'
            return f"rejected: {reason}"
        parts = []
        if output.get('case_id'):
            parts.append(f"id: {output.get('case_id')}")
        if output.get('name'):
            parts.append(f"name: {output.get('name')}")
        if output.get('suite_name'):
            parts.append(f"suite: {output.get('suite_name')}")
        if output.get('status'):
            parts.append(f"status={output.get('status')}")
        if output.get('auto_generated'):
            parts.append("auto=true")
        return "; ".join(parts)

    if name == 'run_eval':
        if output.get('rejected_reason'):
            return f"rejected: {output.get('rejected_reason')}"
        parts = []
        if output.get('run_id'):
            parts.append(f"run_id: {output.get('run_id')}")
        if output.get('status'):
            parts.append(f"status={output.get('status')}")
        passed = output.get('passed', 0)
        failed = output.get('failed', 0)
        total = output.get('total', 0)
        parts.append(f"{passed}/{total} pass, {failed} fail")
        results = output.get('results') or []
        failed_cases = [r for r in results if r.get('status') in ('fail', 'error')]
        if failed_cases:
            shown = []
            for r in failed_cases[:3]:
                label = r.get('case_name') or r.get('case_id') or '?'
                reason = r.get('failure_reason')
                if reason:
                    shown.append(f"{label}: {str(reason)[:80]}")
                else:
                    shown.append(label)
            entry = "failed: [" + "; ".join(shown) + "]"
            if len(failed_cases) > 3:
                entry += f" +{len(failed_cases)-3}"
            parts.append(entry)
        return "; ".join(parts)

    return ""


def _digest_web_search(tool_execution) -> str:
    """Digest for native (provider-executed) web search.

    Summarizes what was searched and what came back so later turns have the
    context. arguments_json carries the query/queries; result_json carries the
    turn's cited sources (attached to the last search of the turn) or none.
    Returns "" for other tools so callers fall through.
    """
    if getattr(tool_execution, 'tool_name', None) != 'web_search':
        return ""
    # Some callers (e.g. token estimation) pass lightweight projections that may
    # not carry every column — read defensively.
    args = getattr(tool_execution, 'arguments_json', None) or {}
    rj = getattr(tool_execution, 'result_json', None) or {}
    if not isinstance(args, dict):
        args = {}
    if not isinstance(rj, dict):
        rj = {}
    queries = args.get('queries') or ([args.get('query')] if args.get('query') else [])
    queries = [q for q in queries if q]
    parts = []
    if queries:
        head = f'"{str(queries[0])[:80]}"'
        if len(queries) > 1:
            head += f" (+{len(queries)-1} more)"
        parts.append(head)
    sources = rj.get('sources') or []
    if isinstance(sources, list) and sources:
        titles = "; ".join((s.get('title') or s.get('url') or '')[:50] for s in sources[:3] if isinstance(s, dict))
        more = f" (+{len(sources)-3} more)" if len(sources) > 3 else ""
        parts.append(f"{len(sources)} sources: {titles}{more}")
    elif getattr(tool_execution, 'status', None) not in ('success', 'completed'):
        parts.append("failed")
    return "web search " + " — ".join(parts) if parts else ""


def _digest_execute_mcp(tool_execution) -> str:
    """Digest for execute_mcp so the planner sees WHAT it called, not just the result.

    Without the input (which MCP tool + arguments) the planner can't tell which
    calls it already tried and loops through variants — and a tool-level failure
    used to surface only as "Tool returned error: None". Include the called tool
    name, a compact argument echo, and the real error message on failure so the
    next turn can correct course instead of guessing. Returns "" for other tools.
    """
    if getattr(tool_execution, 'tool_name', None) != 'execute_mcp':
        return ""
    rj = tool_execution.result_json or {}
    obs = rj.get('observation') or rj
    args = getattr(tool_execution, 'arguments_json', None) or {}
    if not isinstance(args, dict):
        args = {}
    digest_parts = []

    # Echo the call: which underlying MCP tool, with what arguments.
    called = args.get('tool_name')
    if called:
        call_str = f"called: {called}"
        tool_args = args.get('arguments')
        if tool_args is not None:
            try:
                args_str = json.dumps(tool_args, default=str)
            except Exception:
                args_str = str(tool_args)
            if len(args_str) > 300:
                args_str = args_str[:300] + '…'
            call_str += f"({args_str})"
        digest_parts.append(call_str)

    # Failure: surface the real error and mark it clearly so the planner adapts.
    failed = obs.get('success') is False or rj.get('success') is False
    if failed:
        err = obs.get('error_message') or rj.get('error_message') or obs.get('summary') or 'unknown error'
        digest_parts.append(f"FAILED: {str(err)[:300]}")
        # Surface the tool's valid argument names so a later turn corrects the
        # call instead of re-guessing (execute_mcp attaches input_schema on
        # failure).
        schema = obs.get('input_schema') or rj.get('input_schema')
        if isinstance(schema, dict):
            props = schema.get('properties') or {}
            if props:
                required = set(schema.get('required') or [])
                arg_list = ", ".join(f"{n}*" if n in required else n for n in props.keys())
                digest_parts.append(f"valid args: {{{arg_list}}}")
        return "; ".join(digest_parts)

    summary = obs.get('summary') or ''
    if summary:
        digest_parts.append(summary)
    ct = obs.get('content_type') or rj.get('content_type')
    if ct:
        digest_parts.append(f"type: {ct}")
    fid = obs.get('file_id') or rj.get('file_id')
    if fid:
        digest_parts.append(f"file_id: {fid}")
    rc = obs.get('row_count') or rj.get('row_count')
    if rc:
        digest_parts.append(f"{rc} rows")
    return "; ".join(digest_parts)


def _digest_excel_tool(tool_execution) -> str:
    """Digest for Excel bridge tools.

    Covers write_officejs_code, write_to_excel, read_excel_range, and
    read_excel_as_csv. Returns empty string when the tool isn't one of these
    so callers can fall through.

    Why: without this the planner sees only the generic result_summary
    (truncated to 60 chars), losing return_value/data — so multi-turn
    reasoning over Excel has to re-run every probe.
    """
    name = tool_execution.tool_name
    if name not in ('write_officejs_code', 'write_to_excel', 'read_excel_range', 'read_excel_as_csv'):
        return ""
    rj = tool_execution.result_json or {}
    parts: list[str] = []

    if name == 'write_officejs_code':
        args = getattr(tool_execution, 'arguments_json', None) or {}
        desc = args.get('description')
        if desc:
            parts.append(f"desc: {str(desc)[:80]}")
        if rj.get('success') is False:
            err = rj.get('error') or 'unknown error'
            parts.append(f"FAILED: {str(err)[:160]}")
            return "; ".join(parts)
        rv = rj.get('return_value')
        if rv is not None:
            try:
                rv_str = json.dumps(rv, default=str)
            except Exception:
                rv_str = str(rv)
            if len(rv_str) > 400:
                rv_str = rv_str[:400] + '…'
            parts.append(f"return_value: {rv_str}")
        ranges = rj.get('ranges_touched') or []
        if ranges:
            shown = ', '.join(str(r) for r in ranges[:5])
            if len(ranges) > 5:
                shown += f'… +{len(ranges) - 5}'
            parts.append(f"ranges: {shown}")
        return "; ".join(parts)

    if name == 'write_to_excel':
        if rj.get('success') is False:
            err = rj.get('error_message') or rj.get('error') or 'unknown error'
            return f"FAILED: {str(err)[:160]}"
        rc = rj.get('row_count')
        cc = rj.get('column_count')
        if rc is not None and cc is not None:
            return f"wrote {rc} rows × {cc} cols"
        return ""

    if name == 'read_excel_range':
        if rj.get('success') is False:
            err = rj.get('error') or 'unknown error'
            return f"FAILED: {str(err)[:160]}"
        ranges = rj.get('ranges') or []
        for r in ranges[:3]:
            addr = r.get('address', '?')
            rc = r.get('row_count', 0)
            cc = r.get('col_count', 0)
            parts.append(f"{addr} ({rc}×{cc})")
            vals = r.get('values')
            if vals is not None:
                try:
                    vs = json.dumps(vals, default=str)
                except Exception:
                    vs = str(vals)
                if len(vs) > 400:
                    vs = vs[:400] + '…'
                parts.append(f"values: {vs}")
        if len(ranges) > 3:
            parts.append(f"(+{len(ranges) - 3} more ranges)")
        if rj.get('truncated'):
            parts.append("TRUNCATED (cell_limit hit)")
        return "; ".join(parts)

    if name == 'read_excel_as_csv':
        if rj.get('success') is False:
            err = rj.get('error') or 'unknown error'
            return f"FAILED: {str(err)[:160]}"
        rc = rj.get('row_count')
        cc = rj.get('col_count')
        if rc is not None and cc is not None:
            parts.append(f"{rc}×{cc}")
        fid = rj.get('file_id')
        if fid:
            parts.append(f"file_id: {fid}")
        fname = rj.get('file_name')
        if fname:
            parts.append(f"file: {fname}")
        csv = rj.get('csv') or ''
        if csv:
            snippet = csv if len(csv) <= 500 else csv[:500] + '…'
            parts.append(f"csv:\n{snippet}")
        if rj.get('truncated'):
            parts.append("TRUNCATED")
        return "; ".join(parts)

    return ""


def _digest_scheduled_tool(tool_execution) -> str:
    """One-line digest for scheduled-task tools.

    Records in the conversation history what was scheduled / edited / cancelled
    (task id + cron), so the planner can dedupe new schedules and edit or cancel
    the right task on a follow-up turn. Returns an empty string for other tools so callers can
    fall through to the next elif. The active tasks themselves are listed in the
    <scheduled_tasks> context section.
    """
    name = tool_execution.tool_name
    rj = tool_execution.result_json or {}
    if name == 'create_scheduled_task':
        parts = []
        if rj.get('task_id'):
            parts.append(f"task_id: {rj.get('task_id')}")
        if rj.get('cron_schedule'):
            parts.append(f"cron: {rj.get('cron_schedule')}")
        if rj.get('error'):
            parts.append(f"error: {rj.get('error')}")
        return "; ".join(parts)
    if name == 'cancel_scheduled_task':
        parts = []
        if rj.get('task_id'):
            parts.append(f"task_id: {rj.get('task_id')}")
        if rj.get('error'):
            parts.append(f"error: {rj.get('error')}")
        return "; ".join(parts) if parts else "cancelled"
    if name == 'edit_scheduled_task':
        parts = []
        if rj.get('task_id'):
            parts.append(f"task_id: {rj.get('task_id')}")
        if rj.get('cron_schedule'):
            parts.append(f"cron: {rj.get('cron_schedule')}")
        if rj.get('is_active') is not None:
            parts.append(f"active: {rj.get('is_active')}")
        if rj.get('error'):
            parts.append(f"error: {rj.get('error')}")
        return "; ".join(parts) if parts else "edited"
    return ""


def _digest_notification_tool(tool_execution) -> str:
    """One-line digest for notification tools (send_email today; Slack/Teams
    later) so the conversation history records that the user was notified, with
    recipient + subject. Returns an empty string for other tools so callers can
    fall through to the next elif.
    """
    name = tool_execution.tool_name
    rj = tool_execution.result_json or {}
    if name == 'send_email':
        parts = []
        if rj.get('recipient'):
            parts.append(f"to: {rj.get('recipient')}")
        if rj.get('subject'):
            subj = str(rj.get('subject'))
            parts.append(f"subject: {subj[:80]}{'…' if len(subj) > 80 else ''}")
        atts = rj.get('attachments') or []
        if atts:
            sent = [a for a in atts if a.get('success')]
            failed = [a for a in atts if not a.get('success')]
            names = ", ".join((a.get('filename') or a.get('ref_id') or '?') for a in sent[:5])
            if names:
                parts.append(f"attached: {names}")
            if failed:
                parts.append(f"attach_failed: {len(failed)}")
        if rj.get('error'):
            parts.append(f"error: {rj.get('error')}")
        return "; ".join(parts)
    if name == 'notify':
        parts = []
        results = rj.get('results') or []
        reached = [r for r in results if r.get('delivered')]
        if reached:
            who = ", ".join(r.get('email', '?') for r in reached[:5])
            parts.append(f"notified: {who}")
        if rj.get('subject'):
            subj = str(rj.get('subject'))
            parts.append(f"subject: {subj[:80]}{'…' if len(subj) > 80 else ''}")
        if rj.get('rejected'):
            parts.append(f"skipped_non_members: {', '.join(rj.get('rejected'))}")
        if rj.get('error'):
            parts.append(f"error: {rj.get('error')}")
        return "; ".join(parts) if parts else "notified"
    return ""


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

    async def _load_tool_execution_for_context(self, tool_execution_id: str):
        """Load only the tool result fields needed for conversation context.

        create_data can carry full result rows in result_json.data. Message
        history only needs stats/preview/model metadata, so project those
        fields on Postgres and avoid hydrating the full rows JSON.
        """
        base_result = await self.db.execute(
            select(
                ToolExecution.id,
                ToolExecution.tool_name,
                ToolExecution.tool_action,
                ToolExecution.status,
                ToolExecution.result_summary,
                ToolExecution.created_widget_id,
                ToolExecution.created_step_id,
                ToolExecution.error_message,
            )
            .where(ToolExecution.id == tool_execution_id)
        )
        row = base_result.first()
        if not row:
            return None

        tool_execution = SimpleNamespace(
            id=row.id,
            tool_name=row.tool_name,
            tool_action=row.tool_action,
            status=row.status,
            result_summary=row.result_summary,
            created_widget_id=row.created_widget_id,
            created_step_id=row.created_step_id,
            error_message=row.error_message,
            result_json=None,
        )

        if tool_execution.tool_name == "create_data":
            tool_execution.result_json = await self._load_create_data_result_projection(tool_execution_id)
        else:
            result = await self.db.execute(
                select(ToolExecution.result_json).where(ToolExecution.id == tool_execution_id)
            )
            tool_execution.result_json = _json_value(result.scalar_one_or_none())

        return tool_execution

    async def _load_create_data_result_projection(self, tool_execution_id: str) -> Dict[str, Any]:
        if not _is_postgres_session(self.db):
            result = await self.db.execute(
                select(ToolExecution.result_json).where(ToolExecution.id == tool_execution_id)
            )
            return _json_value(result.scalar_one_or_none()) or {}

        stmt = text(
            """
            SELECT json_build_object(
                'success', result_json->'success',
                'data_preview', result_json->'data_preview',
                'stats', result_json->'stats',
                'data_model', result_json->'data_model',
                'view', result_json->'view',
                'created_visualization_ids', result_json->'created_visualization_ids',
                'query_id', result_json->'query_id',
                'query_timings', result_json->'query_timings',
                'codegen_ms', result_json->'codegen_ms',
                'execution_ms', result_json->'execution_ms',
                'errors', result_json->'errors'
            ) AS result_json
            FROM tool_executions
            WHERE id = :tool_execution_id
            """
        )
        result = await self.db.execute(stmt, {"tool_execution_id": tool_execution_id})
        return _json_value(result.scalar_one_or_none()) or {}
    
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
                    
        # Fetch only the most recent window instead of ALL completions then
        # slicing [-max_messages:] in Python (was O(conversation length) every
        # iteration). +1 covers dropping a trailing in-progress user completion.
        report_completions = (await self.db.execute(
            select(Completion)
            .filter(Completion.report_id == self.report.id)
            .order_by(Completion.created_at.desc())
            .limit(max_messages + 1)
        )).scalars().all()
        report_completions = list(reversed(report_completions))  # restore chronological order

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
                in_knowledge_wrap = False

                # Collect reasoning, assistant messages, and tool executions from blocks
                for block in blocks:
                    # Harness blocks use loop_index >= 1000 (see agent_v2._run_knowledge_harness).
                    # Wrap contiguous harness blocks in <post_analysis_knowledge_update>…</…>
                    # so the LLM sees them labeled as knowledge-base updates, not object-level work.
                    is_harness = (block.loop_index or 0) >= 1000
                    if is_harness and not in_knowledge_wrap:
                        system_parts.append("<post_analysis_knowledge_update>")
                        in_knowledge_wrap = True
                    elif (not is_harness) and in_knowledge_wrap:
                        system_parts.append("</post_analysis_knowledge_update>")
                        in_knowledge_wrap = False
                    # Don't truncate reasoning and content - show full text
                    if block.reasoning and block.reasoning.strip():
                        system_parts.append(f"Thinking: {block.reasoning.strip()}")
                    
                    if block.content and block.content.strip():
                        system_parts.append(f"Response: {block.content.strip()}")
                    
                    # Add tool execution details if available
                    if block.tool_execution_id:
                        tool_execution = await self._load_tool_execution_for_context(block.tool_execution_id)
                        
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
                                        head_cols = ", ".join(col_names[:10])
                                        digest_parts.append(f"cols: {head_cols}{'…' if len(col_names) > 10 else ''}")
                                    if sample_row:
                                        try:
                                            digest_parts.append(f"top row: {json.dumps(sample_row)}")
                                        except Exception:
                                            pass
                                    tool_info += " - " + "; ".join(digest_parts)
                                # Digest for create_data results (same style as create_widget)
                                elif tool_execution.tool_name == 'create_data' and tool_execution.result_json:
                                    rj = tool_execution.result_json or {}
                                    preview = rj.get('data_preview') or {}
                                    stats = rj.get('stats') or {}
                                    data_obj = rj.get('data') or {}
                                    columns = preview.get('columns') or data_obj.get('columns', []) or []
                                    rows = data_obj.get('rows', []) or []
                                    preview_rows = preview.get('rows') or []
                                    col_names = [
                                        (c.get('field') or c.get('headerName'))
                                        for c in columns
                                        if isinstance(c, dict) and (c.get('field') or c.get('headerName'))
                                    ]
                                    row_count = (
                                        stats.get('total_rows')
                                        or preview.get('row_count')
                                        or len(rows)
                                        or len(preview_rows)
                                    )
                                    sample_row = None
                                    if allow_llm_see_data:
                                        if preview_rows:
                                            sample_row = preview_rows[0]
                                        elif rows:
                                            sample_row = rows[0]
                                    digest_parts = [f"{row_count} rows × {len(col_names)} cols"]
                                    if col_names:
                                        head_cols = ", ".join(col_names[:10])
                                        digest_parts.append(f"cols: {head_cols}{'…' if len(col_names) > 10 else ''}")
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
                                elif tool_execution.tool_name == 'read_query' and tool_execution.result_json:
                                    rj = tool_execution.result_json or {}
                                    digest_parts = []
                                    if rj.get('title'):
                                        digest_parts.append(f"query: {rj.get('title')}")
                                    if rj.get('query_id'):
                                        digest_parts.append(f"query_id: {rj.get('query_id')}")
                                    if rj.get('visualization_id'):
                                        digest_parts.append(f"viz_id: {rj.get('visualization_id')}")
                                    dp = rj.get('data_preview') or {}
                                    if dp.get('rows') or dp.get('row_count'):
                                        rc = len(dp.get('rows', [])) if dp.get('rows') else dp.get('row_count', 0)
                                        cols = dp.get('columns', [])
                                        col_names = [
                                            (c.get('field') or c.get('headerName'))
                                            for c in cols
                                            if isinstance(c, dict) and (c.get('field') or c.get('headerName'))
                                        ]
                                        digest_parts.append(f"{rc} rows × {len(col_names)} cols")
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
                                elif tool_execution.tool_name == 'edit_artifact' and tool_execution.result_json:
                                    rj = tool_execution.result_json or {}
                                    digest_parts = []
                                    if rj.get('title'):
                                        digest_parts.append(f"artifact: {rj.get('title')}")
                                    if rj.get('mode'):
                                        digest_parts.append(f"mode: {rj.get('mode')}")
                                    if rj.get('artifact_id'):
                                        digest_parts.append(f"artifact_id: {rj.get('artifact_id')}")
                                    # Surface visualization_ids (top-level or nested in artifact_preview)
                                    viz_ids = rj.get('visualization_ids') or (rj.get('artifact_preview') or {}).get('visualization_ids') or []
                                    if viz_ids:
                                        digest_parts.append(f"viz_ids: {', '.join(viz_ids)}")
                                    if rj.get('version'):
                                        digest_parts.append(f"v{rj.get('version')}")
                                    if rj.get('diff_applied') is not None:
                                        digest_parts.append("diff" if rj.get('diff_applied') else "rewrite")
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
                                    viz_ids = rj.get('visualization_ids') or []
                                    if viz_ids:
                                        digest_parts.append(f"viz_ids: {', '.join(viz_ids)}")
                                    if rj.get('version'):
                                        digest_parts.append(f"v{rj.get('version')}")
                                    if digest_parts:
                                        tool_info += " - " + "; ".join(digest_parts)
                                elif tool_execution.tool_name == 'inspect_data' and tool_execution.result_json:
                                    rj = tool_execution.result_json or {}
                                    digest_parts = []
                                    obs = rj.get('observation') or rj
                                    summary = obs.get('summary') or rj.get('summary')
                                    if summary:
                                        digest_parts.append(summary)
                                    if obs.get('success') is False or rj.get('success') is False:
                                        digest_parts.append("FAILED")
                                    dur = obs.get('execution_duration_ms') or rj.get('execution_duration_ms')
                                    if dur:
                                        digest_parts.append(f"{dur}ms")
                                    if digest_parts:
                                        tool_info += " - " + "; ".join(digest_parts)
                                elif tool_execution.tool_name == 'search_mcps' and tool_execution.result_json:
                                    rj = tool_execution.result_json or {}
                                    obs = rj.get('observation') or rj
                                    digest_parts = []
                                    summary = obs.get('summary') or ''
                                    if summary:
                                        digest_parts.append(summary)
                                    tools_list = obs.get('tools') or rj.get('tools') or []
                                    if tools_list:
                                        by_conn = {}
                                        for t in tools_list:
                                            conn_label = t.get('connection_name') or t.get('connection_id') or 'unknown'
                                            by_conn.setdefault(conn_label, []).append(t.get('name', '?'))
                                        for conn_label, names in by_conn.items():
                                            entry = f"{conn_label}: {', '.join(names[:5])}"
                                            if len(names) > 5:
                                                entry += f"… +{len(names)-5}"
                                            digest_parts.append(entry)
                                    if digest_parts:
                                        tool_info += " - " + "; ".join(digest_parts)
                                elif tool_execution.tool_name == 'execute_mcp' and tool_execution.result_json:
                                    digest = _digest_execute_mcp(tool_execution)
                                    if digest:
                                        tool_info += " - " + digest
                                elif tool_execution.tool_name in ('search_instructions', 'create_instruction', 'edit_instruction') and tool_execution.result_json:
                                    digest = _digest_knowledge_tool(tool_execution)
                                    if digest:
                                        tool_info += " - " + digest
                                elif tool_execution.tool_name in ('search_evals', 'create_eval', 'run_eval') and tool_execution.result_json:
                                    digest = _digest_eval_tool(tool_execution)
                                    if digest:
                                        tool_info += " - " + digest
                                elif tool_execution.tool_name in ('write_officejs_code', 'write_to_excel', 'read_excel_range', 'read_excel_as_csv') and tool_execution.result_json:
                                    digest = _digest_excel_tool(tool_execution)
                                    if digest:
                                        tool_info += " - " + digest
                                elif tool_execution.tool_name in ('create_scheduled_task', 'cancel_scheduled_task', 'edit_scheduled_task') and tool_execution.result_json:
                                    digest = _digest_scheduled_tool(tool_execution)
                                    if digest:
                                        tool_info += " - " + digest
                                elif tool_execution.tool_name in ('send_email', 'notify') and tool_execution.result_json:
                                    digest = _digest_notification_tool(tool_execution)
                                    if digest:
                                        tool_info += " - " + digest
                                elif tool_execution.tool_name in ('write_csv', 'materialize') and tool_execution.result_json:
                                    rj = tool_execution.result_json or {}
                                    obs = rj.get('observation') or rj
                                    digest_parts = []
                                    summary = obs.get('summary') or ''
                                    if summary:
                                        digest_parts.append(summary)
                                    fname = rj.get('file_name') or ''
                                    if fname:
                                        digest_parts.append(f"file: {fname}")
                                    fid = rj.get('file_id') or obs.get('file_id')
                                    if fid:
                                        digest_parts.append(f"file_id: {fid}")
                                    # Surface visualization IDs
                                    viz_ids = rj.get('created_visualization_ids') or []
                                    if viz_ids:
                                        digest_parts.append(f"visualization_ids: {viz_ids}")
                                    rc = obs.get('row_count') or rj.get('row_count')
                                    if rc:
                                        digest_parts.append(f"{rc} rows")
                                    cols = obs.get('columns') or rj.get('columns') or []
                                    if cols:
                                        digest_parts.append(f"columns: {cols}")
                                    # Include a data sample from execution log
                                    exec_log = rj.get('execution_log') or ''
                                    if exec_log:
                                        sample_lines = exec_log.strip().split('\n')[:8]
                                        digest_parts.append(f"sample:\n" + "\n".join(sample_lines))
                                    if obs.get('success') is False:
                                        digest_parts.append("FAILED")
                                    if digest_parts:
                                        tool_info += " - " + "; ".join(digest_parts)
                                elif tool_execution.tool_name == 'list_agent_executions' and tool_execution.result_json:
                                    rj = tool_execution.result_json or {}
                                    obs = rj.get('observation') or rj
                                    digest_parts = []
                                    summary = obs.get('summary') or ''
                                    if summary:
                                        digest_parts.append(summary)
                                    else:
                                        arts = obs.get('artifacts') or []
                                        if arts:
                                            art = arts[0]
                                            count = art.get('count')
                                            total = art.get('total')
                                            filt = art.get('filter') or 'all'
                                            if count is not None:
                                                digest_parts.append(f"Listed {count} (filter={filt}, total={total})")
                                    if digest_parts:
                                        tool_info += " - " + "; ".join(digest_parts)
                                elif tool_execution.tool_name == 'web_fetch' and tool_execution.result_json:
                                    rj = tool_execution.result_json or {}
                                    obs = rj.get('observation') or rj
                                    out = rj.get('output') or rj
                                    digest_parts = []
                                    title = out.get('title')
                                    if title:
                                        digest_parts.append(f'"{str(title)[:80]}"')
                                    summary = obs.get('summary') or ''
                                    if summary:
                                        digest_parts.append(summary)
                                    if digest_parts:
                                        tool_info += " - " + "; ".join(digest_parts)
                                elif tool_execution.tool_name == 'web_search':
                                    digest = _digest_web_search(tool_execution)
                                    if digest:
                                        tool_info += " - " + digest
                                elif tool_execution.created_widget_id:
                                    # Only the title is used — project it instead of
                                    # loading the full Widget row.
                                    widget_title = (await self.db.execute(
                                        select(Widget.title).filter(Widget.id == tool_execution.created_widget_id)
                                    )).scalar_one_or_none()
                                    if widget_title is not None:
                                        tool_info += f" - Widget: '{widget_title}'"
                                    else:
                                        tool_info += f" - Widget #{tool_execution.created_widget_id}"

                                elif tool_execution.created_step_id:
                                    # Only the title is used — project it instead of
                                    # loading the full Step row (code + data blobs).
                                    step_title = (await self.db.execute(
                                        select(Step.title).filter(Step.id == tool_execution.created_step_id)
                                    )).scalar_one_or_none()
                                    if step_title is not None:
                                        tool_info += f" - Step: '{step_title}'"
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

                if in_knowledge_wrap:
                    system_parts.append("</post_analysis_knowledge_update>")
                    in_knowledge_wrap = False

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

        # Most-recent window only (was fetch-all + slice in Python).
        report_completions = (await self.db.execute(
            select(Completion)
            .filter(Completion.report_id == self.report.id)
            .order_by(Completion.created_at.desc())
            .limit(max_messages + 1)
        )).scalars().all()
        report_completions = list(reversed(report_completions))

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
                # Only ds.id and ds.name are read downstream — suppress the
                # model-level lazy="selectin" cascade (reports → widgets/
                # queries/completions/…) that would otherwise fire per row.
                from sqlalchemy.orm import lazyload
                rows = await self.db.execute(
                    select(DataSource).where(DataSource.id.in_(list(ds_ids)))
                    .options(lazyload("*"))
                )
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
                    from sqlalchemy.orm import lazyload
                    rows2 = await self.db.execute(
                        select(DataSource).where(DataSource.id.in_(missing_ds))
                        .options(lazyload("*"))
                    )
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
                in_knowledge_wrap = False
                for block in blocks:
                    is_harness = (block.loop_index or 0) >= 1000
                    if is_harness and not in_knowledge_wrap:
                        system_parts.append("<post_analysis_knowledge_update>")
                        in_knowledge_wrap = True
                    elif (not is_harness) and in_knowledge_wrap:
                        system_parts.append("</post_analysis_knowledge_update>")
                        in_knowledge_wrap = False
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
                                    head_cols = ", ".join(col_names[:10])
                                    digest_parts.append(f"cols: {head_cols}{'…' if len(col_names) > 10 else ''}")
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
                                    head_cols = ", ".join(col_names[:10])
                                    digest_parts.append(f"cols: {head_cols}{'…' if len(col_names) > 10 else ''}")
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
                            elif tool_execution.status == 'success' and tool_execution.tool_name == 'read_query' and tool_execution.result_json:
                                rj = tool_execution.result_json or {}
                                digest_parts = []
                                if rj.get('title'):
                                    digest_parts.append(f"query: {rj.get('title')}")
                                if rj.get('query_id'):
                                    digest_parts.append(f"query_id: {rj.get('query_id')}")
                                if rj.get('visualization_id'):
                                    digest_parts.append(f"viz_id: {rj.get('visualization_id')}")
                                dp = rj.get('data_preview') or {}
                                if dp.get('rows') or dp.get('row_count'):
                                    rc = len(dp.get('rows', [])) if dp.get('rows') else dp.get('row_count', 0)
                                    cols = dp.get('columns', [])
                                    col_names = [
                                        (c.get('field') or c.get('headerName'))
                                        for c in cols
                                        if isinstance(c, dict) and (c.get('field') or c.get('headerName'))
                                    ]
                                    digest_parts.append(f"{rc} rows × {len(col_names)} cols")
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
                            elif tool_execution.status == 'success' and tool_execution.tool_name == 'edit_artifact' and tool_execution.result_json:
                                rj = tool_execution.result_json or {}
                                digest_parts = []
                                if rj.get('title'):
                                    digest_parts.append(f"artifact: {rj.get('title')}")
                                if rj.get('mode'):
                                    digest_parts.append(f"mode: {rj.get('mode')}")
                                if rj.get('artifact_id'):
                                    digest_parts.append(f"artifact_id: {rj.get('artifact_id')}")
                                # Surface visualization_ids (top-level or nested in artifact_preview)
                                viz_ids = rj.get('visualization_ids') or (rj.get('artifact_preview') or {}).get('visualization_ids') or []
                                if viz_ids:
                                    digest_parts.append(f"viz_ids: {', '.join(viz_ids)}")
                                if rj.get('version'):
                                    digest_parts.append(f"v{rj.get('version')}")
                                if rj.get('diff_applied') is not None:
                                    digest_parts.append("diff" if rj.get('diff_applied') else "rewrite")
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
                                viz_ids = rj.get('visualization_ids') or []
                                if viz_ids:
                                    digest_parts.append(f"viz_ids: {', '.join(viz_ids)}")
                                if rj.get('version'):
                                    digest_parts.append(f"v{rj.get('version')}")
                                if digest_parts:
                                    tool_info += " - " + "; ".join(digest_parts)
                            elif tool_execution.tool_name == 'inspect_data' and tool_execution.result_json:
                                rj = tool_execution.result_json or {}
                                digest_parts = []
                                obs = rj.get('observation') or rj
                                summary = obs.get('summary') or rj.get('summary')
                                if summary:
                                    digest_parts.append(summary)
                                if obs.get('success') is False or rj.get('success') is False:
                                    digest_parts.append("FAILED")
                                dur = obs.get('execution_duration_ms') or rj.get('execution_duration_ms')
                                if dur:
                                    digest_parts.append(f"{dur}ms")
                                if digest_parts:
                                    tool_info += " - " + "; ".join(digest_parts)
                            elif tool_execution.tool_name == 'search_mcps' and tool_execution.result_json:
                                rj = tool_execution.result_json or {}
                                obs = rj.get('observation') or rj
                                digest_parts = []
                                summary = obs.get('summary') or ''
                                if summary:
                                    digest_parts.append(summary)
                                tools_list = obs.get('tools') or rj.get('tools') or []
                                if tools_list:
                                    by_conn = {}
                                    for t in tools_list:
                                        conn_label = t.get('connection_name') or t.get('connection_id') or 'unknown'
                                        by_conn.setdefault(conn_label, []).append(t.get('name', '?'))
                                    for conn_label, names in by_conn.items():
                                        entry = f"{conn_label}: {', '.join(names[:5])}"
                                        if len(names) > 5:
                                            entry += f"… +{len(names)-5}"
                                        digest_parts.append(entry)
                                if digest_parts:
                                    tool_info += " - " + "; ".join(digest_parts)
                            elif tool_execution.tool_name == 'execute_mcp' and tool_execution.result_json:
                                digest = _digest_execute_mcp(tool_execution)
                                if digest:
                                    tool_info += " - " + digest
                            elif tool_execution.tool_name in ('search_instructions', 'create_instruction', 'edit_instruction') and tool_execution.result_json:
                                digest = _digest_knowledge_tool(tool_execution)
                                if digest:
                                    tool_info += " - " + digest
                            elif tool_execution.tool_name in ('search_evals', 'create_eval', 'run_eval') and tool_execution.result_json:
                                digest = _digest_eval_tool(tool_execution)
                                if digest:
                                    tool_info += " - " + digest
                            elif tool_execution.tool_name in ('write_officejs_code', 'write_to_excel', 'read_excel_range', 'read_excel_as_csv') and tool_execution.result_json:
                                digest = _digest_excel_tool(tool_execution)
                                if digest:
                                    tool_info += " - " + digest
                            elif tool_execution.tool_name in ('create_scheduled_task', 'cancel_scheduled_task', 'edit_scheduled_task') and tool_execution.result_json:
                                digest = _digest_scheduled_tool(tool_execution)
                                if digest:
                                    tool_info += " - " + digest
                            elif tool_execution.tool_name in ('send_email', 'notify') and tool_execution.result_json:
                                digest = _digest_notification_tool(tool_execution)
                                if digest:
                                    tool_info += " - " + digest
                            elif tool_execution.tool_name in ('write_csv', 'materialize') and tool_execution.result_json:
                                rj = tool_execution.result_json or {}
                                obs = rj.get('observation') or rj
                                digest_parts = []
                                summary = obs.get('summary') or ''
                                if summary:
                                    digest_parts.append(summary)
                                fname = rj.get('file_name') or ''
                                if fname:
                                    digest_parts.append(f"file: {fname}")
                                fid = rj.get('file_id') or obs.get('file_id')
                                if fid:
                                    digest_parts.append(f"file_id: {fid}")
                                # Surface visualization IDs
                                viz_ids = rj.get('created_visualization_ids') or []
                                if viz_ids:
                                    digest_parts.append(f"visualization_ids: {viz_ids}")
                                rc = obs.get('row_count') or rj.get('row_count')
                                if rc:
                                    digest_parts.append(f"{rc} rows")
                                cols = obs.get('columns') or rj.get('columns') or []
                                if cols:
                                    digest_parts.append(f"columns: {cols}")
                                # Include a data sample from execution log
                                exec_log = rj.get('execution_log') or ''
                                if exec_log:
                                    sample_lines = exec_log.strip().split('\n')[:8]
                                    digest_parts.append(f"sample:\n" + "\n".join(sample_lines))
                                if obs.get('success') is False:
                                    digest_parts.append("FAILED")
                                if digest_parts:
                                    tool_info += " - " + "; ".join(digest_parts)
                            elif tool_execution.tool_name == 'list_agent_executions' and tool_execution.result_json:
                                rj = tool_execution.result_json or {}
                                obs = rj.get('observation') or rj
                                digest_parts = []
                                summary = obs.get('summary') or ''
                                if summary:
                                    digest_parts.append(summary)
                                else:
                                    arts = obs.get('artifacts') or []
                                    if arts:
                                        art = arts[0]
                                        count = art.get('count')
                                        total = art.get('total')
                                        filt = art.get('filter') or 'all'
                                        if count is not None:
                                            digest_parts.append(f"Listed {count} (filter={filt}, total={total})")
                                if digest_parts:
                                    tool_info += " - " + "; ".join(digest_parts)
                            elif tool_execution.tool_name == 'web_fetch' and tool_execution.result_json:
                                rj = tool_execution.result_json or {}
                                obs = rj.get('observation') or rj
                                out = rj.get('output') or rj
                                digest_parts = []
                                title = out.get('title')
                                if title:
                                    digest_parts.append(f'"{str(title)[:80]}"')
                                summary = obs.get('summary') or ''
                                if summary:
                                    digest_parts.append(summary)
                                if digest_parts:
                                    tool_info += " - " + "; ".join(digest_parts)
                            elif tool_execution.tool_name == 'web_search':
                                digest = _digest_web_search(tool_execution)
                                if digest:
                                    tool_info += " - " + digest
                            elif tool_execution.status == 'error' and tool_execution.error_message:
                                error = tool_execution.error_message
                                if len(error) > 50:
                                    error = error[:50] + "..."
                                tool_info += f" - Error: {error}"
                            system_parts.append(tool_info)
                if in_knowledge_wrap:
                    system_parts.append("</post_analysis_knowledge_update>")
                    in_knowledge_wrap = False
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
