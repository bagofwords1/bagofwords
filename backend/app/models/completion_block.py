from sqlalchemy import Column, String, Integer, Boolean, JSON, ForeignKey, DateTime, Float, UniqueConstraint, event, select, or_
from sqlalchemy.orm import relationship, selectinload
from .base import BaseSchema
import asyncio
from typing import Dict

# Async DB + adapter imports used by event callbacks
from app.settings.database import create_async_session_factory
from app.services.platform_adapters.adapter_factory import PlatformAdapterFactory
from app.models.external_platform import ExternalPlatform
from app.models.completion import Completion
from app.models.tool_execution import ToolExecution
from app.models.plan_decision import PlanDecision
from app.services.slack_notification_service import send_step_result_to_slack
from app.core.fire_and_forget import spawn


class CompletionBlock(BaseSchema):
    __tablename__ = 'completion_blocks'
    __table_args__ = (
        # Prevent duplicate projection rows per source within an execution
        UniqueConstraint('agent_execution_id', 'source_type', 'plan_decision_id', 'tool_execution_id', name='uq_blocks_source'),
        UniqueConstraint('completion_id', 'block_index', name='uq_blocks_completion_block_index'),
    )

    # Ownership
    completion_id = Column(String(36), ForeignKey('completions.id'), nullable=False, index=True)
    agent_execution_id = Column(String(36), ForeignKey('agent_executions.id'), nullable=True, index=True)

    # Source linkage (exactly one of these should be set)
    source_type = Column(String, nullable=False)  # 'decision' | 'tool' | 'final'
    plan_decision_id = Column(String(36), ForeignKey('plan_decisions.id'), nullable=True)
    tool_execution_id = Column(String(36), ForeignKey('tool_executions.id'), nullable=True)

    # Ordering and grouping
    block_index = Column(Integer, nullable=False, default=0)  # order within completion
    loop_index = Column(Integer, nullable=True)

    # Render fields (denormalized for fast UI)
    title = Column(String, nullable=False)
    status = Column(String, nullable=False, default='in_progress')  # in_progress | completed | error
    icon = Column(String, nullable=True)
    content = Column(String, nullable=True)  # from plan_decision.assistant
    reasoning = Column(String, nullable=True)  # from plan_decision.reasoning

    # Timing
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    duration_ms = Column(Float, nullable=True)



# ---------------------------
# Slack DM push for blocks
# ---------------------------

# Best-effort in-process guard to reduce duplicate sends on rapid updates.
# Track text and tool-result sends independently so a block can send both once.
_sent_block_text_ids = set()
_sent_block_tool_ids = set()
# Completions whose processing reaction has already been swapped to a checkmark.
# The finish path can fire more than once (status flip, then transcript rebuild),
# so this guards against re-swapping.
_swapped_completion_ids = set()
# Completions whose failure line has already been delivered to the channel.
# The error finish-update can fire more than once; this keeps the user from
# getting the same "something went wrong" twice.
_error_sent_completion_ids = set()
_block_locks: Dict[str, asyncio.Lock] = {}


async def _send_generated_image_for_block(
    db, completion, te, block_id_str, thread_ts, response_channel, org_id
):
    """Deliver a ``generate_image`` result to the chat platform as an image.

    ``generate_image`` stores its output as a report File (returning a
    ``file_id``), not a Step — so the step-result path
    (``send_step_result_to_slack``, which keys off ``created_step_id``) never
    picks it up and the generated picture only ever reached the web UI. This
    mirrors that path for generated images: load the File the tool produced and
    send it as an image attachment. Platforms that can't attach files under app
    auth (Google Chat) degrade to a text pointer inside their own
    ``send_file_in_thread``.

    Resolves its own adapter from the completion's platform so it works from
    both the per-block listener (where no adapter is in scope on a tool block)
    and the finish-hook loop. Returns True when this block is a generated image
    we handled (so the caller marks it sent), False when it is anything else.
    """
    if not (te and te.tool_name == "generate_image" and te.success):
        return False
    result = te.result_json if isinstance(te.result_json, dict) else {}
    file_id = result.get("file_id")
    if not file_id:
        return False

    from app.models.file import File
    file_row = (
        await db.execute(select(File).where(File.id == str(file_id)))
    ).scalar_one_or_none()
    # Only ship actual image files; guard a missing/retyped row defensively.
    if not file_row or not file_row.path:
        return False
    if file_row.content_type and not file_row.content_type.startswith("image/"):
        return False

    platform = (
        await db.execute(
            select(ExternalPlatform).where(
                ExternalPlatform.organization_id == org_id,
                ExternalPlatform.platform_type == completion.external_platform,
            )
        )
    ).scalar_one_or_none()
    if not platform:
        return False
    adapter = PlatformAdapterFactory.create_adapter(platform)

    # Short caption — the agent's text answer already describes the picture, so
    # keep this to the file's own name (WhatsApp caps the caption anyway).
    caption = (file_row.filename or "image").strip()[:200]
    try:
        await adapter.send_file_in_thread(
            completion.external_user_id,
            file_row.path,
            caption,
            thread_ts,
            channel_id=response_channel,
        )
    except Exception as e:
        print(f"Error sending generated image {file_id} for block {block_id_str}: {e}")
    # Return True even on send failure: the attempt is made once and the dedupe
    # guard should not let a transient error trigger repeated re-uploads.
    return True


