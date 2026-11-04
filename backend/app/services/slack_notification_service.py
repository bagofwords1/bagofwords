import os
import time
import uuid
import csv
import logging
from sqlalchemy import select
from sqlalchemy.orm import lazyload, selectinload
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

# Below this row count, we don't send table data to Slack/WhatsApp (a CSV on
# Slack, a report link on WhatsApp).
# The agent's text answer already describes small results in prose (the
# planner is instructed to do so for small datasets), so attaching a CSV
# on top is noise. Larger results still go through so the user can
# scan/download them (Slack) or open the full table in BOW (WhatsApp). Teams skips table sends entirely (see
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

# Clean, colorblind-friendly palette for chart images sent to chat platforms.
# First color is the primary (single-series bars/lines); the rest cycle for
# multi-category charts (pie slices, etc.).
_CHART_COLORS = [
    "#2563eb",  # blue
    "#0d9488",  # teal
    "#f59e0b",  # amber
    "#db2777",  # pink
    "#7c3aed",  # violet
    "#16a34a",  # green
    "#dc2626",  # red
    "#0891b2",  # cyan
    "#ca8a04",  # gold
    "#64748b",  # slate
]
_INK = "#1e293b"       # axis text / labels
_MUTED = "#94a3b8"     # spines / ticks
_GRID = "#e2e8f0"      # gridlines


def _style_axes(ax):
    """Apply the shared clean look to a chart axes: no top/right spines,
    muted remaining spines, a light horizontal grid behind the data, and
    readable label colors."""
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(_MUTED)
    ax.tick_params(colors=_INK, labelsize=11)
    ax.yaxis.grid(True, color=_GRID, linewidth=1)
    ax.xaxis.grid(False)
    ax.set_axisbelow(True)


def _fmt_value(v) -> str:
    """Compact numeric label for bar annotations (1.2K, 3.5M, else the number)."""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v)
    units = (("", 1.0), ("K", 1e3), ("M", 1e6), ("B", 1e9))
    i = 0
    while i + 1 < len(units) and abs(n) >= units[i + 1][1]:
        i += 1
    if i == 0:
        return f"{n:g}"
    scaled = round(n / units[i][1], 1)
    # Rounding can push a value into the next unit (999,950 -> 1000.0K -> 1M).
    if abs(scaled) >= 1000 and i + 1 < len(units):
        i += 1
        scaled = round(n / units[i][1], 1)
    text = f"{scaled:.1f}".rstrip("0").rstrip(".")
    return f"{text}{units[i][0]}"


def _label_xy(ax, x_col, y_col, rotate_x: bool = True):
    """Axis labels + shared styling for x/y charts (bar, line, area, scatter)."""
    ax.set_xlabel(x_col, color=_INK)
    ax.set_ylabel(y_col, color=_INK)
    if rotate_x:
        plt.setp(ax.get_xticklabels(), rotation=45, ha='right')
    _style_axes(ax)


