import json
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_log import ExecutionLog


class ExecutionLogger:
    """
    Logger for tracking agent execution workflows including steps, timing, input/output data.
    Provides context managers for automatic start/end logging and hierarchical execution tracking.
    """
    
    def __init__(self, db: AsyncSession):
        self.db = db
        self.current_execution_id = None
        self.execution_stack = []  # For nested executions
        
    def generate_execution_id(self) -> str:
        """Generate a unique execution ID for grouping related logs."""
        return f"exec_{uuid.uuid4().hex[:12]}_{int(time.time())}"
    
    async def start_execution(
        self,
        agent_type: str,
        execution_step: str,
        action_type: Optional[str] = None,
        input_data: Optional[Dict[str, Any]] = None,
        report_id: Optional[str] = None,
        completion_id: Optional[str] = None,
        widget_id: Optional[str] = None,
        step_id: Optional[str] = None,
        external_platform: Optional[str] = None,
        external_user_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        execution_id: Optional[str] = None
    ) -> ExecutionLog:
        """
        Start logging an execution step.
        
        Args:
            agent_type: Type of agent (planner, designer, answer, etc.)
            execution_step: Step in the workflow (plan, execute, observe, etc.)
            action_type: Specific action being performed (create_widget, answer_question, etc.)
            input_data: Input data for this execution step
            report_id: Associated report ID
            completion_id: Associated completion ID
            widget_id: Associated widget ID
            step_id: Associated step ID
            external_platform: Platform context (slack, teams, etc.)
            external_user_id: External user context
            metadata: Additional metadata
            execution_id: Use existing execution ID or generate new one
            
        Returns:
            ExecutionLog instance for this execution
        """
        
        # Use provided execution_id or generate new one
        if execution_id is None:
            if self.current_execution_id is None:
                self.current_execution_id = self.generate_execution_id()
            execution_id = self.current_execution_id
        
        # Store current execution context on stack
        self.execution_stack.append({
            'execution_id': execution_id,
            'agent_type': agent_type,
            'execution_step': execution_step,
            'action_type': action_type
        })
        
        # Sanitize input data for JSON storage
        safe_input_data = self._sanitize_for_json(input_data) if input_data else None
        safe_metadata = self._sanitize_for_json(metadata) if metadata else None
        
        execution_log = ExecutionLog(
            execution_id=execution_id,
            agent_type=agent_type,
            execution_step=execution_step,
            action_type=action_type,
            report_id=report_id,
            completion_id=completion_id,
            widget_id=widget_id,
            step_id=step_id,
            status="started",
            start_time=datetime.now(timezone.utc),
            input_data=safe_input_data,
            metadata=safe_metadata,
            external_platform=external_platform,
            external_user_id=external_user_id
        )
        
        self.db.add(execution_log)
        await self.db.commit()
        await self.db.refresh(execution_log)
        
        return execution_log
    
    async def end_execution(
        self,
        execution_log: ExecutionLog,
        status: str = "completed",
        output_data: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> ExecutionLog:
        """
        End an execution step and update the log.
        
        Args:
            execution_log: The ExecutionLog instance to update
            status: Final status (completed, failed, cancelled)
            output_data: Output data from this execution step
            error_message: Error message if execution failed
            metadata: Additional metadata to merge with existing
            
        Returns:
            Updated ExecutionLog instance
        """
        
        end_time = datetime.now(timezone.utc)
        duration_ms = int((end_time - execution_log.start_time).total_seconds() * 1000)
        
        # Sanitize output data for JSON storage
        safe_output_data = self._sanitize_for_json(output_data) if output_data else None
        
        # Merge metadata if provided
        final_metadata = execution_log.metadata or {}
        if metadata:
            final_metadata.update(self._sanitize_for_json(metadata))
        
        execution_log.status = status
        execution_log.end_time = end_time
        execution_log.duration_ms = duration_ms
        execution_log.output_data = safe_output_data
        execution_log.error_message = error_message
        execution_log.metadata = final_metadata if final_metadata else None
        
        await self.db.commit()
        await self.db.refresh(execution_log)
        
        # Pop from execution stack
        if self.execution_stack:
            self.execution_stack.pop()
        
        return execution_log
    
    @asynccontextmanager
    async def log_execution(
        self,
        agent_type: str,
        execution_step: str,
        action_type: Optional[str] = None,
        input_data: Optional[Dict[str, Any]] = None,
        report_id: Optional[str] = None,
        completion_id: Optional[str] = None,
        widget_id: Optional[str] = None,
        step_id: Optional[str] = None,
        external_platform: Optional[str] = None,
        external_user_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        execution_id: Optional[str] = None
    ):
        """
        Context manager for automatic execution logging.
        
        Usage:
            async with execution_logger.log_execution(
                agent_type="planner",
                execution_step="plan",
                input_data={"prompt": "...", "schemas": "..."}
            ) as log:
                # Your execution code here
                result = await some_operation()
                log.output_data = {"result": result}
        """
        
        execution_log = await self.start_execution(
            agent_type=agent_type,
            execution_step=execution_step,
            action_type=action_type,
            input_data=input_data,
            report_id=report_id,
            completion_id=completion_id,
            widget_id=widget_id,
            step_id=step_id,
            external_platform=external_platform,
            external_user_id=external_user_id,
            metadata=metadata,
            execution_id=execution_id
        )
        
        try:
            # Create a context object that allows setting output data during execution
            class ExecutionContext:
                def __init__(self, log):
                    self.log = log
                    self.output_data = None
                    self.metadata = None
            
            context = ExecutionContext(execution_log)
            yield context
            
            # Successfully completed
            await self.end_execution(
                execution_log,
                status="completed",
                output_data=context.output_data,
                metadata=context.metadata
            )
            
        except Exception as e:
            # Execution failed
            await self.end_execution(
                execution_log,
                status="failed",
                error_message=str(e),
                metadata=getattr(context, 'metadata', None) if 'context' in locals() else None
            )
            raise
    
    async def log_observation(
        self,
        agent_type: str,
        observation_data: Dict[str, Any],
        execution_id: Optional[str] = None,
        report_id: Optional[str] = None,
        completion_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> ExecutionLog:
        """
        Log an observation step in the ReAct workflow.
        
        Args:
            agent_type: Type of agent making the observation
            observation_data: The observation data (widgets, results, etc.)
            execution_id: Associated execution ID
            report_id: Associated report ID
            completion_id: Associated completion ID
            metadata: Additional metadata
            
        Returns:
            ExecutionLog instance for this observation
        """
        
        return await self.start_execution(
            agent_type=agent_type,
            execution_step="observe",
            action_type="observation",
            input_data=observation_data,
            report_id=report_id,
            completion_id=completion_id,
            metadata=metadata,
            execution_id=execution_id or self.current_execution_id
        )
    
    def _sanitize_for_json(self, data: Any) -> Any:
        """
        Sanitize data for JSON serialization, handling objects that can't be serialized.
        """
        if data is None:
            return None
        
        try:
            # Try to serialize to test if it's JSON-safe
            json.dumps(data, default=str)
            return data
        except (TypeError, ValueError):
            # If it fails, convert to string representation
            if isinstance(data, dict):
                return {k: self._sanitize_for_json(v) for k, v in data.items()}
            elif isinstance(data, (list, tuple)):
                return [self._sanitize_for_json(item) for item in data]
            else:
                return str(data)
    
    def get_current_execution_id(self) -> Optional[str]:
        """Get the current execution ID."""
        return self.current_execution_id
    
    def get_current_execution_context(self) -> Optional[Dict[str, Any]]:
        """Get the current execution context from the stack."""
        return self.execution_stack[-1] if self.execution_stack else None
    
    def set_execution_id(self, execution_id: str):
        """Set the current execution ID."""
        self.current_execution_id = execution_id