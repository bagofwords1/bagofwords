from sqlalchemy import Column, String, Integer, Boolean, JSON, ForeignKey, DateTime, UniqueConstraint, event, select
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
from app.services.slack_notification_service import send_step_result_to_slack


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



# ---------------------------
# Slack DM push for blocks
# ---------------------------

# Best-effort in-process guard to reduce duplicate sends on rapid updates.
# Track text and tool-result sends independently so a block can send both once.
_sent_block_text_ids = set()
_sent_block_tool_ids = set()
_block_locks: Dict[str, asyncio.Lock] = {}


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

            # Route only if originated from Slack
            if not (completion.external_platform == 'slack' and completion.external_user_id):
                return

            # Get all terminal completion blocks for this completion
            blocks_stmt = select(CompletionBlock).where(
                CompletionBlock.completion_id == completion_id,
                CompletionBlock.source_type.in_(['decision', 'tool', 'final']),
                CompletionBlock.status.in_(['completed', 'success', 'error'])
            ).order_by(CompletionBlock.block_index)
            
            blocks_result = await db.execute(blocks_stmt)
            blocks = blocks_result.scalars().all()
            
            if not blocks:
                return

            # Resolve Slack platform for the organization
            org_id = completion.report.organization_id if completion.report else None
            if not org_id:
                return

            platform_stmt = select(ExternalPlatform).where(
                ExternalPlatform.organization_id == org_id,
                ExternalPlatform.platform_type == 'slack'
            )
            platform_result = await db.execute(platform_stmt)
            platform = platform_result.scalar_one_or_none()
            if not platform:
                return

            adapter = PlatformAdapterFactory.create_adapter(platform)

            # Send each block as a separate message (text first, then tool output)
            for block in blocks:
                block_id_str = str(block.id)
                lock = _block_locks.setdefault(block_id_str, asyncio.Lock())
                async with lock:
                    content = (block.content or '').strip()
                    # Send text for decision/final blocks once
                    if (block.source_type in ('decision', 'final') and
                        content and len(content) >= 10 and
                        block_id_str not in _sent_block_text_ids):
                        await adapter.send_dm(completion.external_user_id, content)
                        _sent_block_text_ids.add(block_id_str)
                    
                    # If this block has a tool execution that created a step, send the step result (chart/table)
                    if block.tool_execution_id:
                        try:
                            te_stmt = select(ToolExecution).where(ToolExecution.id == block.tool_execution_id)
                            te_result = await db.execute(te_stmt)
                            te = te_result.scalar_one_or_none()
                            if te and te.created_step_id and block_id_str not in _sent_block_tool_ids:
                                # Pass routing details explicitly and mark as sent (tool result)
                                await send_step_result_to_slack(
                                    str(te.created_step_id),
                                    completion.external_user_id,
                                    org_id
                                )
                                _sent_block_tool_ids.add(block_id_str)
                        except Exception as e:
                            print(f"Error sending step result for block {block.id}: {e}")

        except Exception as e:
            print(f"Error sending Slack DMs for completion {completion_id}: {e}")


async def _send_block_to_slack(block_id: str):
    session_maker = create_async_session_factory()
    async with session_maker() as db:
        try:
            # Load block
            block_stmt = select(CompletionBlock).where(CompletionBlock.id == block_id)
            block_result = await db.execute(block_stmt)
            block = block_result.scalar_one_or_none()
            if not block:
                return

            # Load parent completion with report for organization routing
            comp_stmt = select(Completion).options(selectinload(Completion.report)).where(Completion.id == block.completion_id)
            comp_result = await db.execute(comp_stmt)
            completion = comp_result.scalar_one_or_none()
            if not completion:
                return

            # Route only if originated from Slack
            if not (completion.external_platform == 'slack' and completion.external_user_id):
                return

            block_id_str = str(block_id)
            
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
                        ExternalPlatform.platform_type == 'slack'
                    )
                    platform_result = await db.execute(platform_stmt)
                    platform = platform_result.scalar_one_or_none()
                    if platform:
                        adapter = PlatformAdapterFactory.create_adapter(platform)

                        # Format a concise Slack message for decision/final blocks
                        content = (block.content or '').strip()

                        # Skip very short content (likely partial streaming)
                        if len(content) >= 10:
                            # Debounce: wait briefly and re-check if content changed (still streaming)
                            initial_updated_at = block.updated_at
                            await asyncio.sleep(0.5)

                            # Re-read block to check if it's still being updated
                            fresh_stmt = select(CompletionBlock).where(CompletionBlock.id == block_id)
                            fresh_result = await db.execute(fresh_stmt)
                            fresh_block = fresh_result.scalar_one_or_none()
                            if fresh_block and fresh_block.updated_at == initial_updated_at:
                                _sent_block_text_ids.add(block_id_str)
                                await adapter.send_dm(completion.external_user_id, fresh_block.content or content)

                # Tool-origin content: if a tool execution exists and finished, send the step output (chart/table/file) once
                if getattr(block, 'tool_execution_id', None) and (block.status in ('success', 'error', 'completed')) and (block_id_str not in _sent_block_tool_ids):
                    try:
                        te_stmt = select(ToolExecution).where(ToolExecution.id == block.tool_execution_id)
                        te_result = await db.execute(te_stmt)
                        te = te_result.scalar_one_or_none()
                    except Exception:
                        te = None
                    if te and te.created_step_id:
                        # Pass routing details explicitly to avoid relying on Completion.step_id linkage
                        await send_step_result_to_slack(
                            str(te.created_step_id),
                            completion.external_user_id,
                            org_id
                        )
                        _sent_block_tool_ids.add(block_id_str)
        except Exception as e:
            # Swallow errors to avoid interrupting transaction lifecycles
            print(f"Error sending Slack DM for block {block_id}: {e}")


def after_insert_block(mapper, connection, target):
    try:
        # Only send when a block transitions to a terminal state
        if getattr(target, 'status', None) in ('completed', 'success', 'error'):
            asyncio.create_task(_send_block_to_slack(str(target.id)))
    except Exception:
        pass


def after_update_block(mapper, connection, target):
    try:
        # Fire-and-forget on updates only for terminal states
        if getattr(target, 'status', None) in ('completed', 'success', 'error'):
            asyncio.create_task(_send_block_to_slack(str(target.id)))
    except Exception:
        pass


# Register realtime block listeners
event.listen(CompletionBlock, 'after_insert', after_insert_block)
event.listen(CompletionBlock, 'after_update', after_update_block)