def create_plot(data_model: dict, data: dict, title: str) -> str:
    """Creates a styled plot image from a step's data and data_model."""
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
    primary = _CHART_COLORS[0]

    try:
        fig, ax = plt.subplots(figsize=(10, 6), facecolor="white")
        ax.set_facecolor("white")

        if chart_type == 'bar_chart':
            x_col, y_col = series['key'], series['value']
            bars = ax.bar(df[x_col].astype(str), df[y_col], color=primary, width=0.65)
            _label_xy(ax, x_col, y_col)
            try:  # value labels above each bar (matplotlib >= 3.4)
                ax.bar_label(bars, labels=[_fmt_value(v) for v in df[y_col]],
                             padding=3, color=_INK, fontsize=10)
            except Exception:
                pass

        elif chart_type == 'line_chart':
            x_col, y_col = series['key'], series['value']
            if pd.api.types.is_string_dtype(df[x_col]):
                try:
                    df[x_col] = pd.to_datetime(df[x_col])
                    df = df.sort_values(by=x_col)
                except (ValueError, TypeError):
                    pass
            ax.plot(df[x_col], df[y_col], marker='o', color=primary, linewidth=2.2,
                    markersize=5, markerfacecolor="white", markeredgecolor=primary)
            _label_xy(ax, x_col, y_col)

        elif chart_type == 'pie_chart':
            labels_col, values_col = series['key'], series['value']
            if len(df) > 10:
                df_sorted = df.nlargest(10, values_col)
                other_sum = df[~df.index.isin(df_sorted.index)][values_col].sum()
                df_plot = pd.concat([df_sorted, pd.DataFrame([{labels_col: 'Other', values_col: other_sum}])])
            else:
                df_plot = df
            colors = [_CHART_COLORS[i % len(_CHART_COLORS)] for i in range(len(df_plot))]
            wedges, texts, autotexts = ax.pie(
                df_plot[values_col], labels=df_plot[labels_col], autopct='%1.1f%%',
                startangle=90, colors=colors,
                wedgeprops=dict(edgecolor="white", linewidth=1.5),
                textprops=dict(color=_INK, fontsize=11))
            for at in autotexts:
                at.set_color("white")
                at.set_fontsize(10)
            ax.axis('equal')

        elif chart_type == 'area_chart':
            x_col, y_col = series['key'], series['value']
            if pd.api.types.is_string_dtype(df[x_col]):
                try:
                    df[x_col] = pd.to_datetime(df[x_col])
                    df = df.sort_values(by=x_col)
                except (ValueError, TypeError):
                    pass
            ax.fill_between(df[x_col], df[y_col], alpha=0.25, color=primary)
            ax.plot(df[x_col], df[y_col], color=primary, linewidth=2.2)
            _label_xy(ax, x_col, y_col)

        elif chart_type == 'scatter_plot':
            x_col, y_col = series['x'], series['y']
            ax.scatter(df[x_col], df[y_col], color=primary, alpha=0.75, edgecolor="white", s=55)
            _label_xy(ax, x_col, y_col, rotate_x=False)

        elif chart_type == 'heatmap':
            x_col, y_col, val_col = series['x'], series['y'], series['value']
            pivot_df = df.pivot(index=y_col, columns=x_col, values=val_col)
            im = ax.imshow(pivot_df, cmap='viridis', aspect='auto')
            fig.colorbar(im, ax=ax)
            ax.set_xticks(range(len(pivot_df.columns)))
            ax.set_xticklabels(pivot_df.columns, rotation=90)
            ax.set_yticks(range(len(pivot_df.index)))
            ax.set_yticklabels(pivot_df.index)
            ax.tick_params(colors=_INK, labelsize=11)

        elif chart_type in ['candlestick', 'map', 'treemap', 'radar_chart']:
            ax.text(0.5, 0.5, f"'{chart_type}' is a complex chart.\nPlotting support is coming soon!",
                    ha='center', va='center', size=12, bbox=dict(facecolor='lightgray', alpha=0.5))
            ax.axis('off')

        else:
            logger.warning("Unsupported chart type: %s", chart_type)
            return None

        ax.set_title(title, color=_INK, fontsize=15, fontweight="bold", pad=14)
        fig.tight_layout()

        image_path = f"/tmp/{uuid.uuid4()}.png"
        fig.savefig(image_path, dpi=150, facecolor="white", bbox_inches="tight")
        return image_path

    except Exception as e:
        logger.warning("Error creating plot for chart type %r: %s", chart_type, e)
        return None
    finally:
        plt.close('all')

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

# (external_user_id, report_id) -> monotonic time the WhatsApp report link was
# sent. Per-process only; it just keeps one answer from repeating its link.
_REPORT_LINK_DEDUP_SECONDS = 120
_recent_report_links: dict = {}


