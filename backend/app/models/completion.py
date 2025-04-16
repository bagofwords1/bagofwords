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

    mentions = relationship("Mention", back_populates="completion", lazy='selectin')

# Callback functions


async def broadcast_event(data):
    try:
        # Extract report_id from the data
        report_id = str(data.get("report_id"))
        if not report_id:
            print("Error: No report_id found in data")
            return
            
        print(f"Broadcasting event to report {report_id}: {data}")
        await websocket_manager.broadcast_to_report(report_id, json.dumps(data))
        print("Broadcast completed")
    except Exception as e:
        print(f"Error broadcasting event: {e}")

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
            "report_id": str(target.report_id)
        }
        

        if target.widget_id:
            data["widget_id"] = str(target.widget_id)
        if target.step_id:
            data["step_id"] = str(target.step_id)
        
        print(f"Triggered after_insert_completion with data: {data}")
        asyncio.create_task(broadcast_event(data))

    except Exception as e:
        print(f"Error in after_insert_completion: {e}")

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
            "sigkill": target.sigkill.isoformat() if target.sigkill else None
        }

        if target.widget_id:
            data["widget_id"] = str(target.widget_id)
        if target.step_id:
            data["step_id"] = str(target.step_id)

        asyncio.create_task(broadcast_event(data))

    except Exception as e:
        print(f"Error in after_update_completion: {e}")

# Register the event listeners
event.listen(Completion, 'after_insert', after_insert_completion)
event.listen(Completion, 'after_update', after_update_completion)
