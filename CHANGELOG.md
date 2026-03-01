# Release Notes

## Version 0.0.320 (February 24, 2026)
- Improve table lookup
- Improve OAuth MCP integration

## Version 0.0.320 (February 22, 2026)
- Support deployment in airgapped systems
- Improve PowerBI integration
- Improve Thumbnail generatio for Artifacts


## Version 0.0.319 (February 22, 2026)
- Fixed edit connection "Test Connection" to validate new credentials instead of using saved ones
- Credentials in edit mode are now locked by default with a "Change" button to explicitly unlock
- Renamed "Domains" to "Data Agents" in connection detail modal

## Version 0.0.318 (February 22, 2026)
- Added Sybase SQL Anywhere data source connector (enterprise license required)
- Uses FreeTDS ODBC driver for TDS protocol connectivity on port 2638

## Version 0.0.316 (February 21, 2026)
- Added filters for low score agent executions in monitoring/diagnosis
- Enhanced file upload and completion context handling, and special support for images
- Pass images and screenshots to create_artifact tool

## Version 0.0.315 (February 19, 2026)
- Improved organization logo upload
- Power BI: one table per internal table, relationship support, cleaner SharePoint names
## Version 0.0.314 (February 18, 2026)
- Added Microsoft Fabric data source integration (Warehouse and Lakehouse SQL endpoints)
- Azure AD Service Principal authentication support for Fabric
- Added `read_artifact` tool and improved context engineering for designing dashboards

## Version 0.0.313 (February 16, 2026)
- Update license env variable and secret configuration in k8s and docker-compose

## Version 0.0.312 (February 14, 2026)
- Refactor sidebar to use nav config and proper active states
- Improved slides artifact generation 

## Version 0.0.311 (February 13, 2026)
- Multi-connection support: data sources can now have multiple connections
- Added PowerBI and Qlik (QVD) data source integrations (Enterprise)
- Configurable step retention per organization (Enterprise)
- Exclude shared conversations and published reports from step cleanup
- Connection icons shown when describing/inspecting tables
- Schema enrichment with metadata and column comments
- Data agents and example agent templates
- Delete connections support
- Artifact thumbnails
- Added filtering for reports by schedule to easily view reports based on their schedule settings
- Added domain filtering for monitoring diagnosis to filter by specific domains
- Added report thumbnail generation and preview cards on home page for quick visual reference
- Added support for Claude Opus 4.6 model

## Version 0.0.309 (February 4, 2026)
- Create artifact (dashboard/slides) tool is now available via MCP 
- Added support for Databricks SQL
- Add enterprise license management and audit log

## Version 0.0.308 (January 31, 2026)
- Instruction @mentions now only show published instructions from the main build
- Referenced instructions are automatically loaded into AI context when a parent instruction mentions them
- Schema index and full schema now display instruction count per table, guiding the planner to use `describe_tables` for business rules
- Updated MCP `get_context` tool to expose instruction count per table
- **Microsoft Teams Integration**: Full bot support for Teams channels and 1:1 chats
  - Send questions via @mention in channels or direct message the bot
  - Thread-based conversations with report reuse across replies
  - User verification flow with Adaptive Cards
  - Markdown tables, count results, and report links rendered natively in Teams
  - JWT signature verification for inbound webhooks
  - Teams setup UI in Settings > Integrations

## Version 0.0.307 (January 28, 2026)
- Separated code and queries for better UX
- Added created/approved by metadata for instructions

## Version 0.0.306 (January 26, 2026)
- **New Interactive Dashboards**: Dashboards are now generated as executable React/HTML code, enabling rich interactivity, custom styling, and dynamic visualizations
- **Visual Feedback**: Upload screenshots or images with your prompts to show the AI exactly what you want—perfect for requesting design tweaks or pointing out issues
- Dashboard validation now includes automatic screenshot capture, allowing the AI to visually verify the output before finalizing
- Added vision model support for OpenAI, Anthropic, and Google Gemini LLM providers

## Version 0.0.305 (January 24, 2026)
- **Rebuilt Dashboards**: Now fully AI-generated as executable code (React/HTML) with iterative refinement based on conversation history
- Fixed @ mention detection in prompt input (no longer triggers inside existing mentions)

