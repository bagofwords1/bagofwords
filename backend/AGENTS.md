# AGENTS Guidelines for backend

### Purpose
Concise overview of `@backend/` with emphasis on the `app` library layout and how modules fit together.

### Structure
- **root**
  - `main.py`: FastAPI bootstrap, app factory/lifespan, middleware, and router mounting.
  - `alembic/`: Database migrations.
  - `tests/`: Unit and e2e tests.
  - `db/`: Local SQLite files for dev/testing.
  - `configs/`: Runtime configuration files.

- **app/**
  - `ai/`: Agent orchestration, tools, planners, LLM providers, and prompt logic.
  - `core/`: Cross-cutting concerns (database/session, auth/security, logging, exceptions, utilities).
  - `data_sources/`: Integrations and connectors for external systems and files; ingestion/ETL helpers.
  - `models/`: SQLAlchemy ORM models (persistent domain entities).
  - `schemas/`: Pydantic models (request/response DTOs and validation).
  - `services/`: Business logic, coordination between data sources and models; side-effectful operations.
  - `routes/`: FastAPI routers grouped by domain; thin request handlers that call services.
  - `settings/`: Typed configuration and environment parsing.
  - `streaming/`: Token/response streaming utilities (SSE or similar).
  - `serializers/`: Response shaping/normalization helpers.
  - `utils/`: Generic helpers with no external dependencies.
  - `project_manager.py`: Multi-project orchestration helpers.
  - `websocket_manager.py`: WebSocket connection/session manager.
  - `dependencies.py`: FastAPI dependency providers (DB session, auth, pagination, etc.).

### Data flow (typical request)
`routes/*` → `dependencies` (auth/db) → `services/*` → `models/*` (DB) and/or `data_sources/*` (external) → `schemas/*` (serialize) → HTTP response → optional `streaming/` or `websocket_manager.py` for live updates.

### Module roles and boundaries
- **routes**: Validate/parse inputs, call services, return `schemas`.
- **services**: Encapsulate business logic; never import from `routes`.
- **models**: ORM only; no business logic beyond simple helpers.
- **schemas**: I/O contracts; avoid DB/session awareness.
- **core/settings**: Centralized configuration; avoid circular imports.
- **ai**: Keep provider-specific code and orchestration isolated from web concerns.

### Conventions
- **Imports**: Higher-level modules may depend on lower-level ones, not vice versa (e.g., `routes` → `services` → `models`).
- **Validation**: Use `schemas` for request/response validation; prefer service-level validation for domain rules.
- **Error handling**: Raise domain-specific exceptions; map to HTTP errors at the route layer.
- **Streaming**: Use `streaming/` utilities for token streams; use `websocket_manager.py` for bi-directional updates.

### Adding a feature (quick checklist)
1. Add/extend `models` as needed; create alembic migration.
2. Define `schemas` for inputs/outputs.
3. Implement `services` for business logic.
4. Expose endpoints in `routes` and mount in `main.py` if new group.
5. Wire any `ai` or `data_sources` integrations behind services.
6. Update tests under `backend/tests`.
