from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc, and_, or_
from typing import List, Optional, Dict, Any
from datetime import datetime, timedelta

from app.database import get_db
from app.models.execution_log import ExecutionLog, LLMCallLog
from app.models.report import Report
from app.models.completion import Completion
from app.models.widget import Widget
from app.models.step import Step
from app.auth.dependencies import get_current_user
from app.models.user import User

router = APIRouter(prefix="/execution-logs", tags=["execution-logs"])


@router.get("/executions")
async def get_execution_logs(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    report_id: Optional[str] = Query(None, description="Filter by report ID"),
    agent_type: Optional[str] = Query(None, description="Filter by agent type (planner, designer, answer, etc.)"),
    execution_step: Optional[str] = Query(None, description="Filter by execution step (plan, execute, observe, etc.)"),
    action_type: Optional[str] = Query(None, description="Filter by action type"),
    status: Optional[str] = Query(None, description="Filter by status (started, completed, failed, cancelled)"),
    external_platform: Optional[str] = Query(None, description="Filter by external platform"),
    start_date: Optional[datetime] = Query(None, description="Filter executions after this date"),
    end_date: Optional[datetime] = Query(None, description="Filter executions before this date"),
    limit: int = Query(50, le=500, description="Maximum number of records to return"),
    offset: int = Query(0, description="Number of records to skip"),
    include_llm_calls: bool = Query(False, description="Include associated LLM calls")
):
    """
    Retrieve execution logs with filtering and pagination.
    """
    
    # Build base query
    query = select(ExecutionLog)
    
    # Apply filters
    filters = []
    
    if report_id:
        filters.append(ExecutionLog.report_id == report_id)
    
    if agent_type:
        filters.append(ExecutionLog.agent_type == agent_type)
    
    if execution_step:
        filters.append(ExecutionLog.execution_step == execution_step)
    
    if action_type:
        filters.append(ExecutionLog.action_type == action_type)
    
    if status:
        filters.append(ExecutionLog.status == status)
    
    if external_platform:
        filters.append(ExecutionLog.external_platform == external_platform)
    
    if start_date:
        filters.append(ExecutionLog.start_time >= start_date)
    
    if end_date:
        filters.append(ExecutionLog.start_time <= end_date)
    
    if filters:
        query = query.where(and_(*filters))
    
    # Add ordering and pagination
    query = query.order_by(desc(ExecutionLog.start_time)).limit(limit).offset(offset)
    
    # Execute query
    result = await db.execute(query)
    execution_logs = result.scalars().all()
    
    # Convert to dict format
    response_data = []
    for log in execution_logs:
        log_data = {
            "id": str(log.id),
            "execution_id": log.execution_id,
            "agent_type": log.agent_type,
            "execution_step": log.execution_step,
            "action_type": log.action_type,
            "status": log.status,
            "start_time": log.start_time,
            "end_time": log.end_time,
            "duration_ms": log.duration_ms,
            "input_data": log.input_data,
            "output_data": log.output_data,
            "error_message": log.error_message,
            "metadata": log.metadata,
            "external_platform": log.external_platform,
            "external_user_id": log.external_user_id,
            "report_id": str(log.report_id) if log.report_id else None,
            "completion_id": str(log.completion_id) if log.completion_id else None,
            "widget_id": str(log.widget_id) if log.widget_id else None,
            "step_id": str(log.step_id) if log.step_id else None,
            "created_at": log.created_at,
            "updated_at": log.updated_at
        }
        
        # Include LLM calls if requested
        if include_llm_calls:
            llm_calls_query = select(LLMCallLog).where(LLMCallLog.execution_log_id == log.id)
            llm_calls_result = await db.execute(llm_calls_query)
            llm_calls = llm_calls_result.scalars().all()
            
            log_data["llm_calls"] = [
                {
                    "id": str(call.id),
                    "provider": call.provider,
                    "model_id": call.model_id,
                    "call_type": call.call_type,
                    "status": call.status,
                    "start_time": call.start_time,
                    "end_time": call.end_time,
                    "duration_ms": call.duration_ms,
                    "input_tokens": call.input_tokens,
                    "output_tokens": call.output_tokens,
                    "total_tokens": call.total_tokens,
                    "input_cost": call.input_cost,
                    "output_cost": call.output_cost,
                    "total_cost": call.total_cost,
                    "is_streaming": call.is_streaming,
                    "chunks_count": call.chunks_count
                } for call in llm_calls
            ]
        
        response_data.append(log_data)
    
    return {
        "execution_logs": response_data,
        "total_count": len(response_data),
        "limit": limit,
        "offset": offset
    }