## Version 0.0.304 (January 22, 2026)
- SQLite data source now available in production (previously dev-only)
- Security updates and dependency patches

## Version 0.0.303 (January 22, 2026)
- AI-suggested instructions now show persistent "Published" status with timestamp
- Added checkbox selection when publishing AI suggestions 
- Fixed AI builds not being linked to agent executions

## Version 0.0.302 (January 20, 2026)
- Rename Catalog to Queries
- Show chart and visualization in query page

## Version 0.0.301 (January 20, 2026)
- Support for local DuckDB databases via file:// or absolute path i.e /data/myduck.db
- Set global git repo management

## Version 0.0.300 (January 19, 2026)
- **Slack Integration Enhancements**
  - Thread-based responses: replies now appear in threads instead of separate messages
  - Each thread corresponds to a single report for better conversation continuity
  - Added support for @mentions in channels (in addition to DMs)
  - Visual feedback via emoji reactions: 👀 when processing, ✅ when complete
  - Data source access control: channel mentions query only public data sources, while DMs include private data sources the user has access to

## Version 0.0.298 (January 18, 2026)
- Added guardrails around code execution
- Removed code validation flag, as it's now deterministic and built-in 

# Version 0.0.297 (January 18, 2026)
- Introducing: Training Mode
  - A dedicated mode for documenting and managing your data domain knowledge
  - Explore schemas, inspect data, and create instructions to guide AI behavior
  - New tools: `create_instruction` and `edit_instruction` for real-time instruction management
  - Instructions are versioned and tracked in draft builds until finalized
- Improve DuckDB system prormpt
- HBD!

## Version 0.0.296 (January 12, 2026)
- Added PostHog integration for analytics
- Fix Dockerfile

## Version 0.0.294 (January 12, 2026)
- improve streaming performance
- support heatmap charts
- block sending prompts if no llm or data source/file were set
- improve conversation layout for mobile presentation
- add delete connection

## Version 0.0.293 (January 10, 2026)
- Fix tables page not showing all tables when navigating between pages

## Version 0.0.292 (January 9, 2026)
- Fix demo data sources not loading in Docker container

## Version 0.0.291 (January 6, 2026)
- Improve streaming for final_message
- Fix multi bar chart rendering bug 

## Version 0.0.290 (January 1, 2026)
- Happy new year!
- Connections and data sources are now decoupled. You can attach multiple data sources to a single connection, each with its own tables, instructions, and evals. This brings much greater flexibility, reliability, and organization to your workspace.
- New: Context Selector – easily control which data sources are currently active throughout the application.
- Added ability to share report conversations with others
- Clarify tool and prompt optimizations

## Version 0.0.288 (December 26, 2025)
- UI improvements: eval, build ID
- Added modal to manage test suites
- Added new MCP tools: list, create, and delete instructions

## Version 0.0.286 (December 25, 2025)
- Auto suggest instructions if user provided negative feedback to an answer
- Improve auto-detect uvicorn workers

## Version 0.0.284 (December 23, 2025)
- Git providers: Now support Personal Access Token (PAT) authentication for seamless integration.
- You can now create pull requests and branches for build (instruction versions) directly from the interface.
- Each build now includes integration tests and eval runs to ensure greater reliability and code quality.
- Simplified instruction status life cycle and integrating to buid statuses
- UI/UX upgrades: Enhanced workflows for adding instructions and reviewing builds, making navigation and use smoother.
- Code clean ups and tests

## Version 0.0.282 (December 22, 2025)
- Launched instruction build/versioning system: every instruction update creates a new version, with point-in-time builds (snapshots), approval workflow, diff, and rollback.
- All instructions now tied to builds; `is_main` build sets active instruction set for org, with full history & audit.
- Added `/builds` API: get builds, build diffs, rollback, and detailed version/content lineage for every instruction.
- Test/Eval runs can select which build to use.
- Exposed top-k instructions retrieval API.
- Extensive automated E2E test coverage for build/version/rollback/git flows.

