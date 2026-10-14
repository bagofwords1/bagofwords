# Feedback Loop — custom document libraries missing from discovery

Visible document libraries created from custom templates must be available to
the on-prem SharePoint connector through both SOAP and REST. This loop uses
only synthetic HTTP responses, example-domain URLs, invented template IDs,
and existing synthetic file fixtures. No customer data or media is included.

## Root cause (validated)

At baseline `7365a9094`,
`backend/app/data_sources/clients/_sharepoint_2010.py:101` and `:111` required
`ServerTemplate == '101'`, both for initial metadata and the `GetList` response
used when a collection row has no root folder. The REST query at
`backend/app/data_sources/clients/_sharepoint_rest.py:57` required
`BaseTemplate eq 101`. These conditions exclude document libraries whose
template differs from the standard template, despite `BaseType == 1`.

Microsoft's [SharePoint 2010 template/base-type table](https://learn.microsoft.com/en-us/previous-versions/office/developer/sharepoint-2010/ee549420%28v%3Doffice.14%29)
distinguishes the template identifier from the document-library base type.
The [GetListCollection response example](https://learn.microsoft.com/en-us/previous-versions/office/developer/sharepoint-2010/bb263778%28v%3Doffice.14%29)
documents the SOAP `BaseType`, `ServerTemplate`, and `Hidden` attributes.

## Loop A — deterministic reproduction

From a fresh checkout with Python 3.12:

```bash
cd backend
uv sync --frozen --extra dev
mkdir -p db
TESTING=true BOW_DATABASE_URL=sqlite:///db/test.db uv run pytest \
  tests/unit/test_sharepoint_2010_client.py \
  tests/unit/test_sharepoint_onprem_client.py \
  -k 'custom_document_libraries or hidden_and_non_document' \
  -q --tb=short --disable-warnings
```

The regression tests exercise public catalog, search, and read methods with
mocked HTTP only. The synthetic SOAP server exposes configurable list metadata
and can omit collection root folders, forcing a real `GetList` round trip.
The REST server evaluates the request's OData equality predicates against its
synthetic metadata, so a restrictive request cannot pass by receiving an
unconditionally complete response.

With these tests added before the production fix, the observed result was:

```text
17 failed, 3 passed, 114 deselected
```

Custom libraries were omitted in wildcard mode or rejected when selected
explicitly. Additional negative cases showed that a standard template ID alone
could admit metadata with a non-document or missing base type.

## The fix and verification

SOAP now requires `BaseType == '1'` at both metadata checks. REST requests
`BaseType eq 1 and Hidden eq false`. Explicitly selected libraries follow the
same classification as wildcard discovery. Hidden libraries remain excluded;
the existing authentication, scope validation, and file access checks remain
in force. There is no custom-template allowlist.

Run the full affected suites:

```bash
TESTING=true BOW_DATABASE_URL=sqlite:///db/test.db uv run pytest \
  tests/unit/test_sharepoint_2010_client.py \
  tests/unit/test_sharepoint_onprem_client.py -q --tb=short --disable-warnings
TESTING=true BOW_DATABASE_URL=sqlite:///db/test.db uv run pytest \
  tests/e2e/test_sharepoint_2010.py tests/e2e/test_sharepoint_onprem.py \
  --db=sqlite -q --tb=short --disable-warnings
```

Observed after the fix: **134 unit tests passed**, including all 20 new cases;
**8 API/SQLite tests passed**. Existing suites also cover authentication,
pagination, folder scoping, traversal rejection, size limits, and member
permission boundaries. `git diff --check` passed.

## Limits and on-prem confirmation

This proves connector behavior against synthetic SOAP/REST responses and the
real local application/SQLite API flow. It is not a real SharePoint farm test.
After deployment, use a new client instance to avoid previously cached library
metadata; verify discovery and read a known permitted file from a custom
document library. File limits apply across the selected scope. Broader library
discovery does not imply that every file format can be parsed into text.