@router.get("/executions/{execution_id}")
async def get_execution_by_id(
    execution_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    include_llm_calls: bool = Query(True, description="Include associated LLM calls")
):
    """
    Get all execution logs for a specific execution ID (groups related logs).
    """
    
    query = select(ExecutionLog).where(ExecutionLog.execution_id == execution_id)
    result = await db.execute(query)
    execution_logs = result.scalars().all()
    
    if not execution_logs:
        raise HTTPException(status_code=404, detail="Execution not found")
    
    # Group by execution step
    execution_data = {
        "execution_id": execution_id,
        "steps": []
    }
    
    for log in execution_logs:
        step_data = {
            "id": str(log.id),
            "agent_type": log.agent_type,
            "execution_step": log.execution_step,
            "action_type": log.action_type,
            "status": log.status,
            "start_time": log.start_time,
            "end_time": log.end_time,
            "duration_ms": log.duration_ms,
            "input_data": log.input_data,
            "output_data": log.output_data,
            "error_message": log.error_message,
            "metadata": log.metadata
        }
        
        # Include LLM calls if requested
        if include_llm_calls:
            llm_calls_query = select(LLMCallLog).where(LLMCallLog.execution_log_id == log.id)
            llm_calls_result = await db.execute(llm_calls_query)
            llm_calls = llm_calls_result.scalars().all()
            
            step_data["llm_calls"] = [
                {
                    "id": str(call.id),
                    "provider": call.provider,
                    "model_id": call.model_id,
                    "call_type": call.call_type,
                    "status": call.status,
                    "start_time": call.start_time,
                    "end_time": call.end_time,
                    "duration_ms": call.duration_ms,
                    "prompt": call.prompt,
                    "response": call.response,
                    "input_tokens": call.input_tokens,
                    "output_tokens": call.output_tokens,
                    "total_tokens": call.total_tokens,
                    "input_cost": call.input_cost,
                    "output_cost": call.output_cost,
                    "total_cost": call.total_cost,
                    "is_streaming": call.is_streaming,
                    "chunks_count": call.chunks_count,
                    "error_message": call.error_message
                } for call in llm_calls
            ]
        
        execution_data["steps"].append(step_data)
    
    # Sort steps by start time
    execution_data["steps"].sort(key=lambda x: x["start_time"])
    
    return execution_data


