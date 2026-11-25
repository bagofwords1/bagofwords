from sqlalchemy import Column, Integer, String, ForeignKey, Text, JSON, event, UUID, DateTime
from sqlalchemy.orm import relationship, selectinload
from sqlalchemy import select, inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import DateTime
from datetime import datetime
from .base import BaseSchema
import asyncio
from app.core.fire_and_forget import spawn
from app.streaming.completion_event_bus import websocket_manager
import json
from app.models.mention import MentionType
from sqlalchemy.orm.exc import DetachedInstanceError
from app.ee.encryption import EncryptedJSON

# Add these imports for the new functionality
from app.settings.database import create_async_session_factory
from app.services.platform_adapters.adapter_factory import PlatformAdapterFactory
from app.models.external_platform import ExternalPlatform
from app.settings.logging_config import get_logger

logger = get_logger(__name__)


class Completion(BaseSchema):
    __tablename__ = 'completions'

    # User and assistant text restate query results, so both are encrypted at
    # rest (enterprise data_encryption). Anything that searches these columns
    # in SQL must handle ciphertext — see ReportService.get_reports.
    prompt = Column(EncryptedJSON, nullable=False, default="")
    completion = Column(EncryptedJSON, nullable=False, default="")

    status = Column(String, nullable=False, default='success')
    model = Column(String, nullable=False, default='gpt4o')
    turn_index = Column(Integer, nullable=False, default=0)
    feedback_score = Column(Integer, nullable=False, default=0)
    sigkill = Column(DateTime, nullable=False, default=None)

    parent_id = Column(String(36), ForeignKey('completions.id'), nullable=True)

    # message type
    message_type = Column(String, nullable=False, default='ai_completion')

    # Steering rows only (message_type='steering'): when the agent picked the
    # steer up, and the loop step that first acted on it. The chat renders the
    # steer right above the first block with loop_index >= steering_loop_index.
    # NULL while pending / never picked up -> send-time placement.
    steering_applied_at = Column(DateTime, nullable=True, default=None)
    steering_loop_index = Column(Integer, nullable=True, default=None)
    role = Column(String, nullable=False, default='system')

    # report
    report_id = Column(String(36), ForeignKey('reports.id'), nullable=False)
    report = relationship("Report", back_populates="completions", lazy='selectin')

    # widget - optional
    widget_id = Column(String(36), ForeignKey('widgets.id'), nullable=True)
    widget = relationship("Widget", back_populates="completions", lazy='selectin')

    # step - optional
    step_id = Column(String(36), ForeignKey('steps.id'), nullable=True, index=True)  # dashfc04: deleting a release's steps checks it
    step = relationship("Step", back_populates="completions", lazy='select')

    user_id = Column(String(36), ForeignKey('users.id'), nullable=True)
    user = relationship("User", back_populates="completions", lazy='select')

    main_router = Column(String, nullable=False, default='table')

    instructions_effectiveness = Column(Integer, nullable=True, default=4)
    context_effectiveness = Column(Integer, nullable=True, default=4)
    response_score = Column(Integer, nullable=True, default=4)  # 1-5 rating of AI performance, where 1=poor and 5=excellent

    # LLM judge's per-dimension score + free-text rationale. Single JSON blob so
    # the shape can evolve; the scalar score columns above stay as the queryable
    # source for diagnosis filters/metrics (and the display fallback). Only
    # populated when the judge actually ran. Shape:
    #   {"instructions": {"score": int, "reasoning": str},
    #    "context":      {"score": int, "reasoning": str},
    #    "response":     {"score": int, "reasoning": str}}
    judge_json = Column(JSON, nullable=True, default=None)

    # Suggested follow-up questions generated (on the small/default LLM) at the
    # end of a web-session agent run. Shape: ["question 1", "question 2", ...].
    # Only populated for web completions when the org's enable_follow_ups setting
    # is on. Persisted so the chips survive a page reload (the live render comes
    # from the in-stream `completion.follow_ups` SSE event).
    follow_ups = Column(JSON, nullable=True, default=None)

    mentions = relationship("Mention", back_populates="completion", lazy='selectin')
    feedbacks = relationship("CompletionFeedback", back_populates="completion", cascade="all, delete-orphan", lazy='select')
    
    # Fork summary fields
    is_fork_summary = Column(String, nullable=True, default=None)  # truthy when this is a fork summary
    source_report_id = Column(String(36), nullable=True, default=None)  # original report this was forked from
    fork_asset_refs = Column(JSON, nullable=True, default=None)  # [{type, id, title}] for queries/viz/artifacts

    # Scheduled prompt
    scheduled_prompt_id = Column(String(36), ForeignKey('scheduled_prompts.id'), nullable=True, index=True)
    scheduled_prompt = relationship("ScheduledPrompt", back_populates="completions", lazy='select')

    # Webhook provenance. Set on every completion originating from an inbound
    # webhook: the visible event entry (role='external'), the hidden trigger
    # (role='user'), and the agent reply (role='system'). The internal trigger is
    # hidden from the timeline via (webhook_id IS NOT NULL AND role='user').
    webhook_id = Column(String(36), ForeignKey('webhooks.id'), nullable=True, index=True)

    # Generic machine-turn provenance ('eval_run', 'wait', ...). Same
    # three-completion idiom as webhooks: the visible role='external' event
    # entry, the hidden role='user' trigger prompt (filtered out of the
    # timeline via trigger_source IS NOT NULL AND role='user'), and the
    # agent's role='system' reply. Null for human-initiated turns.
    trigger_source = Column(String, nullable=True, index=True)

    # How this run was started, when it matters to the tool permission rules.
    # 'api_key': a prompt created by an API-key request (nobody is in the loop
    # to answer an approval), stamped at insert on role='user' rows by
    # app/core/request_origin.py. On a machine turn (wait wake, check-in,
    # sub-agent, wave or message reply) the mark of the turn that started it:
    # 'api_key', 'unattended' (a scheduled, webhook or eval run) or 'channel'
    # (a Slack, Teams, email or app-surface run); see
    # app/services/machine_turn.py. NULL for every other run.
    started_via = Column(String(16), nullable=True)

    external_platform = Column(String, nullable=True)  # 'slack', 'teams', 'email', null
    external_message_id = Column(String, nullable=True)  # Platform-specific message ID
    external_user_id = Column(String, nullable=True)  # Platform-specific user ID
    external_thread_ts = Column(String, nullable=True, index=True)  # Thread parent timestamp for Slack threading
    external_message_ts = Column(String, nullable=True)  # User's message timestamp for reactions
    external_channel_id = Column(String, nullable=True)  # Channel ID for reactions and thread replies
    external_channel_type = Column(String, nullable=True)  # 'im' for DM, 'channel' for public channel (future use)

