# Infor EPM (Application Engine) connector — design

**Status:** implemented (`infor_epm`), verified against a mock Application Engine
(`tools/agent/mock_infor_epm_server.py`) — see
`docs/feedback-loops/infor-epm-appengine-connector.md`.

## Why a second Infor connector

`infor_olap` talks XMLA directly to the Infor d/EPM OLAP provider. That path
requires **Basic** credentials: Infor documents XMLA authentication as
`<UserName>`/`<Password>` in the SOAP `PropertyList` only, and IFS/OAuth2 is
not supported for direct XMLA requests on-premises. Farms that allow **only
Infor Federation Services** identities therefore cannot be queried over XMLA
from outside — a Repository user with a password is exactly what such a
policy forbids.

Infor's sanctioned alternative is the **Application Engine**: BI# processes
run *inside* the farm on a named OLAP connection (no credentials in code;
the engine carries the IFS identity), and every published process appears as
a REST endpoint in the ION API Gateway, where a client-credentials OAuth2
token is all a caller needs. This connector is that route.

Investigation notes that shaped the design (all generic):

- The Application Engine's native `OLAP*` API is data-area/element based and
  has **no MDX**; its `OLAPXMLRequest` speaks the Alea XML API
  (`Database`/`Cube`/`Dimension`/`Connection` classes), also without MDX.
- MDX **is** available to a process through `XMLACreateNamedConnection` +
  `XMLAExecuteMdxString`, bound to an XMLA data connection defined in EPM
  Administration — which is what `BOW_ExecuteMdx` uses.
- `Dimension GetProperties` (Alea XML API, via `OLAPXMLRequest`) returns
  descriptions, `ODBOType`, hierarchies and level names per dimension;
  element samples come from `OLAPGetElementList`.

## Process contract (customer side)

Three BI# processes, published in the DEPM API suite. All return one string.

| Process | Input | Output |
|---|---|---|
| `BOW_GetCubeList` | `OLAPName` | cube names, one per line (`#` system cubes excluded) |
| `BOW_GetCubeSchema` | `OLAPName`, `CubeName` | `<BOWSchema cube db>` with one `<BOWSection kind="dimension" name position>` per dimension wrapping the raw Alea `Dimension GetProperties` response plus `<BOWElements count>` samples |
| `BOW_ExecuteMdx` | `OLAPName`, `mdxQuery`, `rowLimit` | one cell per line: `coord1<TAB>…<TAB>coordN<TAB>STR\|NUM<TAB>value`; trailer `BOW_TRUNCATED` when capped |

Failure signals: a process returns `BOW_ERROR: <message>`; the engine wraps
uncaught failures as `{"error": "..."}` with HTTP 200. `BOW_ExecuteMdx`
reports an empty cell set as `BOW_ERROR: MDX returned no cells`, which the
client maps to an empty DataFrame.

Transport: `POST <api_url>/<Process>` (sync) or `POST <api_url>/<Process>/async`
→ `{"taskId"}` then `POST <api_url>/getasyncresult {"taskId"}` until a
completed status with the result. Bearer token from the ION token endpoint
(client credentials) or a static token for testing.

## Our side

- `backend/app/data_sources/clients/infor_epm_client.py` — `InforEpmClient`:
  token exchange with refresh + 401 retry, sync/async process calls,
  `parse_bow_schema`, `parse_mdx_cells`, the MDX system prompt.
- `backend/app/schemas/data_sources/configs.py` — `InforEpmConfig`,
  `InforEpmIonCredentials`, `InforEpmTokenCredentials` (the form).
- `backend/app/schemas/data_source_registry.py` — `infor_epm` entry
  (`ion_oauth` default, `bearer_token` for testing; no password variant).

### Schema model

One table per cube, `Database/Cube`. Each dimension is a `dimension` column
with `unique_name` (`[Dim]`), cube position, ODBO type (time = 1, measures =
2), hierarchies/levels, element count and a capped element sample with
their unique names. Infor OLAP has no separate measure objects: the
**measures dimension** is detected by ODBO type 2, else by the configurable
name pattern (default `measure`); its sampled elements become `measure`
columns (`[Dim].[Elem]`). Table metadata carries `cubeUniqueName`,
`dimension_order` and `measure_dimension`.

### Query model

`execute_query(mdx, table_name, max_rows)` relays the MDX to `BOW_ExecuteMdx`
with `rowLimit`, parses the tab lines into one column per axis coordinate
(named from the coordinate's first bracket segment, value = last segment)
plus `value` (float for `NUM`), and sets `df.attrs["truncated"]`.

## Verification

- Unit: `backend/tests/unit/test_infor_epm_client.py` (HTTP boundary mocked;
  fixtures shaped like the real farm output, including embedded XML prologs
  and `Alea:Error` nodes).
- Sandbox: `tools/agent/mock_infor_epm_server.py` + `tools/agent/e2e_infor_epm_sandbox.mjs`
  drive the real UI (form → test → index → agent question) against a mock farm.