async def send_completion_blocks_to_slack(completion_id: str):
    """Send all terminal completion blocks for a finished completion to Slack."""
    session_maker = create_async_session_factory()
    async with session_maker() as db:
        try:
            # Load completion with report for organization routing
            comp_stmt = select(Completion).options(selectinload(Completion.report)).where(Completion.id == completion_id)
            comp_result = await db.execute(comp_stmt)
            completion = comp_result.scalar_one_or_none()
            if not completion:
                return

            # Route only if originated from a supported chat platform
            if not (completion.external_platform in ('slack', 'teams', 'whatsapp', 'google_chat') and completion.external_user_id):
                return

            # Get thread context from completion
            thread_ts = completion.external_thread_ts
            message_ts = completion.external_message_ts
            channel_id = completion.external_channel_id
            channel_type = completion.external_channel_type

            # Determine response channel:
            # - Slack DMs: None (adapter opens DM by user_id)
            # - Teams / Google Chat: always the originating conversation/space id
            # - Channel mentions: use channel_id on both platforms
            if completion.external_platform in ("teams", "google_chat"):
                response_channel = channel_id
            else:
                response_channel = channel_id if channel_type == "channel" else None

            # Resolve the platform + adapter BEFORE loading blocks so the
            # eyes->checkmark reaction swap at the end always runs once the
            # completion has finished — even if no transcript blocks are
            # queryable yet. Background (chat) completions flip to
            # status=success *before* their blocks finish persisting (the
            # transcript rebuild/drain runs afterward), so this path can run
            # with an empty/partial block set. The per-block listener
            # (_send_block_to_slack) delivers the message text; this path owns
            # the reaction swap and must not be gated on blocks being ready.
            org_id = completion.report.organization_id if completion.report else None
            if not org_id:
                return

            platform_stmt = select(ExternalPlatform).where(
                ExternalPlatform.organization_id == org_id,
                ExternalPlatform.platform_type == completion.external_platform
            )
            platform_result = await db.execute(platform_stmt)
            platform = platform_result.scalar_one_or_none()
            if not platform:
                return

            adapter = PlatformAdapterFactory.create_adapter(platform)

            # Get all terminal completion blocks for this completion, excluding knowledge harness
            blocks_stmt = (
                select(CompletionBlock)
                .outerjoin(PlanDecision, CompletionBlock.plan_decision_id == PlanDecision.id)
                .where(
                    CompletionBlock.completion_id == completion_id,
                    CompletionBlock.source_type.in_(['decision', 'tool', 'final']),
                    CompletionBlock.status.in_(['completed', 'success', 'error']),
                    # NULL-safe: phase is NULL for regular main-loop decisions,
                    # and NULL != 'knowledge_harness' is NULL (not true) in SQL —
                    # without the explicit IS NULL arm this filter silently
                    # dropped every normal block.
                    or_(
                        CompletionBlock.plan_decision_id == None,
                        PlanDecision.phase == None,
                        PlanDecision.phase != 'knowledge_harness',
                    ),
                )
                .order_by(CompletionBlock.block_index)
            )

            # Background completions flip to status=success BEFORE their blocks
            # finish persisting (the transcript rebuild runs afterwards), so a
            # single immediate query can see an empty/partial set — and the
            # per-block listener's debounce can drop sends when the rebuild
            # keeps touching rows. This path is the safety net, so wait
            # (bounded) for terminal blocks instead of running against nothing;
            # the _sent_block_* dedupe sets make any overlap with the per-block
            # listener harmless. Waiting also keeps the eyes→checkmark swap
            # below AFTER the text lands, not before it.
            blocks = []
            for _ in range(20):  # up to ~10s
                blocks_result = await db.execute(blocks_stmt)
                blocks = blocks_result.scalars().all()
                if blocks:
                    break
                await asyncio.sleep(0.5)

            # Send each block as a separate message in the thread (no-op if none yet)
            for block in blocks:
                block_id_str = str(block.id)
                lock = _block_locks.setdefault(block_id_str, asyncio.Lock())
                async with lock:
                    content = (block.content or '').strip()
                    # Send text for decision/final blocks once (in thread)
                    if (block.source_type in ('decision', 'final') and
                        content and len(content) >= 10 and
                        block_id_str not in _sent_block_text_ids):
                        await adapter.send_dm_in_thread(completion.external_user_id, content, thread_ts, channel_id=response_channel)
                        _sent_block_text_ids.add(block_id_str)

                    # If this block has a tool execution that produced output,
                    # send it once: a step result (chart/table) or a generated
                    # image file (generate_image stores a File, not a Step).
                    if block.tool_execution_id and block_id_str not in _sent_block_tool_ids:
                        try:
                            te_stmt = select(ToolExecution).where(ToolExecution.id == block.tool_execution_id)
                            te_result = await db.execute(te_stmt)
                            te = te_result.scalar_one_or_none()
                            if te and te.created_step_id:
                                # Pass routing details explicitly with thread context
                                await send_step_result_to_slack(
                                    str(te.created_step_id),
                                    completion.external_user_id,
                                    org_id,
                                    thread_ts=thread_ts,
                                    channel_id=response_channel,
                                    platform_type=completion.external_platform
                                )
                                _sent_block_tool_ids.add(block_id_str)
                            elif await _send_generated_image_for_block(
                                db, completion, te, block_id_str, thread_ts, response_channel, org_id
                            ):
                                _sent_block_tool_ids.add(block_id_str)
                        except Exception as e:
                            print(f"Error sending tool result for block {block.id}: {e}")

            # Once the completion has finished, swap the processing reaction:
            # remove eyes, add checkmark. Guarded so repeated finish-updates
            # (status flip, then transcript rebuild) don't re-swap.
            if channel_id and message_ts and completion_id not in _swapped_completion_ids:
                _swapped_completion_ids.add(completion_id)
                try:
                    await adapter.remove_reaction(channel_id, message_ts, "eyes")
                    await adapter.add_reaction(channel_id, message_ts, "white_check_mark")
                except Exception as e:
                    _swapped_completion_ids.discard(completion_id)
                    print(f"Error updating reactions for completion {completion_id}: {e}")

        except Exception as e:
            print(f"Error sending Slack DMs for completion {completion_id}: {e}")


