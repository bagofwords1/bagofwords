<div align="center">
  <img src="./media/logo-128.png" alt="Bag of Words" width="128" />

## The open-source agentic analytics platform

BOW connects any LLM to your data and gives agents the context they need to do useful work.

Each agent gets its own data, tools, credentials, instructions, permissions. Start in chat, then run the same agents in reports, dashboards, automations, scheduled tasks, team channels, and MCP clients.

Set evals and use self-improving loops to make agents more reliable over time.

[![Website](https://img.shields.io/badge/Website-bagofwords.com-blue)](https://bagofwords.com)
[![Docs](https://img.shields.io/badge/Docs-Documentation-blue)](https://docs.bagofwords.com)
[![Docker](https://img.shields.io/badge/Docker-Hub-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/bagofwords/bagofwords)
[![e2e tests](https://github.com/bagofwords1/bagofwords/actions/workflows/e2e-tests.yml/badge.svg)](https://github.com/bagofwords1/bagofwords/actions/workflows/e2e-tests.yml)
</div>

## Features

- **Analysis:** Create reports and dashboards, generate queries, and run deep or root cause analysis.
- **Agent context:** Configure each agent with the right data, tools, credentials, instructions, permissions, and starters.
- **Automations:** Schedule reports, run recurring tasks, and trigger investigations from events and webhooks.
- **Channels:** Run headlessly via Claude Code, Codex, and other MCP clients, or through Microsoft Teams, Slack, WhatsApp, email, Excel, and the web app.
- **MCP gateway:** Connect agents to MCP servers and custom APIs, then expose their context and tools to MCP clients through one governed gateway.
- **Evals and self-improvement:** Set evals for expected behavior. When they fail, agents can draft instruction fixes and re-run the evals; passing changes can wait for approval or be promoted automatically.
- **Governance:** Control access with RBAC, approvals, audit logs, service accounts, SSO, and model policies.

[Deploy anywhere](https://docs.bagofwords.com/install)

[![Bag of Words demo](./media/hero3.png)](https://bagofwords.com/demos/hero4.mp4)

---

## Quick Start

```bash
# Run with SQLite (default)
docker run -p 3000:3000 bagofwords/bagofwords
```

### Run with PostgreSQL

```bash
docker run -p 3000:3000 \
  -e BOW_DATABASE_URL=postgresql://user:password@localhost:5432/dbname \
  bagofwords/bagofwords
```

Docker Compose and Kubernetes deployments are also available and recommended for servers. See the [installation docs](https://docs.bagofwords.com/install).

## From Analysis Context to Action

### Start with chat

Ask questions across your data and get queries, charts, reports, dashboards, deep analysis, and root cause analysis. Agents plan their work, use tools, and reflect on the result.

<div align="center">
  <img src="./media/chat.png" alt="Chat with data in Bag of Words" width="100%" />
</div>

### Give every agent the right context

Choose the data and tools an agent can use, provide the right credentials, and add instructions and starters for its job. Manage shared business definitions and guardrails with versioning, review flows, and Git sync for dbt, markdown, code, and more.

<div align="center">
  <img src="./media/instructions-table.png" alt="Manage agent instructions in Bag of Words" width="100%" />
</div>

### Turn analysis into repeatable work

Schedule reports and investigations, trigger agents from webhooks, and send results to the people and systems that need them. The same analysis context is available in chat, automations, channels, and external MCP clients.

### Evaluate and improve

Define eval sets for the behavior that matters and run them as agents change. When an eval fails, a self-improving loop can draft instruction changes and re-run the tests up to a configured limit. Passing candidates can wait for approval or be promoted automatically. Traces, plans, tool calls, LLM judges, and feedback are available for inspection throughout.

<div align="center">
  <img src="./media/monitoring.png" alt="Monitor agent runs in Bag of Words" width="100%" />
</div>

### Reuse trusted work

Save and share useful queries, datasets, reports, and dashboards. Reusing reviewed work gives both people and agents a stronger starting point for the next analysis.

<div align="center">
  <img src="./media/catalog.png" alt="Shared data catalog in Bag of Words" width="100%" />
</div>

## Architecture

Bag of Words sits between your models, enterprise data, tools, and channels. It builds a governed analysis context for each agent, then carries that context from an interactive question to a scheduled or event-driven workflow.

Bring your own models and infrastructure. Connect databases, warehouses, BI systems, files, business apps, MCP servers, and custom APIs without tying the workflow to one provider.

<div align="center">
  <img src="./media/arch.png" alt="Bag of Words architecture" width="100%" />
</div>

## Integrations

### Bring Any LLM

Use your own API keys, endpoints, and model deployments. Multiple providers and models can be configured in the same environment.

| Provider | Supported models and APIs | Notes |
|---|---|---|
| **OpenAI** | GPT and reasoning models | OpenAI API support |
| **Azure OpenAI** | GPT and reasoning models | Azure endpoints and deployment names |
| **Google Gemini** | Gemini and Flash models | Google API key support |
| **Anthropic** | Claude models | Anthropic API key support |
| **AWS Bedrock** | Foundation models available through Bedrock | API key, AWS access key, or IAM authentication |
| **Any OpenAI-compatible API** | Ollama, Groq, Together AI, vLLM, LM Studio, and more | Provide a base URL and optional API key |

### Connect Any Data

| Connector | Category |
|---|---|
| PostgreSQL | Database / warehouse |
| Snowflake | Database / warehouse |
| Google BigQuery | Database / warehouse |
| Databricks SQL | Database / warehouse |
| Microsoft Fabric | Database / warehouse |
| MySQL | Database / warehouse |
| AWS Athena | Database / warehouse |
| MariaDB | Database / warehouse |
| DuckDB | Database / warehouse |
| Microsoft SQL Server | Database / warehouse |
| ClickHouse | Database / warehouse |
| Azure Data Explorer | Database / warehouse |
| Vertica | Database / warehouse |
| AWS Redshift | Database / warehouse |
| Trino | Database / warehouse |
| Apache Pinot | Database / warehouse |
| Apache Druid | Database / warehouse |
| Oracle Database | Database / warehouse |
| MongoDB | Database / warehouse |
| Sybase SQL Anywhere | Database / warehouse |
| Teradata Vantage | Database / warehouse |
| SQLite | Database / warehouse |
| Spark | Database / warehouse |
| NetSuite | Business app |
| Salesforce | Business app |
| ServiceNow | Business app |
| AWS Cost Explorer | Business app |
| PostHog | Business app |
| Outlook Mail | Business app |
| Elasticsearch | Search and observability |
| OpenSearch | Search and observability |
| Splunk | Search and observability |
| Zabbix | Monitoring and observability |
| Jaeger | Tracing and observability |
| Tableau | BI tool |
| Power BI | BI tool |
| Power BI Report Server | BI tool |
| Qlik Sense | BI tool |
| Qlik QVD | BI tool |
| Sisense | BI tool |
| Oracle BI | BI tool |
| Infor OLAP | BI tool |
| Microsoft Analysis Services | BI tool |
| Timbr AI | Semantic layer |
| Files and Directories | Files |
| Amazon S3 | Files |
| CSV | Files |
| OneDrive | Files |
| SharePoint | Files |

### Connect Tools Through MCP

Bag of Words can connect to any MCP server or custom API. Ready-to-connect MCP integrations include:

| Integration | What it adds |
|---|---|
| Monday | Boards, items, updates, and workflows |
| Notion | Pages, databases, and workspace search |
| Jira / Atlassian | Jira issues and Confluence pages |
| Linear | Issues, projects, and cycles |
| Sentry | Errors, issues, releases, and diagnostics |
| GitHub | Repositories, issues, and pull requests |
| Google Drive | File search and content access |
| Gmail | Messages, threads, labels, and drafts |
| X | Posts, users, search, and trends |
| X (Write) | Create and delete posts through a custom API |
| Custom MCP server | Any compatible remote or self-hosted MCP server |
| Custom API | Internal and third-party HTTP APIs |

### Run Anywhere

| Surface | Use |
|---|---|
| Web app | Chat, reports, dashboards, evals, and monitoring |
| Claude Code, Codex, and MCP clients | Use agents headlessly through MCP |
| Excel | Bring governed analysis into spreadsheets |
| Microsoft Teams | Ask questions and receive results in Teams |
| Slack | Ask questions and receive results in Slack |
| WhatsApp | Run agent conversations from WhatsApp |
| Email | Ask questions and receive scheduled results by email |
| Webhooks and APIs | Trigger agents from other systems |
| Scheduled tasks | Run recurring reports, checks, and investigations |

## Enterprise

For teams that need stronger security, compliance, and governance:

- **Self-hosted:** Deploy on your own infrastructure and keep control of your data.
- **SSO and provisioning:** Connect Google Workspace and OIDC-compatible identity providers, with SCIM and LDAP support.
- **RBAC:** Apply fine-grained permissions to agents, data, tools, and administration.
- **Approvals and audit:** Review changes and track agent and data operations.
- **Service access:** Use API keys and service accounts for headless workflows.
- **Model controls:** Decide which providers and models are available to each organization.

## Security and Privacy

Bag of Words captures basic usage statistics from self-hosted instances to help improve the product. Disable telemetry in `bow-config.yaml`:

```yaml
telemetry:
  enabled: false
```

You can also disable Intercom support chat:

```yaml
intercom:
  enabled: false
```
