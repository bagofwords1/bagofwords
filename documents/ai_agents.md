# AI Agents
Bag of words is a data platform that uses AI agents to help users analyze and understand their data. To achieve this, we use an agentic approach

## Overview
At high level, Bag of words consits the following key components:
- Main execution loop
- Agents
- Data Sources Integration
- Project Management
- LLM Integrations
- Context Management

--

## Main Execution Loop

The main execution loop is the core orchestration component of Bag of Words that processes user inputs into actionable data analysis steps. It coordinates between different AI agents, data sources, and project management components to produce meaningful data visualizations and insights.

### Architecture

#### 1. Plan Generation Phase
- **Input Processing**: Takes user prompts and context
- **Plan Creation**: Uses Planner agent to generate structured action plans
- **Context Building**: 
  - Schemas from data sources
  - Previous conversation history
  - Memory context from past analyses

#### 2. Action Processing Phase
Each action goes through a processing pipeline:

##### Action Types
1. **Create Widget**
   - Creates new data visualization widgets
   - Generates data models
   - Executes data queries
   - Formats results for display

2. **Modify Widget**
   - Updates existing widgets
   - Transforms data models
   - Re-executes queries with modifications

3. **Answer Question**
   - Processes direct questions
   - Generates natural language responses
   - Uses context from schemas and previous interactions

4. **Design Dashboard**
   - Arranges widgets spatially
   - Creates text annotations
   - Optimizes layout for readability

##### 3. State Management
- Tracks action completion status
- Maintains widget and step relationships
- Handles error states and retries
- Updates database with results

##### 4. Integration Points
- **Data Sources**: Connects to databases and files
- **LLM**: Interfaces with language models
- **Project Management**: Updates report and widget states
- **Memory System**: Maintains context across interactions

### Error Handling
- Implements retry logic for data generation
- Maximum 3 retries per action
- Graceful fallback mechanisms
- Error logging and user feedback

### Output
- Structured widget data
- Dashboard layouts
- Natural language responses
- Updated project state

This architecture ensures a robust and maintainable system for processing complex data analysis requests while maintaining context and providing meaningful user feedback.

---

## Agents

### Planner 
- **Purpose**: Orchestrates analysis by breaking down user requests into actionable steps
- **Inputs**: Schemas, Persona, User prompt, Memories, Previous messages, Selected widget (optional)
- **Outputs**: Structured plan with actions (create_widget, modify_widget, answer_question, clarify_question, design_dashboard)
- **Role**: First agent called to determine action sequence

### Coder
- **Purpose**: Translates data models into executable Python code for data processing
- **Inputs**: Data model spec, Source schemas, Data source clients, Excel files, Previous code/errors, Memories, Previous messages
- **Outputs**: Python function that generates dataframes
- **Role**: Called after Planner for widget creation/modification

### Answer
- **Purpose**: Provides natural language responses to user questions
- **Inputs**: Schemas, User prompt, Widget data (if applicable), Memories, Previous messages
- **Outputs**: Formatted markdown response
- **Role**: Called for direct question answering actions

### Dashboard Designer
- **Purpose**: Creates layout and organization of dashboard elements
- **Inputs**: User prompt, Available widgets, Analysis steps, Previous messages
- **Outputs**: Dashboard design specification with widget positioning, text elements, layout parameters
- **Role**: Called for dashboard design actions

### Reporter
- **Purpose**: Generates report titles and summaries
- **Inputs**: Messages history, Analysis plan
- **Outputs**: Concise report title
- **Role**: Called when creating new reports/dashboards



--

## Data Sources Integration


--

## LLM Integrations

--

## Project Management

Project Manager handles database operations for managing completions, and analysis workflow components (reports, widgets, steps).

## Key Methods
- `create_message()`: Creates message completions
- `create_widget()`: Creates visualization widgets  
- `create_step()`: Creates analysis steps
- `update_*()`: Updates entities (completions, widgets, steps)

Agent main execution loop uses ProjectManager to:
- Process analysis actions
- Store analysis state and results 
- Manage relationships between components
- Handle errors and user feedback
- Create dashboard layouts

ProjectManager enables Agent to focus on orchestration while handling all database operations.

--

## Context Management

The agent builds context from 3 main sources:

1. `_build_schemas_context()`: 
- Collects schemas and business rules from data sources and files

2. `_build_messages_context()`:
- Gathers conversation history with associated widgets

3. `_build_memories_context()`:
- Retrieves relevant memories with their data models and samples

These contexts together provide agents with the necessary data structure, conversation history, and past analysis context to process new requests effectively.