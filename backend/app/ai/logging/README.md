# AI Agent Execution and LLM Call Logging System

This comprehensive logging system tracks all agent executions and LLM API calls, providing detailed insights into workflow performance, costs, and debugging information.

## Features

### 🔍 **Execution Logging**
- **Workflow Tracking**: Complete agent execution workflows (plan → execute → observe)
- **Timing Metrics**: Start/end times and duration tracking
- **Input/Output Capture**: Safe JSON serialization of data
- **Error Handling**: Automatic error capture and status tracking
- **Hierarchical Execution**: Support for nested agent calls with execution IDs

### 💬 **LLM Call Logging**
- **API Call Tracking**: Every LLM inference and streaming call
- **Token Usage**: Input/output token counts and cost calculation
- **Performance Metrics**: Response times and chunk counts for streaming
- **Provider Support**: OpenAI, Anthropic, Google, and custom providers
- **Cost Analytics**: Automatic cost calculation based on model pricing

### 📊 **Analytics & Reporting**
- **Execution Analytics**: Success rates, agent performance, duration statistics
- **Cost Tracking**: Token usage and monetary costs across models
- **Performance Monitoring**: Identify bottlenecks and optimization opportunities
- **Historical Analysis**: Time-based filtering and trend analysis

## Database Schema

### ExecutionLog Table
```sql
CREATE TABLE execution_logs (
    id UUID PRIMARY KEY,
    execution_id VARCHAR NOT NULL,  -- Groups related logs
    agent_type VARCHAR NOT NULL,    -- planner, designer, answer, etc.
    execution_step VARCHAR NOT NULL, -- plan, execute, observe, etc.
    action_type VARCHAR,            -- create_widget, answer_question, etc.
    status VARCHAR DEFAULT 'started', -- started, completed, failed, cancelled
    start_time TIMESTAMP WITH TIME ZONE,
    end_time TIMESTAMP WITH TIME ZONE,
    duration_ms INTEGER,
    input_data JSON,               -- Sanitized input data
    output_data JSON,              -- Results and outputs
    error_message TEXT,
    metadata JSON,                 -- Additional context
    -- Foreign key relationships
    report_id UUID,
    completion_id UUID,
    widget_id UUID,
    step_id UUID,
    -- User context
    external_platform VARCHAR,
    external_user_id VARCHAR,
    created_at TIMESTAMP WITH TIME ZONE,
    updated_at TIMESTAMP WITH TIME ZONE
);
```

### LLMCallLog Table
```sql
CREATE TABLE llm_call_logs (
    id UUID PRIMARY KEY,
    execution_log_id UUID,        -- Links to execution
    provider VARCHAR NOT NULL,     -- openai, anthropic, google, etc.
    model_id VARCHAR NOT NULL,     -- gpt-4, claude-3, etc.
    call_type VARCHAR DEFAULT 'inference', -- inference, inference_stream
    status VARCHAR DEFAULT 'started',
    start_time TIMESTAMP WITH TIME ZONE,
    end_time TIMESTAMP WITH TIME ZONE,
    duration_ms INTEGER,
    prompt TEXT,
    response TEXT,
    error_message TEXT,
    -- Token tracking
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    -- Cost tracking
    input_cost FLOAT,
    output_cost FLOAT,
    total_cost FLOAT,
    -- Streaming support
    is_streaming BOOLEAN DEFAULT FALSE,
    chunks_count INTEGER,
    metadata JSON,
    -- Foreign keys
    report_id UUID,
    completion_id UUID,
    created_at TIMESTAMP WITH TIME ZONE,
    updated_at TIMESTAMP WITH TIME ZONE
);
```

## Usage

### 1. Basic Setup

The logging system is automatically initialized in the main Agent class when a database session is available:

```python
from app.ai.agent import Agent
from app.ai.logging import ExecutionLogger, LLMCallLogger

# Logging is automatically set up in Agent.__init__
agent = Agent(db=db_session, model=model, ...)
# agent.execution_logger and agent.llm_call_logger are now available
```

