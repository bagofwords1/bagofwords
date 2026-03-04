<div align="center">

# MetricChat

**Your data team's AI analyst — ask questions, get answers, build dashboards.**

An agentic analytics platform that connects to your data warehouse, learns your business context over time, and delivers trusted, observable results.

[Quick Start](#quick-start) &middot; [Features](#why-metricchat) &middot; [Integrations](#integrations) &middot; [Enterprise](#enterprise) &middot; [Development](#development)

</div>

---

## Why MetricChat?

Most AI analytics tools are glorified SQL generators. MetricChat is a full analytics agent — it remembers your data, follows your rules, and shows its work.

<table>
<tr>
<td width="50%">

### Memory & Context

The agent builds a semantic understanding of your data down to the table and column level. It learns terminology, business logic, and usage patterns over time — so queries get smarter with every conversation.

</td>
<td width="50%">

### Rules & Guardrails

Define instructions, approved terms, and constraints with versioning and approval workflows. Sync rules from git to auto-index dbt models, markdown docs, and code — so the AI always stays within bounds.

</td>
</tr>
<tr>
<td width="50%">

### Full Observability

Every agent decision is traced — plans, tool calls, guardrail checks, LLM judge evaluations, and user feedback. Debug any answer. Improve the loop continuously.

</td>
<td width="50%">

### Dashboards & Sharing

Build and share dashboards from conversations. Save queries to a searchable catalog. Give your team a single place for trusted, reusable analytics.

</td>
</tr>
<tr>
<td width="50%">

### Any LLM, Any Warehouse

OpenAI, Anthropic, Gemini, Ollama — connected to Snowflake, BigQuery, Postgres, Redshift, and 20+ more. Swap or combine providers without breaking workflows.

</td>
<td width="50%">

### Upload Your Own Data

Upload CSV, Excel, or PDF files to instantly create queryable DuckDB data sources — no database setup required. Perfect for ad-hoc analysis and quick data exploration.

</td>
</tr>
<tr>
<td width="50%">

### MCP Support

Use MetricChat from Cursor, Claude Desktop, and other AI clients via the Model Context Protocol — while reliably tracking every request and data operation.

</td>
<td width="50%">

### Slack & Teams

Deploy a bot into Slack or Microsoft Teams so your team can ask data questions where they already work — with full authentication and access controls.

</td>
</tr>
</table>

---

## Quick Start

Get running in 30 seconds:

```bash
docker run -p 3000:3000 walrusquant/metricchat
```

Then open [http://localhost:3000](http://localhost:3000) and connect your first data source.

<details>
<summary><strong>Run with PostgreSQL (recommended for production)</strong></summary>

```bash
docker run -p 3000:3000 \
  -e MC_DATABASE_URL=postgresql://user:password@host:5432/dbname \
  walrusquant/metricchat
```

</details>

<details>
<summary><strong>Docker Compose</strong></summary>

```bash
# Production (Postgres + Caddy SSL)
docker compose up -d

# Development (no SSL)
docker compose -f docker-compose.dev.yaml up -d
```

</details>

<details>
<summary><strong>Kubernetes</strong></summary>

Helm chart included. See [`k8s/README.md`](./k8s/README.md) for setup instructions.

</details>

---

## Integrations

### LLM Providers

| Provider | Models | Notes |
|:---|:---|:---|
| **OpenAI** | GPT-5.1, GPT-5, GPT-4.1, etc. | Any OpenAI-compatible endpoint (vLLM, LM Studio, etc.) |
| **Azure OpenAI** | GPT-5.1, GPT-5, GPT-4.1, etc. | Azure resource/endpoint support with deployment names |
| **Anthropic** | Claude 4.5 Opus, Claude 4.5 Sonnet, etc. | Direct API key |
| **Google Gemini** | Gemini 2.5 Pro, Gemini 2.5 Flash | Google Cloud API key |
| **Ollama / Self-hosted** | Any model | Point to any OpenAI-compatible base URL |

### Data Sources

<details>
<summary><strong>26 supported databases, warehouses, and services</strong></summary>

**Databases & Warehouses**

PostgreSQL &middot; Snowflake &middot; BigQuery &middot; Databricks SQL &middot; Microsoft Fabric &middot; MySQL &middot; AWS Athena &middot; MariaDB &middot; DuckDB &middot; Microsoft SQL Server &middot; ClickHouse &middot; Azure Data Explorer &middot; Vertica &middot; AWS Redshift &middot; Presto &middot; Apache Pinot &middot; Oracle DB &middot; MongoDB &middot; Sybase SQL Anywhere

**Services & Platforms**

Salesforce &middot; NetSuite &middot; Tableau &middot; PowerBI &middot; QlikView &middot; AWS Cost Explorer &middot; PostHog

**File Uploads**

CSV &middot; Excel &middot; PDF → instant DuckDB data sources

</details>

---

## How It Works

```
You ask a question
  -> MetricChat agent plans an approach
    -> Pulls relevant context (memory, rules, schema)
    -> Writes and executes SQL against your warehouse
    -> Validates results against guardrails
    -> Returns an answer with full trace
      -> Builds memory for next time
```

Every step is observable. Every decision is logged. Nothing is a black box.

---

## Enterprise

For teams that need more:

| | |
|:---|:---|
| **SSO** | Google Workspace, OIDC-compatible identity providers |
| **RBAC** | Role-based access control with fine-grained permissions |
| **Self-hosted** | Deploy on your own infrastructure with full data control |
| **Audit logs** | Complete trail of every query, access, and configuration change |

---

## Development

See [DEV.md](./DEV.md) for local development setup (Python 3.12+, Node 18+, Yarn).

```bash
# Backend
cd backend && python main.py       # http://localhost:8000

# Frontend
cd frontend && yarn dev            # http://localhost:3000
```

---

## Documentation

Full documentation at [metricchat.com/docs](https://www.metricchat.com/docs). Quick references for [getting started](./docs/quickstart.md) and [deployment](./docs/install.md) are also in this repo.

---

## License

AGPL-3.0 — see [LICENSE](./LICENSE).

MetricChat is forked from [Bag of Words](https://github.com/bagofwords1/bagofwords), an open-source agentic analytics platform.
