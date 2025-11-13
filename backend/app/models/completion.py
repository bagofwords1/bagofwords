from sqlalchemy import Column, Integer, String, ForeignKey, Text, JSON, event, UUID, DateTime
from sqlalchemy.orm import relationship, selectinload
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import DateTime
from datetime import datetime
from .base import BaseSchema
import asyncio
from app.websocket_manager import websocket_manager
import json
from app.models.mention import MentionType
from sqlalchemy.orm.exc import DetachedInstanceError

# Add these imports for the new functionality
from app.settings.database import create_async_session_factory
from app.services.platform_adapters.adapter_factory import PlatformAdapterFactory
from app.models.external_platform import ExternalPlatform
from app.settings.logging_config import get_logger

logger = get_logger(__name__)


class Completion(BaseSchema):
    __tablename__ = 'completions'

    prompt = Column(JSON, nullable=False, default="")
    completion = Column(JSON, nullable=False, default="")

    status = Column(String, nullable=False, default='success')
    model = Column(String, nullable=False, default='gpt4o')
    turn_index = Column(Integer, nullable=False, default=0)
    feedback_score = Column(Integer, nullable=False, default=0)
    sigkill = Column(DateTime, nullable=False, default=None)

    parent_id = Column(String(36), ForeignKey('completions.id'), nullable=True)

    # message type
    message_type = Column(String, nullable=False, default='ai_completion')
    role = Column(String, nullable=False, default='system')

    # report
    report_id = Column(String(36), ForeignKey('reports.id'), nullable=False)
    report = relationship("Report", back_populates="completions", lazy='selectin')

    # widget - optional
    widget_id = Column(String(36), ForeignKey('widgets.id'), nullable=True)
    widget = relationship("Widget", back_populates="completions", lazy='selectin')

    # step - optional
    step_id = Column(String(36), ForeignKey('steps.id'), nullable=True)
    step = relationship("Step", back_populates="completions", lazy='select')

    user_id = Column(String(36), ForeignKey('users.id'), nullable=True)
    user = relationship("User", back_populates="completions", lazy='select')

    main_router = Column(String, nullable=False, default='table')

    instructions_effectiveness = Column(Integer, nullable=True, default=4)
    context_effectiveness = Column(Integer, nullable=True, default=4)
    response_score = Column(Integer, nullable=True, default=4)  # 1-5 rating of AI performance, where 1=poor and 5=excellent

    mentions = relationship("Mention", back_populates="completion", lazy='selectin')
    feedbacks = relationship("CompletionFeedback", back_populates="completion", cascade="all, delete-orphan", lazy='select')
    
    external_platform = Column(String, nullable=True)  # 'slack', 'teams', 'email', null
    external_message_id = Column(String, nullable=True)  # Platform-specific message ID
    external_user_id = Column(String, nullable=True)  # Platform-specific user ID

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
            
        logger.debug("Broadcasting event to report %s: %s", report_id, data)
        await websocket_manager.broadcast_to_report(report_id, json.dumps(data))
        logger.debug("Broadcast completed")
    except Exception as e:
        logger.error("Error broadcasting event: %s", e)

def after_insert_completion(mapper, connection, target):
    try:

        data = {
            "event": "insert_completion",
            "id": str(target.id),
            "completion_id": str(target.id),
            "completion": target.completion,
            "prompt": target.prompt,
            "status": target.status,
            "sigkill": target.sigkill.isoformat() if target.sigkill else None,
            "model": target.model,
            "turn_index": target.turn_index,
            "parent_id": target.parent_id,
            "message_type": target.message_type,
            "role": target.role,
            "report_id": str(target.report_id),
            "external_platform": target.external_platform,
            "external_message_id": target.external_message_id,
            "external_user_id": target.external_user_id
        }
        

        if target.widget_id:
            data["widget_id"] = str(target.widget_id)
        if target.step_id:
            data["step_id"] = str(target.step_id)
        
        logger.debug("Triggered after_insert_completion with data: %s", data)
        asyncio.create_task(broadcast_event(data))

    except Exception as e:
        logger.error("Error in after_insert_completion: %s", e)

def after_update_completion(mapper, connection, target):
    try:
        data = {
            "event": "update_completion",
            "id": str(target.id),
            "completion_id": str(target.id),
            "report_id": str(target.report_id),
            "completion": target.completion,
            "prompt": target.prompt,
            "status": target.status,
            "model": target.model,
            "turn_index": target.turn_index,
            "parent_id": target.parent_id,
            "message_type": target.message_type,
            "role": target.role,
            "sigkill": target.sigkill.isoformat() if target.sigkill else None,
            "external_platform": target.external_platform,
            "external_message_id": target.external_message_id,
            "external_user_id": target.external_user_id
        }

        if target.widget_id:
            data["widget_id"] = str(target.widget_id)
        if target.step_id:
            data["step_id"] = str(target.step_id)

        # Send completion blocks to Slack when completion finishes
        if (target.status == "success" and
            target.role == "system" and
            target.external_platform == "slack" and
            target.external_user_id is not None):
            
            logger.debug("SLACK_SENDER: Triggering completion blocks DM for completion %s", target.id)
            from app.models.completion_block import send_completion_blocks_to_slack
            asyncio.create_task(send_completion_blocks_to_slack(str(target.id)))

        asyncio.create_task(broadcast_event(data))

    except Exception as e:
        logger.error("Error in after_update_completion: %s", e)

# Register the event listeners
event.listen(Completion, 'after_insert', after_insert_completion)
event.listen(Completion, 'after_update', after_update_completion)
