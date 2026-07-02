# Sandbox Feedback Loop — Network Directory file connector (ls / find / grep / cp)

Builds and validates a new **`network_dir`** data source: a connection that
points at a directory (a local folder or an already-mounted SMB/NFS share) and
gives the agent filesystem primitives —

| shell analogue | agent tool | capability |
| --- | --- | --- |
| `ls` / `find` | `list_files` | `list_files` |
| `grep -ril` (name + content) | `search_files` | `search_files` |
| `cat` | `read_file` | `read_file` |
| `cp` / `put` | `write_file` | `write_file` (new) |

Use case: *search contracts to find the right file, read one to confirm, then
put related files together in a directory.*

This doc is the runnable feedback loop used to build and confirm the feature in
a fresh cloud sandbox.

---

## What was added

- **`Capability.WRITE_FILE`** + `write_file()` / `awrite_file()` on
  `DataSourceClient` (`backend/app/data_sources/clients/base.py`) — the first
  *mutating* file capability. Read-only sources must not declare it.
- **`NetworkDirClient`** (`backend/app/data_sources/clients/network_dir_client.py`)
  — filesystem-backed client declaring `LIST_FILES + READ_FILE + SEARCH_FILES`
  always and `WRITE_FILE` only when the connection is configured writable. Every
  id resolves to a path **inside `root_path`**; traversal (`..`, absolute
  escapes, symlinks out) is rejected at a single chokepoint (`_resolve`).
- **Registry entry `network_dir`** (`backend/app/schemas/data_source_registry.py`)
  — `data_shape="files"`, `catalog_ownership="shared"`, no-auth credentials
  (`NetworkDirConfig` / `NetworkDirCredentials` in
  `backend/app/schemas/data_sources/configs.py`).
- **`write_file` agent tool** (`backend/app/ai/tools/implementations/write_file.py`)
  — writes generated text, or copies an existing session file (`source_file_id`)
  into the directory. `category="action"`, `requires_capability="write_file"`.
- Wiring: `network_dir` added to `FILE_SOURCE_TYPES`
  (`_file_tool_common.py`) and to `_FILE_METADATA_KEYS` (`list_files.py`);
  `WriteFileInput/Output` schemas in `schemas/file_tools.py`; catalog icon at
  `frontend/public/data_sources_icons/network_dir.png`.

The read tools (`list_files` / `read_file` / `search_files`) needed **no
changes** — they resolve any client that declares the capability, so the new
connector inherits them.

---

## Environment setup (fresh sandbox)

The app targets **Python 3.12**. The sandbox default `python` may be 3.11 — the
uv-managed 3.12 download can 403 behind the proxy, but a system `python3.12` is
present and `uv sync` links a venv against it.

```bash
cd backend
pip install uv
BOW_DATABASE_URL="sqlite:///db/app.db" uv sync --frozen --extra dev --python 3.12

export BOW_DATABASE_URL="sqlite:///db/app.db"
mkdir -p db
.venv/bin/python -m alembic upgrade head   # no new migration — config/creds are generic JSON columns
```

Generate the fixture directory (many CSVs + PNG charts/logos + markdown/text):

```bash
.venv/bin/python scripts/gen_network_dir_fixtures.py /tmp/netdir_demo
# -> contracts/ invoices/ reports/ images/ notes/ README.md  (~118 files)
```

---

## Loop A — App-logic reproduction (no network needed)

Two suites. The unit suite exercises the client + tool in isolation; the e2e
suite drives the **real** stack (tool → `ConnectionService.construct_client` →
`NetworkDirClient` → filesystem) over a seeded org/user/connection/report.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
.venv/bin/python -m pytest \
  tests/unit/test_network_dir_client.py \
  tests/unit/test_write_file_tool.py \
  tests/e2e/test_network_dir_e2e.py -v
```

**Observed (PASS):** 37 tests — list/read/search/write, filename + content
search, CSV→DataFrame, binary→bytes, path-traversal rejected, write-on-read-only
rejected, overwrite guard, extension filter, and the full e2e
`search → read → write` (writable) and `write blocked` (read-only) flows.

Regression check (read tools must be unaffected):

```bash
.venv/bin/python -m pytest tests/unit/test_file_tools.py tests/unit/test_drive_clients.py -q
# -> passes; the shared file-tool layer is unchanged in behavior.
```

Iterate here: edit the client / tool and re-run — the tests pin every invariant.

---

## Loop B — Live LLM confirmation (real Anthropic model)

Confirms the **LLM-facing contract**: given only the real tool metadata
(descriptions + JSON schemas), a model can operate the connector end to end —
search the directory, read a contract, and `write_file` a summary back.

Secrets via **env var only — never commit the key**:

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
export ANTHROPIC_API_KEY=...    # a Haiku-capable key

.venv/bin/python scripts/gen_network_dir_fixtures.py /tmp/netdir_demo
.venv/bin/python scripts/network_dir_live_agent.py /tmp/netdir_demo
```

**Observed (PASS):** the model (Haiku 4.5) issues 3 tool calls —
`search_files("contract")` → `read_file(<a contract csv>)` →
`write_file(folder="_related", filename="index.md", overwrite=true, ...)` — and
the loop verifies `/tmp/netdir_demo/_related/index.md` exists with the contract
ids the model chose. Prints `LIVE E2E: PASS`.

---

## What this proves

- **App:** a directory becomes a first-class, agent-attachable file source with
  no new migration and no changes to the existing read tools; writes are a
  separately-gated, per-instance capability so a read-only mount can never be
  mutated.
- **Safety:** every read and write is confined to `root_path`; traversal and
  read-only violations fail closed (tested).
- **Live:** a real model can drive the tools purely from their schemas to
  accomplish the "find the file, put related files together" workflow.
