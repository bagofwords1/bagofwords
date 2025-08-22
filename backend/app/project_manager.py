from app.models.completion import Completion
from app.models.text_widget import TextWidget
from sqlalchemy.orm import Session
import datetime
from app.schemas.completion_schema import PromptSchema
from app.models.widget import Widget
from app.models.step import Step
from app.models.plan import Plan
from app.models.report import Report
from sqlalchemy import select, delete
import logging
from app.services.table_usage_service import TableUsageService
from app.schemas.table_usage_schema import TableUsageEventCreate
from app.utils.lineage import extract_tables_from_data_model

class ProjectManager:

    def __init__(self) -> None:
        self.logger = logging.getLogger(__name__)
        self.table_usage_service = TableUsageService()

    async def emit_table_usage(self, db, report: Report, step: Step, data_model: dict, user_id: str | None = None, user_role: str | None = None, source_type: str = "sql"):
        try:
            report_ds_ids = [str(ds.id) for ds in (getattr(report, 'data_sources', []) or [])]
            lineage_entries = await extract_tables_from_data_model(db, data_model, report_ds_ids)
            for entry in lineage_entries:
                ds_id = entry.get("datasource_id") or (report_ds_ids[0] if len(report_ds_ids) == 1 else None)
                table_name = entry.get("table_name")
                if not ds_id or not table_name:
                    continue
                table_fqn = table_name.lower()
                payload = TableUsageEventCreate(
                    org_id=str(report.organization_id),
                    report_id=str(report.id),
                    data_source_id=ds_id,
                    step_id=str(step.id),
                    user_id=user_id,
                    table_fqn=table_fqn,
                    datasource_table_id=entry.get("datasource_table_id"),
                    source_type=source_type,
                    columns=entry.get("columns") or [],
                    success=(step.status == "success"),
                    user_role=user_role,
                    role_weight=None,
                )
                await self.table_usage_service.record_usage_event(db=db, payload=payload)
        except Exception as e:
            self.logger.warning(f"emit_table_usage failed: {e}")

    async def create_error_completion(self, db, head_completion, error):
        error_completion = Completion(model=head_completion.model,completion={"content": error, "error": True},
            prompt=None,
            status="error",
            parent_id=head_completion.id,
            message_type="error",
            role="system",
            report_id=head_completion.report_id if head_completion.report_id else None,
            widget_id=head_completion.widget_id if head_completion.widget_id else None,
            external_platform=head_completion.external_platform,
            external_user_id=head_completion.external_user_id
        )

        db.add(error_completion)
        await db.commit()
        await db.refresh(error_completion)
        return error_completion

    async def create_message(self, db, report, message=None, status="in_progress", reasoning=None, completion=None, widget=None, role="system", step=None, external_platform=None, external_user_id=None):
        completion_message = PromptSchema(content="", reasoning="")
        if message is not None:
            completion_message.content = message
        if reasoning is not None:
            completion_message.reasoning = reasoning

        completion_message = completion_message.dict()

        new_completion = Completion(
            completion=completion_message,
            model="gpt4o",
            status=status,
            turn_index=0,
            parent_id=completion.id if completion else None,
            message_type="ai_completion",
            role=role,
            report_id=report.id,  # Assuming 'report' is an instance of the Report model
            widget_id=widget.id if widget else None,   # or pass a widget ID if available
            step_id=step.id if step else None,
            external_platform=external_platform,
            external_user_id=external_user_id
        )

        db.add(new_completion)
        await db.commit()
        await db.refresh(new_completion)

        return new_completion
    
    async def update_completion_with_step(self, db, completion, step):
        completion.step_id = step.id
        db.add(completion)
        await db.commit()
        await db.refresh(completion)
        return completion

    async def update_completion_with_widget(self, db, completion, widget):
        completion.widget_id = widget.id
        db.add(completion)
        await db.commit()
        await db.refresh(completion)
        return completion
    
    async def update_message(self, db, completion, message=None, reasoning=None):
        # Handle the case where completion.completion might be a string
        if isinstance(completion.completion, str):
            completion.completion = {'content': message, 'reasoning': reasoning}
        else:
            # Create a new dictionary to ensure SQLAlchemy detects the change
            completion.completion = {
                **completion.completion,  # Spread existing completion data
                'content': message,
                'reasoning': reasoning
            }
        #  Mark as modified to ensure SQLAlchemy picks up the change
        db.add(completion)
        await db.commit()
        await db.refresh(completion)
        return completion
    
    async def create_widget(self, db, report, title):
        widget = Widget(
            title=title,
            report_id=report.id,
            status="draft",
            x=0,
            y=0,
            width=5,
            height=9,
            slug=title.lower().replace(" ", "-")
        )

        db.add(widget)
        await db.commit()
        await db.refresh(widget)

        return widget
    
    async def create_step(self, db, title, widget, step_type):
        step = Step(
            title=title,
            slug=title.lower().replace(" ", "-"),
            type=step_type,
            widget_id=widget.id,
            code="",
            data={},
            data_model={},
            status="draft"
        )

        db.add(step)
        await db.commit()
        await db.refresh(step)

        return step
    
    async def update_step_with_code(self, db, step, code):
        step.code = code
        db.add(step)
        await db.commit()
        await db.refresh(step)
        return step
    
    async def update_step_with_data(self, db, step, data):
        step.data = data
        db.add(step)
        await db.commit()
        await db.refresh(step)
        return step
    
    async def update_step_status(self, db, step, status, status_reason=None):
        step.status = status
        step.status_reason = status_reason
        db.add(step)
        await db.commit()
        await db.refresh(step)
        return step
    
    async def update_widget_position_and_size(self, db, widget_id, x, y, width, height):
        widget = await db.get(Widget, widget_id)
        widget.x = x
        widget.y = y
        widget.width = width
        widget.height = height
        widget.status = "published"

        db.add(widget)
        await db.commit()
        await db.refresh(widget)
        return widget
    
    async def create_text_widget(self, db, content, x, y, width, height, report_id):
        text_widget = TextWidget(
            content=content,
            x=x,
            y=y,
            width=width,
            height=height,
            report_id=report_id
        )

        db.add(text_widget)
        await db.commit()
        await db.refresh(text_widget)

        return text_widget
    
    async def delete_text_widgets_for_report(self, db, report_id):
        """Deletes all TextWidget entries associated with a given report_id."""
        stmt = delete(TextWidget).where(TextWidget.report_id == report_id)
        await db.execute(stmt)
        await db.commit()
        # No object to refresh after deletion
        print(f"Deleted existing text widgets for report {report_id}") # Optional logging
    
    async def update_report_title(self, db, report, title):
        # Instead of merging, let's fetch a fresh instance
        stmt = select(Report).where(Report.id == report.id)
        report = (await db.execute(stmt)).scalar_one()
        
        # Update the title
        report.title = title
        
        # Explicitly mark as modified
        db.add(report)
        await db.commit()
        await db.refresh(report)
        return report
    
    async def create_plan(self, db, report, content, completion):
        plan = Plan(
            content=content,
            completion_id=completion.id,
            report_id=report.id,
            organization_id=report.organization_id,
            user_id=completion.user_id
        )

        db.add(plan)
        await db.commit()
        await db.refresh(plan)

        return plan
    
    async def update_plan(self, db, plan, content):
        plan.content = content
        db.add(plan)
        await db.commit()
        await db.refresh(plan)
        return plan
    

    async def update_completion_status(self, db, completion, status):
        completion.status = status
        db.add(completion)
        await db.commit()
        await db.refresh(completion)
        return completion
        
    async def update_completion_scores(self, db, completion, instructions_score=None, context_score=None):
        """Update instructions and context effectiveness scores for a completion."""
        if instructions_score is not None:
            completion.instructions_effectiveness = instructions_score
        if context_score is not None:
            completion.context_effectiveness = context_score
        
        db.add(completion)
        await db.commit()
        await db.refresh(completion)
        return completion

    async def update_completion_response_score(self, db, completion, response_score):
        """Update response score for a completion."""
        completion.response_score = response_score
        db.add(completion)
        await db.commit()
        await db.refresh(completion)
        return completion
        