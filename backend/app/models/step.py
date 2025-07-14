# Path: backend/app/models/step.py

from sqlalchemy import Column, Integer, String, ForeignKey, Text, JSON, UUID, event
from sqlalchemy.orm import relationship
from .base import BaseSchema
import asyncio
from app.websocket_manager import websocket_manager
import json
from sqlalchemy import select
from app.models.widget import Widget
import os
import uuid
import csv
from app.settings.database import create_async_session_factory
from app.services.platform_adapters.adapter_factory import PlatformAdapterFactory
from app.models.external_platform import ExternalPlatform
from app.models.completion import Completion
from sqlalchemy.orm import selectinload

def df_to_img(data: dict) -> str:
    """
    Creates a dummy image file for testing purposes.
    In a real application, this would generate an actual image.
    """
    try:
        image_path = f"/tmp/{uuid.uuid4()}.png"
        with open(image_path, "w") as f:
            f.write("dummy image data")
        return image_path
    except Exception as e:
        print(f"Error creating image file: {e}")
        return None

def df_to_csv(data: dict) -> str:
    """
    Creates a dummy CSV file for testing purposes.
    In a real application, this would generate an actual CSV.
    """
    try:
        csv_path = f"/tmp/{uuid.uuid4()}.csv"
        with open(csv_path, "w") as f:
            writer = csv.writer(f)
            writer.writerow(["key", "value"])
            for key, value in data.items():
                writer.writerow([key, value])
        return csv_path
    except Exception as e:
        print(f"Error creating CSV file: {e}")
        return None

async def send_step_slack_dm(step_id: str):
    """
    Sends a step's data as a DM on Slack if it was initiated from a Slack command.
    It checks the completion associated with the step to find the user.
    """
    session_maker = create_async_session_factory()
    async with session_maker() as db:
        try:
            # Get the step and its report organization
            stmt = select(Step).options(
                selectinload(Step.widget).selectinload(Widget.report)
            ).where(Step.id == step_id)
            result = await db.execute(stmt)
            step = result.scalar_one_or_none()

            if not step:
                print(f"STEP_DM_SENDER: Could not find step with id {step_id}")
                return

            # Find the most recent completion linked to this step
            comp_stmt = select(Completion).where(Completion.step_id == step_id).order_by(Completion.created_at.desc()).limit(1)
            comp_result = await db.execute(comp_stmt)
            completion = comp_result.scalar_one_or_none()

            if not completion:
                print(f"STEP_DM_SENDER: No completion found for step {step_id}. Cannot send DM.")
                return

            if not (completion.external_platform == "slack" and completion.external_user_id):
                print(f"STEP_DM_SENDER: Step {step_id} was not initiated from Slack. Ignoring.")
                return

            external_user_id = completion.external_user_id
            organization_id = step.widget.report.organization_id

            # Get the Slack platform configuration
            platform_stmt = select(ExternalPlatform).where(
                ExternalPlatform.organization_id == organization_id, ExternalPlatform.platform_type == "slack")
            platform_result = await db.execute(platform_stmt)
            platform = platform_result.scalar_one_or_none()

            if not platform:
                print(f"STEP_DM_SENDER: No active Slack platform for organization {organization_id}")
                return

            adapter = PlatformAdapterFactory.create_adapter(platform)
            file_path, success = None, False
            try:
                if step.type == "table":
                    file_path = df_to_csv(step.data)
                    if file_path:
                        success = await adapter.send_file_in_dm(external_user_id, file_path, step.title)
                else:
                    file_path = df_to_img(step.data)
                    if file_path:
                        success = await adapter.send_image_in_dm(external_user_id, file_path, step.title)

                if success:
                    print(f"STEP_DM_SENDER: Successfully sent step data to Slack user {external_user_id}")
                else:
                    print(f"STEP_DM_SENDER: Failed to send step data to Slack user {external_user_id}")

            finally:
                if file_path and os.path.exists(file_path):
                    os.remove(file_path)

        except Exception as e:
            print(f"STEP_DM_SENDER: Error for step {step_id}: {e}")
            await db.rollback()


class Step(BaseSchema):
    __tablename__ = 'steps'

    title = Column(String, index=True, nullable=False, unique=False, default="")
    slug = Column(String, index=True, nullable=False, unique=True)
    status = Column(String, nullable=False, default='draft')
    prompt = Column(Text, nullable=False, default="")
    code = Column(Text, nullable=False, default="")
    data = Column(JSON, nullable=True, default=dict)
    description = Column(Text, nullable=False, default="")
    type = Column(String, nullable=False, default="table")
    data_model = Column(JSON, nullable=True, default=dict)

    widget_id = Column(String(36), ForeignKey('widgets.id'), nullable=False)
    widget = relationship("Widget", back_populates="steps")
    completions = relationship("Completion", back_populates="step")

    memories = relationship("Memory", back_populates="step")

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
        print(f"Broadcasting step update: {data}")
        asyncio.create_task(broadcast_step_update(data))

        if target.status == "success":
            print(f"STEP_DM_SENDER: Triggering Slack DM for successful step {target.id}")
            asyncio.create_task(send_step_slack_dm(str(target.id)))

    except Exception as e:
        print(f"Error in after_update_step: {e}")

async def broadcast_step_update(data):
    try:
        await websocket_manager.broadcast_to_report(
            str(data["report_id"]),
            json.dumps(data)
        )
        print(f"Broadcasted step update: {data}")
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