# New async function to handle sending the DM safely
async def send_final_slack_dm(completion_id: str):
    """
    Fetches the final answer for a completion and sends it as a DM on Slack.
    This is triggered when the main system_completion is marked as 'success'.
    """
    session_maker = create_async_session_factory()
    async with session_maker() as db:
        try:
            # Get the system completion that triggered this event
            stmt = select(Completion).options(selectinload(Completion.report)).where(Completion.id == completion_id)
            result = await db.execute(stmt)
            system_completion = result.scalar_one_or_none()

            if not system_completion:
                logger.error("DM_SENDER: Could not find system_completion with id %s", completion_id)
                return

            # Use the content from the triggering completion directly
            if not (system_completion.completion and system_completion.completion.get('content')):
                logger.error("DM_SENDER: Completion %s has no content to send.", completion_id)
                return

            final_answer_text = system_completion.completion.get('content')
            external_user_id = system_completion.external_user_id
            organization_id = system_completion.report.organization_id

            # Get the Slack platform configuration to create the adapter
            platform_stmt = select(ExternalPlatform).where(
                ExternalPlatform.organization_id == organization_id,
                ExternalPlatform.platform_type == "slack"
            )
            platform_result = await db.execute(platform_stmt)
            platform = platform_result.scalar_one_or_none()

            if not platform:
                logger.error("DM_SENDER: No active Slack platform found for organization %s", organization_id)
                return

            # Create adapter and send the DM
            adapter = PlatformAdapterFactory.create_adapter(platform)
            success = await adapter.send_dm(external_user_id, final_answer_text)

            if success:
                logger.debug("DM_SENDER: Successfully sent final answer to Slack user %s", external_user_id)
            else:
                logger.error("DM_SENDER: Failed to send final answer to Slack user %s", external_user_id)

        except Exception as e:
            logger.error("DM_SENDER: Error sending final Slack DM for completion %s: %s", completion_id, e)
            await db.rollback()

