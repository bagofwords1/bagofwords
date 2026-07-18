# Path: backend/app/models/step.py

from sqlalchemy import Column, Integer, String, ForeignKey, Text, JSON, UUID, event
from sqlalchemy.orm import relationship
from .base import BaseSchema
import asyncio
import logging
from app.websocket_manager import websocket_manager
import json
from sqlalchemy import select
from app.models.widget import Widget
# from app.services.slack_notification_service import send_step_result_to_slack # This is removed

# These event listeners fire from SQLAlchemy's after_update/after_insert
# hooks, which run inside an active commit. The bg tasks they spawn
# (asyncio.create_task) outlive that request, so any print() in their
# bodies risked ValueError("I/O operation on closed file") whenever
# uvicorn was rotating stdout under the surviving task. Use logger
# instead — its handlers don't fail mid-flush.
logger = logging.getLogger(__name__)

class Step(BaseSchema):
    __tablename__ = 'steps'

    title = Column(String, index=True, nullable=False, unique=False, default="")
    slug = Column(String, index=True, nullable=False, unique=True)
    status = Column(String, nullable=False, default='draft')
    status_reason = Column(String, nullable=True, default=None)
    prompt = Column(Text, nullable=False, default="")
    code = Column(Text, nullable=False, default="")
    # SHARED snapshot — materialized under the CREATOR's data-source
    # credentials. In viewer-identity mode on user-scoped connections this is
    # credential-differentiated data other users must not see. NEVER serve
    # step.data directly to a reader: resolve what they may see through
    # app.services.viewer_data_policy.resolve_step_data (or
    # report_snapshot_withheld for report-level renders with no user).
    data = Column(JSON, nullable=True, default=dict)
    description = Column(Text, nullable=False, default="")
    type = Column(String, nullable=False, default="table")
    data_model = Column(JSON, nullable=True, default=dict)
    view = Column(JSON, nullable=True, default=dict)

    widget_id = Column(String(36), ForeignKey('widgets.id'), nullable=False)
    widget = relationship("Widget", back_populates="steps")
    # Optional linkage to Query for grouping/versioning
    query_id = Column(String(36), ForeignKey('queries.id'), nullable=True)
    query = relationship("Query", back_populates="steps", foreign_keys=[query_id], lazy="selectin")
    completions = relationship("Completion", back_populates="step")
    
    # Bidirectional relationship: Step can see which Entity was created from it
    # This uses Entity.source_step_id as the foreign key (no FK on this side)
    created_entity = relationship(
        "Entity",
        foreign_keys="Entity.source_step_id",
        back_populates="source_step",
        uselist=False,
        lazy="selectin"
    )

def after_update_step(mapper, connection, target):
    try:
        data = {
            "event": "update_step",
            "id": str(target.id),
            "step_id": str(target.id),
            "widget_id": str(target.widget_id),
            "report_id": str(target.widget.report_id),
            "title": target.title,
            "slug": target.slug,
            "status": target.status,
            "prompt": target.prompt,
            "code": target.code,
            "data": target.data,
            "description": target.description,
            "type": target.type,
            "data_model": target.data_model
        }
        asyncio.create_task(broadcast_step_update(data))

        if target.status == "success":
            from app.services.slack_notification_service import send_step_result_to_slack
            logger.debug("STEP_UPDATE: Triggering Slack DM for successful step %s", target.id)
            asyncio.create_task(send_step_result_to_slack(str(target.id)))

    except Exception as e:
        logger.warning("Error in after_update_step: %s", e)

async def _strip_withheld_step_data(data):
    """A report broadcast reaches every subscriber indiscriminately, so it
    can't serve per-user rows. In viewer-identity mode on user-scoped
    connections the shared snapshot is credential-differentiated creator data —
    strip it from the payload (subscribers load their own via the API)."""
    try:
        report_id = data.get("report_id")
        if not report_id:
            return data
        from app.dependencies import async_session_maker
        from app.services.viewer_data_policy import report_snapshot_withheld
        async with async_session_maker() as db:
            if await report_snapshot_withheld(db, str(report_id)):
                data = {**data, "data": {}, "data_model": {}, "snapshot_withheld": True}
    except Exception as e:
        logger.warning("Error checking step broadcast withholding: %s", e)
    return data

async def broadcast_step_update(data):
    try:
        data = await _strip_withheld_step_data(data)
        await websocket_manager.broadcast_to_report(
            str(data["report_id"]),
            json.dumps(data)
        )
    except Exception as e:
        logger.warning("Error broadcasting step update: %s", e)

async def broadcast_step_insert(data):
    try:
        data = await _strip_withheld_step_data(data)
        await websocket_manager.broadcast_to_report(
            str(data["report_id"]),
            json.dumps(data)
        )
    except Exception as e:
        logger.warning("Error broadcasting step insert: %s", e)

def after_insert_step(mapper, connection, target):
    try:
        # Get report_id directly from the database using the widget_id
        result = connection.execute(
            select(Widget.report_id).filter(Widget.id == target.widget_id)
        ).first()
        
        if not result:
            logger.warning("Widget %s not found for step %s, skipping broadcast", target.widget_id, target.id)
            return
            
        report_id = result[0]
        
        data = {
            "event": "insert_step",
            "id": str(target.id),
            "step_id": str(target.id),
            "widget_id": str(target.widget_id),
            "report_id": str(report_id),
            "title": target.title,
            "slug": target.slug,
            "status": target.status,
            "prompt": target.prompt,
            "code": target.code,
            "data": target.data,
            "description": target.description,
            "type": target.type,
            "data_model": target.data_model
        }
        asyncio.create_task(broadcast_step_insert(data))
    except Exception as e:
        logger.warning("Error in after_insert_step: %s", e)

# Register the event listener
event.listen(Step, 'after_update', after_update_step)
event.listen(Step, 'after_insert', after_insert_step)