### 2. Manual Logger Usage

For custom logging in individual agents:

```python
from app.ai.logging import ExecutionLogger, LLMCallLogger

# Initialize loggers
execution_logger = ExecutionLogger(db_session)
llm_call_logger = LLMCallLogger(db_session)

# Context manager approach (recommended)
async with execution_logger.log_execution(
    agent_type="custom_agent",
    execution_step="custom_task",
    input_data={"param": "value"}
) as log_context:
    # Your agent logic here
    result = await perform_task()
    
    # Set output data
    log_context.output_data = {"result": result}
    log_context.metadata = {"additional": "info"}
```

### 3. LLM Call Logging

LLM calls are automatically logged when using the LLM class with a logger:

```python
from app.ai.llm.llm import LLM

# LLM with automatic logging
llm = LLM(model=model, llm_call_logger=llm_call_logger)

# Regular inference (automatically logged)
response = llm.inference(
    prompt="What is AI?",
    execution_log_id="exec_123",  # Optional: link to execution
    report_id="report_456"        # Optional: link to report
)

# Streaming inference (automatically logged)
async for chunk in llm.inference_stream(
    prompt="Explain machine learning",
    execution_log_id="exec_123"
):
    print(chunk)
```

### 4. Custom Execution Logging

For more control over execution logging:

```python
# Start execution manually
execution_log = await execution_logger.start_execution(
    agent_type="planner",
    execution_step="plan",
    action_type="generate_plan",
    input_data={"schemas": schemas, "prompt": prompt},
    report_id=report_id,
    completion_id=completion_id
)

try:
    # Your execution logic
    plan = await generate_plan()
    
    # End with success
    await execution_logger.end_execution(
        execution_log,
        status="completed",
        output_data={"plan": plan}
    )
except Exception as e:
    # End with failure
    await execution_logger.end_execution(
        execution_log,
        status="failed",
        error_message=str(e)
    )
```

## API Endpoints

### Execution Logs

#### GET `/execution-logs/executions`
Retrieve execution logs with filtering and pagination.

**Query Parameters:**
- `report_id`: Filter by report ID
- `agent_type`: Filter by agent type (planner, designer, answer, etc.)
- `execution_step`: Filter by execution step (plan, execute, observe, etc.)
- `action_type`: Filter by action type
- `status`: Filter by status (started, completed, failed, cancelled)
- `external_platform`: Filter by external platform
- `start_date`: Filter executions after this date
- `end_date`: Filter executions before this date
- `limit`: Maximum records to return (max 500)
- `offset`: Number of records to skip
- `include_llm_calls`: Include associated LLM calls

**Example:**
```bash
GET /execution-logs/executions?agent_type=planner&status=completed&limit=20
```

#### GET `/execution-logs/executions/{execution_id}`
Get all logs for a specific execution ID (groups related logs).

**Query Parameters:**
- `include_llm_calls`: Include associated LLM calls (default: true)

### LLM Call Logs

#### GET `/execution-logs/llm-calls`
Retrieve LLM call logs with filtering and pagination.

**Query Parameters:**
- `provider`: Filter by LLM provider
- `model_id`: Filter by model ID
- `call_type`: Filter by call type
- `status`: Filter by status
- `report_id`: Filter by report ID
- `execution_log_id`: Filter by execution log ID
- `start_date`: Filter calls after this date
- `end_date`: Filter calls before this date
- `limit`: Maximum records to return
- `offset`: Number of records to skip

### Analytics

#### GET `/execution-logs/analytics/summary`
Get analytics summary for executions and LLM calls.

**Query Parameters:**
- `start_date`: Analytics start date (default: 30 days ago)
- `end_date`: Analytics end date (default: now)
- `report_id`: Filter by report ID

**Response includes:**
- Total executions and success rates
- Agent type distribution
- Duration statistics
- Total LLM calls and costs
- Token usage by provider/model
- Cost breakdown