# Callback functions


async def broadcast_event(data):
    try:
        # Extract report_id from the data
        report_id = str(data.get("report_id"))
        if not report_id:
            logger.error("Error: No report_id found in data")
            return
            
        await websocket_manager.broadcast_to_report(report_id, json.dumps(data))
    except Exception as e:
        logger.error("Error broadcasting event: %s", e)

def _bus_event(event_name: str, target) -> dict:
    """Payload for the in-process completion event bus.

    Its only subscriber is a same-worker AgentV2 run
    (``_handle_completion_update``), which reads the Stop signal (sigkill) and
    steering rows. The transcript (``completion``) and chat routing fields are
    deliberately left out: every completion update used to serialize the full
    transcript here only for the subscriber to parse and drop it.
    """
    data = {
        "event": event_name,
        "id": str(target.id),
        "completion_id": str(target.id),
        "report_id": str(target.report_id),
        "status": target.status,
        "role": target.role,
        "message_type": target.message_type,
        "parent_id": target.parent_id,
        "sigkill": target.sigkill.isoformat() if target.sigkill else None,
    }
    if target.message_type == "steering":
        data["prompt"] = target.prompt
    return data


_RUN_ENDED_STATUSES = ("success", "error", "stopped", "cancelled")


def after_insert_completion(mapper, connection, target):
    try:
        spawn(broadcast_event(_bus_event("insert_completion", target)))
    except Exception as e:
        logger.error("Error in after_insert_completion: %s", e)

def after_update_completion(mapper, connection, target):
    try:
        # Send completion blocks to the chat platform when completion finishes.
        # WhatsApp is driven through the same path as Slack (text answer + step
        # data + eyes->checkmark reaction swap). Teams delivers via the
        # per-block listeners in completion_block.py instead. Google Chat
        # delivers per-block too but still needs this finish hook: its
        # "reaction swap" is what deletes the 👀 working-marker message.
        if (target.status == "success" and
            target.role == "system" and
            target.external_platform in ("slack", "whatsapp", "google_chat") and
            target.external_user_id is not None):

            logger.debug("SLACK_SENDER: Triggering completion blocks DM for completion %s", target.id)
            from app.models.completion_block import send_completion_blocks_to_slack
            spawn(send_completion_blocks_to_slack(str(target.id)))

        # Error finish: the run failed with no answer, so the chat user gets a
        # localized "something went wrong" line instead of silence + a stuck 👀.
        # Gated on the status actually flipping to 'error' so a later same-status
        # update can't re-send. Teams reports its own failure via the progress
        # bubble (completion_service), so it's not routed here.
        if (target.status == "error" and
            target.role == "system" and
            target.external_platform in ("slack", "whatsapp", "google_chat") and
            target.external_user_id is not None and
            inspect(target).attrs.status.history.has_changes()):

            from app.models.completion_block import send_completion_error_to_slack
            spawn(send_completion_error_to_slack(str(target.id)))

        # Teams 1:1 progress bubble: the run just finished, failed or was
        # stopped — let it close the bubble. (The run's own finally in
        # completion_service reports it too; end_run counts each run once.)
        if (target.external_platform == "teams" and
            target.role == "system" and
            target.status in _RUN_ENDED_STATUSES and
            inspect(target).attrs.status.history.has_changes()):
            from app.services.platform_adapters import teams_stream
            spawn(teams_stream.end_run(
                target.external_channel_id, str(target.id), failed=target.status == "error",
            ))

        # WhatsApp "typing…": the run is over, stop refreshing it. (Also
        # reported from the run's finally; end_run counts each run once.)
        if (target.external_platform == "whatsapp" and
            target.role == "system" and
            target.status in _RUN_ENDED_STATUSES and
            inspect(target).attrs.status.history.has_changes()):
            from app.services.platform_adapters import whatsapp_typing
            whatsapp_typing.end_run(target.external_channel_id, str(target.id))

        spawn(broadcast_event(_bus_event("update_completion", target)))

    except Exception as e:
        logger.error("Error in after_update_completion: %s", e)

