# Bag of words
> Deploy chat-with-your-data AI agents in minutes

[![Website](https://img.shields.io/badge/Website-bagofwords.com-blue)](https://bagofwords.com)
[![Docs](https://img.shields.io/badge/Docs-Documentation-blue)](https://docs.bagofwords.com)
[![Docker](https://img.shields.io/badge/Docker-Hub-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/bagofwords/bagofwords)
[![e2e tests](https://github.com/bagofwords1/bagofwords/actions/workflows/e2e-tests.yml/badge.svg)](https://github.com/bagofwords1/bagofwords/actions/workflows/e2e-tests.yml)
---

Bag of words is an open-source AI platform that helps data teams deploy and manage chat-with-your-data agents in a controlled, reliable, and self-learning environment.


* **Chat**: Create charts, tables, and full dashboards by chatting with your data—powered by an agentic loop for tool use, reflection, and reasoning.
* **Observability**: Capture AI decisions, traces, user feedback, latency; analyze quality and usage in the console.
* **Self-learning**: Automatically improve prompts, instructions, and data mappings from feedback and usage patterns.
* **Data sources**: Snowflake, BigQuery, Postgres, and more. Enrich context with `dbt`, `LookML`, docs, and code repos.
* **LLM integration**: Bring your own API key (OpenAI, Azure OpenAI, Anthropic Claude, or any OpenAI-compatible API).
* **Governance & integrations**: Users and orgs, RBAC, audit logs, SSO (Google), SMTP.
***Deployment**: Self-host in your VPC via VMs, Docker/Compose, or Kubernetes.

Additional integrations to offer an AI Analyst in Slack, Excel, Google Sheets, and more. Get started in minutes, scale to org-wide analytics

<div style="text-align: center; margin: 40px 0;">
    <img src="./media/home.png" alt="Bag of words" style="width: 100%; max-width: 1200px;">
</div>

<div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin: 40px 0;">
    <img src="./media/dashboard-split.png" alt="Dashboard Split" style="width: 30%;">
    <img src="./media/product-dashboard.png" alt="Product Dashboard" style="width: 30%;">
    <img src="./media/revenue.png" alt="Revenue" style="width: 30%;">
    <img src="./media/churn-list.png" alt="Churn List" style="width: 30%;">
    <img src="./media/cloud-spend.png" alt="Cloud Spend" style="width: 30%;">
</div>



## Quick Start

### Docker (Recommended)
```bash
# Run with SQLite (default)
docker run -p 3000:3000 bagofwords/bagofwords
```

#### Run with PostgreSQL
```bash
docker run -p 3000:3000 \
  -e BOW_DATABASE_URL=postgresql://user:password@localhost:5432/dbname \
  bagofwords/bagofwords
```

#### Custom deployments
For more advanced deployments, see the [docs](https://docs.bagofwords.com).

---
---

### Local Development

#### Prerequisites
- Python 3.12+
- Node.js 18+
- Yarn

#### Backend Setup
```bash
# Setup Python environment
cd backend
python3 -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows
pip install -r requirements_versioned.txt

# Run migrations
alembic upgrade head

# Start server
python main.py  # Available at http://localhost:8000
```

#### Frontend Setup
```bash
cd frontend
yarn install
yarn dev      # Regular mode
```

- OpenAPI docs: http://localhost:8000/docs

## Links

- Website: https://bagofwords.com
- Docs: https://docs.bagofwords.com

## License
AGPL-3.0