async def send_completion_error_to_slack(completion_id: str):
    """Tell the originating chat channel when a run failed with no answer.

    Mirrors ``send_completion_blocks_to_slack`` for the error case: a Slack /
    WhatsApp / Google Chat run that flips to ``status='error'`` produces no
    answer blocks, so without this the user who asked is left with a 👀 reaction
    and silence. Sends one localized failure line in the originating thread and
    swaps 👀 → ❌ so the working marker never hangs. Teams is excluded — its own
    progress bubble reports the failure (see ``completion_service``)."""
    if completion_id in _error_sent_completion_ids:
        return
    _error_sent_completion_ids.add(completion_id)
    session_maker = create_async_session_factory()
    async with session_maker() as db:
        try:
            comp_stmt = select(Completion).options(selectinload(Completion.report)).where(Completion.id == completion_id)
            completion = (await db.execute(comp_stmt)).scalar_one_or_none()
            if not completion:
                return
            if not (completion.external_platform in ('slack', 'whatsapp', 'google_chat') and completion.external_user_id):
                return

            thread_ts = completion.external_thread_ts
            message_ts = completion.external_message_ts
            channel_id = completion.external_channel_id
            channel_type = completion.external_channel_type
            # Same response-channel routing as the success path.
            if completion.external_platform == "google_chat":
                response_channel = channel_id
            else:
                response_channel = channel_id if channel_type == "channel" else None

            org_id = completion.report.organization_id if completion.report else None
            if not org_id:
                return
            # Load the org for its locale (settings is lazy='joined', so no extra
            # round trip); report.organization isn't eager-loaded here.
            from app.models.organization import Organization
            org = (await db.execute(
                select(Organization).where(Organization.id == org_id)
            )).scalar_one_or_none()

            platform_stmt = select(ExternalPlatform).where(
                ExternalPlatform.organization_id == org_id,
                ExternalPlatform.platform_type == completion.external_platform,
            )
            platform = (await db.execute(platform_stmt)).scalar_one_or_none()
            if not platform:
                return
            adapter = PlatformAdapterFactory.create_adapter(platform)

            from app.services.channel_error_strings import bow_error_message

            # The status→error flip can fire a beat BEFORE agent_v2 writes the
            # classified error payload into completion.completion — at flip time
            # the dict often holds only {'content': …} with no 'error' key yet.
            # Re-read the column briefly until the error details materialize, so
            # the channel shows the real failure and not an empty fallback.
            import asyncio as _asyncio
            payload = None
            err = None
            for _attempt in range(10):  # ~2.5s budget; the write usually lands at once
                payload = completion.completion if isinstance(completion.completion, dict) else None
                err = payload.get("error") if payload else None
                # err present in any form (the classified dict, or the background
                # bare bool) → the payload has settled, nothing to wait for.
                if err is not None:
                    break
                await _asyncio.sleep(0.25)
                try:
                    await db.refresh(completion, ["completion"])
                except Exception:
                    break

            # Show the SAME failure text BOW shows in its own web chat: the
            # classifier's summary joined to the provider's raw message. Parity
            # with the product beats a localized paraphrase, and the raw provider
            # text is always accurate even for an error the classifier couldn't
            # place (a friendly bucket would risk mislabeling it).
            content = payload.get("content") if isinstance(payload, dict) else None
            message = bow_error_message(err, content)

            await adapter.send_dm_in_thread(
                completion.external_user_id, message, thread_ts, channel_id=response_channel
            )

            # Swap the 👀 working reaction for ❌ (Google Chat deletes its working
            # marker on "x"). Guarded like the success swap so repeated
            # finish-updates don't re-swap.
            if channel_id and message_ts and completion_id not in _swapped_completion_ids:
                _swapped_completion_ids.add(completion_id)
                try:
                    await adapter.remove_reaction(channel_id, message_ts, "eyes")
                    await adapter.add_reaction(channel_id, message_ts, "x")
                except Exception as e:
                    _swapped_completion_ids.discard(completion_id)
                    print(f"Error updating error reactions for completion {completion_id}: {e}")

        except Exception as e:
            _error_sent_completion_ids.discard(completion_id)
            print(f"Error sending Slack error DM for completion {completion_id}: {e}")