@router.get("/llm-calls")
async def get_llm_call_logs(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    provider: Optional[str] = Query(None, description="Filter by LLM provider"),
    model_id: Optional[str] = Query(None, description="Filter by model ID"),
    call_type: Optional[str] = Query(None, description="Filter by call type"),
    status: Optional[str] = Query(None, description="Filter by status"),
    report_id: Optional[str] = Query(None, description="Filter by report ID"),
    execution_log_id: Optional[str] = Query(None, description="Filter by execution log ID"),
    start_date: Optional[datetime] = Query(None, description="Filter calls after this date"),
    end_date: Optional[datetime] = Query(None, description="Filter calls before this date"),
    limit: int = Query(50, le=500, description="Maximum number of records to return"),
    offset: int = Query(0, description="Number of records to skip")
):
    """
    Retrieve LLM call logs with filtering and pagination.
    """
    
    # Build base query
    query = select(LLMCallLog)
    
    # Apply filters
    filters = []
    
    if provider:
        filters.append(LLMCallLog.provider == provider)
    
    if model_id:
        filters.append(LLMCallLog.model_id == model_id)
    
    if call_type:
        filters.append(LLMCallLog.call_type == call_type)
    
    if status:
        filters.append(LLMCallLog.status == status)
    
    if report_id:
        filters.append(LLMCallLog.report_id == report_id)
    
    if execution_log_id:
        filters.append(LLMCallLog.execution_log_id == execution_log_id)
    
    if start_date:
        filters.append(LLMCallLog.start_time >= start_date)
    
    if end_date:
        filters.append(LLMCallLog.start_time <= end_date)
    
    if filters:
        query = query.where(and_(*filters))
    
    # Add ordering and pagination
    query = query.order_by(desc(LLMCallLog.start_time)).limit(limit).offset(offset)
    
    # Execute query
    result = await db.execute(query)
    llm_call_logs = result.scalars().all()
    
    # Convert to dict format
    response_data = []
    for call in llm_call_logs:
        call_data = {
            "id": str(call.id),
            "execution_log_id": str(call.execution_log_id) if call.execution_log_id else None,
            "report_id": str(call.report_id) if call.report_id else None,
            "completion_id": str(call.completion_id) if call.completion_id else None,
            "provider": call.provider,
            "model_id": call.model_id,
            "call_type": call.call_type,
            "status": call.status,
            "start_time": call.start_time,
            "end_time": call.end_time,
            "duration_ms": call.duration_ms,
            "prompt": call.prompt,
            "response": call.response,
            "error_message": call.error_message,
            "input_tokens": call.input_tokens,
            "output_tokens": call.output_tokens,
            "total_tokens": call.total_tokens,
            "input_cost": call.input_cost,
            "output_cost": call.output_cost,
            "total_cost": call.total_cost,
            "is_streaming": call.is_streaming,
            "chunks_count": call.chunks_count,
            "metadata": call.metadata,
            "created_at": call.created_at,
            "updated_at": call.updated_at
        }
        response_data.append(call_data)
    
    return {
        "llm_call_logs": response_data,
        "total_count": len(response_data),
        "limit": limit,
        "offset": offset
    }