## Version 0.0.280 (December 19, 2025)
- Context and instructions are now unified
- Instructions now show detailed usage statistics
- New rules for instruction application: always apply, or smart based on relevance/search
- Instructions table redesigned—now with filters, git-sourced instructions, and other enhancements
- Improved create/edit instruction workflow with a refreshed design
- Expanded and updated automated end-to-end tests

## Version 0.0.279 (December 17, 2025)
- Added **MCP Server** for integration with Claude, Cursor, and other MCP clients
- Available tools: `create_report`, `get_context`, `inspect_data`, `create_data`
- MCP sessions are fully tracked in reports with tool executions and visualizations
- Added per-user API keys for MCP and external integrations

## Version 0.0.278 (December 15, 2025)
- Enhancing MongoDB integration to support Atlas/SRV connections
- Add more triggers for autogenerate suggestions 
- UI improvements/fixes

## Version 0.0.277 (December 14, 2025)
- Frontend tests (playwright) and CI/CD improvements

## Version 0.0.274 (December 12, 2025)
- Added support for GPT-5.2 model
- Enhanced the describe entity tool for better usability and accuracy
- Fixed a user authentication bug affecting specific environments

## Version 0.0.271 (December 10, 2025)
- Describe entity from catalog - new tool!
- Remove forgot password/etc when SMTP is not available

## Version 0.0.270 (December 10, 2025)

- bug fixes, performance and reliability

## Version 0.0.269 (December 10, 2025)
- Performance and speed

## Version 0.0.268 (December 9, 2025)
- Speed and readme

## Version 0.0.266 (December 8, 2025)
- Added a new **Inspect Data** tool for quickly examining the structure and sample content of a dataset and preview data before generating insights or diagnosing issues
- Docker Compose now bundled for both development and production environments
- Added sample databases to assist onboarding and demos
- Enhanced overall system reliability and robustness

## Version 0.0.265 (December 7, 2025)
- Bug fixes

## Version 0.0.264 (December 6, 2025)
- Enhanced file management and analysis capabilities (supports xls, csv, and pdf files)
- Improved MariaDB improvements
- Add support for loading up to 60K tables when connecting data sources
- Added automated tests for postgres database

## Version 0.0.263 (December 4, 2025)
- System prompt improvements and a new section for analytical standards
- Improvements to custom LLM integration (set default/small default models)
- Data source onboarding improvement

## Version 0.0.262 (December 2, 2025)
- Added data source integration to MongoDB
- Added native support for Custom LLM endpoints (openai compatible)
- Added support for Claude Opus 4.5

## Version 0.0.261 (December 2, 2025)
- Bias partitions in bigquery

## Version 0.0.260 (December 2, 2025)
- Dependencies updates
- Improve instructions list modal 

## Version 0.0.259 (December 1, 2025)
- Introducing Filters in dashboards
- Performance improvements, page loads, indices, reliability, and more
- Improved resources selector in context page (toggle between chunks/files, index status info, and more)
- UI enhancements


## Version 0.0.258 (December 1, 2025)
- Increase anthropic max tokens to 32k
- Impove behavior of reindexing (do not auto-add)

## Version 0.0.257 (November 30, 2025)
- Added Azure Data Explorer data source (thanks @licanhua)
- Improved BigQuery system prompt to consider special syntax guidelines when generating code

## Version 0.0.256 (November 29, 2025)
- Improved visualization features
- Enhanced dashboard creation workflow
- Suggestions now cover more user actions, such as corrections, querying the same tables, and sharing code
- Expanded instruction categories for system, dashboard, and visualizations
- UI improvements for agent trace, observations, and reduced visualization flicker
- Improved data source onboarding and test connections
- Added integration tests for LLMs and popular data sources

## Version 0.0.255 (November 27, 2025)
- Extended user token validity to one week, reducing the need for frequent logins
- Improved evaluation (Evals) features for more robust and insightful testing
- Added support for anonymous MySQL connections


## Version 0.0.254 (Noveber 25, 2025)
- Fix azure llm integration
- Improve mysql authentication 

## Version 0.0.253 (November 24, 2025)
- Gemini 3 Pro Preview added!

