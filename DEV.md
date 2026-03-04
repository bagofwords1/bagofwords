# Local Development

## Prerequisites

- **Python 3.12+** — [python.org/downloads](https://www.python.org/downloads/)
- **Node.js 18+** — [nodejs.org](https://nodejs.org/) (recommended: install via [nvm](https://github.com/nvm-sh/nvm))
- **Yarn 1.22+** — Install globally after Node.js: `npm install -g yarn`

### macOS Quick Install

```bash
# Install Homebrew if needed
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install Python and Node.js
brew install python@3.12 node

# Install Yarn globally
npm install -g yarn
```

---

## Backend Setup

```bash
cd backend

# Create and activate Python virtual environment
python3 -m venv venv
source venv/bin/activate  # macOS/Linux
# venv\Scripts\activate   # Windows

# Install dependencies
pip install -r requirements_versioned.txt

# Run database migrations (creates SQLite DB in development)
alembic upgrade head

# Start the backend server
python main.py
```

The API server runs at **http://localhost:8000**. OpenAPI docs at **http://localhost:8000/docs**.

> **Important:** Always activate the virtual environment (`source venv/bin/activate`) before running `python main.py` or any backend commands. Without it, Python won't find the installed packages.

---

## Frontend Setup

```bash
cd frontend

# Install dependencies
yarn install

# Start the dev server
yarn dev
```

The frontend runs at **http://localhost:3000** and proxies `/api/*` requests to the backend at `:8000`.

> **If `yarn` is not found:** Run `npm install -g yarn` first, then retry.

---

## Running Both Together

Open two terminal tabs:

**Tab 1 — Backend:**
```bash
cd backend
source venv/bin/activate
python main.py
```

**Tab 2 — Frontend:**
```bash
cd frontend
yarn dev
```

Then open **http://localhost:3000** in your browser.

---

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `MC_DATABASE_URL` | Database connection string | SQLite (auto-created) |
| `MC_ENCRYPTION_KEY` | Fernet key for credential encryption | Auto-generated in dev |
| `ENVIRONMENT` | `development`, `staging`, or `production` | `development` |

Generate an encryption key:
```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## Running Tests

```bash
cd backend
source venv/bin/activate

# End-to-end tests (SQLite)
pytest -s -m e2e --db=sqlite

# Unit tests
pytest -s -m unit --db=sqlite

# Single test file
pytest -s -m e2e --db=sqlite tests/e2e/test_report.py

# Single test
pytest -s -m e2e --db=sqlite tests/e2e/test_report.py::test_name

# AI agent tests (requires OPENAI_API_KEY_TEST env var)
pytest -s -m ai --db=sqlite
```

---

## Database Migrations

```bash
cd backend
source venv/bin/activate

# Create a new migration
alembic revision --autogenerate -m "description of change"

# Apply migrations
alembic upgrade head

# Rollback one migration
alembic downgrade -1
```

---

## Docker (Production)

```bash
# Production (Postgres + Caddy SSL)
docker compose up -d

# Development (no SSL)
docker compose -f docker-compose.dev.yaml up -d
```

---

## Troubleshooting

### `ModuleNotFoundError: No module named '...'`
You forgot to activate the virtual environment. Run `source venv/bin/activate` in the `backend/` directory first.

### `zsh: command not found: yarn`
Install Yarn globally: `npm install -g yarn`

### `zsh: command not found: python3`
Install Python 3.12+ via Homebrew (`brew install python@3.12`) or from [python.org](https://www.python.org/downloads/).

### Frontend shows "Network Error" or blank page
Make sure the backend is running on port 8000. The frontend proxies API calls to it.

---

## License

AGPL-3.0 — Forked from [Bag of Words](https://github.com/bagofwords1/bagofwords).
