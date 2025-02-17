# Path: backend/app/models/widget.py
from sqlalchemy import Column, Integer, String, ForeignKey, UUID, Boolean
from sqlalchemy.orm import relationship
from .base import BaseSchema
from app.websocket_manager import websocket_manager
from sqlalchemy import event
import asyncio
import json

class Widget(BaseSchema):
    __tablename__ = 'widgets'

    title = Column(String, index=True, nullable=False, unique=False, default="")
    slug = Column(String, index=True, nullable=False, unique=True)
    status = Column(String, nullable=False, default='draft')
    x = Column(Integer, nullable=False, default=0)
    y = Column(Integer, nullable=False, default=0)
    width = Column(Integer, nullable=False, default=900)
    height = Column(Integer, nullable=False, default=400)

    report_id = Column(String(36), ForeignKey('reports.id'), nullable=False)
    report = relationship("Report", back_populates="widgets")
    
    # Use string reference to avoid circular import issues
    steps = relationship("Step", back_populates="widget", lazy="selectin")
    completions = relationship("Completion", back_populates="widget")
    memories = relationship("Memory", back_populates="widget")


def after_update_widget(mapper, connection, target):
    try:
        data = {
            "event": "update_widget",
            "id": str(target.id),
            "widget_id": str(target.id),
            "report_id": str(target.report_id),
            "title": target.title,
            "slug": target.slug,
            "status": target.status,
            "x": target.x,
            "y": target.y,
            "width": target.width,
            "height": target.height
        }

        print(f"Broadcasting widget update: {data}")
        asyncio.create_task(broadcast_widget_update(data))
    except Exception as e:
        print(f"Error in after_update_widget: {e}")

async def broadcast_widget_update(data):
    try:
        # Print all running tasks before broadcasting
        tasks = asyncio.all_tasks()
        print(f"Current running tasks ({len(tasks)}):")
        for task in tasks:
            print(f"- {task.get_name()}: {task}")
            
        await websocket_manager.broadcast_to_report(
            str(data["report_id"]),
            json.dumps(data)
        )
        print(f"Broadcasted widget update: {data}")
    except Exception as e:
        print(f"Error broadcasting widget update: {e}")

event.listen(Widget, 'after_update', after_update_widget)