## Version 0.0.252 (November 22, 2025)
- Implemented tracking of LLM usage and associated costs in the console dashboard
- Enhanced metadata resource handling:
  - Remove objects no longer found during reindexing
  - Newly discovered objects are no longer auto-activated by default
- Introduced SQLite integration (for testing and development), and expanded test coverage for git repositories, metadata resources, and more
- Improved the process for deleting data sources
- Added bulk archive functionality for reports and revamped the main reports index page

## Version 0.0.251 (November 20, 2025)
- Data sources deletion

## Version 0.0.250 (November 19, 2025)
- Add context estimator when writing prompts

## Version 0.0.249 (November 19, 2025)
- Pinot get tables to use user:pass when creating the HTTP request

## Version 0.0.248 (November 18, 2025)
- Resolve flickering in the Reasoning section and enhance the reliability of data source deletion and modal overlays
- Improve stability and robustness of table auto-activation and deactivation

## Version 0.0.247 (November 17, 2025)
- Instruction labels added for more effective categorization and management
- Instructions can now be auto-enhanced with AI suggestions
- Message display now clearly distinguishes between user and agent responses
- Trace modal correctly navigates to the selected completion ID within the reports page

## Version 0.0.246 (November 16, 2025)
- Snowflake keypair auth
- Repair migrations

## Version 0.0.245 (November 16, 2025)
- Repair migrations

## Version 0.0.244 (November 15, 2025)
- Updating dependencies

## Version 0.0.243 (November 15, 2025)
- Fixing a couple of bugs and renaming release notes to CHANGELOG

## Version 0.0.242 (November 14, 2025)
- Enhanced markdown parser for better handling of complex formatting and edge cases
- Added support for Dataform projects and introduced SQLX file parsing, enriching contextual metadata for queries and models
- Integrated GPT-5.1 as an available LLM by default
- Improved metadata indexing service with additional guardrails for git repository management and error management
- Upgraded user interface for reports and tables

## Version 0.0.241 (November 14, 2025)
- Optimize datbase migrations to include report_type
- Wrap maintenance job with guardrails

## Version 0.0.240 (November 13, 2025)
- Introducing Evals! You can now create and run custom sets of tests on demand to assess system performance. Define your own test cases and assertions, such as:
  - User prompts triggering create_data on table1 and table2
  - Validating that specific data columns (e.g., a, b, c) are present
  - Using custom LLM Judge prompts to automatically determine pass/fail outcomes
- Added the ability to adjust the sample k size for schema tables and metadata resources
- Improved the data source pages for a faster, smoother experience, including enhanced loading indicators and improved item removal
- Unused steps are now auto-deleted after 14 days. You can restore them anytime by rerunning the code.

## Version 0.0.236 (November 13, 2025)
- Added sorting and filtering capabilities to the table selector
- Reduced logging verbosity in production environments
- Enforced strict limits on context section sizes

## Version 0.0.235 (November 12, 2025)
- Added ability to select and deselect items in table and metadata resource selectors
- Enhanced BigQuery integration to allow connections to multiple datasets
- Enforced organization-level uniqueness for data source and LLM provider names
- Allow service json for BigQuery required user auth mode

## Version 0.0.233 (November 11, 2025)
- Improved instructions visibility in prompts' context
- Introduced an "Analysis Panel" for admins when creating or approving instructions:
  - Impact Score Estimation: Evaluate how the new instruction relates to existing prompts and user questions
  - Related Instructions: Identify potential redundancy or conflicts with other instructions
  - Related Metadata Resources: Review if the instruction overlaps or conflicts with current enriched context (such as dbt, markdown, etc.)

## Version 0.0.232 (November 10, 2025)
- Introduced default small models: you can now designate a default "small" model for back-office operations such as evals, judge tasks, instruction generation, and more
- User feedback (thumbs up/down) is now attributed at the table level

## Version 0.0.231 (November 8, 2025)
- Enhanced the UI for agentic retrieval and search for greater clarity and usability
- Refined the agent head prompt to more effectively leverage and guide the use of search tools
- Improved the agent trace user interface for better readability and interaction

