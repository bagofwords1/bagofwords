# Feedback Loop — SharePoint Server HTTP 404 on a SharePoint 2010 farm

The existing connector called modern REST endpoints on a reported SharePoint
2010 deployment. This change adds an explicit legacy protocol mode while keeping
the connector type, file metadata contract, and Windows authentication. The
screenshot alone cannot prove whether the customer's site/library path is also
wrong. The API mismatch is reproduced independently; real farm verification will
be run on-prem by the deployment owner.

## Root cause (validated)

At base commit `caf4c58da`,
`backend/app/data_sources/clients/sharepoint_onprem_client.py:92` constructs the
`/_api/` base unconditionally. Connection checking and file operations use modern
REST/ResourcePath calls. A simulated 2010 server that exposes the documented SOAP
services and returns 404 for modern REST reproduces the failed connection.

The new `api_mode` defaults to `modern` for backward compatibility; `2010` routes
through `backend/app/data_sources/clients/_sharepoint_2010.py`. It makes only four
read operations, reusing the existing thread-local NTLM/Kerberos session. No
automatic fallback, new authentication, Graph call, or source mutation is added.

## Evidence and source comparison

Reviewed Microsoft protocol specifications and SharePlum pinned at
[`55d860b99cccc4bb86437d48df9b0e2d3a974769`](https://github.com/jasonrollins/shareplum/tree/55d860b99cccc4bb86437d48df9b0e2d3a974769).
The Python project is corroborating implementation evidence, not an installed
dependency or a substitute for the file-transfer protocol specification.

| Contract | Evidence | Implementation decision |
|---|---|---|
| 2010 uses legacy SOAP | SharePlum [`site.py`](https://github.com/jasonrollins/shareplum/blob/55d860b99cccc4bb86437d48df9b0e2d3a974769/shareplum/site.py) maps `Version.v2010` to `_Site2007` and uses the caller's session auth | Explicit selector; same auth session |
| SOAP envelope, operation namespace and action | SharePlum [`soap.py`](https://github.com/jasonrollins/shareplum/blob/55d860b99cccc4bb86437d48df9b0e2d3a974769/shareplum/soap.py) and [`list.py`](https://github.com/jasonrollins/shareplum/blob/55d860b99cccc4bb86437d48df9b0e2d3a974769/shareplum/list.py) | SOAP 1.1, UTF-8, qualified operation/parameters, XML-built CAML |
| Enumerating document libraries | Microsoft [Lists.GetListCollection](https://learn.microsoft.com/en-us/previous-versions/office/developer/sharepoint-2010/bb263778%28v%3Doffice.14%29), [Lists.GetList](https://learn.microsoft.com/en-us/previous-versions/office/developer/sharepoint-2010/bb249862%28v%3Doffice.14%29) | Lists.asmx collection, visible template 101, GetList root-folder fallback; title or GUID |
| File rows and lookups | Microsoft [GetListItemsResponse](https://learn.microsoft.com/en-us/openspecs/sharepoint_protocols/ms-listsws/2394ffe9-21cb-4f31-a276-ab7b038c0e9f) | Namespace-aware row lookup; explicit fields; `ows_` and numeric `id;#` handling |
| Folder scope and continuation | Microsoft [CamlQueryOptions](https://learn.microsoft.com/en-us/openspecs/sharepoint_protocols/ms-listsws/fa650bde-c79b-4a2a-bb5d-c43e74166322), [Scope](https://learn.microsoft.com/en-us/openspecs/sharepoint_protocols/ms-wsscaml/1d32f90d-4fd7-41fa-8ace-147956c50657) | Immediate folder pages, explicit ID ordering, opaque paging token, optional recursive walk; loop/bounds protection |
| File bytes | Microsoft [GetItem semantics](https://learn.microsoft.com/en-us/openspecs/sharepoint_protocols/ms-copys/442d78b3-96a8-4eb3-9e4d-b47410c14e4a), [successful response](https://learn.microsoft.com/en-us/openspecs/sharepoint_protocols/ms-copys/5c08f990-c62d-4784-99c0-b6da9bc2621f), [missing-file response](https://learn.microsoft.com/en-us/openspecs/sharepoint_protocols/ms-copys/5b635946-246b-4381-acef-9fa8cda04b4d) | Same-server Copy.GetItem URL; decode Stream; missing Stream fails; existing empty Stream is a valid empty file; ignore numeric result |

SharePlum's legacy listing implementation makes a single GetListItems request;
this adapter adds explicit continuation handling. Its site collection method uses
**SiteData.asmx**, so that response must not be confused with the selected
**Lists.asmx** contract. Its [file documentation](https://github.com/jasonrollins/shareplum/blob/55d860b99cccc4bb86437d48df9b0e2d3a974769/docs/files.rst)
limits its Folder/file API to 2013+ REST and describes creating missing folders.
That API cannot implement this read-only 2010 feature. Its `tests/data/2010xml.xml`
and `validation.xml` fixtures exercise list-schema parsing, not paginated document
downloads. We use synthetic document fixtures with explicitly documented shapes
and reject malformed/unsafe XML instead of copying its positional/recovering parser.

## Loop A — deterministic reproduction and regression

In a fresh worktree, install the repository's locked Python 3.12 dependencies:

```bash
cd backend
uv sync --frozen --extra dev
mkdir -p db
export TESTING=true
export BOW_DATABASE_URL=sqlite:///db/test.db
uv run pytest tests/unit/test_sharepoint_2010_client.py \
  tests/unit/test_sharepoint_onprem_client.py \
  tests/e2e/test_sharepoint_2010.py tests/e2e/test_sharepoint_onprem.py \
  tests/e2e/test_data_source.py tests/e2e/test_connection.py \
  tests/e2e/test_connection_file_browse.py -q --tb=short --disable-warnings
```

Only the external SharePoint HTTP boundary is simulated. Services, connection
validation, encrypted credential storage, indexing, RBAC, and the database run
normally. To reproduce the original failure at the base commit, copy the new
`test_sharepoint_2010_client.py` into a disposable base worktree and run only
`test_legacy_connection_lists_and_reads_documents` before adding the implementation.
Observed before: `1 failed`, at `assert c.test_connection()['success']` (False).
The old client ignored `api_mode` through `**kwargs` and requested REST, which the
fixture rejects with 404. After: the same public connection/list/read test passes.

Final SQLite run: **136 passed**. Coverage includes named/all libraries, nested
and empty folders, multiple pages, globs, capped enumeration, names containing
Unicode/quotes/ampersands, ambiguity, byte equality and extraction, missing files,
malformed XML, DTDs, SOAP faults, denied access, redirect rejection, metadata and
stream byte limits, bad returned paths, retries, TLS failure, and Kerberos session
selection. Existing modern REST tests remain green.

The member-credential API test runs in both modes: missing member credentials
block access, saved member credentials are used instead of the system account,
and deleting them blocks access again. API selector/default/validation and saved
config edits are also covered.

### PostgreSQL

Use a dedicated test database, never the application database:

```bash
TEST_DATABASE_URL=postgresql://<test-role>@localhost/<dedicated-test-db> \
  uv run pytest tests/e2e/test_sharepoint_2010.py tests/e2e/test_sharepoint_onprem.py \
  tests/e2e/test_data_source.py tests/e2e/test_connection.py \
  tests/e2e/test_connection_file_browse.py --db=postgres -q --tb=short
```

The first PostgreSQL pass exposed premature reads in the new scope-edit test:
refresh returns a background job, not a completed catalog. The test now polls
the public indexing status before checking or editing the catalog. The final broader PostgreSQL rerun passed **38/38 tests**, including all eight
SharePoint API cases and the 30 generic API cases.

## Loop B — UI and real HTTP simulator

An isolated SQLite app ran on backend 8011/frontend 3011, separate from the
existing developer stack. Enterprise entitlement was supplied by a temporary
sandbox bootstrap matching `tests/e2e/conftest.py`; no license override is shipped.
The seed account and all file/credential data were synthetic. A loopback HTTP
server on 8012 served `LegacyServer.handle` from the regression fixture, exercising
the actual requests transport and normal app connection/indexing endpoints.

The base config schema was loaded for the before capture, then restored to the
modified schema and the sandbox restarted. The frontend changes are opt-in schema
metadata, so absent keys leave the baseline form unchanged. The UI creates a
2010 connection with NTLM selected and discovers four documents. The agent file
browser shows all four, and opening the CSV renders its `North`/`South` rows.
Hebrew/RTL renders the selector and library guidance correctly. This proves the
form/backend integration, not an actual NTLM challenge exchange.

Evidence is in `media/pr/sharepoint-2010/`:

| Before | After |
|---|---|
| ![Original form](../../media/pr/sharepoint-2010/before.png) | ![2010 selector](../../media/pr/sharepoint-2010/after.png) |

[Connection flow](../../media/pr/sharepoint-2010/connect.gif),
[saved connection](../../media/pr/sharepoint-2010/connected.png),
[file browser](../../media/pr/sharepoint-2010/files.png),
[CSV preview](../../media/pr/sharepoint-2010/preview.png), and
[Hebrew form](../../media/pr/sharepoint-2010/after-he.png).

The file-browser/preview checks opened the saved agent's schema URL directly;
they do not assert the wizard's automatic navigation after agent creation.
The locale sweep passed **30/30**:

```bash
cd frontend
PLAYWRIGHT_BASE_URL=http://127.0.0.1:3011 \
  npx playwright test --config=playwright.i18n.config.ts
```

The production frontend build (`npx nuxt build`, with the sandbox dev server
stopped) completed successfully. Python syntax checks and `git diff --check`
also passed.

All ten catalogs contain the new keys. Existing catalog key drift was checked
against the base and is unchanged. The legacy library hint explicitly disallows a
blank selection; the unused inherited Graph concurrency control is hidden.

The read-only smoke tool was also run over loopback HTTP: four files, six
successful continuation requests, exact SHA-256 match for the independently known
33-byte CSV, and successful filename search. A wrong hash returns nonzero failure.

## Loop C — deployment owner runs on-prem

See [setup and smoke command](../sharepoint-server.md#read-only-on-prem-smoke-test).
No SharePoint 2010 farm or credentials were available to this Mac. Deployment
acceptance still needs the same tests on the actual farm and a denied-account
check. Specifically unproven here: enabled ASMX endpoints, URL mappings, Windows
authentication negotiation/KDC, security trimming, list-view thresholds, and the
farm's actual document-library response variants. A lower row limit does not
remove a server query threshold. Narrow folder scope when needed.

Legacy search is bounded filename/path matching only, not full-text search.
Existing catalogs without `api_mode` keep modern REST behavior. No migration or
new dependency is required; the same BOW file metadata/extraction interfaces are
used. A full natural-language report run was not performed in the sandbox because
no LLM provider was configured; deterministic CSV/text extraction is verified.
