# OpenText Documentum connector — reproduce → fix → verify

Adds `documentum`: a read-only file connector over **Documentum REST Services**
(`/dctm-rest`) with **OTDS** (OpenText Directory Services) for service and
per-user identity. Built and verified against a spec-shaped simulator, then
driven end-to-end through the real UI with a real LLM (Claude 4.5 Haiku).

No real Documentum Content Server was available (licensed OpenText binaries;
see `docs/documentum-lab-access.md`), so the REST/OTDS boundary is simulated in
every loop below. Everything above that boundary — registry, forms, connection
and credential services, per-user overlay, file tools, indexing job, chat — is
the real application.

## What shipped

| File | Why |
|---|---|
| `backend/app/data_sources/clients/documentum_client.py` | The client — hypermedia feeds, folder walk, DQL, formats, downloads, 4 auth methods |
| `backend/app/schemas/data_sources/configs.py` | `DocumentumConfig` + `userpass` / `otds_client` / `otds_impersonation` credential schemas (these *are* the form) |
| `backend/app/schemas/data_source_registry.py` | `REGISTRY["documentum"]`, `category="files"`, `data_shape="files"`, enterprise, beta |
| `backend/app/services/connection_service.py` | Per-user default modes for Documentum; generic per-user credential save for manual-identity file connectors; overlay on the delegated resolve path |
| `backend/app/services/connection_oauth_service.py` | OTDS authorization-code parameters (`/otdsws/oauth2/auth|token`) |
| `backend/app/ai/…/_file_tool_common.py`, `list_files.py`, `search_files.py`, `connection_catalog_common.py`, `schema_context_builder.py` | Documentum joins the file-source set; live-only listing; friendly 401/403 errors |
| `frontend/components/UserDataSourceCredentialsModal.vue` + file panels/icons | Per-user save goes to the connection endpoint; Documentum shows in Files/Knowledge/Scope UIs |
| `frontend/public/data_sources_icons/documentum.png` | Brand icon (default resolver picks `<type>.png`) |
| `tools/documentum/mock_documentum_server.py`, `docker-compose.yaml` | Stdlib simulator of `/dctm-rest` + `/otdsws/oauth2/token` with ACL-trimmed data |
| `tools/agent/stub_llm_documentum.py` | OpenAI-compatible scripted planner (search → read → answer) for LLM-free UI runs |
| `backend/tests/unit/test_documentum_client.py`, `backend/tests/e2e/test_documentum.py` | 31 client tests + 3 API lifecycle tests against the simulator |
| `docs/documentum.md` | User-facing setup and auth documentation |

No new dependency: `requests`, `pypdf`, `python-docx`, `openpyxl` were already pinned.

## Design decisions

