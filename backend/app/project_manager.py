from app.models.completion import Completion
from app.models.text_widget import TextWidget
from sqlalchemy.orm import Session
import datetime
from app.schemas.completion_schema import PromptSchema
from app.models.widget import Widget
from app.models.step import Step
from app.models.plan import Plan
from app.models.report import Report
from sqlalchemy import select

class ProjectManager:

    def __init__(self) -> None:
        pass

    async def create_error_completion(self, db, head_completion, error):
        error_completion = Completion(model=head_completion.model,completion={"content": error, "error": True},
            prompt=None,
            status="error",
            parent_id=head_completion.id,
            message_type="error",
            role="system",
            report_id=head_completion.report_id if head_completion.report_id else None,
            widget_id=head_completion.widget_id if head_completion.widget_id else None
        )

        db.add(error_completion)
        await db.commit()
        await db.refresh(error_completion)
        return error_completion

    async def create_message(self, db, report, message, completion, widget, role, step=None):
        
        completion_message = PromptSchema(content=message).dict()

        new_completion = Completion(
            completion=completion_message,
            model="gpt4o",
            status="success",
            turn_index=0,
            parent_id=None,
            message_type="ai_completion",
            role=role,
            report_id=report.id,  # Assuming 'report' is an instance of the Report model
            widget_id=widget.id if widget else None,   # or pass a widget ID if available
            step_id=step.id if step else None
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
    
    async def update_message(self, db, completion, message):
        # Handle the case where completion.completion might be a string
        if isinstance(completion.completion, str):
            completion.completion = {'content': message}
        else:
            # Create a new dictionary to ensure SQLAlchemy detects the change
            completion.completion = {
                **completion.completion,  # Spread existing completion data
                'content': message        # Update content
            }
        
        # Mark as modified to ensure SQLAlchemy picks up the change
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
            width=400,
            height=400,
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
    
    async def update_step_status(self, db, step, status):
        step.status = status
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
        return plan