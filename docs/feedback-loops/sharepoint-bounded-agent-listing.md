# Feedback Loop — SharePoint agent listing scans too much before returning

Interactive SharePoint listing must stop once it has enough matching files.
This loop uses synthetic HTTP responses and transient connection models with
synthetic credentials. No customer data, screenshots, videos, or images are
part of the reproduction or change.

## Root cause (validated)

On baseline `d3f9c1f4d`, `backend/app/ai/tools/implementations/list_files.py:207`
forced `recursive=True` without a folder and invoked `alist_files` without a
limit, then trimmed the already-collected results. The base async wrapper at `backend/app/data_sources/clients/base.py:324` did
not accept a limit. On-prem adapters already yielded individual entries, but
the tool drained them. Graph's folder walkers also collected all child pages
before enforcing their result limit.

## Loop A — deterministic reproduction

Use Python 3.12 and the repository's locked dependencies:

```bash
cd backend
uv sync --frozen --extra dev
mkdir -p db
TESTING=true BOW_DATABASE_URL=sqlite:///db/test.db uv run pytest \
  tests/unit/test_sharepoint_bounded_listing.py -q --tb=short --disable-warnings
```

The first 18 cases were run before changing production code: **12 failed,
6 passed**. Requests for one or three files returned the full synthetic tree;
non-recursive requests also traversed a nested folder. The bounded result and
connection-cap metadata were absent. These failures ran through the real agent
tool, resolver, encrypted credential handling, and clients; only provider HTTP
was stubbed. Models are transient, with no database access on this system-context
path. Later cases extend coverage beyond the initial reproduction.

## The fix

- `ListFilesInput.limit`: SharePoint-only result limit, default 50, range 1–500.
- `_sharepoint_listing.py`: explicit opt-in shared by on-prem SharePoint and the
  SharePoint Graph subclass. Its async wrapper consumes a lazy iterator, applies
  name/path filtering before counting results, and stops without fetching a
  lookahead page. It preserves connection filtering and caps.
- On-prem adapters accept an optional interactive page size; SOAP uses `rowLimit`
  and REST uses `$top`. Their existing indexing/listing defaults are unchanged.
- `SharepointClient` has a serial, page-by-page interactive folder iterator;
  it does not drain Graph's existing full-folder or concurrent indexing walks.
  It validates selected folder scope and continuation-link destinations.
- The agent tool selects this path only for opted-in SharePoint clients, under
  the existing resolved identity. It honors recursion and reports `incomplete`
  with `result_limit` or `catalog_limit`. Hitting a limit does not prove that
  another matching file exists, so the result says more files *may* exist.

SharePoint Online interactive listing now uses this live identity-scoped path
rather than the tool's shared-catalog path. This intentionally trades cached
inventory for bounded live access. OneDrive, network directory, S3, and other
file connectors retain their existing behavior and receive no new arguments.
The generic async wrapper is unchanged. Direct indexing and search APIs retain
existing behavior.

## Verification

```bash
TESTING=true BOW_DATABASE_URL=sqlite:///db/test.db uv run pytest \
  tests/unit/test_sharepoint_bounded_listing.py \
  tests/unit/test_sharepoint_2010_client.py tests/unit/test_sharepoint_onprem_client.py \
  tests/unit/test_file_tools.py tests/unit/test_graph_drive_catalog_cost.py \
  tests/unit/test_graph_drive_multi_library.py tests/unit/test_graph_throttling.py \
  tests/unit/test_drive_clients.py tests/unit/test_network_dir_client.py \
  tests/unit/test_s3_client.py -q --tb=short --disable-warnings
TESTING=true BOW_DATABASE_URL=sqlite:///db/test.db uv run pytest \
  tests/e2e/test_sharepoint_2010.py tests/e2e/test_sharepoint_onprem.py \
  --db=sqlite -q --tb=short --disable-warnings
```

Observed: **393 unit tests passed**, including **42 bounded-listing cases**;
**8 API/SQLite tests passed**. Coverage includes later-page filename matches,
recursion, connection globs/extensions, all-library limits, authentication
failures, Graph folder confinement and foreign continuation rejection, default
and invalid limits, unchanged indexing, and real tool-level compatibility for
OneDrive and network-directory listing. No failing tests in the executed suites.

## Reference implementations and limits

- [python-o365 folder listing](https://github.com/O365/python-o365/blob/6b129ecf7d7f056f9c6147ec1342fc5e43aeaf1f/O365/drive.py#L1030-L1073)
  separates total result limits from batch sizes.
- [Office365 REST collection iteration](https://github.com/vgrem/office365-rest-python-client/blob/bb63f4ba3eaa06096452b8cf6f7e80695a9ce00e/office365/runtime/client_object_collection.py#L231-L242)
  advances pages lazily, separately from explicit `get_all`.
- [Microsoft SOAP paging contract](https://learn.microsoft.com/en-us/previous-versions/office/developer/sharepoint-2010/bb263567%28v%3Doffice.14%29)
  defines `rowLimit` as page size and continuation tokens as opaque bookmarks.

This does not verify a real SharePoint farm, confirm the cause of any particular
production timeout, or bound the latency of the first provider request. Sparse
filters can still scan many pages before finding a match or reaching existing
caps. Request/time budgets, persistent continuation cursors, and search changes
are follow-ups. A file limit does not promise uniform random sampling.