### Management

#### DELETE `/execution-logs/executions/{execution_id}`
Delete all logs for a specific execution ID.

## Example API Responses

### Execution Log
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "execution_id": "exec_abc123_1704067200",
  "agent_type": "planner",
  "execution_step": "plan",
  "action_type": "generate_plan",
  "status": "completed",
  "start_time": "2024-01-01T10:00:00Z",
  "end_time": "2024-01-01T10:00:05Z",
  "duration_ms": 5000,
  "input_data": {
    "prompt": "Create a sales dashboard",
    "schemas": "..."
  },
  "output_data": {
    "plan": [
      {"action": "create_widget", "details": {...}}
    ]
  },
  "llm_calls": [
    {
      "id": "...",
      "provider": "openai",
      "model_id": "gpt-4",
      "input_tokens": 1500,
      "output_tokens": 500,
      "total_cost": 0.045
    }
  ]
}
```

### Analytics Summary
```json
{
  "date_range": {
    "start_date": "2024-01-01T00:00:00Z",
    "end_date": "2024-01-31T23:59:59Z"
  },
  "execution_analytics": {
    "total_executions": 1250,
    "status_counts": {
      "completed": 1100,
      "failed": 120,
      "cancelled": 30
    },
    "agent_type_counts": {
      "planner": 400,
      "answer": 350,
      "designer": 300,
      "coder": 200
    },
    "duration_stats": {
      "avg_duration_ms": 3500,
      "min_duration_ms": 500,
      "max_duration_ms": 30000
    }
  },
  "llm_call_analytics": {
    "total_calls": 2800,
    "provider_counts": {
      "openai": 2000,
      "anthropic": 600,
      "google": 200
    },
    "model_counts": {
      "gpt-4": 1200,
      "gpt-3.5-turbo": 800,
      "claude-3-sonnet": 600,
      "gemini-pro": 200
    },
    "token_stats": {
      "total_input_tokens": 2500000,
      "total_output_tokens": 800000,
      "total_tokens": 3300000,
      "total_cost": 75.25
    }
  }
}
```

## Best Practices

### 1. Data Privacy
- Input/output data is automatically sanitized for JSON storage
- Sensitive data should be filtered out before logging
- Consider data retention policies for log cleanup

### 2. Performance
- Logging is designed to be non-blocking
- Failed logging operations don't interrupt main execution
- Use appropriate limits when querying large datasets

### 3. Cost Monitoring
- Token pricing is configurable in `LLMCallLogger.token_prices`
- Regular cost monitoring helps optimize model usage
- Consider setting up alerts for cost thresholds

### 4. Debugging
- Use execution IDs to trace complete workflows
- Error messages and stack traces are automatically captured
- Input/output data helps reproduce issues

### 5. Analytics
- Regular analytics help identify performance bottlenecks
- Monitor success rates by agent type
- Track cost trends over time

## Configuration

### Token Pricing
Update token pricing in `LLMCallLogger.__init__()`:

```python
self.token_prices = {
    'gpt-4': {'input': 0.03, 'output': 0.06},
    'custom-model': {'input': 0.001, 'output': 0.002},
    # Add your models here
}
```

### Data Retention
Consider implementing data retention policies:
- Archive old logs to reduce database size
- Set up automated cleanup for completed executions
- Balance storage costs with debugging needs

## Migration

To add the logging tables to your database:

1. Create a new Alembic migration:
```bash
alembic revision --autogenerate -m "Add execution and LLM call logging tables"
```

2. Run the migration:
```bash
alembic upgrade head
```

3. The tables will be automatically created and relationships established.

## Integration with Existing Agents

The logging system is already integrated into:
- ✅ Main Agent class (`agent.py`)
- ✅ LLM class (`llm.py`) 
- ⏳ Individual agents (planner, designer, answer, etc.) - **Next phase**

For individual agent integration, add logging to each agent's execute method following the patterns shown in the main Agent class.