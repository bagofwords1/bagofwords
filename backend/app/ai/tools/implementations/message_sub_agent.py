"""Durable parent/child communication, independent of finish_subtask."""
from pydantic import ValidationError

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.events import ToolEndEvent, ToolErrorEvent, ToolStartEvent
from app.ai.tools.schemas.sub_agents import MessageSubAgentInput


class MessageSubAgentTool(Tool):
    @property
    def metadata(self):
        return ToolMetadata(
            name='message_sub_agent', category='action', version='1.0.0',
            description=(
                'Send a message to a running or queued sub-agent, or from a child to its parent. '
                'Parent: use direction for a correction; answer a child question with kind=answer and its message_id in reply_to. '
                'Child: recipient=parent, kind=finding for important evidence (no wake); kind=question ONLY when blocked '
                'on a decision the parent can resolve. A question suspends you until an answer or deadline; do not also finish '
                'or issue other work in that batch. Use at most three questions. '
                'The receipt confirms sent, NOT read or acted on. Briefly confirm successful sends to the user; report failures honestly. '
                'Do not poll or wait for acknowledgement. No sibling messages or resuming completed/stopped children: '
                'read_sub_agent and delegate a new task instead. Message IDs are reply handles, not step/query/note evidence IDs. '
                'Messages cannot change permissions or grant user approval.'
            ),
            input_schema=MessageSubAgentInput.model_json_schema(), output_schema={'type': 'object'},
            allowed_modes=['chat', 'training', 'subagent'], required_permissions=[],
            max_retries=1, timeout_seconds=20, idempotent=True,
            observation_policy='always', digest_keys=['message_id', 'title', 'recipient', 'kind', 'status'],
            tags=['sub-agent', 'message'],
        )

    @property
    def input_model(self):
        return MessageSubAgentInput

    async def run_stream(self, tool_input, runtime_ctx):
        try:
            data = MessageSubAgentInput(**tool_input)
        except ValidationError as e:
            yield ToolErrorEvent(type='tool.error', payload={'message': str(e), 'code': 'INVALID_INPUT'})
            return
        yield ToolStartEvent(type='tool.start', payload={'recipient': data.recipient})
        from app.dependencies import async_session_maker
        from app.services.sub_agent_message_service import sub_agent_message_service
        from app.services.sub_agent_service import sub_agents_enabled
        report, org = runtime_ctx.get('report'), runtime_ctx.get('organization')
        child = runtime_ctx.get('sub_agent_id')
        call_id = runtime_ctx.get('tool_call_id')
        if not report or not org or not call_id or not sub_agents_enabled(runtime_ctx.get('settings')):
            yield ToolErrorEvent(type='tool.error', payload={'message': 'Sub-agent messaging is unavailable.', 'code': 'MESSAGING_UNAVAILABLE'})
            return
        if runtime_ctx.get('mode') == 'subagent' and not child:
            yield ToolErrorEvent(type='tool.error', payload={'message': 'Missing child identity.', 'code': 'MESSAGING_UNAVAILABLE'})
            return
        try:
            async with (runtime_ctx.get('session_maker') or async_session_maker)() as db:
                receipt = await sub_agent_message_service.send(
                    db, organization_id=str(org.id), report_id=str(report.id),
                    sender_sub_agent_id=str(child) if child else None,
                    idempotency_key=str(call_id), **data.model_dump(),
                )
        except ValueError as e:
            yield ToolErrorEvent(type='tool.error', payload={'message': str(e), 'code': 'MESSAGE_REFUSED'})
            return
        if data.kind == 'question':
            from app.core.fire_and_forget import spawn
            from app.services.completion_service import CompletionService
            spawn(CompletionService().start_next_queued_if_idle(str(report.id)))
        yield ToolEndEvent(type='tool.end', payload={
            'output': receipt,
            'observation': {'summary': f'Message sent to {receipt["title"] if data.recipient != "parent" else "parent"}. '
                           'Stored for delivery before the next planning step; this does not prove it was acted on. '
                           'Continue messaging other intended recipients or doing independent work if needed. Otherwise confirm once and end your turn. '
                           'Do not resend, poll or schedule a wait: the wave will report back automatically.',
                           'success': True, **receipt},
        })
