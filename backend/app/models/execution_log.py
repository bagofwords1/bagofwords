from sqlalchemy import Column, Integer, String, DateTime, Text, JSON, ForeignKey, Boolean, Float
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.database import Base
import uuid
from sqlalchemy.dialects.postgresql import UUID

class ExecutionLog(Base):
    __tablename__ = "execution_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # Execution metadata
    execution_id = Column(String, nullable=False, index=True)  # Groups related logs
    agent_type = Column(String, nullable=False)  # planner, designer, answer, etc.
    execution_step = Column(String, nullable=False)  # plan, execute, observe, etc.
    action_type = Column(String, nullable=True)  # create_widget, answer_question, etc.
    
    # Relationships
    report_id = Column(UUID(as_uuid=True), ForeignKey("reports.id"), nullable=True)
    completion_id = Column(UUID(as_uuid=True), ForeignKey("completions.id"), nullable=True)
    widget_id = Column(UUID(as_uuid=True), ForeignKey("widgets.id"), nullable=True)
    step_id = Column(UUID(as_uuid=True), ForeignKey("steps.id"), nullable=True)
    
    # Execution details
    status = Column(String, nullable=False, default="started")  # started, completed, failed, cancelled
    start_time = Column(DateTime(timezone=True), server_default=func.now())
    end_time = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(Integer, nullable=True)
    
    # Input/Output data
    input_data = Column(JSON, nullable=True)  # Prompt, schemas, context, etc.
    output_data = Column(JSON, nullable=True)  # Generated plan, answer, etc.
    error_message = Column(Text, nullable=True)
    
    # Metadata
    metadata = Column(JSON, nullable=True)  # Additional context, settings, etc.
    
    # User context
    external_platform = Column(String, nullable=True)
    external_user_id = Column(String, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    report = relationship("Report", back_populates="execution_logs")
    completion = relationship("Completion", back_populates="execution_logs") 
    widget = relationship("Widget", back_populates="execution_logs")
    step = relationship("Step", back_populates="execution_logs")
    llm_call_logs = relationship("LLMCallLog", back_populates="execution_log", cascade="all, delete-orphan")

class LLMCallLog(Base):
    __tablename__ = "llm_call_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # Relationships
    execution_log_id = Column(UUID(as_uuid=True), ForeignKey("execution_logs.id"), nullable=True)
    report_id = Column(UUID(as_uuid=True), ForeignKey("reports.id"), nullable=True)
    completion_id = Column(UUID(as_uuid=True), ForeignKey("completions.id"), nullable=True)
    
    # LLM details
    provider = Column(String, nullable=False)  # openai, anthropic, google, etc.
    model_id = Column(String, nullable=False)  # gpt-4, claude-3, etc.
    call_type = Column(String, nullable=False, default="inference")  # inference, inference_stream
    
    # Call metadata
    start_time = Column(DateTime(timezone=True), server_default=func.now())
    end_time = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(Integer, nullable=True)
    status = Column(String, nullable=False, default="started")  # started, completed, failed, cancelled
    
    # Input/Output
    prompt = Column(Text, nullable=True)
    response = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    
    # Token usage
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)
    
    # Cost tracking
    input_cost = Column(Float, nullable=True)
    output_cost = Column(Float, nullable=True)
    total_cost = Column(Float, nullable=True)
    
    # Stream tracking
    is_streaming = Column(Boolean, default=False)
    chunks_count = Column(Integer, nullable=True)
    
    # Additional metadata
    metadata = Column(JSON, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    execution_log = relationship("ExecutionLog", back_populates="llm_call_logs")
    report = relationship("Report", back_populates="llm_call_logs")
    completion = relationship("Completion", back_populates="llm_call_logs")