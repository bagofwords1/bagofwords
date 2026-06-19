
### Local Development

#### Prerequisites
- Python 3.12+
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (package manager)
- Node.js 22+
- Yarn

#### Backend Setup
```bash
cd backend
uv sync --extra dev      # creates .venv and installs all deps

# Run migrations
uv run alembic upgrade head

# Start server
uv run python main.py    # Available at http://localhost:8000
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