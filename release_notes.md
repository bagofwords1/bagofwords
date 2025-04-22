# Release Notes

## Verion 0.0.164 (April 22nd, 2025)

- Refactored dashboard visualization capabilities:
  - Improved chart rendering performance and responsiveness
  - Enhanced data handling for large datasets
  - Added better error handling and validation
  - Streamlined chart configuration options
- Fixed candlestick chart bug where single stock data was not properly displayed when no ticker field was present

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
