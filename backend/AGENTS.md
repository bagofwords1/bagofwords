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


### debugging
- when working locally, we use `sqlite` db by default, so it is a great resource to debug
- sqlite3 `backend/db/app.db`

### Locale / i18n

- **Config**: `settings/bow_config.py` defines `I18nConfig` with
  `default_locale`, `enabled_locales` (defaults to `["en", "es", "he"]`),
  and `fallback_locale`. Accessible as `settings.bow_config.i18n.*`.
- **Org override**: stored on `OrganizationSettings.config["locale"]` as a
  plain string (nullable = "no override"). Set via the `update_locale`
  service method and the `PUT /api/organization/locale` route; must be
  in `enabled_locales` or the request is rejected.
- **Dependencies**:
  - `get_current_locale(request)` — unauthed-safe. Reads `X-Locale`
    header, falls back to system default. No DB access.
  - `get_org_locale(request, organization)` — for authed endpoints that
    need org-aware resolution (`X-Locale` → org locale → system default).
  - `_locale_from_org(organization)` — helper used by services when they
    already have an `Organization` object in hand (email dispatch,
    scheduled jobs).
- **Routes**: `GET/PUT /api/organization/locale` (read-any, PUT gated by
  `manage_settings`) and the public `GET /api/config/i18n` used by the
  frontend plugin on boot.
- **Typed errors** (`app/errors/`): raise `AppError.not_found(ErrorCode.X,
  ...)` / `.forbidden` / `.bad_request` etc. instead of `HTTPException`.
  The registered handler returns `{detail, error_code, params,
  status_code}` — the frontend localizes via the `error_code`. Enum
  catalog in `app/errors/codes.py`; new code = new enum entry + a
  matching `errors.<code>` key in every locale catalog.
- **Emails** (`app/services/email_renderer.py` +
  `app/services/email_strings.py`): share/schedule notifications render
  per-locale via shared Jinja templates at
  `app/templates/emails/*.jinja2` that honor `lang` and `dir`. Caller
  passes `locale` from `_locale_from_org(organization)`. Keep strings
  HTML-escaped at the substitution layer — the `description` field in
  `share.html.jinja2` is `| safe`, so never feed it raw user input.
- **LLM prompts** (`app/ai/prompt_language.py`): `build_language_directive`
  injects a "respond in {language}" snippet for **conversational** agents
  only (planner / answer / judge / reporter / suggest_instructions).
  Code/artifact agents (coder, dashboard_designer, excel, doc,
  data_source) stay English so identifiers, SQL, and JSON fields don't
  get translated. Returns empty string for `en` to save tokens.

See `docs/design/i18n.md` for the full design and authoring rules.