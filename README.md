<div>
  <img src="./media/logo-128.png" style="width:64px;" />
</div>

# Bag of words — deploy an AI Analyst in minutes
Connect any LLM to any data source with centralized context management (instructions, dbt, code, docs, BI metadata) and full observability and governance.

Let users run analysis, build beautiful dashboards, or schedule reports — all executed through an agentic analytics-oriented loop.

### [Docs: Deploy in less than 2 minutes](https://docs.bagofwords.com)
[![Website](https://img.shields.io/badge/Website-bagofwords.com-blue)](https://bagofwords.com)
[![Docs](https://img.shields.io/badge/Docs-Documentation-blue)](https://docs.bagofwords.com)
[![Docker](https://img.shields.io/badge/Docker-Hub-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/bagofwords/bagofwords)
[![e2e tests](https://github.com/bagofwords1/bagofwords/actions/workflows/e2e-tests.yml/badge.svg)](https://github.com/bagofwords1/bagofwords/actions/workflows/e2e-tests.yml)
---

[![Demo](./media/hero3.png)](https://bagofwords.com/demos/hero4.mp4)


Bag of words is an open-source AI data layer — connect any LLM to any data source with centralized context management, trust, observability, and control.


* **Chat with any data source** 

  Ask questions in web, Slack, or else. Create charts, tables, and full beautiful reports/dashboards by chatting with your data—powered by an agentic loop for tool use, reflection, and reasoning.

*  **Context-aware & customizable** 
  
   Define terms, tables, KPIs, rules and instructions. Ingest from dbt, Tableau, code, AGENTS.md, and have AI continiously maintain and monitor.

* **Any LLM, any data**

   Connect multiple data sources: Snowflake, BigQuery, Azure Data Explorer, Redshift, Postgres, dbt, Tableau, and more — then pair with the LLM of your choice (OpenAI, Anthropic, or local models). Swap models/data sources without breaking workflows.

*  **Transparency, trust & deployment**

   Track every AI decision, trace, and feedback. Analyze quality and usage in the console. Deploy fully in your VPC with Docker/Compose, VMs, or Kubernetes. Enterprise-ready with RBAC, SSO (OIDC), audit logs, SMTP.

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

#### Custom deployments
For more advanced deployments, see the [docs](https://docs.bagofwords.com).

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

