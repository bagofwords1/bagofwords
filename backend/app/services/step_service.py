import datetime
from app.models.step import Step

from sqlalchemy.orm import Session
from app.schemas.step_schema import StepCreate, StepSchema, StepUpdate
from app.models.widget import Widget
import uuid
import json
import pandas as pd
import numpy as np

from app.ai.agent import Agent
from app.ai.prompt_formatters import TableFormatter
from app.models.completion import Completion
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.report import Report



class StepService:

    def __init__(self):
        pass

    async def get_step_by_id(self, db: AsyncSession, step_id: str):
        step = await db.execute(select(Step).filter(Step.id == step_id))
        step = step.scalar_one_or_none()
        return step

    async def create_step(self, db: AsyncSession, widget_id: str, completion_id: str) -> StepSchema:

        widget = await db.execute(select(Widget).filter(Widget.id == widget_id))
        widget = widget.scalar_one_or_none()
        if not widget:
            raise ValueError("Widget not found")
        

        # code_context = self._build_code_context(completion)
        completion = await db.execute(select(Completion).filter(Completion.id == completion_id))
        completion = completion.scalar_one_or_none()
        if not completion:
            raise ValueError("Completion not found")
        
        # Ensure the completion is fully loaded
        if completion.report is None:
            await db.refresh(completion)  # Refresh to load the report if it's lazy-loaded
        
        step_data = StepCreate(
            prompt=completion.prompt["content"],
            widget_id=widget.id,
            code="hello world",
            title="Step {uuid}".format(uuid=uuid.uuid4().hex[:4]),
            slug="step-{uuid}".format(uuid=uuid.uuid4().hex[:4]),
            status="published",
            data={"key": "value"},
            type="table",
            data_model={"key": "value"},
        )

        try:
            step = Step(**step_data.dict())
            
            db.add(step)
            await db.commit()
            await db.refresh(step)

        except Exception as e:
            print(f"Error creating Step object: {e}")
            raise
        
        step_schema = StepSchema.from_orm(step)

        return step_schema
    
    async def rerun_step(self, db: AsyncSession, step_id: str):

        step = await self.get_step_by_id(db, step_id)


        if not step:
            raise ValueError("Step not found")
        
        widget = await db.execute(select(Widget).filter(Widget.id == step.widget_id))
        widget = widget.scalar_one_or_none()
        if not widget:
            raise ValueError("Widget not found")

        report = await db.execute(select(Report).filter(Report.id == widget.report_id))
        report = report.scalar_one_or_none()
        if not report:
            raise ValueError("Report not found")
        
        # get original head completion
        completion = await db.execute(select(Completion).filter(Completion.widget_id == widget.id).order_by(Completion.created_at.asc()).limit(1))
        completion = completion.scalar_one_or_none()
        if not completion:
            raise ValueError("Completion not found")
        
        # get messages from the original step
        default_model = await report.organization.get_default_llm_model(db)

        agent = Agent(db=db, report=report, messages=[], head_completion=completion, step=step, model=default_model)
        df = agent.execute_code_and_return_df(step.code)
        df = agent._format_df(df)
        
        # Update existing step instead of creating new one
        step.data = df
        await db.commit()
        await db.refresh(step)

        return StepSchema.from_orm(step)

    async def get_steps_by_widget(self, db: AsyncSession, widget_id: str):
        steps = await db.execute(select(Step).filter(Step.widget_id == widget_id))
        steps = steps.scalars().all()
        return steps