# Register the event listeners
event.listen(Completion, 'after_insert', after_insert_completion)
event.listen(Completion, 'after_update', after_update_completion)


# ---------------------------------------------------------------------------
# Rewound completions are invisible to ORM reads.
#
# Rewind (POST /api/reports/{id}/rewind) soft-deletes the turns after a prompt
# by setting ``deleted_at``. Completions are read in dozens of places — the
# transcript, the agent's history window, compaction, search, report lists,
# check-ins — and most of them never filtered ``deleted_at``. Rather than rely
# on every one of them (and every future one) remembering, a rewound row is
# hidden from all ORM SELECTs here, relationship loads included.
#
# SELECT only: UPDATE/DELETE statements still reach rewound rows, so FK
# hygiene (e.g. ``update(Completion).where(step_id IN ...).values(step_id=None)``
# before steps are deleted) keeps working for them.
#
# Not covered: an object a session already holds in its identity map is
# returned by ``session.get`` without a SELECT. Sessions are per request, and a
# rewind is refused while anything runs on the report, so no live session
# holds rows that are rewound under it.
#
# Opting out — for code that reports on runs that really happened (the console,
# usage, the report Summary of what was built):
#   - one statement: ``.execution_options(include_rewound=True)``
#   - a block / call tree: ``with rewound_completions_visible(): ...``
#   - every method of a service class: ``@sees_rewound_completions``
# ---------------------------------------------------------------------------
import contextlib as _contextlib
import functools as _functools
import inspect as _inspect
from contextvars import ContextVar as _ContextVar

from sqlalchemy.orm import Session as _OrmSession, with_loader_criteria as _with_loader_criteria

_REWOUND_VISIBLE: _ContextVar[bool] = _ContextVar("bow_rewound_completions_visible", default=False)


@_contextlib.contextmanager
def rewound_completions_visible():
    """ORM reads inside this block (and the coroutines it awaits) see
    rewound completions."""
    token = _REWOUND_VISIBLE.set(True)
    try:
        yield
    finally:
        _REWOUND_VISIBLE.reset(token)


def sees_rewound_completions(cls):
    """Class decorator: every public async method runs with rewound
    completions visible."""
    for name, fn in list(vars(cls).items()):
        # Plain async functions only: a staticmethod/classmethod rebound as a
        # plain function would lose its binding.
        if name.startswith("_") or not _inspect.isfunction(fn) or not _inspect.iscoroutinefunction(fn):
            continue

        def _wrap(f):
            @_functools.wraps(f)
            async def wrapper(*args, **kwargs):
                with rewound_completions_visible():
                    return await f(*args, **kwargs)
            return wrapper

        setattr(cls, name, _wrap(fn))
    return cls


@event.listens_for(_OrmSession, "do_orm_execute")
def _hide_rewound_completions(execute_state):
    if (
        not execute_state.is_select
        or execute_state.is_column_load
        or execute_state.execution_options.get("include_rewound", False)
        or _REWOUND_VISIBLE.get()
    ):
        return
    # Relationship loads too: a parent loaded outside this listener's reach
    # (e.g. an object added in this session) must not lazy-load rewound rows.
    execute_state.statement = execute_state.statement.options(
        _with_loader_criteria(
            Completion,
            lambda cls: cls.deleted_at.is_(None),
            include_aliases=True,
        )
    )
