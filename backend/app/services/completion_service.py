import asyncio
from fastapi.responses import StreamingResponse
import json
import logging

from app.models.plan import Plan
from app.models.completion import Completion
from app.models.report import Report
from app.models.widget import Widget
from app.models.mention import Mention, MentionType
from app.models.organization import Organization
from app.models.step import Step
from app.models.user import User

from sqlalchemy.ext.asyncio import AsyncSession
from app.schemas.completion_schema import CompletionSchema, CompletionCreate, PromptSchema
from app.schemas.step_schema import StepSchema
from app.schemas.widget_schema import WidgetSchema


from app.services.step_service import StepService
from app.services.widget_service import WidgetService
from app.services.report_service import ReportService
from app.services.mention_service import MentionService
from app.services.memory_service import MemoryService
from app.websocket_manager import websocket_manager

from sqlalchemy import select, update

from fastapi import BackgroundTasks, HTTPException

from app.ai.agent import Agent

import re

class CompletionService:

    def __init__(self):
        self.step_service = StepService()
        self.widget_service = WidgetService()
        self.report_service = ReportService()
        self.memory_service = MemoryService()
        self.mention_service = MentionService()

    async def _serialize_completion(self, db: AsyncSession, completion: Completion, current_user: User = None, organization: Organization = None) -> CompletionSchema:
        """Serialize a completion model to a schema following get_completions format"""
        if completion.role == "user":
            prompt = PromptSchema.from_orm(completion.prompt)
            completion_prompt = None
        else: # ai_agent or system
            completion_prompt = PromptSchema.from_orm(completion.completion)
            prompt = None

        if completion.widget_id and current_user and organization:
            widget = await self.widget_service.get_widget_by_id(db, str(completion.widget_id), current_user, organization)
        else:
            widget = None

        if completion.step_id:
            step = await self.step_service.get_step_by_id(db, completion.step_id)
        else:
            step = None

        return CompletionSchema(
            id=completion.id,
            prompt=prompt,
            completion=completion_prompt,
            model=completion.model,
            status=completion.status,
            turn_index=completion.turn_index,
            parent_id=completion.parent_id,
            message_type=completion.message_type,
            role=completion.role,
            report_id=completion.report_id,
            created_at=completion.created_at,
            updated_at=completion.updated_at,
            step_id=completion.step_id,
            step=StepSchema.from_orm(step) if step else None,
            widget=WidgetSchema.from_orm(widget).copy(
                update={"last_step": await self.widget_service._get_last_step(db, widget.id)}
            ) if completion.role == "system" and widget else None
        )

    async def create_completion(
        self, 
        db: AsyncSession, 
        report_id: str, 
        completion_data: CompletionCreate, 
        current_user: User, 
        organization: Organization, 
        background_tasks: BackgroundTasks,
        background: bool = True
    ):
        try:
            print("CompletionService: Starting create_completion")

            # Validate report exists
            result = await db.execute(select(Report).filter(Report.id == report_id))
            report = result.scalar_one_or_none()
            if not report:
                raise HTTPException(status_code=404, detail="Report not found")

            # Validate widget if provided
            if completion_data.prompt.widget_id:
                result = await db.execute(select(Widget).filter(Widget.id == completion_data.prompt.widget_id))
                widget = result.scalar_one_or_none()
                if not widget:
                    raise HTTPException(status_code=404, detail="Widget not found")
            else:
                widget = None
            
            # Validate step if provided
            if completion_data.prompt.step_id:
                step = await db.execute(select(Step).filter(Step.id == completion_data.prompt.step_id))
                step = step.scalar_one_or_none()
                if not step:
                    raise HTTPException(status_code=404, detail="Step not found")
            else:
                step = None

            # Get default model - this is critical
            default_model = await organization.get_default_llm_model(db)
            if not default_model:
                raise HTTPException(
                    status_code=400, 
                    detail="No default LLM model configured. Please configure a default model in organization settings."
                )

            # Create user completion
            prompt_dict = completion_data.prompt.dict()
            prompt_dict['widget_id'] = str(prompt_dict['widget_id']) if prompt_dict['widget_id'] else None
            
            last_completion = await self.get_last_completion(db, report.id)
            
            completion = Completion(
                prompt=prompt_dict,
                model=default_model.model_id,  # We know this exists now
                widget_id=str(widget.id) if widget else None,
                report_id=report.id,
                parent_id=last_completion.id if last_completion else None,
                turn_index=last_completion.turn_index + 1 if last_completion else 0,
                message_type="table",
                role="user",
                status="success",
                user_id=current_user.id
            )

            try:
                db.add(completion)
                await db.commit()
                await db.refresh(completion)
            except Exception as e:
                await db.rollback()
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to save user completion: {str(e)}"
                )

            # Create system completion
            system_completion = Completion(
                prompt=None,
                completion={"content": ""},
                model=default_model.model_id,
                widget_id=prompt_dict['widget_id'],
                report_id=report.id,
                parent_id=completion.id,
                turn_index=completion.turn_index + 1,
                message_type="table",
                role="system",
                status="in_progress"
            )

            try:
                db.add(system_completion)
                await db.commit()
                await db.refresh(system_completion)
            except Exception as e:
                await db.rollback()
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to save system completion: {str(e)}"
                )

            # Setup agent
            try:
                org_settings = await organization.get_settings(db)
                mentions = await self.mention_service.create_completion_mentions(db, completion)
                agent = Agent(
                    db=db,
                    organization_settings=org_settings,
                    model=default_model,
                    report=report,
                    messages=[],
                    head_completion=completion,
                    system_completion=system_completion,
                    widget=widget,
                    step=step
                )
            except Exception as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to initialize agent: {str(e)}"
                )

            if background:
                background_tasks.add_task(agent.main_execution)
                return None
            else:
                try:
                    await agent.main_execution()
                    return await self._serialize_completion(db, system_completion, current_user, organization)
                except Exception as e:
                    # Create error completion and raise exception
                    await self._create_error_completion(db, completion, str(e))
                    raise HTTPException(
                        status_code=500,
                        detail=f"Agent execution failed: {str(e)}"
                    )

        except HTTPException as he:
            # Log the error and re-raise HTTP exceptions
            logging.error(f"HTTP Exception in create_completion: {str(he)}")
            raise he
        except Exception as e:
            # Log and convert unexpected errors to HTTP exceptions
            logging.error(f"Unexpected error in create_completion: {str(e)}")
            raise HTTPException(
                status_code=500,
                detail=f"Unexpected error: {str(e)}"
            )

    async def get_completion_stream(self, db: AsyncSession, completion_id: str, report_id: str):
        completion = await db.execute(select(Completion).where(Completion.id == completion_id))
        completion = completion.scalars().first()

        if not completion:
            raise HTTPException(status_code=404, detail="Completion not found")
        
        return completion


    def _validate_prompt(self, prompt):
        return prompt


    async def get_completions(self, db: AsyncSession, report_id: str, organization: Organization, current_user: User):
        report = await self.report_service.get_report(db, report_id, current_user, organization)

        if not report:
            raise HTTPException(status_code=404, detail="Report not found")
        
        completions = await db.execute(select(Completion).where(Completion.report_id == report_id).order_by(Completion.created_at.asc()))
        completions = completions.scalars().all()
        
        response = []
        for completion in completions:
            serialized_completion = await self._serialize_completion(db, completion, current_user, organization)
            response.append(serialized_completion)

        return response


    async def get_memories(self, db: AsyncSession, completion_id: str, organization: Organization):
        completion = await db.execute(select(Completion).where(Completion.id == completion_id))
        completion = completion.scalars().first()

        report = await self._can_access(db, Report, completion.report_id, organization)

        memories = select(Mention).where(Mention.completion_id == completion_id, Mention.type == MentionType.MEMORY)
        memories = await db.execute(memories)
        memories = memories.scalars().all()
        return memories

    async def get_last_completion(self, db: AsyncSession, report_id: str):
        stmt = select(Completion).where(Completion.report_id == report_id).order_by(Completion.created_at.desc()).limit(1)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()
    
    async def _create_error_completion(self, db: AsyncSession, completion: Completion, error: str):
        error_completion = Completion(
            model=completion.model,
            completion={"content": error, "error": True},
            prompt=None,
            status="error",
            parent_id=completion.id,
            message_type="error",
            role="system",
            report_id=completion.report_id,
            widget_id=completion.widget_id
        )

        db.add(error_completion)
        await db.commit()
        await db.refresh(error_completion)
        return error_completion
    

    async def get_completion_plan(self, db: AsyncSession, current_user: User, organization: Organization, completion_id: str):
        completion = await db.execute(select(Completion).where(Completion.id == completion_id))
        completion = completion.scalars().first()

        if not completion:
            raise HTTPException(status_code=404, detail="Completion not found")

        plan = await db.execute(select(Plan).where(Plan.completion_id == completion_id))
        plan = plan.scalars().first()

        if not plan:
            raise HTTPException(status_code=404, detail="Plan not found")

        return plan

    async def update_completion_feedback(self, db: AsyncSession, completion_id: str, vote: int):
        completion = await db.execute(select(Completion).where(Completion.id == completion_id))
        completion = completion.scalars().first()

        if not completion:
            raise HTTPException(status_code=404, detail="Completion not found")

        completion.feedback_score = completion.feedback_score + vote
        await db.commit()
        await db.refresh(completion)

        return completion