## Version 0.0.230 (November 6, 2025)
- Introduced a new create_data tool that is more robust, reliable and accurate data generation
- Enhanced code generation for more accurate and robust SQL and Python outputs
- Improved chart visualizations for clearer and more informative data presentation
- Added new data source integration support: Apache Pinot and Oracle DB
- Table browsing now displays detailed statistics, including usage frequency, scoring, and feedback metrics
- Launched the new `read_resources` tool for intelligent, on-demand searching across all metadata resources
- Added successful executed queries in the same tables for when agent is generating code


## Version 0.0.220 (November 4, 2025)
- Added BigQuery support for `maximum_bytes_billed` for cost guardrails and support for `use_query_cache`
- Improved main AI loop with additional observations from sub-agent create data (code, errors, etc)
- Improved UI for list of instrusctions modal - pagination, visibility, etc

## Version 0.0.219 (November 3, 2025)
- Improved table discovery and retrieval in main agent loop
- Introduced describe_tables tool for better data modeling, with light UI signaling
- Reduced the main agent's context footprint by 5x, significantly faster and leaner
- The create data sub-agent now receives a provided list of tables instead of inferring the data model itself

## Version 0.0.218 (November 1, 2025)
- Fixed issue where the data source form was not fully rendered in the onboarding screen
- Fixed issue where Claude outputs a Python code fence before the actual code

## Version 0.0.217 (November 1, 2025)
- Basic telemetry (configurable in bow-config)

## Version 0.0.215 (October 31, 2025)
- Support multi schema for Postgres client

## Version 0.0.214 (October 30, 2025)
- Support multi-db connection for ClickHouse

## Version 0.0.213 (October 28, 2025)
- Clickhouse fix
- Better rendering of booleans in connection form

## Version 0.0.212 (October 20, 2025)
- Integrate Mentions component and enhance prompt capabilities
- Implement mentions context integration in tools and agents
- Released: Catalog feature for efficient management and discovery of models, metrics, visualizations, and queries. Enables reusable components and enhances AI analyst intelligence
- Fix yarn cache issue in docker image

## Version 0.0.206 (October 19, 2025)
- Bug fix reloading tables in schema

## Version 0.0.205 (October 17, 2025)
- Added support for multiple schemas in Snowflake
- Added `MSSQL` driver into Dockerfile 

## Version 0.0.204 (October 16, 2025)
- Fixed permission issue in Docker when uploading files
- Fixed instructions not showing creator in instruction list

## Version 0.0.203 (October 12, 2025)
- Enhanced the chat interaction and conversation flow with the AI agent
  - Improved prompt capabilities by auto setting thinking levels
  - Enhanced message context with processed data and answer metadata for better LLM interactions
- Optimized CI/CD workflows by integrating GitHub Release automation

## Version 0.0.202 (October 8th, 2025)
- Added DuckDB support for object store files (aws, gcs, azure)
- Added Claude Sonnet 4.5 support

## Version 0.0.200 (September 27, 2025)
- Enhanced data source setup experience for new users
- Redesigned user interface for data source management
- Introduced "require user authentication" option for data sources
- Sample questions for data sources is now customizable
- Added to organizations ability to set judge, autogen instructions and code editing as enabled/disabled
- Added a bunch of AGENTS.md files throughout the repo for faster and better coding

## Version 0.0.199 (September 20, 2025)
- Redesigned application onboarding experience
- Implemented automatic instruction suggestions throughout the onboarding process
- Added support to Tableau as a data source
- Some general updates, bug fixes and new tests and sentry removal

## Version 0.0.198 (September 17, 2025)
- Adding login with OpenID Connect (Okta, etc)
- Updating Helm to allow oidc params and auth mode (hybrid, local or sso)
- Touch up to signin/signup screens
- Fix docker image to include client for openssh

## Version 0.0.197 (September 15, 2025)
- Introduced Tableau data source integration: TDS files can now be imported to enhance contextual information for data sources
- Deprecated AI Rules feature at the data source level, consolidating rule management into the centralized instruction system
- Added support for Google Gemini LLM
- Added verbosity to git integration
- Squashed bugs and improved overall usability


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