async def _handle_table_step_link(adapter, external_user_id: str, step: 'Step', thread_ts: str = None, channel_id: str = None):
    """WhatsApp: link to the full table in BOW instead of a raw CSV file.

    A UUID-named CSV attachment is useless inside a phone chat — the customer
    can't open or act on it. A link opens the real, interactive table in the
    web report instead.
    """
    from app.settings.config import settings
    # widget.report is eager-loaded by send_step_result_to_slack.
    report = step.widget.report if step.widget else None
    if not report:
        logger.info("SLACK_NOTIFIER: No report for table step %s; skipping WhatsApp link", step.id)
        return False
    # Every table step of one answer belongs to the same report, so they would
    # all send the same URL. Send it once per user+report within a short window.
    key = (external_user_id, str(report.id))
    now = time.monotonic()
    for k, ts in list(_recent_report_links.items()):
        if now - ts > _REPORT_LINK_DEDUP_SECONDS:
            del _recent_report_links[k]
    if key in _recent_report_links:
        logger.info("SLACK_NOTIFIER: Report link for %s already sent; skipping duplicate", report.id)
        return True
    _recent_report_links[key] = now
    title = step.title or "Table"
    url = f"{settings.bow_config.base_url}/reports/{report.id}"
    # Bare URL (not a markdown link) so WhatsApp auto-links it; *title* is WhatsApp bold.
    msg = f"*{title}*\n{url}"
    return await adapter.send_dm_in_thread(external_user_id, msg, thread_ts, channel_id=channel_id)

async def _handle_chart_step_dm(adapter, external_user_id: str, step: 'Step', thread_ts: str = None, channel_id: str = None, platform_type: str = None):
    """Handles sending chart data (as an image), optionally in a thread."""
    title = step.title or "Chart"

    # Teams: send text summary (Bot Connector doesn't support data: URLs for image uploads)
    # Google Chat: media upload rejects app-auth credentials, so same fallback.
    if platform_type in ("teams", "google_chat"):
        bold = f"**{title}**" if platform_type == "teams" else f"*{title}*"
        msg = f"{bold}\n_Chart visualization is available in the web report._"
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
            # Discover routing details only if not explicitly provided. Resolve
            # them BEFORE loading the step: the step-update hook calls this for
            # every successful step, and web runs (no chat-linked completion)
            # must exit after this one lookup rather than after loading the
            # step's widget/report graph.
            completion = None
            if external_user_id is None or organization_id is None:
                comp_stmt = (
                    select(Completion)
                    .options(lazyload("*"))
                    .where(Completion.step_id == step_id)
                    .order_by(Completion.created_at.desc())
                    .limit(1)
                )
                comp_result = await db.execute(comp_stmt)
                completion = comp_result.scalar_one_or_none()

                if not (completion and completion.external_platform in ("slack", "teams", "whatsapp", "google_chat") and completion.external_user_id):
                    logger.info("SLACK_NOTIFIER: No chat-linked completion found for step %s. Caller should supply routing details.", step_id)
                    return

            stmt = select(Step).options(
                selectinload(Step.widget).selectinload(Widget.report)
            ).where(Step.id == step_id)
            result = await db.execute(stmt)
            step = result.scalar_one_or_none()
            if not step:
                logger.info("SLACK_NOTIFIER: Could not find step with id %s", step_id)
                return

            if completion is not None:
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

            # Teams / Google Chat: never send table data. The agent's final
            # text answer describes the data in-message, and neither platform
            # can take a CSV upload under app auth (Teams renders its own
            # markdown table; Chat's media upload rejects app credentials).
            # Slack gets a CSV attachment and WhatsApp a link to the report
            # (see below), not a second table in the chat.
            if data_type == "table" and platform_type in ("teams", "google_chat"):
                logger.info(
                    "SLACK_NOTIFIER: Skipping table send for step %s (%s gets data via the text answer)",
                    step_id, platform_type,
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
                if platform_type == "whatsapp":
                    # WhatsApp: a raw CSV is useless on a phone — send a link to
                    # the full table in BOW instead (customer request).
                    success = await _handle_table_step_link(adapter, external_user_id, step, thread_ts, channel_id)
                else:
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
