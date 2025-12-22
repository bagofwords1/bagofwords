from sqlalchemy import Column, String, JSON, ForeignKey, DateTime
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class TestSuite(BaseSchema):
    __tablename__ = "test_suites"

    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False, index=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)

    cases = relationship("TestCase", back_populates="suite", cascade="all, delete-orphan")


class TestCase(BaseSchema):
    __tablename__ = "test_cases"

    suite_id = Column(String(36), ForeignKey('test_suites.id'), nullable=False, index=True)
    name = Column(String, nullable=False)

    # Store PromptSchema as JSON:
    # { content, widget_id, step_id, mentions, mode, model_id }
    prompt_json = Column(JSON, nullable=False, default=dict)

    # Assertions spec (the Y we designed)
    expectations_json = Column(JSON, nullable=False, default=dict)

    # Optional: limit impact/trigger scope (list of DataSource ids)
    data_source_ids_json = Column(JSON, nullable=True, default=list)

    suite = relationship("TestSuite", back_populates="cases")


class TestRun(BaseSchema):
    __tablename__ = "test_runs"

    title = Column(String, nullable=False)
    # Comma-separated list of suite IDs participating in this run.
    # Chosen for portability across SQLite and Postgres without JSON/ARRAY types.
    suite_ids = Column(String, nullable=False)
    requested_by_user_id = Column(String(36), ForeignKey('users.id'), nullable=True, index=True)
    trigger_reason = Column(String, nullable=True)  # 'manual' | 'context_change' | 'schedule'
    status = Column(String, nullable=False, default="in_progress")  # in_progress | success | error
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    summary_json = Column(JSON, nullable=True, default=dict)
    
    # Build system: which instruction build was used for this test run
    # Enables comparison of test results across different instruction versions
    build_id = Column(String(36), ForeignKey('instruction_builds.id'), nullable=True, index=True)
    
    # Relationship to build (lazy loaded by default)
    build = relationship("InstructionBuild", foreign_keys=[build_id], lazy="joined")
    
    @property
    def build_number(self):
        """Get the build number from the related build, if available."""
        if self.build:
            return self.build.build_number
        return None


class TestResult(BaseSchema):
    __tablename__ = "test_results"

    run_id = Column(String(36), ForeignKey('test_runs.id'), nullable=False, index=True)
    case_id = Column(String(36), ForeignKey('test_cases.id'), nullable=False, index=True)

    # Link to the head (user) completion created for this test execution
    head_completion_id = Column(String(36), ForeignKey('completions.id'), nullable=False, index=True)

    status = Column(String, nullable=False, default="in_progress")  # pass | fail | error
    failure_reason = Column(String, nullable=True)

    # Optional drill-down link (can be null; system completion can be derived from head)
    agent_execution_id = Column(String(36), ForeignKey('agent_executions.id'), nullable=True, index=True)

    # Optional: report associated with this run/result
    report_id = Column(String(36), ForeignKey('reports.id'), nullable=True, index=True)

    # Assertion result
    result_json = Column(JSON, nullable=True, default=None)