**Identity flows through OTDS, not Documentum.** Since 23.4 Documentum REST
delegates authentication to OTDS, which fronts LDAP/AD and Entra. The connector
therefore offers: repository username/password (`userpass`, system or per-user),
an OTDS OAuth client (`otds_client`, system), **OTDS impersonation**
(`otds_impersonation`, per-user overlay: the system client exchanges a token for
the member's Documentum login via RFC 8693 token exchange), and OTDS
authorization-code sign-in (`oauth`, per-user). Entra on-behalf-of does not
apply because Documentum does not accept Entra tokens directly.

**Stable ids, root-relative paths.** Documents are addressed by `r_object_id`.
The agent sees paths relative to the configured root folder; a document filed in
several folders appears once. Reads re-check root + globs + object types even
when given a raw id, so a member cannot read outside scope by guessing ids.

**Content links are validated.** Documentum may hand out ACS (Accelerated
Content Services) URLs on a different host. The client requests
`media-url-policy=local` and refuses any enclosure that is not under the
configured REST URL, so credentials never go to an unvetted host.

**Live listing.** `cheap_live_listing = True`: `list_files`/`search_files` hit
the repository rather than the indexed catalog, so ACL trimming is always the
live user's, and search merges the repository full-text index with a DQL name /
title match so freshly filed documents are found.

## What the feedback loop found

None of these were visible before running the stack end to end.

1. **Folder-walk DQL for the root.** `FOLDER('/', DESCEND)` returned nothing in
   the simulator (and is not valid DQL). Root scope now enumerates cabinets and
   walks them; DQL is used only for non-root folders.
2. **Indexing is a background job.** The first API lifecycle test asserted the
   catalog right after create and saw 0 tables. The test now polls
   `GET /api/connections/{id}/indexing`. The simulator also cost ~40 ms per
   request due to Nagle/delayed-ACK on small writes; buffered writes and
   `disable_nagle_algorithm` brought the 12-file walk from 0.84 s to 0.03 s.
3. **Credential variant follows `config.auth_type`.** Creating a user-required
   connection with OTDS client credentials returned 422 until the request set
   `auth_type="otds_client"` in the config, matching how other connectors select
   a variant.
4. **Per-user save wrote to the wrong scope.** The generic per-user modal stored
   data-source-scoped rows while the files endpoint resolves connection-scoped
   rows; a member stayed `connect_required` after Save. The connection endpoint
   previously used only for SharePoint Server now accepts any user-scoped,
   non-OAuth variant for manual-identity file connectors, validated with the
   variant's schema (400 for the wrong mode, 422 for bad fields).
5. **Overlay missing on the delegated path.** With `oauth` among the allowed
   modes the connection takes the delegated resolve branch, which returned the
   raw per-user row (`documentum_login` only) and reported the OTDS fields as
   missing. Non-OAuth rows on that branch now go through
   `overlay_system_credentials`, so impersonation gets the system OTDS client.
6. **Big-file limit fixture.** The 1 MB download-limit test needed a fixture
   larger than 1 MB; the simulator serves a 1.6 MB binary.

## Loop A — deterministic, no third-party credentials

```bash
cd backend
uv sync --extra dev
env -u REQUESTS_CA_BUNDLE TESTING=true BOW_DATABASE_URL='sqlite:///db/app.db' uv run pytest \
  tests/unit/test_documentum_client.py \
  tests/e2e/test_documentum.py \
  tests/e2e/test_sharepoint_onprem.py \
  tests/unit/test_default_user_auth_modes.py \
  tests/unit/test_connection_oauth.py \
  tests/integrations/ds_clients.py -k "not (ds_clients and not documentum)" \
  --db=sqlite -q --disable-warnings
```

The unit suite starts `tools/documentum/mock_documentum_server.py` in-process
(a real HTTP server on a free port, not a patched client). It covers:

- ACL trimming per identity: dmadmin sees all 12 documents; alice 12 (Finance +
  HR); bob 10 (Finance, no HR); carol 4 (public only). Search and DQL trim too.
- Root scoping (`/Finance`, `/HR/Policies`), multi-filed `board_summary.txt`
  listed once, include globs, object types (`bow_invoice` subtype), scope
  escapes on direct ids and paths → `GlobScopeError`, 403 → `DocumentumHTTPError`.
- Formats pipeline (`msw12`, `excel12book`, `pdf`, `crtext` → MIME/ext),
  CSV/XLSX DataFrames, DOCX/PDF text, download limit (rejects before fetching),
  off-REST enclosure refused, `next`-link pagination with small pages, catalog
  cap, `index_mode="none"` makes no network calls, `test_connection`.
- Auth: basic, OTDS client credentials with one refresh on 401, OTDS
  impersonation for alice/bob (and the `noimp` client refused), delegated
  bearer token, invalid configs (missing OTDS fields listed by name, `http://`
  without `allow_http`, userinfo/query in URL).

The API suite creates a Documentum connection through the real routes, waits for
the indexing job, checks the catalog, then verifies a user-required connection:
member is `connect_required`, saves an impersonation login through
`/connections/{id}/my-credentials`, `test-my-credentials` succeeds, and the
files endpoint reaches the simulator **as bob** (asserted via the simulator's
request log).

Final run of the command above: **118 passed, 1 skipped** (31 Documentum
client, 3 Documentum API, 4 SharePoint Server API, the shared auth-mode and
OAuth parameter suites; the skip is the live Documentum integration case, which
needs `integrations.json`). The generic data-source and connection API suites
(`tests/e2e/test_data_source.py`, `tests/e2e/test_connection.py`) were run
separately and stayed green. `REQUESTS_CA_BUNDLE` is unset only because the remote
sandbox pre-sets it, which breaks unrelated SharePoint assertions on a clean
tree as well.

## Loop B — real UI, simulator backend, real LLM

Fresh sandbox: backend on 8000 (SQLite, pinned `BOW_ENCRYPTION_KEY`,
`BOW_CHROMIUM_EXECUTABLE`), Nuxt dev on 3000, simulator on 8081
(`DCTM_MOCK_PORT=8081 python tools/documentum/mock_documentum_server.py`), and
either a real Anthropic provider or the scripted planner on 9099
(`python tools/agent/stub_llm_documentum.py`, added as an OpenAI-compatible
provider with base URL `http://127.0.0.1:9099/v1`).

Admin flow (Playwright against the real UI, all screenshots in
`media/pr/documentum-connector/`):

1. Catalog shows the **OpenText Documentum** tile with the brand icon
   (`01-catalog-documentum-tile.png`).
2. The schema-generated form: REST URL, repository `bow_demo`, root `/`,
   `allow_http` toggle for the lab, username/password alice
   (`02`, `03`). **Test connection** → "Connected successfully. Found 12 files"
   (`04`); create → "Discovered 12 files" (`05`); attach to agent
   *Documentum Finance* (`06`, `07`).
3. Chat with Claude 4.5 Haiku, scoped to the agent (`haiku-07b`):
   - "total revenue by region across Q1 and Q2" → table from the two CSVs;
     tool trail in `tool_executions`: `search_files`, `list_files`,
     `read_file` ×3, `write_csv` (`haiku-08`).
   - "summarize the travel policy … per-trip cap" → €500 cap, receipts, no
     business class, read from the DOCX (`haiku-09`).
   - "How much is invoice INV-1001 and who is the vendor?" → Haiku answered
     from memory that it had no invoice data **without calling search_files**
     (`haiku-10`). Rephrased as "Search the Documentum repository for invoice
     INV-1001 …" it searched, read the PDF and answered Acme / 12,400
     (`haiku-10b`). The scripted planner run (`stub-10`) and the unit tests
     prove the same PDF path deterministically; the first miss is an LLM
     planning choice, not a connector fault.

Member flow (second BOW user `bob.member`, invited; connection *Documentum HR &
Finance (per-user)* created with `auth_policy=user_required` and OTDS client
credentials):

4. Member sees the agent as **connect required** (`11`); the Connect modal
   offers *OTDS impersonation* and *OTDS sign-in* (`12`); enters Documentum
   login `bob` (`13`); Test → success (`14`); Save → connected (`15`).
5. Member chat: "list the folders and files" shows Finance, Engineering and
   Temp but **not HR** (`member-16`); "what are the salary bands?" → denied /
   not visible (`member-17`); "Q1 North revenue" → $184,000 (`member-18`).
   The simulator's request log shows every call carrying bob's impersonated
   token, and the HR salary document is never returned.

HTTP-layer verification: every `/api/` response during the runs was logged to a
local `http.jsonl` (kept out of the repo); no 5xx occurred in the final passes.

## Boundaries / remaining acceptance

- **No real Documentum was contacted.** The simulator follows the public REST
  Services and OTDS OAuth 2.0 documentation (link rels, feed paging, DQL
  subset, `media-url-policy`, token exchange). Acceptance against a real
  Content Server + OTDS remains: run
  `TESTING=true uv run pytest tests/integrations/ds_clients.py -k documentum`
  with `integrations.json` pointing at the instance (see the comment in
  `tests/integrations/ds_clients.py`), or point the same UI flow at it.
- xPlore full-text semantics, ACS/BOCS distributed content, custom SSO
  filters, and repositories with tens of thousands of documents are not
  exercised. Non-root scopes are DQL-driven and tested only on the simulator.
- OTDS authorization-code sign-in is verified at the parameter level (URLs,
  client, scopes); the browser round trip needs a real OTDS.
- Frontend changes are Vue SFCs already rendered by the dev server in Loop B;
  no production build or Docker image build is claimed.