async def _send_block_to_slack(block_id: str):
    session_maker = create_async_session_factory()
    async with session_maker() as db:
        try:
            # Route only runs that originated from a chat platform. Check that
            # first with a column-only lookup: this fires for every terminal
            # block of every run, and web runs must not load the block,
            # plan decision and completion->report graph just to find out.
            routing = (await db.execute(
                select(Completion.external_platform, Completion.external_user_id)
                .join(CompletionBlock, CompletionBlock.completion_id == Completion.id)
                .where(CompletionBlock.id == block_id)
            )).first()
            if not routing or not (routing[0] in ('slack', 'teams', 'whatsapp', 'google_chat') and routing[1]):
                return

            # Load block
            block_stmt = select(CompletionBlock).where(CompletionBlock.id == block_id)
            block_result = await db.execute(block_stmt)
            block = block_result.scalar_one_or_none()
            if not block:
                return

            # Skip knowledge harness blocks — not surfaced in messaging contexts
            if block.plan_decision_id:
                pd_result = await db.execute(select(PlanDecision).where(PlanDecision.id == block.plan_decision_id))
                pd = pd_result.scalar_one_or_none()
                if pd and pd.phase == 'knowledge_harness':
                    return

            # Load parent completion with report for organization routing
            comp_stmt = select(Completion).options(selectinload(Completion.report)).where(Completion.id == block.completion_id)
            comp_result = await db.execute(comp_stmt)
            completion = comp_result.scalar_one_or_none()
            if not completion:
                return

            # Route only if originated from a supported chat platform
            if not (completion.external_platform in ('slack', 'teams', 'whatsapp', 'google_chat') and completion.external_user_id):
                return

            block_id_str = str(block_id)

            # Get thread context from completion
            thread_ts = completion.external_thread_ts
            channel_id = completion.external_channel_id
            channel_type = completion.external_channel_type

            # Determine response channel:
            # - Slack DMs: None (adapter opens DM by user_id)
            # - Teams / Google Chat: always the originating conversation/space id
            # - Channel mentions: use channel_id on both platforms
            if completion.external_platform in ("teams", "google_chat"):
                response_channel = channel_id
            else:
                response_channel = channel_id if channel_type == "channel" else None

            # Resolve organization once for both tool and text sends
            org_id = completion.report.organization_id if completion.report else None
            if not org_id:
                return

            # Concurrency guard per block
            lock = _block_locks.setdefault(block_id_str, asyncio.Lock())
            async with lock:
                # Decision/final blocks: send concise text when meaningful (send first)
                is_user_facing_source = (block.source_type in ('decision', 'final'))
                has_content = bool((block.content or '').strip())
                is_terminal_status = (block.status in ('completed', 'success', 'error'))

                if is_user_facing_source and has_content and is_terminal_status and (block_id_str not in _sent_block_text_ids):
                    platform_stmt = select(ExternalPlatform).where(
                        ExternalPlatform.organization_id == org_id,
                        ExternalPlatform.platform_type == completion.external_platform
                    )
                    platform_result = await db.execute(platform_stmt)
                    platform = platform_result.scalar_one_or_none()
                    if platform:
                        adapter = PlatformAdapterFactory.create_adapter(platform)

                        # Format a concise Slack message for decision/final blocks
                        content = (block.content or '').strip()

                        # Skip very short content (likely partial streaming)
                        if len(content) >= 10:
                            # Debounce: wait for the row to stop changing (the
                            # transcript rebuild touches blocks repeatedly).
                            # Previously a single changed re-read DROPPED the
                            # send and hoped a later event would retry — when
                            # the last touch landed inside the wait window the
                            # answer was never delivered. Retry until stable,
                            # then send the freshest content; if it never
                            # settles, send the latest anyway (the block is
                            # already terminal) rather than dropping it.
                            fresh_block = block
                            last_seen = block.updated_at
                            for _ in range(6):
                                await asyncio.sleep(0.5)
                                fresh_stmt = select(CompletionBlock).where(CompletionBlock.id == block_id)
                                fresh_result = await db.execute(fresh_stmt)
                                fresh_block = fresh_result.scalar_one_or_none() or fresh_block
                                if fresh_block.updated_at == last_seen:
                                    break
                                last_seen = fresh_block.updated_at
                            if block_id_str not in _sent_block_text_ids:
                                _sent_block_text_ids.add(block_id_str)
                                await adapter.send_dm_in_thread(completion.external_user_id, fresh_block.content or content, thread_ts, channel_id=response_channel)

                # Tool-origin content: if a tool execution exists and finished,
                # send its output (chart/table step, or a generated image file)
                # once. generate_image stores a File rather than a Step, so it
                # needs its own send path alongside send_step_result_to_slack.
                if getattr(block, 'tool_execution_id', None) and (block.status in ('success', 'error', 'completed')) and (block_id_str not in _sent_block_tool_ids):
                    try:
                        te_stmt = select(ToolExecution).where(ToolExecution.id == block.tool_execution_id)
                        te_result = await db.execute(te_stmt)
                        te = te_result.scalar_one_or_none()
                    except Exception:
                        te = None
                    if te and te.created_step_id:
                        # Pass routing details explicitly with thread context
                        await send_step_result_to_slack(
                            str(te.created_step_id),
                            completion.external_user_id,
                            org_id,
                            thread_ts=thread_ts,
                            channel_id=response_channel,
                            platform_type=completion.external_platform
                        )
                        _sent_block_tool_ids.add(block_id_str)
                    elif await _send_generated_image_for_block(
                        db, completion, te, block_id_str, thread_ts, response_channel, org_id
                    ):
                        _sent_block_tool_ids.add(block_id_str)
        except Exception as e:
            # Swallow errors to avoid interrupting transaction lifecycles
            print(f"Error sending Slack DM for block {block_id}: {e}")


def after_insert_block(mapper, connection, target):
    try:
        # Only send when a block transitions to a terminal state
        if getattr(target, 'status', None) in ('completed', 'success', 'error'):
            spawn(_send_block_to_slack(str(target.id)))
    except Exception:
        pass


def after_update_block(mapper, connection, target):
    try:
        # Fire-and-forget on updates only for terminal states
        if getattr(target, 'status', None) in ('completed', 'success', 'error'):
            spawn(_send_block_to_slack(str(target.id)))
    except Exception:
        pass


# Register realtime block listeners
event.listen(CompletionBlock, 'after_insert', after_insert_block)
event.listen(CompletionBlock, 'after_update', after_update_block)

