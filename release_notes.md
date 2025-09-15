# Release Notes

## Version 0.0.197 (September 15, 2025)
- Introduced Tableau data source integration: TDS files can now be imported to enhance contextual information for data sources
- Deprecated AI Rules feature at the data source level, consolidating rule management into the centralized instruction system

## Version 0.0.196 (September 14, 2025)
- Added inline code editor for queries with full execution capabilities: users can now edit query code, preview data results, visualize outputs, and save changes directly within the interface
- Added widget customization controls for labels, titles, and styling
- Rebuilt query/visualization engine for improved scalability
- Improved dashboard layout, reactivness and synchronization to other visualizations
- Enhanced backend architecture and data modeling to support query versioning and multi-visualization relations
- Added ability to test LLM connection before saving as a new provider

## Version 0.0.195 (September 10, 2025)
- Introducing Deep Analysis: Users can now change from Chat mode to Deep Analytics for doing a more comprehensive open ended analytics research to identify root cause, anomalies, opportunities, and more!
- New Prompt box for both home/report page, including customizing LLM per prompt
- Roles with console/monitoring access can now view the full agent loop trace inside the report chat

## Version 0.0.194 (September 9, 2025)
- **Enhanced Dashboards**
  - Improved dashboard creation, allowing more control on styles and the new dashboards look amazing!
  - User can now select themes (default, retro, hacker, or research)
- Added the answer question tool, allowing agent to search across schema, resources, and other pieces context to come up with the answer
- Improvements to Slack bot integration
- Enhancements around: cron visibility, excel files, and sharing

## Version 0.0.193 (September 6, 2025)
- Introduced automatic instruction suggestion system to enhance AI decision-making and performance. The system generates suggestions triggered by:
  - User clarifications regarding terms, facts, or metrics
  - AI successfully resolving data generation code after encountering multiple failures
- All generated suggestions are stored globally and require administrative review and approval before implementation
- Improved main AI agent planner prompt
- Redesigned and expanded the navigation menu, elevating monitoring and instructions to prominent first-class menu items
- Bug fixes and enhancements

## Version 0.0.192
- Fixed file upload functionality within Docker container environment
- Resolved issues with report rerunning capabilities
- Reduced database logging output to only display warnings and errors

## Version 0.0.190 (August 31, 2025)
- Launched Agent 2.0, a comprehensive redesign of the backend agentic architecture
  - Implements ReAct methodology with single-tool execution per planning cycle
  - Enhanced tool registry featuring comprehensive tracking and governance capabilities
  - Added clarify tool for detecting user queries with undefined metrics/measures or ambiguous requirements
  - Improved error handling, tool schema validation, and enhanced reliability throughout agent execution
  - Comprehensive tracking system for agent executions, tool usage, and AI decision-making processes
- Released Context Management 1.0, providing robust and reliable context tracking for both warm and cold AI interactions
  - Complete monitoring of context utilization patterns
  - Streamlined interface for context construction and management during agent operations
