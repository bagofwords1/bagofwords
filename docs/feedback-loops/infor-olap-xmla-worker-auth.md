# Feedback Loop - Infor OLAP XMLA reaches the worker but discovery has no user

An Infor EPM OLAP connection could reach the documented XMLA manager endpoint
and resolve a database-specific GUID URL, but cube discovery failed because the
database worker received an empty application username. This loop reproduces
the protocol mismatch without a live Infor farm and verifies direct and ION API
Gateway transport behavior.

## Root cause (validated)

`XmlaClient` sends credentials through HTTP Basic and its generic XMLA
`PropertyList` contains only format, content, and catalog. Infor database
workers additionally authenticate from `UserName`, `Password`, and `Tenant`
inside each XMLA worker request. HTTP Basic alone therefore reached the worker
but did not establish the Infor application identity.

Three related issues made diagnosis and gateway use unreliable:

1. Manager discovery did not send the documented `Secured` restriction.
2. SOAP faults discarded Infor's nested `ErrorCode` and `Description`, leaving
   only the generic fault string.
3. Host rewriting retained an internally advertised scheme and port, so an
   HTTPS ION route could become an unusable internal HTTP worker URL.
4. With a configured Catalog, the generic connection test trusted the saved
   name and could succeed without sending any request to the database worker.

The relevant fixes are in
`backend/app/data_sources/clients/infor_olap_client.py:92-280` and
`backend/app/data_sources/clients/xmla_base.py:496-529`.

## Loop A - deterministic reproduction

The regression suite mocks only the HTTP boundary. It exercises public schema
discovery and query methods, parses the emitted SOAP, and asserts that all
worker calls carry the Infor identity and tenant.

```bash
cd backend
UV_CACHE_DIR=/tmp/uv-cache uv run pytest \
  tests/unit/test_infor_olap_client.py::TestConnect::test_infor_soap_fault_includes_error_code_and_description \
  tests/unit/test_infor_olap_client.py::TestDiscovery::test_worker_discovery_carries_infor_context_and_credentials \
  tests/unit/test_infor_olap_client.py::TestExecuteQuery::test_execute_carries_infor_context_and_credentials \
  tests/unit/test_infor_olap_client.py::TestManagerDiscovery::test_discovery_declares_datasource_security \
  -q
```

Before the fix, the fault assertion saw only the generic SOAP message and the
worker envelopes had no `Tenant`, `UserName`, or `Password` elements:

```text
5 failed
AssertionError: assert None == 'tenant&one'
Input: 'An exception was thrown to inform the client about error condition.'
```

The gateway regressions separately reproduced the missing runtime contract:

```text
TypeError: InforOlapClient.__init__() got an unexpected keyword argument
'worker_url_base'
TypeError: InforOlapClient.__init__() got an unexpected keyword argument
'gateway_token_url'
```

## The fix

`InforOlapClient` now:

- Adds XML-escaped `Catalog`, `Tenant`, `UserName`, and `Password` properties to
  every database-worker `Discover` and `Execute` request.
- Sends the configured `Secured` value during `DISCOVER_DATASOURCES`.
- Uses the database GUID URL returned by the manager; no client connection to a
  dynamic worker port is invented.
- Supports an explicit external worker base for ION/reverse-proxy routes.
- Mints and refreshes ION OAuth2 client-credentials bearer tokens while keeping
  the EPM application credentials in XMLA.
- Retries one gateway request after a `401` with a fresh token.
- Verifies cube discovery on the resolved worker during `Test Connection` when
  a catalog is configured.

`XmlaClient` now surfaces nested XMLA error codes and descriptions for SOAP
faults and inline rowset errors.

The saved connection registry exposes separate direct and ION credential
variants. ION values correspond to the token URL (`pu` + `ot`), client ID
(`ci`), and client secret (`cs`) from a backend-service `.ionapi` file.

## Verification

```bash
cd backend
UV_CACHE_DIR=/tmp/uv-cache uv run pytest \
  tests/unit/test_infor_olap_client.py -q --disable-warnings
```

```text
46 passed in 35.76s
```

Neighboring XMLA and BI clients remain compatible:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run pytest \
  tests/unit/test_analysis_services_client.py \
  tests/unit/test_oracle_bi_client.py -q --disable-warnings
```

```text
31 passed in 26.25s
```

The registry-driven connection API remains green:

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run pytest \
  tests/e2e/test_data_source.py tests/e2e/test_connection.py \
  --db=sqlite -q --disable-warnings
```

```text
11 passed in 13.88s
```

## Live confirmation

The live farm confirmation established the premise without committing any
credentials or customer endpoints:

1. `DISCOVER_DATASOURCES` returned a database-specific GUID URL on the manager
   listener.
2. Posting `MDSCHEMA_CUBES` to that URL produced a fault from a dynamic worker,
   proving the manager routed the request internally.
3. Omitting XMLA identity properties produced an empty-username fault.
4. Supplying them changed the response to Infor error `1042`, proving the
   worker parsed the identity and rejected only the supplied credential.

Final acceptance against a customer farm requires a valid read-only EPM user:
`MDSCHEMA_CUBES` must return rows and one read-only MDX `Execute` must return a
tabular result. Secrets must be provided through environment/configured
credentials and never added to this document.
