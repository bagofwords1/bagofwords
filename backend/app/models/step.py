# Path: backend/app/models/step.py

from sqlalchemy import Column, Integer, String, ForeignKey, Text, JSON, UUID, event
from sqlalchemy.orm import relationship
from .base import BaseSchema
import asyncio
from app.websocket_manager import websocket_manager
import json
from sqlalchemy import select
from app.models.widget import Widget
# from app.services.slack_notification_service import send_step_result_to_slack # This is removed

class Step(BaseSchema):
    __tablename__ = 'steps'

    title = Column(String, index=True, nullable=False, unique=False, default="")
    slug = Column(String, index=True, nullable=False, unique=True)
    status = Column(String, nullable=False, default='draft')
    status_reason = Column(String, nullable=True, default=None)
    prompt = Column(Text, nullable=False, default="")
    code = Column(Text, nullable=False, default="")
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
        #print(f"Broadcasting step update: {data}")
        asyncio.create_task(broadcast_step_update(data))

        if target.status == "success":
            from app.services.slack_notification_service import send_step_result_to_slack
            print(f"STEP_UPDATE: Triggering Slack DM for successful step {target.id}")
            asyncio.create_task(send_step_result_to_slack(str(target.id)))

    except Exception as e:
        print(f"Error in after_update_step: {e}")

async def broadcast_step_update(data):
    try:
        await websocket_manager.broadcast_to_report(
            str(data["report_id"]),
            json.dumps(data)
        )
        #print(f"Broadcasted step update: {data}")
    except Exception as e:
        print(f"Error broadcasting step update: {e}")

async def broadcast_step_insert(data):
    try:
        await websocket_manager.broadcast_to_report(
            str(data["report_id"]),
            json.dumps(data)
        )
    except Exception as e:
        print(f"Error broadcasting step insert: {e}")

def after_insert_step(mapper, connection, target):
    try:
        # Get report_id directly from the database using the widget_id
        result = connection.execute(
            select(Widget.report_id).filter(Widget.id == target.widget_id)
        ).first()
        
        if not result:
            print(f"Warning: Widget {target.widget_id} not found for step {target.id}, skipping broadcast")
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
        print(f"Error in after_insert_step: {e}")

# Register the event listener
event.listen(Step, 'after_update', after_update_step)
event.listen(Step, 'after_insert', after_insert_step)