- Enhanced compatibility with LLMs that generate prefix/postfix formatting symbols such as json/``` markers
- Redesigned streaming architecture with server-sent events (SSE) implementation for real-time user prompt processing
- Enhanced admin interface for monitoring agent execution flows and tracking user request patterns
- Introduced new analytics visualization in console dashboard displaying metrics for data request creation (user-initiated), AI clarification requests, and additional operational insights
- Added automated testing for the system
- As this change was signifcant, old reports (in version prior 0.0.190) will be set as read-only.
- Introduced customizable branding and AI identity features, allowing organizations to upload their own logos, remove Bow attribution, and personalize their AI assistant's identity


## Version 0.0.189 (August 25, 2025)
- Enhanced table usage analytics with comprehensive success/failure tracking, performance scoring, and intelligent usage pattern recognition
- Implemented automated TableStats model to capture query performance metrics, execution outcomes, and user satisfaction data in real-time
- Advanced code generation now leverages historical success patterns and proven code snippets, significantly improving accuracy and reliability
- Upgraded AI planner with feedback-driven decision algorithms that incorporate table performance scores and usage data for continuous self-improvement
- Added weighted performance/feedback scoring based on user role (admin vs. rest)
- Added tests covering llm providers, azure backend, and console metrics

## Version 0.0.188 (August 23, 2025)
- Enhanced streaming reliability for data models and query results in chat interface
- Strengthened completion termination handling with comprehensive SIGKILL support across all agent lifecycle stages
- Introduced custom base URL configuration for OpenAI provider deployments
- Resolved console metrics and usage data functionality issues
- Corrected admin permissions to allow deletion (not just archival/rejection) of suggested instructions


## Version 0.0.186 (August 19, 2025)
- Enhanced instructions functionality with support for referencing dbt models, tables and other metadata resources
- Updated data source section with improved views of dbt and other metadata resources
- Fixed various bugs and enhanced overall usability

## Version 0.0.181 (August 10, 2025)
- Added data source visibility controls - admins can now set data sources as public or private within organizations and manage granular access permissions through user memberships
- Improved interface and user experience with differentiated views and controls for administrators versus regular users in the data source management area
- Integrated OpenAI's latest GPT-5 language model into the platform
- Updated Docker image to use Ubuntu base with latest security patches
- Updated Python package dependencies to latest stable versions
- Implemented container vulnerability scanning using Trivy in CI/CD pipeline

## Version 0.0.180 (August 6, 2025)
- Enhanced security by updating Dockerfile with latest vulnerability patches
- Integrated Claude 4 Sonnet and Opus language models
- Implemented full support for Vertica database connectivity and querying
- Added capability to incorporate markdown files from git repositories to enhance data sources with contextual information
- Added support for Azure OpenAI and custom model endpoints
- Added support for AWS Redshift database connectivity

## Version 0.0.177 (July 30, 2025)
- Added comprehensive admin console with three main sections: Explore, Diagnose, and Instructions management
- **Explore**: Organization analytics dashboard with real-time metrics, activity charts, performance tracking, table usage analysis, table joins heatmap, failed queries overview, recent instructions, top users, and prompt type analytics
- **Diagnose**: Advanced troubleshooting interface featuring failed query tracking, negative feedback analysis, instructions effectiveness scoring, detailed trace debugging, and issue categorization with actionable insights
- **Instructions**: Centralized instruction management system with search and filtering capabilities, add/edit functionality, data source associations, and user permission controls
- Added LLM Judge system for automated quality assessment - scores instruction effectiveness and context relevance on a 1-5 scale, evaluates AI response quality against user intent, and provides detailed reasoning for continuous system improvement

## Version 0.0.176 (July 26, 2025)
- Added ability to provide detailed feedback messages when submitting negative feedback on AI completions
- Improved reports main page UI

## Version 0.0.175 (July 26, 2025)
- Added ability for users to suggest new instructions and view published instructions
- Added workflow for admins and privileged users to review, approve, or reject suggested instructions
- Enhanced instruction management with data source associations - instructions can now be set globally or scoped to specific data sources
- Added visibility controls allowing admins to hide certain instructions from unprivileged users

## Version 0.0.174 (July 23rd, 2025)
- Filters and pagination for reports
- Reports are now invisible for other users when not published

## Version 0.0.172 (July 17th, 2025)
- Slack integration! Now admins can integrate their Slack organization account and have users converse with bow via slack. Includes user-level authorization, formatting, charts, and tables
- LookML support for git integration indexing
- Download steps data as CSV is now available in UI
- Added *Instructions*: add custom rules and instructions for LLM calls

## Version 0.0.166 (July 13th, 2025)
- Resolved membership invitation handling for closed deployments with OAuth authentication
- Corrected query count calculation in admin dashboard metrics

## Version 0.0.165 (July 7th, 2025)
- Added admin dashboard with usage analytics, query history tracking, and LLM feedback collection
- Implemented secure password recovery workflow with email verification
- Enhanced Kubernetes deployment configuration with expanded Helm chart coverage and options

## Version 0.0.164 (April 24th, 2025)

- Refactored dashboard visualization capabilities:
  - Improved chart rendering performance and responsiveness
  - Enhanced data handling for large datasets
  - Added better error handling and validation
  - Streamlined chart configuration options
- Fixed candlestick chart bug where single stock data was not properly displayed when no ticker field was present
- Added "File" top level navigation item. You can now see all files uploaded in the org
- You can now mention files outside of the report
- Support older version of Excel (97-03)

## Version 0.0.163 (April 21, 2025)

- Added new charts: area, map, treemap, heatmap, candletick, and more
- Better experience for charts to handle zoom, resize and overall better rendering

## Version 0.0.162 (April 16, 2025)

- Added ability to stop AI generation mid-completion with a graceful shutdown option
- Enhanced application startup reliability with automatic database connection retries
- Moved configuration management to server-side, enabling centralized client configuration
- Introduced support for deploying the application on Kubernetes clusters using Helm charts

## Version 0.0.161 (April 14, 2025)

- Added support to OpenAI GPT-4.1 model series

## Version 0.0.160 (April 12, 2025)

- Enhanced AI reasoning with ReAct framework and advanced planning capabilities
- Added upvote/downvote system for users to provide feedback on AI responses
- Added detailed reasoning explanations for AI responses in both UI and backend
- Improved Completion API to support synchronous jobs and return multiple completions
- Added OpenAPI support for global authentication and organization ID handling
- Enhanced organization settings and key management system
- Added visual source tracing in data modeling interface


## Version 0.0.155 (March 30, 2025)

- Added code validation for generated code
- Added safeguards for planner and coder agents
- Enabled code review for user's own code
- Fixed memory bug
- Added reasoning for planner agent
- Added data preview for LLM to achieve ReAct like flow with code generation
- Added organization settings to control AI features (specific agent skills) and additional settings (LLM viewing data, etc)
- Added df summary for tables
- Refactored code execution to be more robust and handle edge cases better

## Version 0.0.154 (March 24, 2025)

- Added advanced logging infrastructure
- Added e2e tests infrastructure and created first e2e test for user onboarding
- Improved ci/cd to run tests before building image

## Version 0.0.153 (March 22, 2025)

- Added support with dbt (via git repo) models and metrics
- Added context building for dbt models
- Added token usage to plan
- Added x-ray view for completions for admin roles

## Version 0.0.152 (March 16, 2025)

- Added AWS Athena integration
- Fixed bug when generating data source items
- Fixed bug when deleting data sources

## Version 0.0.151 (February 25, 2025)

- Added Claude 3.7 Sonnet to LLM models
- Added sync provider with latest models

## Version 0.0.15 (February 24, 2025)

- Added active toggle to data source tables to hide from context
- Fixed bug when generating data source items
- Add top bar to index page when no LLMs are available

## Version 0.0.14 (January 3, 2025)

- Added basic self-hosting support
- Added printing in code gen for better healing
- Improve answering agent and planner agent
- Replaced highcharts with ECharts
- Added intercom
- Various fixes and improvements

## Version 0.0.13 (December 26, 2024)

- Added prompt guidelines 
- Fixed modify, creation of widgets
- Fix proxy in nuxt/fastapi 
- Improved agents: dashboard, data model, chart, and prompt
- Added email validation for signups
- Dockerized the application
- Kubernetesized the application

## Version 0.0.12 (December 13, 2024)

- Added functionality to rerun dashboard steps, including cron support with configurable intervals
- Enabled automated LLM-generated summaries, starters, and reports for connected data sources
- Integrated Google Sign-In for seamless user authentication
- Added support for nginx reverse proxy
- Redesigned the home page for improved usability
- Added `bow-llm`, an abstracted LLM provider to set as the default
- Enhanced error handling with interactive toasts for better feedback
- Improved agent capabilities for code generation with better data source context and refined JSON parsing
- Enabled dynamic modifications to agent plans
- Resolved the "thinking bug"
- Made LLM provider presets uneditable
- Fixed WebSocket functionality in production
- Completed end-to-end tests for completions and data sources

## Version 0.0.11 (December 5, 2024)

- Completed integrations for Presto, Salesforce, and Google Analytics
- Added support to CRUD model providers and LLM models
- Added Claude AI model support
- Implemented data source credential security
- Enhanced agent capabilities:
  - Added clarification questions feature
  - Fixed dashboard layout generation
  - Fixed chart parameter rendering
  - Improved data model modifications
- UI Improvements:
  - Fixed report title updates
  - Resolved copy-paste styling issues in prompt box
  - Completed memberships interface
  - Enhanced mention component
- Infrastructure updates:
  - Added configuration file support
  - Removed Excel special routes
  - Cleaned up Nuxt from git repository
  - Fixed default menu data source association
  - Removed unique organization name requirement

## Version 0.0.10 (November 28, 2024)

- Edge left menu is now scrollable.  
- Fixed logo scaling issue in Edge browser.  
- Added schema browser for data sources.  
- Enabled manual test connection for data sources.  
- Converted data source list in prompts to a dictionary for better position handling.  
- Added Markdown support for completions in both agent and UI.  
- MySQL, BigQuery, Snowflake, MariaDB, and ClickHouse integrations are complete.  
- Initial scaffold for service type data sources
- Fixed `_build_schemas_context` to run only once during agent initialization.  
- Improved data source error messages.  
- Only active data sources are now displayed.  
- Data sources failing test connection are automatically set to inactive.  
- Introduced a service-type architecture for data source handling in code generation.  
- Permissions module completed.  
- Public dashboard completed.
