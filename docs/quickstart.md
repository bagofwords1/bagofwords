# Quickstart

Get MetricChat running in under 5 minutes.

## Install

```bash
docker run --pull always -d -p 3000:3000 walrusquant/metricchat
```

Uses SQLite by default. For PostgreSQL:

```bash
docker run --pull always -d -p 3000:3000 \
  -e MC_DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/dbname \
  walrusquant/metricchat
```

Open [http://localhost:3000](http://localhost:3000) to begin setup.

## Onboarding

The setup wizard walks you through 6 steps:

1. **Welcome** — Launches automatically on first run
2. **Configure LLM** — Connect OpenAI, Anthropic, Google, Azure, or Ollama with your API key
3. **Connect a Data Source** — PostgreSQL, Snowflake, BigQuery, DuckDB, Salesforce, and more. You can also upload CSV/Excel files to create instant DuckDB sources.
4. **Select Tables** — Choose which tables the AI can access
5. **Add Context** — Optionally add business rules, KPI definitions, or connect a Git repo for dbt/LookML/markdown docs
6. **Start Asking Questions** — Type a question and get answers with charts and tables

## Next Steps

Full documentation at [metricchat.com/docs](https://www.metricchat.com/docs).
