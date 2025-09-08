# Path: backend/app/models/text_widget.py
from sqlalchemy import Column, Integer, String, ForeignKey, UUID, Boolean, event, JSON
from sqlalchemy.orm import relationship
from .base import BaseSchema
import asyncio
from app.websocket_manager import websocket_manager
import json

class TextWidget(BaseSchema):
    __tablename__ = 'text_widgets'

    status = Column(String, nullable=False, default='published')
    x = Column(Integer, nullable=False, default=0)
    y = Column(Integer, nullable=False, default=0)
    width = Column(Integer, nullable=False, default=5)
    height = Column(Integer, nullable=False, default=9)
    content = Column(String, nullable=False, default="")
    view = Column(JSON, nullable=True, default=dict)

    report_id = Column(String(36), ForeignKey('reports.id'), nullable=False)
    report = relationship("Report", back_populates="text_widgets")

# Callback functions
async def broadcast_event(data):
    try:
        report_id = str(data.get("report_id"))
        if not report_id:
            print("Error: No report_id found in data")
            return
            
        print(f"Broadcasting text widget event to report {report_id}: {data}")
        await websocket_manager.broadcast_to_report(report_id, json.dumps(data))
        print("Broadcast completed")
    except Exception as e:
        print(f"Error broadcasting event: {e}")

def after_insert_text_widget(mapper, connection, target):
    try:
        data = {
            "event": "insert_text_widget",
            "text_widget_id": str(target.id),
            "report_id": str(target.report_id)
        }
        
        print(f"Triggered after_insert_text_widget with data: {data}")
        asyncio.create_task(broadcast_event(data))

    except Exception as e:
        print(f"Error in after_insert_text_widget: {e}")

# Register the event listener
event.listen(TextWidget, 'after_insert', after_insert_text_widget)

