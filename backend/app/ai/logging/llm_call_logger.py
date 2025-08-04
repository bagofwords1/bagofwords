import json
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional, AsyncGenerator
from contextlib import asynccontextmanager
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_log import LLMCallLog


class LLMCallLogger:
    """
    Logger for tracking LLM API calls including timing, token usage, costs, and streaming information.
    Provides context managers for automatic call logging and detailed metrics tracking.
    """
    
    def __init__(self, db: AsyncSession):
        self.db = db
        
        # Token pricing per 1K tokens (approximate, should be configurable)
        self.token_prices = {
            'gpt-4': {'input': 0.03, 'output': 0.06},
            'gpt-4-turbo': {'input': 0.01, 'output': 0.03},
            'gpt-3.5-turbo': {'input': 0.0015, 'output': 0.002},
            'claude-3-opus': {'input': 0.015, 'output': 0.075},
            'claude-3-sonnet': {'input': 0.003, 'output': 0.015},
            'claude-3-haiku': {'input': 0.00025, 'output': 0.00125},
            'gemini-pro': {'input': 0.00025, 'output': 0.0005},
            # Add more models as needed
        }
    
    async def start_llm_call(
        self,
        provider: str,
        model_id: str,
        call_type: str = "inference",
        prompt: Optional[str] = None,
        is_streaming: bool = False,
        execution_log_id: Optional[str] = None,
        report_id: Optional[str] = None,
        completion_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> LLMCallLog:
        """
        Start logging an LLM API call.
        
        Args:
            provider: LLM provider (openai, anthropic, google, etc.)
            model_id: Model identifier (gpt-4, claude-3, etc.)
            call_type: Type of call (inference, inference_stream)
            prompt: The prompt sent to the LLM
            is_streaming: Whether this is a streaming call
            execution_log_id: Associated execution log ID
            report_id: Associated report ID
            completion_id: Associated completion ID
            metadata: Additional metadata
            
        Returns:
            LLMCallLog instance for this call
        """
        
        # Sanitize metadata for JSON storage
        safe_metadata = self._sanitize_for_json(metadata) if metadata else None
        
        llm_call_log = LLMCallLog(
            execution_log_id=execution_log_id,
            report_id=report_id,
            completion_id=completion_id,
            provider=provider,
            model_id=model_id,
            call_type=call_type,
            start_time=datetime.now(timezone.utc),
            status="started",
            prompt=prompt,
            is_streaming=is_streaming,
            chunks_count=0 if is_streaming else None,
            metadata=safe_metadata
        )
        
        self.db.add(llm_call_log)
        await self.db.commit()
        await self.db.refresh(llm_call_log)
        
        return llm_call_log
    
    async def end_llm_call(
        self,
        llm_call_log: LLMCallLog,
        status: str = "completed",
        response: Optional[str] = None,
        error_message: Optional[str] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        chunks_count: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> LLMCallLog:
        """
        End an LLM call and update the log with results and metrics.
        
        Args:
            llm_call_log: The LLMCallLog instance to update
            status: Final status (completed, failed, cancelled)
            response: The response from the LLM
            error_message: Error message if call failed
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
            chunks_count: Number of chunks in streaming response
            metadata: Additional metadata to merge with existing
            
        Returns:
            Updated LLMCallLog instance
        """
        
        end_time = datetime.now(timezone.utc)
        duration_ms = int((end_time - llm_call_log.start_time).total_seconds() * 1000)
        
        # Calculate token totals
        total_tokens = None
        if input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens
        
        # Calculate costs if tokens are available
        input_cost, output_cost, total_cost = self._calculate_costs(
            llm_call_log.model_id, input_tokens, output_tokens
        )
        
        # Merge metadata if provided
        final_metadata = llm_call_log.metadata or {}
        if metadata:
            final_metadata.update(self._sanitize_for_json(metadata))
        
        llm_call_log.status = status
        llm_call_log.end_time = end_time
        llm_call_log.duration_ms = duration_ms
        llm_call_log.response = response
        llm_call_log.error_message = error_message
        llm_call_log.input_tokens = input_tokens
        llm_call_log.output_tokens = output_tokens
        llm_call_log.total_tokens = total_tokens
        llm_call_log.input_cost = input_cost
        llm_call_log.output_cost = output_cost
        llm_call_log.total_cost = total_cost
        llm_call_log.chunks_count = chunks_count
        llm_call_log.metadata = final_metadata if final_metadata else None
        
        await self.db.commit()
        await self.db.refresh(llm_call_log)
        
        return llm_call_log
    
    @asynccontextmanager
    async def log_llm_call(
        self,
        provider: str,
        model_id: str,
        call_type: str = "inference",
        prompt: Optional[str] = None,
        is_streaming: bool = False,
        execution_log_id: Optional[str] = None,
        report_id: Optional[str] = None,
        completion_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ):
        """
        Context manager for automatic LLM call logging.
        
        Usage:
            async with llm_call_logger.log_llm_call(
                provider="openai",
                model_id="gpt-4",
                prompt="What is AI?",
                is_streaming=False
            ) as call_log:
                response = await llm_client.inference(prompt)
                call_log.response = response
                call_log.input_tokens = count_tokens(prompt)
                call_log.output_tokens = count_tokens(response)
        """
        
        llm_call_log = await self.start_llm_call(
            provider=provider,
            model_id=model_id,
            call_type=call_type,
            prompt=prompt,
            is_streaming=is_streaming,
            execution_log_id=execution_log_id,
            report_id=report_id,
            completion_id=completion_id,
            metadata=metadata
        )
        
        try:
            # Create a context object that allows setting response data during execution
            class LLMCallContext:
                def __init__(self, log):
                    self.log = log
                    self.response = None
                    self.input_tokens = None
                    self.output_tokens = None
                    self.chunks_count = None
                    self.metadata = None
            
            context = LLMCallContext(llm_call_log)
            yield context
            
            # Successfully completed
            await self.end_llm_call(
                llm_call_log,
                status="completed",
                response=context.response,
                input_tokens=context.input_tokens,
                output_tokens=context.output_tokens,
                chunks_count=context.chunks_count,
                metadata=context.metadata
            )
            
        except Exception as e:
            # Call failed
            await self.end_llm_call(
                llm_call_log,
                status="failed",
                error_message=str(e),
                metadata=getattr(context, 'metadata', None) if 'context' in locals() else None
            )
            raise
    
    async def log_streaming_call(
        self,
        provider: str,
        model_id: str,
        prompt: str,
        stream_generator: AsyncGenerator[str, None],
        execution_log_id: Optional[str] = None,
        report_id: Optional[str] = None,
        completion_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> AsyncGenerator[str, None]:
        """
        Log a streaming LLM call and wrap the generator to count chunks and accumulate response.
        
        Args:
            provider: LLM provider
            model_id: Model identifier
            prompt: The prompt sent to the LLM
            stream_generator: The original async generator from the LLM
            execution_log_id: Associated execution log ID
            report_id: Associated report ID
            completion_id: Associated completion ID
            metadata: Additional metadata
            
        Yields:
            str: Chunks from the original generator
        """
        
        llm_call_log = await self.start_llm_call(
            provider=provider,
            model_id=model_id,
            call_type="inference_stream",
            prompt=prompt,
            is_streaming=True,
            execution_log_id=execution_log_id,
            report_id=report_id,
            completion_id=completion_id,
            metadata=metadata
        )
        
        full_response = ""
        chunks_count = 0
        error_occurred = False
        error_message = None
        
        try:
            async for chunk in stream_generator:
                full_response += chunk
                chunks_count += 1
                yield chunk
                
            # Successfully completed streaming
            await self.end_llm_call(
                llm_call_log,
                status="completed",
                response=full_response,
                chunks_count=chunks_count
            )
            
        except Exception as e:
            error_occurred = True
            error_message = str(e)
            
            # Log failed streaming call
            await self.end_llm_call(
                llm_call_log,
                status="failed",
                response=full_response if full_response else None,
                error_message=error_message,
                chunks_count=chunks_count
            )
            raise
    
    def _calculate_costs(
        self, 
        model_id: str, 
        input_tokens: Optional[int], 
        output_tokens: Optional[int]
    ) -> tuple[Optional[float], Optional[float], Optional[float]]:
        """
        Calculate costs based on token usage and model pricing.
        
        Args:
            model_id: Model identifier
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
            
        Returns:
            Tuple of (input_cost, output_cost, total_cost)
        """
        
        if input_tokens is None or output_tokens is None:
            return None, None, None
        
        # Normalize model ID to match pricing table
        normalized_model = model_id.lower()
        for model_key in self.token_prices:
            if model_key in normalized_model:
                pricing = self.token_prices[model_key]
                input_cost = (input_tokens / 1000) * pricing['input']
                output_cost = (output_tokens / 1000) * pricing['output']
                total_cost = input_cost + output_cost
                return round(input_cost, 6), round(output_cost, 6), round(total_cost, 6)
        
        # If model not found in pricing table, return None
        return None, None, None
    
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
    
    async def update_token_usage(
        self,
        llm_call_log: LLMCallLog,
        input_tokens: int,
        output_tokens: int
    ) -> LLMCallLog:
        """
        Update token usage for an existing LLM call log.
        Useful when token counts are calculated after the call completes.
        
        Args:
            llm_call_log: The LLMCallLog instance to update
            input_tokens: Number of input tokens
            output_tokens: Number of output tokens
            
        Returns:
            Updated LLMCallLog instance
        """
        
        total_tokens = input_tokens + output_tokens
        input_cost, output_cost, total_cost = self._calculate_costs(
            llm_call_log.model_id, input_tokens, output_tokens
        )
        
        llm_call_log.input_tokens = input_tokens
        llm_call_log.output_tokens = output_tokens
        llm_call_log.total_tokens = total_tokens
        llm_call_log.input_cost = input_cost
        llm_call_log.output_cost = output_cost
        llm_call_log.total_cost = total_cost
        
        await self.db.commit()
        await self.db.refresh(llm_call_log)
        
        return llm_call_log