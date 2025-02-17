# Bag of words
> Build and share smart data apps using AI

[![GitHub](https://img.shields.io/badge/GitHub-Repository-181717?logo=github)](https://github.com/bagofwords1/bagofwords)
[![Docker](https://img.shields.io/badge/Docker-Hub-2496ED?logo=docker&logoColor=white)](https://hub.docker.com/r/bagofwords/bagofwords)
[![Docs](https://img.shields.io/badge/Docs-Documentation-blue)](https://docs.bagofwords.com)
---

Bag of words enables users to create comprehensive dashboards with a single prompt and refine them iteratively. It integrates seamlessly with various data sources, including databases, APIs, and business systems, allowing for effective data utilization.

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

**Key Features:**

	• Data Source Integration: Connect to databases, APIs, files, and business systems.

	• Natural Language Queries: Formulate complex queries using natural language.

	• Dashboard Management: Schedule and share beautiful dashboards effortlessly.

	• LLM Compatibility: Use your preferred LLM (OpenAI, Anthropic, etc.).

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