@router.get("/analytics/summary")
async def get_execution_analytics(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    start_date: Optional[datetime] = Query(None, description="Analytics start date"),
    end_date: Optional[datetime] = Query(None, description="Analytics end date"),
    report_id: Optional[str] = Query(None, description="Filter by report ID")
):
    """
    Get analytics summary for executions and LLM calls.
    """
    
    # Default to last 30 days if no dates provided
    if not start_date:
        start_date = datetime.utcnow() - timedelta(days=30)
    if not end_date:
        end_date = datetime.utcnow()
    
    # Build base filters
    execution_filters = [
        ExecutionLog.start_time >= start_date,
        ExecutionLog.start_time <= end_date
    ]
    llm_filters = [
        LLMCallLog.start_time >= start_date,
        LLMCallLog.start_time <= end_date
    ]
    
    if report_id:
        execution_filters.append(ExecutionLog.report_id == report_id)
        llm_filters.append(LLMCallLog.report_id == report_id)
    
    # Execution analytics
    exec_count_query = select(func.count(ExecutionLog.id)).where(and_(*execution_filters))
    exec_count_result = await db.execute(exec_count_query)
    total_executions = exec_count_result.scalar()
    
    exec_status_query = select(
        ExecutionLog.status,
        func.count(ExecutionLog.id)
    ).where(and_(*execution_filters)).group_by(ExecutionLog.status)
    exec_status_result = await db.execute(exec_status_query)
    execution_status_counts = dict(exec_status_result.all())
    
    exec_agent_query = select(
        ExecutionLog.agent_type,
        func.count(ExecutionLog.id)
    ).where(and_(*execution_filters)).group_by(ExecutionLog.agent_type)
    exec_agent_result = await db.execute(exec_agent_query)
    agent_type_counts = dict(exec_agent_result.all())
    
    exec_duration_query = select(
        func.avg(ExecutionLog.duration_ms),
        func.min(ExecutionLog.duration_ms),
        func.max(ExecutionLog.duration_ms)
    ).where(and_(*execution_filters, ExecutionLog.duration_ms.isnot(None)))
    exec_duration_result = await db.execute(exec_duration_query)
    duration_stats = exec_duration_result.first()
    
    # LLM call analytics
    llm_count_query = select(func.count(LLMCallLog.id)).where(and_(*llm_filters))
    llm_count_result = await db.execute(llm_count_query)
    total_llm_calls = llm_count_result.scalar()
    
    llm_provider_query = select(
        LLMCallLog.provider,
        func.count(LLMCallLog.id)
    ).where(and_(*llm_filters)).group_by(LLMCallLog.provider)
    llm_provider_result = await db.execute(llm_provider_query)
    provider_counts = dict(llm_provider_result.all())
    
    llm_model_query = select(
        LLMCallLog.model_id,
        func.count(LLMCallLog.id)
    ).where(and_(*llm_filters)).group_by(LLMCallLog.model_id)
    llm_model_result = await db.execute(llm_model_query)
    model_counts = dict(llm_model_result.all())
    
    # Token and cost analytics
    token_query = select(
        func.sum(LLMCallLog.input_tokens),
        func.sum(LLMCallLog.output_tokens),
        func.sum(LLMCallLog.total_tokens),
        func.sum(LLMCallLog.total_cost)
    ).where(and_(*llm_filters))
    token_result = await db.execute(token_query)
    token_stats = token_result.first()
    
    return {
        "date_range": {
            "start_date": start_date,
            "end_date": end_date
        },
        "execution_analytics": {
            "total_executions": total_executions,
            "status_counts": execution_status_counts,
            "agent_type_counts": agent_type_counts,
            "duration_stats": {
                "avg_duration_ms": float(duration_stats[0]) if duration_stats[0] else None,
                "min_duration_ms": duration_stats[1] if duration_stats[1] else None,
                "max_duration_ms": duration_stats[2] if duration_stats[2] else None
            }
        },
        "llm_call_analytics": {
            "total_calls": total_llm_calls,
            "provider_counts": provider_counts,
            "model_counts": model_counts,
            "token_stats": {
                "total_input_tokens": token_stats[0] if token_stats[0] else 0,
                "total_output_tokens": token_stats[1] if token_stats[1] else 0,
                "total_tokens": token_stats[2] if token_stats[2] else 0,
                "total_cost": float(token_stats[3]) if token_stats[3] else 0.0
            }
        }
    }


@router.delete("/executions/{execution_id}")
async def delete_execution_logs(
    execution_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Delete all logs for a specific execution ID.
    """
    
    # First delete associated LLM call logs
    llm_delete_query = select(LLMCallLog).where(
        LLMCallLog.execution_log_id.in_(
            select(ExecutionLog.id).where(ExecutionLog.execution_id == execution_id)
        )
    )
    llm_result = await db.execute(llm_delete_query)
    llm_logs_to_delete = llm_result.scalars().all()
    
    for llm_log in llm_logs_to_delete:
        await db.delete(llm_log)
    
    # Then delete execution logs
    exec_delete_query = select(ExecutionLog).where(ExecutionLog.execution_id == execution_id)
    exec_result = await db.execute(exec_delete_query)
    exec_logs_to_delete = exec_result.scalars().all()
    
    if not exec_logs_to_delete:
        raise HTTPException(status_code=404, detail="Execution not found")
    
    for exec_log in exec_logs_to_delete:
        await db.delete(exec_log)
    
    await db.commit()
    
    return {
        "message": f"Deleted {len(exec_logs_to_delete)} execution logs and {len(llm_logs_to_delete)} LLM call logs for execution {execution_id}"
    }