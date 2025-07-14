import os
import uuid
import csv
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.step import Step
from app.models.widget import Widget
from app.models.completion import Completion
from app.models.external_platform import ExternalPlatform
from app.settings.database import create_async_session_factory
from app.services.platform_adapters.adapter_factory import PlatformAdapterFactory

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
    """Creates a CSV file from dictionary data."""
    rows = data.get('rows', [])
    columns = data.get('columns', [])

    if not rows or not columns:
        return None

    headers = [col.get('headerName', col.get('field', '')) for col in columns]
    fields = [col['field'] for col in columns]

    try:
        csv_path = f"/tmp/{uuid.uuid4()}.csv"
        with open(csv_path, 'w', newline='') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(headers)
            for row in rows:
                writer.writerow([row.get(field, '') for field in fields])
        return csv_path
    except Exception as e:
        print(f"Error creating CSV file: {e}")
        return None

async def _handle_table_step_dm(adapter, external_user_id: str, step: 'Step'):
    """Handles sending table data to Slack."""
    title = step.title or "Table Data"
    
    file_path = df_to_csv(step.data)
    if not file_path:
        return False
    
    success = False
    try:
        success = await adapter.send_file_in_dm(external_user_id, file_path, title)
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
    return success

async def _handle_chart_step_dm(adapter, external_user_id: str, step: 'Step'):
    """Handles sending chart data (as an image) to Slack."""
    title = step.title or "Chart"
    file_path = df_to_img(step.data)
    if not file_path:
        return False

    success = False
    try:
        success = await adapter.send_image_in_dm(external_user_id, file_path, title)
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
    return success

async def send_step_result_to_slack(step_id: str):
    """
    Sends a step's data as a DM on Slack if it was initiated from a Slack command.
    """
    session_maker = create_async_session_factory()
    async with session_maker() as db:
        try:
            stmt = select(Step).options(
                selectinload(Step.widget).selectinload(Widget.report)
            ).where(Step.id == step_id)
            result = await db.execute(stmt)
            step = result.scalar_one_or_none()

            if not step:
                print(f"SLACK_NOTIFIER: Could not find step with id {step_id}")
                return

            comp_stmt = select(Completion).where(Completion.step_id == step_id).order_by(Completion.created_at.desc()).limit(1)
            comp_result = await db.execute(comp_stmt)
            completion = comp_result.scalar_one_or_none()

            if not (completion and completion.external_platform == "slack" and completion.external_user_id):
                print(f"SLACK_NOTIFIER: Step {step_id} not from Slack or no completion found. Ignoring.")
                return

            external_user_id = completion.external_user_id
            organization_id = step.widget.report.organization_id

            platform_stmt = select(ExternalPlatform).where(
                ExternalPlatform.organization_id == organization_id, ExternalPlatform.platform_type == "slack")
            platform_result = await db.execute(platform_stmt)
            platform = platform_result.scalar_one_or_none()

            if not platform:
                print(f"SLACK_NOTIFIER: No active Slack platform for organization {organization_id}")
                return

            adapter = PlatformAdapterFactory.create_adapter(platform)
            success = False

            if step.type == "table":
                success = await _handle_table_step_dm(adapter, external_user_id, step)
            else:
                success = await _handle_chart_step_dm(adapter, external_user_id, step)

            if success:
                print(f"SLACK_NOTIFIER: Successfully sent step data to Slack user {external_user_id}")
            else:
                print(f"SLACK_NOTIFIER: Failed to send step data to Slack user {external_user_id}")

        except Exception as e:
            print(f"SLACK_NOTIFIER: Error for step {step_id}: {e}")
            await db.rollback()
