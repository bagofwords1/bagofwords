from sqlalchemy import Column, String, DateTime, Integer, Float, JSON, Boolean, ForeignKey, Text
from sqlalchemy.orm import relationship
from .base import BaseSchema


class AgentExecution(BaseSchema):
    __tablename__ = 'agent_executions'

    # Links
    completion_id = Column(String(36), ForeignKey('completions.id'), nullable=False, index=True)
    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=True)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=True)
    report_id = Column(String(36), ForeignKey('reports.id'), nullable=True)

    # Status and timing
    status = Column(String, nullable=False, default='in_progress')
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    total_duration_ms = Column(Float, nullable=True)
    first_token_ms = Column(Float, nullable=True)
    thinking_ms = Column(Float, nullable=True)

    # Streaming resume
    latest_seq = Column(Integer, nullable=False, default=0)

    # Metrics and config
    token_usage_json = Column(JSON, nullable=True, default=dict)
    error_json = Column(JSON, nullable=True)
    config_json = Column(JSON, nullable=True)

    # Build
    build_id = Column(String(36), ForeignKey('instruction_builds.id'), nullable=True)

    # Version tracking
    bow_version = Column(String, nullable=True, index=True)

    # True when this execution is running inside a TestRun (i.e. the agent
    # was spawned by ``TestRunService`` to evaluate a test case). Used by
    # the ``run_eval`` tool to refuse nested invocations and keep recursion
    # from stacking new TestRuns inside an in-flight one.
    is_eval_run = Column(Boolean, nullable=False, default=False, index=True)

    # Diagnosis rollups — denormalised at run end (and on feedback / judge /
    # late-usage events) by app.services.diagnosis.rollup.refresh_rollup so the
    # diagnosis explorer filters and sorts on this table alone. Never written
    # on the hot path during a run.
    prompt_text = Column(Text, nullable=True)          # first 2,000 chars of the user prompt
    error_text = Column(Text, nullable=True)           # error_json.message, first 2,000 chars
    platform = Column(String, nullable=True)           # web | slack | teams | email | mcp | api
    feedback_direction = Column(Integer, nullable=True)  # 1 | -1 | 0 (none)
    feedback_message = Column(Text, nullable=True)
    judge_response_score = Column(Integer, nullable=True)      # completions.response_score
    judge_instructions_score = Column(Integer, nullable=True)  # completions.instructions_effectiveness
    judge_context_score = Column(Integer, nullable=True)       # completions.context_effectiveness
    primary_model_id = Column(String, nullable=True)   # the planner's model
    primary_provider = Column(String, nullable=True)
    total_cost_usd = Column(Float, nullable=True)
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)
    tool_count = Column(Integer, nullable=True)
    failed_tool_count = Column(Integer, nullable=True)
    turn_index = Column(Integer, nullable=True)        # 1-based position of the run in its report
    cost_is_partial = Column(Boolean, nullable=True)   # cost attributed by report+window, not by run id
    rollup_at = Column(DateTime, nullable=True)

    # Relationships (optional lazy loading)
    plan_decisions = relationship('PlanDecision', back_populates='agent_execution', lazy='select')
    tool_executions = relationship('ToolExecution', back_populates='agent_execution', lazy='select')
    context_snapshots = relationship('ContextSnapshot', back_populates='agent_execution', lazy='select')
    instructions = relationship('Instruction', back_populates='agent_execution', lazy='select')


