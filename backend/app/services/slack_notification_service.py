import os
import uuid
import csv
import logging
from sqlalchemy import select
from sqlalchemy.orm import selectinload
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from app.models.step import Step
from app.models.widget import Widget
from app.models.completion import Completion
from app.models.external_platform import ExternalPlatform
from app.settings.database import create_async_session_factory
from app.services.platform_adapters.adapter_factory import PlatformAdapterFactory

# This service runs as a fire-and-forget asyncio.create_task() spawned
# from Step.after_update. The bg task outlives the request that created
# it, so any print() here would raise ValueError("I/O operation on
# closed file") whenever uvicorn rotated stdout under us. Use the
# standard module logger.
logger = logging.getLogger(__name__)

# Below this row count, we don't send the raw table data to Slack/WhatsApp.
# The agent's text answer already describes small results in prose (the
# planner is instructed to do so for small datasets), so attaching a CSV
# on top is noise. Larger results still go through so the user can
# scan/download them. Teams skips table sends entirely (see
# send_step_result_to_slack): the agent's text answer renders its own
# markdown table there, so a second raw table is a duplicate.
MIN_ROWS_TO_SEND = 10


def _step_row_count(step: 'Step') -> int:
    """Return the number of data rows on a step, or 0 if unavailable."""
    try:
        rows = (step.data or {}).get('rows')
        return len(rows) if rows else 0
    except Exception:
        return 0

def create_plot(data_model: dict, data: dict, title: str) -> str:
    """Creates a plot from a step's data and data_model."""
    chart_type = data_model.get('type')
    series_info = data_model.get('series')
    rows = data.get('rows')

    if not all([chart_type, series_info, rows]):
        logger.warning("Plot creation failed: Missing chart_type, series, or data rows.")
        return None

    df = pd.DataFrame(rows)
    if df.empty:
        logger.warning("Plot creation failed: DataFrame is empty.")
        return None

    series = series_info[0]

    try:
        plt.figure(figsize=(10, 6))

        if chart_type == 'bar_chart':
            x_col, y_col = series['key'], series['value']
            plt.bar(df[x_col], df[y_col])
            plt.xlabel(x_col)
            plt.ylabel(y_col)
            plt.xticks(rotation=45, ha='right')

        elif chart_type == 'line_chart':
            x_col, y_col = series['key'], series['value']
            if pd.api.types.is_string_dtype(df[x_col]):
                try:
                    df[x_col] = pd.to_datetime(df[x_col])
                    df = df.sort_values(by=x_col)
                except (ValueError, TypeError):
                    pass
            plt.plot(df[x_col], df[y_col], marker='o')
            plt.xlabel(x_col)
            plt.ylabel(y_col)
            plt.xticks(rotation=45, ha='right')

        elif chart_type == 'pie_chart':
            labels_col, values_col = series['key'], series['value']
            if len(df) > 10:
                df_sorted = df.nlargest(10, values_col)
                other_sum = df[~df.index.isin(df_sorted.index)][values_col].sum()
                df_plot = pd.concat([df_sorted, pd.DataFrame([{labels_col: 'Other', values_col: other_sum}])])
            else:
                df_plot = df
            plt.pie(df_plot[values_col], labels=df_plot[labels_col], autopct='%1.1f%%', startangle=90)
            plt.axis('equal')

        elif chart_type == 'area_chart':
            x_col, y_col = series['key'], series['value']
            if pd.api.types.is_string_dtype(df[x_col]):
                try:
                    df[x_col] = pd.to_datetime(df[x_col])
                    df = df.sort_values(by=x_col)
                except (ValueError, TypeError):
                    pass
            plt.fill_between(df[x_col], df[y_col], alpha=0.4)
            plt.xlabel(x_col)
            plt.ylabel(y_col)
            plt.xticks(rotation=45, ha='right')
        
        elif chart_type == 'scatter_plot':
            x_col, y_col = series['x'], series['y']
            plt.scatter(df[x_col], df[y_col])
            plt.xlabel(x_col)
            plt.ylabel(y_col)

        elif chart_type == 'heatmap':
            x_col, y_col, val_col = series['x'], series['y'], series['value']
            pivot_df = df.pivot(index=y_col, columns=x_col, values=val_col)
            cax = plt.matshow(pivot_df, cmap='viridis')
            plt.colorbar(cax)
            plt.xticks(ticks=range(len(pivot_df.columns)), labels=pivot_df.columns, rotation=90)
            plt.yticks(ticks=range(len(pivot_df.index)), labels=pivot_df.index)

        elif chart_type in ['candlestick', 'map', 'treemap', 'radar_chart']:
            plt.text(0.5, 0.5, f"'{chart_type}' is a complex chart.\nPlotting support is coming soon!", 
                     ha='center', va='center', size=12, bbox=dict(facecolor='lightgray', alpha=0.5))
            plt.axis('off')

        else:
            logger.warning("Unsupported chart type: %s", chart_type)
            return None

        plt.title(title)
        plt.grid(True, linestyle='--', alpha=0.6)
        plt.tight_layout()
        
        image_path = f"/tmp/{uuid.uuid4()}.png"
        plt.savefig(image_path)
        return image_path

    except Exception as e:
        logger.warning("Error creating plot for chart type %r: %s", chart_type, e)
        return None
    finally:
        plt.close()

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
        logger.warning("Error creating CSV file: %s", e)
        return None

