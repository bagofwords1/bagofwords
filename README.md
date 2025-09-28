<div>
  <img src="./media/logo-128.png" style="width:64px;" />
</div>

# Bag of words
Deploy an agentic AI data tool that can **chat with any data**.  With full observability, deep customizability, and secure self-hosting.

<div style="text-align: center; margin: 20px 0;">
    <img src="./media/homev2.png" alt="Bag of words" style="width: 100%; max-width: 1200px;">
</div>

[![Website](https://img.shields.io/badge/Website-bagofwords.com-blue)](https://bagofwords.com)
[![Docs](https://img.shields.io/badge/Docs-Documentation-blue)](https://docs.bagofwords.com)
[![Docker](https://img.shields.io/badge/Docker-Hub-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/bagofwords/bagofwords)
[![e2e tests](https://github.com/bagofwords1/bagofwords/actions/workflows/e2e-tests.yml/badge.svg)](https://github.com/bagofwords1/bagofwords/actions/workflows/e2e-tests.yml)
---

Bag of words is an open-source AI platform that helps data teams deploy and manage chat-with-your-data agents in a controlled, reliable, and self-learning environment.

* **Chat:** Create charts, tables, and full dashboards by chatting with your data—powered by an agentic loop for tool use, reflection, and reasoning.

*  **Context Management & Self-Learning:** Define terms, KPIs, rules and instructions. Load from external knowledge stores or let AI autogenerate based from usage patterns.

*  **Observability**: Log and analyze SQL/python queries, AI decisions, traces, user feedback, latency, and instructions; analyze quality and usage in the console.

*  **Data Sources**: Snowflake, BigQuery, Postgres, and more. Enrich context with `Tableau`, `dbt`, `LookML`, `AGENTS.md`, docs, and code.

*  **LLM Integration**: Bring your own API key (OpenAI, Anthropic, or any OpenAI-compatible API).

*  **Governance & Integrations**: Users and orgs, RBAC, audit logs, SSO (OIDC), SMTP.

*  **Deployment**: Self-host in your VPC via VMs, Docker/Compose, or Kubernetes.

## Quick Start

### Docker (Recommended)
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

## Architecture

Bag of words acts as a **context-aware analytics layer** that connects to any database or service, works with any LLM, and enriches queries with docs, BI models, or code.

The architecture is fully flexible: plug in any data source, any model, and any interface — giving your team maximum freedom of choice, without sacrificing governance or reliability.

<div style="text-align: center; margin: 20px 0;">
    <img src="./media/arch.png" alt="Bag of words" style="width: 100%; max-width: 1200px;">
</div>

---