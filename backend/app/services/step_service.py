from datetime import datetime
from app.models.step import Step

from sqlalchemy.orm import Session
from app.schemas.step_schema import StepCreate, StepSchema, StepUpdate
from app.models.widget import Widget
import uuid
import json
import pandas as pd
import numpy as np
from sqlalchemy.orm import selectinload

from app.ai.prompt_formatters import TableFormatter
from app.models.completion import Completion
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.report import Report

from app.ai.code_execution.code_execution import CodeExecutionManager



class StepService:

    def __init__(self):
        pass

    async def get_step_by_id(self, db: AsyncSession, step_id: str):
        result = await db.execute(
            select(Step).options(
                selectinload(Step.widget)
                .selectinload(Widget.report)
                .selectinload(Report.data_sources),
                selectinload(Step.widget)
                .selectinload(Widget.report)
                .selectinload(Report.files)
            ).filter(Step.id == step_id)
        )
        step = result.scalar_one_or_none()
        return step

    async def export_step_to_csv(self, db: AsyncSession, step_id: str) -> tuple[pd.DataFrame, Step]:
        step = await self.get_step_by_id(db, step_id)
        if not step:
            raise ValueError(f"Step {step_id} not found")

        data = step.data
        if not data or 'rows' not in data or 'columns' not in data:
            return pd.DataFrame(), step

        rows = data.get('rows', [])
        columns = data.get('columns', [])

        if not rows or not columns:
            return pd.DataFrame(), step

        headers = [col.get('headerName', col.get('field', '')) for col in columns if 'field' in col]
        fields = [col['field'] for col in columns if 'field' in col]

        data_for_df = []
        for row in rows:
            data_for_df.append([row.get(field) for field in fields])

        df = pd.DataFrame(data_for_df, columns=headers)
        return df, step

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
        
        # get messages from the original step
        report = step.widget.report
        if not report:
            raise ValueError("Report not found")
        
        db_clients = {data_source.name: data_source.get_client() for data_source in report.data_sources}

        excel_files = report.files
        code_execution_manager = CodeExecutionManager()
        code = step.code
        
        df, output_log = code_execution_manager.execute_code(code=code, db_clients=db_clients, excel_files=excel_files)
        df = code_execution_manager.format_df_for_widget(df)
        
        # Update existing step instead of creating new one
        step.data = df
        await db.commit()
        await db.refresh(step)

        return StepSchema.from_orm(step)

    async def get_steps_by_widget(self, db: AsyncSession, widget_id: str):
        steps = await db.execute(select(Step).filter(Step.widget_id == widget_id))
        steps = steps.scalars().all()
        return steps