async def _handle_table_step_dm(adapter, external_user_id: str, step: 'Step', thread_ts: str = None, channel_id: str = None):
    """Handles sending table data as a CSV file, optionally in a thread."""
    title = step.title or "Table Data"

    file_path = df_to_csv(step.data)
    if not file_path:
        return False

    success = False
    try:
        success = await adapter.send_file_in_thread(external_user_id, file_path, title, thread_ts, channel_id=channel_id)
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
    return success

async def _handle_chart_step_dm(adapter, external_user_id: str, step: 'Step', thread_ts: str = None, channel_id: str = None, platform_type: str = None):
    """Handles sending chart data (as an image), optionally in a thread."""
    title = step.title or "Chart"

    # Teams: send text summary (Bot Connector doesn't support data: URLs for image uploads)
    if platform_type == "teams":
        msg = f"**{title}**\n_Chart visualization is available in the web report._"
        return await adapter.send_dm_in_thread(external_user_id, msg, thread_ts, channel_id=channel_id)

    file_path = create_plot(step.data_model, step.data, title)

    if not file_path:
        return False

    success = False
    try:
        success = await adapter.send_file_in_thread(external_user_id, file_path, title, thread_ts, channel_id=channel_id)
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
    return success

async def send_step_result_to_slack(step_id: str, external_user_id: str | None = None, organization_id: str | None = None, thread_ts: str | None = None, channel_id: str | None = None, platform_type: str | None = None):
    """
    Sends a step's data to Slack, optionally in a thread.
    If channel_id is provided, sends directly to that channel (for channel mentions).
    Otherwise, opens a DM with the user.
    Prefer caller-provided routing details (external_user_id and organization_id).
    If not provided, falls back to discovering them from the latest completion
    associated with the step.
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
                logger.info("SLACK_NOTIFIER: Could not find step with id %s", step_id)
                return

            # Discover routing details only if not explicitly provided
            if external_user_id is None or organization_id is None:
                comp_stmt = select(Completion).where(Completion.step_id == step_id).order_by(Completion.created_at.desc()).limit(1)
                comp_result = await db.execute(comp_stmt)
                completion = comp_result.scalar_one_or_none()

                if not (completion and completion.external_platform in ("slack", "teams", "whatsapp") and completion.external_user_id):
                    logger.info("SLACK_NOTIFIER: No chat-linked completion found for step %s. Caller should supply routing details.", step_id)
                    return

                external_user_id = external_user_id or completion.external_user_id
                organization_id = organization_id or step.widget.report.organization_id
                platform_type = platform_type or completion.external_platform
                # Also get thread_ts from completion if not provided
                if thread_ts is None:
                    thread_ts = completion.external_thread_ts

            if not platform_type:
                logger.info("SLACK_NOTIFIER: No platform_type available for step %s", step_id)
                return

            platform_stmt = select(ExternalPlatform).where(
                ExternalPlatform.organization_id == organization_id, ExternalPlatform.platform_type == platform_type)
            platform_result = await db.execute(platform_stmt)
            platform = platform_result.scalar_one_or_none()

            if not platform:
                logger.info("SLACK_NOTIFIER: No active Slack platform for organization %s", organization_id)
                return

            adapter = PlatformAdapterFactory.create_adapter(platform)
            success = False
            data_type = step.data_model.get('type')

            # Teams: never send table data. The agent's final text answer
            # renders its own (better-formatted) markdown table in Teams, so a
            # separate raw table arrives as a duplicate. Slack/WhatsApp are
            # unaffected — their data goes out as a CSV attachment, not a
            # second table in the chat.
            if data_type == "table" and platform_type == "teams":
                logger.info(
                    "SLACK_NOTIFIER: Skipping table send for step %s (Teams gets data via the text answer)",
                    step_id,
                )
                return

            # Don't send small table results — the agent's text answer already
            # describes them. Charts (sent as images) and counts (scalar
            # answers) are unaffected.
            if data_type == "table" and _step_row_count(step) < MIN_ROWS_TO_SEND:
                logger.info(
                    "SLACK_NOTIFIER: Skipping data send for step %s (%d rows < %d threshold)",
                    step_id, _step_row_count(step), MIN_ROWS_TO_SEND,
                )
                return

            if data_type == "table":
                success = await _handle_table_step_dm(adapter, external_user_id, step, thread_ts, channel_id)
            elif data_type == "count":
                if step.data and 'rows' in step.data and step.data['rows'] and 'columns' in step.data and step.data['columns']:
                    count_value = step.data['rows'][0].get(step.data['columns'][0].get('field', ''))
                    message = f"**{step.title or 'Count'}**: {count_value}" if platform_type == "teams" else f"*{step.title or 'Count'}*: {count_value}"
                    success = await adapter.send_dm_in_thread(external_user_id, message, thread_ts, channel_id=channel_id)
                else:
                    success = await adapter.send_dm_in_thread(external_user_id, f"I couldn't retrieve the value for '{step.title or 'Count'}'.", thread_ts, channel_id=channel_id)
            else:
                success = await _handle_chart_step_dm(adapter, external_user_id, step, thread_ts, channel_id, platform_type=platform_type)

            if success:
                logger.info("SLACK_NOTIFIER: Successfully sent step data to Slack user %s", external_user_id)
            else:
                logger.warning("SLACK_NOTIFIER: Failed to send step data to Slack user %s", external_user_id)

        except Exception as e:
            logger.warning("SLACK_NOTIFIER: Error for step %s: %s", step_id, e)
            await db.rollback()
