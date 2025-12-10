<div>
  <img src="./media/logo-128.png" style="width:64px;" />
</div>

# Bag of words — deploy an AI Analyst in minutes

[![Website](https://img.shields.io/badge/Website-bagofwords.com-blue)](https://bagofwords.com)
[![Docs](https://img.shields.io/badge/Docs-Documentation-blue)](https://docs.bagofwords.com)
[![Docker](https://img.shields.io/badge/Docker-Hub-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/bagofwords/bagofwords)
[![e2e tests](https://github.com/bagofwords1/bagofwords/actions/workflows/e2e-tests.yml/badge.svg)](https://github.com/bagofwords1/bagofwords/actions/workflows/e2e-tests.yml)

Connect any LLM to any data source through a unified context layer (instructions, docs, dbt, code) with full observability.

Built for reliable quick answers and deeper investigations like root-cause analysis.

**Features:**

- **Agentic loop** - chat with data, ask complex questions, run multi-step analysis with tools, reasoning, and reflection  
- **Any LLM ↔ Any warehouse** - OpenAI, Anthropic, Gemini, Ollama ↔ Snowflake, BigQuery, Postgres, Redshift  
- **Git repo indexing** - use `AGENTS.md`, dbt, LookML, Dataform, Tableau, and code as live context  
- **Centralized instructions** - one place for definitions, business terms, and rules with approval workflows  
- **Self-learning** - semantic layer that auto-improves from feedback, corrections, and usage  
- **Reliability** - full observability: agent traces, plans, guardrails, LLM judges, evals  
- **Auth & deploy** - run via Docker/Compose/k8s with SSO, RBAC, warehouse inherited permissions, and audit trail  

[Deploy anywhere](https://docs.bagofwords.com/install)

[![Demo](./media/hero3.png)](https://bagofwords.com/demos/hero4.mp4)

---

## Quick Start 🚀

```bash
# runs with SQLite (default)
docker run -p 3000:3000 bagofwords/bagofwords
```

### Or, run with a ready PostgreSQL instance
```bash
docker run -p 3000:3000 \
  -e BOW_DATABASE_URL=postgresql://user:password@localhost:5432/dbname \
  bagofwords/bagofwords
```

#### Other deployment options
docker compose and Kubernete deployments are also available (and recommended for servers). See the [docs](https://docs.bagofwords.com/install).

## Product Overview

### Chat with any data
Create reports, deep analysis or quick visuals with an AI interface powered by an agentic-loop with tools, reasoning and reflection built in. 
<div style="text-align: center; margin: 20px 0;">
    <img src="./media/chat.png" alt="Bag of words" style="width: 100%; max-width: 1200px;">
    <i></i>
</div>

### Create and customize AI instructions and rules
Manage your AI rules and instructions with review process and control
<div style="text-align: center; margin: 20px 0;">
    <img src="./media/instructions.png" alt="Bag of words" style="width: 100%; max-width: 1200px;">
    <i></i>
</div>

### Connect dbt, Tableau, and more for better AI context
Enrich your AI context with dbt models, Tableau data sources, AGENTS.md and your git repo
<div style="text-align: center; margin: 20px 0;">
    <img src="./media/dbt.png" alt="Bag of words" style="width: 100%; max-width: 1200px;">
    <i></i>
</div>

### Save data and queries to the Catalog
Leverage the catalog to store, share, and explore reusable queries and datasets. This feature also improves discoverability and searchability for AI, contributing to smarter AI decisions.
<div style="text-align: center; margin: 20px 0;">
    <img src="./media/catalog.png" alt="Bag of words" style="width: 100%; max-width: 1200px;">
</div>

### Monitor AI and data operations
Full observability into queries, feedback, and context — powering self-learning and high quality AI results
<div style="text-align: center; margin: 20px 0;">
    <img src="./media/monitoring.png" alt="Bag of words" style="width: 100%; max-width: 1200px;">
</div>


## Architecture

Bag of words acts as a **context-aware analytics layer** that connects to any database or service, works with any LLM, and enriches queries with docs, BI models, or code.

The architecture is fully flexible: plug in any data source, any model, and any interface — giving your team maximum freedom of choice, without sacrificing governance or reliability.

<div style="text-align: center; margin: 20px 0;">
    <img src="./media/arch.png" alt="Bag of words" style="width: 100%; max-width: 1200px;">
</div>

## Integrations

### Supported LLM Integrations

Bag of words supports a wide range of LLM providers out of the box. You can bring your own API key for any of the following:

| Provider         | Supported Models / APIs         | Notes                                                                 |
|------------------|---------------------------------|-----------------------------------------------------------------------|
| **OpenAI**       | GPT-5, GPT-4.1, o-models, etc.    | Any OpenAI-compatible endpoint (including self-hosted, vLLM, etc.)    |
| **Azure OpenAI** | GPT-5, GPT-4.1, o-models, etc.            | Azure resource/endpoint support, including model deployment names      |
| **Google Gemini**| Gemini 2.5, Flash versions, etc.    | Requires Google Cloud API key                                         |
| **Anthropic**    | Claude, Sonnet, Haiku    | Just provide the API key          |
| **Any OpenAI-compatible** | vLLM, LM Studio, Ollama, etc. | Just provide the base URL and API key                                 |

> **Tip:** You can configure multiple providers and models, set defaults, and more.

### Data Sources


#### Supported Data Sources

Below is a list of all data sources supported by Bag of words, as defined in the data source registry. Each entry is marked as either a **Database/Warehouse** or a **Service**.

| Title                    | Kind                |
|--------------------------|---------------------|
| PostgreSQL               | Database/Warehouse  |
| Snowflake                | Database/Warehouse  |
| Google BigQuery          | Database/Warehouse  |
| NetSuite                 | Service             |
| MySQL                    | Database/Warehouse  |
| AWS Athena               | Database/Warehouse  |
| MariaDB                  | Database/Warehouse  |
| DuckDB                   | Database/Warehouse  |
| Salesforce               | Service             |
| Microsoft SQL Server     | Database/Warehouse  |
| ClickHouse               | Database/Warehouse  |
| Azure Data Explorer      | Database/Warehouse  |
| AWS Cost Explorer        | Service             |
| Vertica                  | Database/Warehouse  |
| AWS Redshift             | Database/Warehouse  |
| Tableau                  | Service             |
| Presto                   | Database/Warehouse  |
| Apache Pinot             | Database/Warehouse  |
| Oracle DB                | Database/Warehouse  |
| MongoDB                  | Database/Warehouse  |

> **Note:** Some data sources (like NetSuite) may be marked as inactive or beta in the registry. "Service" refers to APIs or SaaS platforms, while "Database/Warehouse" refers to systems that store and query structured data.

## 🔒 Security & Privacy
We take data security and privacy seriously.  

### Telemetry
By default, Bag of words captures basic usage stats of self-hosted instances to a centralized server. The data helps us improve the product.

You can disable by setting in `bow-config.yaml`

```yaml
telemetry
  enabled: false
```

You can also disable the Intercom chat for support

```yaml
intercom
  enabled: false
```

