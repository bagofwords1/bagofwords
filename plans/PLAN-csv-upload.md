# Plan: CSV Upload → Instant Queryable Data Source

## Goal
When a user uploads a CSV or Excel file, they should be able to immediately query it with the AI agent. No manual DuckDB configuration. Upload → Query in two clicks.

## What Already Exists

### Backend (ready to use)
- `backend/app/routes/file.py` — Full file upload API (`POST /files`, `GET /files`, etc.)
- `backend/app/services/file_service.py` — File storage + auto-preview generation (columns, dtypes, shape, first 50 rows)
- `backend/app/services/file_preview.py` — CSV/Excel/PDF parsing with `generate_file_preview()`
- `backend/app/models/file.py` — File model with preview JSON, org/user associations
- `backend/app/data_sources/clients/duckdb_client.py` — DuckDB client that auto-creates SQL views from CSV/Parquet via `read_csv_auto()`
- `backend/app/schemas/data_source_registry.py` — DuckDB registered with no-auth option
- `backend/tests/e2e/test_file.py` — Existing tests for upload, preview, removal

### Frontend (ready to use)
- `frontend/components/FileUploadComponent.vue` — Drag-and-drop multi-file upload with progress
- `frontend/pages/files/index.vue` — Files management page

### DuckDB Key Detail
The DuckDB client already does this:
```python
con.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_csv_auto({normalized_path})")
```
So pointing DuckDB at an uploaded CSV file automatically makes it queryable with full schema discovery.

---

## What Needs to Be Built

### Step 1: Backend — Auto-create data source from uploaded file

**New endpoint:** `POST /api/files/{file_id}/create_data_source`

Location: `backend/app/routes/file.py` (add to existing router)

Logic:
1. Look up the File by ID (verify org ownership)
2. Check file type is CSV or Excel (reject PDF etc.)
3. Get the file's storage path from `file.path` (e.g., `uploads/files/{uuid}_{filename}`)
4. Create a new DataSource record:
   - `name` = filename without extension (e.g., "sports_data")
   - `type` = "duckdb"
   - `connection` config = `{ "uri": "/app/backend/{file.path}" }` (absolute path inside container)
   - `credentials` = `{ "auth_type": "none" }` (local file, no auth needed)
5. Create a new Connection record linked to the DataSource
6. Trigger schema discovery (DuckDB client's `get_tables()` / `get_table_schema()`)
7. Return the new DataSource ID

**Service method:** Add `create_data_source_from_file()` to `file_service.py` or create a new helper.

**Key files to reference:**
- `backend/app/services/data_source_service.py` — See how data sources are created normally
- `backend/app/services/connection_service.py` — See how connections are created
- `backend/app/schemas/data_source_registry.py` — DuckDB config schema (`DuckDBConfig`)
- `backend/app/data_sources/clients/duckdb_client.py` — How DuckDB resolves file paths

### Step 2: Backend — Handle Excel multi-sheet

For Excel files (.xlsx), DuckDB can't directly `read_csv_auto`. Options:
- **Option A (simpler):** Convert Excel to CSV on upload using pandas/openpyxl (already in requirements), save as CSV, then use DuckDB on the CSV
- **Option B (cleaner):** Use DuckDB's `read_xlsx()` extension or pandas to load into a DuckDB table directly

Recommendation: Option A. On upload of .xlsx, auto-convert each sheet to a separate CSV. Store CSVs alongside original. Each sheet becomes a table in DuckDB.

### Step 3: Frontend — "Query This File" button

**Location:** `frontend/components/FileUploadComponent.vue` and `frontend/pages/files/index.vue`

Changes:
1. After successful upload, show a "Query this data" button next to each CSV/Excel file
2. Button calls `POST /api/files/{file_id}/create_data_source`
3. On success, redirect to home page (`/`) with the new data source pre-selected
4. User is immediately in the chat with their data ready to query

### Step 4: Frontend — Upload from home page

**Location:** `frontend/pages/index.vue`

Add an upload option alongside the existing "Connect data source" flow:
- If user has no data sources, show both options:
  - "Connect a database" (existing flow)
  - "Upload a file" (new — opens FileUploadComponent, triggers auto data source creation, lands in chat)
- Could also add a small "Upload CSV" button near the data source selector for users who already have connections but want to add a quick file

### Step 5: Onboarding flow update

**Location:** `frontend/components/onboarding/OnboardingView.vue`

In the "Connect data source" onboarding step, add a secondary option:
- "Don't have a database? Upload a CSV or Excel file instead"
- Routes to file upload → auto data source creation → continues onboarding

---

## File Path Mapping (Container vs Local Dev)

Important: File paths differ between Docker and local dev.

- **Docker:** Files stored at `/app/backend/uploads/files/...`, DuckDB URI needs absolute path
- **Local dev:** Files stored at `backend/uploads/files/...`, relative to working directory

Solution: Use `os.path.abspath(file.path)` when constructing the DuckDB URI. This works in both environments.

---

## Testing

1. **Backend test:** Upload CSV via API → call create_data_source → verify DuckDB data source created → verify schema discovery returns correct tables/columns
2. **Backend test:** Upload Excel → verify multi-sheet handling → verify each sheet becomes a table
3. **Frontend test:** Upload CSV → click "Query" → verify redirect to chat → verify data source is selected
4. **E2E test:** Upload CSV → ask a question about the data → verify AI generates correct SQL and returns results

Test file: `backend/tests/e2e/test_file_to_datasource.py` (new)

---

## Edge Cases to Handle

- **Duplicate filenames:** Append UUID or timestamp to data source name
- **Large files:** DuckDB handles large CSVs well (streams, doesn't load all into memory). May want to warn on files > 500MB.
- **Bad CSV formatting:** `read_csv_auto` is pretty resilient but catch errors and return useful messages
- **File deletion:** When a file is deleted, should the DuckDB data source also be deleted? Probably yes, with a confirmation.
- **Re-upload:** If user uploads a new version of the same file, update the existing data source rather than creating a duplicate

---

## Estimated Effort

| Task | Estimate |
|------|----------|
| Step 1: Backend endpoint + service | 2-3 hours |
| Step 2: Excel multi-sheet handling | 1-2 hours |
| Step 3: Frontend "Query" button | 1-2 hours |
| Step 4: Upload from home page | 1-2 hours |
| Step 5: Onboarding update | 1 hour |
| Testing | 1-2 hours |
| **Total** | **~8-12 hours (one full day)** |

---

## Future Enhancements (not for tomorrow)

- **Google Sheets connector:** Sync on schedule, auto-refresh DuckDB views
- **Drag-and-drop onto chat:** Drop a CSV directly into the chat input, auto-creates data source inline
- **Multiple file joins:** Upload multiple CSVs, AI auto-detects relationships and joins them
- **S3/GCS upload:** For larger datasets, upload to object storage and query via DuckDB's S3 support
- **Managed Postgres migration:** "Promote" a DuckDB file data source to a proper Postgres table for better performance and persistence (this is the bridge to the managed database play)
