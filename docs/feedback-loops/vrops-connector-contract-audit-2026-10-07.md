# VROps connector contract audit — 2026-10-07

Review of `backend/app/data_sources/clients/aria_operations_client.py` at repository HEAD `73e9ffe72`. Scope: every fixed virtual table, discovered metric tables, schema discovery, transport, query limits, and the existing mock/tests. The original audit below records the pre-fix behavior. The implementation and verification section describes the changes made afterward; original line references refer to that baseline.

## Implementation and verification

Implemented in the client, mock, and shared code executor:

- Alert and symptom queries now default to **started during the window**, forwarding the requested millisecond bounds as `startTimeRange`. Explicit `time_mode: "overlap"` preserves the old overlap semantics. Existing saved queries that require earlier open incidents must add that option. Explicit bounds apply even with `active_only: true`; active-only without bounds still means open now.
- Pagination stops at the requested matching-row limit, respects server-reported page sizes, filters definition searches before limiting, and raises on incomplete pagination instead of silently returning a truncated scan. Only returned incidents need name enrichment.
- A per-execution, context-local 150-second budget prevents subsequent HTTP calls after budget exhaustion and reduces each request timeout to the remaining budget. This is cooperative: it does not forcibly abort an in-flight socket or replace the executor's cancellation mechanism. `requests` timeouts are socket inactivity limits, not a hard whole-response wall-clock guarantee.
- Table-specific field validation rejects unsupported keys, invalid positive integers, non-boolean flags, and malformed timestamps. Unsupported wide-table threshold requests now fail with guidance to use the long metrics table instead of silently losing bands.
- Top-N reads the official singular `resourceStats[].stat` response; the simulator emits it. Threshold values join by `dtTimestamps`, not array position.
- Alert definitions retain full states as JSON. Contributors retain all definition IDs and condition objects as JSON alongside the compatible first-item summary columns. The prompt explains SELF-only contributor coverage and avoids promising numeric thresholds in every symptom message.
- Group queries request policies explicitly and bound member pagination. Zero discovered-metric-table configuration avoids needless catalog discovery calls. Resource-kind output limits stop further adapter work.
- Successful generated-code retries emit empty terminal `errors` and retain prior failures in `attempt_errors`. Exhausted retries still fail. A 403 now identifies the failed endpoint without asserting an unverified missing role.

### Reproduce and verify

All new inputs are synthetic. No customer payload, screenshot, trace, identifier, incident timestamp, or credential is used.

From `backend/`:

```sh
.venv/bin/python -m pytest tests/unit/test_aria_operations_client.py tests/unit/test_aria_mock_contract.py tests/unit/test_code_execution_heartbeat.py --confcutdir=tests/unit -q --disable-warnings
.venv/bin/python -m pytest tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q --disable-warnings
```

The same expanded regression suite against baseline production files: **22 failed, 37 passed**. With the fix: **59 passed**. Generic data-source/connection API tests: **12 passed**. Socket-based mock integration: **1 passed** (22 other connectors deselected). Registry resolution succeeds. Existing library deprecation warnings remain.

The ASGI integration checks exercise every fixed table plus a discovered wide metric table, token invalidation/refresh, official Top-N shape, and symptom server-side time filtering through the real client. They run the corrected mock, not a real appliance.

For the socket-based integration check, run the mock from the repository root:

```sh
backend/.venv/bin/python -m uvicorn mock_suite_api:app --app-dir tools/aria_operations --host 127.0.0.1 --port 18443
```

Then from `backend/`, use the **synthetic mock credentials**:

```sh
ARIA_TEST_URL=http://127.0.0.1:18443 ARIA_TEST_USERNAME=admin ARIA_TEST_PASSWORD='Aria!2024' .venv/bin/python -m pytest tests/integrations/ds_clients.py -k aria_operations --confcutdir=tests/integrations -q --disable-warnings
```

### Boundaries and remaining audit items

This patch fixes the reproduced query failures and contract mismatches; it does not establish compatibility with every appliance release. A real appliance remains necessary for version-specific behavior and performance. Overlap remains explicitly more expensive than started-during queries. Broad metric/resource expansion, per-resource property calls, composite resource-kind relationships, metric type discovery, and cross-chunk Top-N aggregation semantics remain follow-up items from the broader audit below. No live UI change was made or tested.

## Evidence and limits

Read the client end to end, its config/registry, unit tests, mock API, the VROps sections of `docs/vmware-aria-storage-connectors-analysis.md` (especially §§3 and 6e), and `docs/feedback-loops/aria-operations-connector.md`.

Retrieved the official [VCF Operations OpenAPI specification](https://github.com/vmware/vcf-api-specs/blob/main/specifications/vcf-operations/vcf-operations-openapi.json). The retrieved specification identifies itself as version **9.1.1.0**. Also read Broadcom's [alert-definition query](https://developer.broadcom.com/xapis/vcf-operations-api/latest/suite-api/api/alertdefinitions/query/post/), [Top-N](https://developer.broadcom.com/xapis/vcf-operations-api/latest/suite-api/api/resources/stats/topn/get/), [symptom query](https://developer.broadcom.com/xapis/vcf-operations-api/latest/suite-api/api/symptoms/query/post/), and [alert query](https://developer.broadcom.com/xapis/vmware-vrealize-operations-api/latest/api/alerts/query/post/) references. The referenced 8.18 programming-guide pages returned HTTP 403 to this research session; they were not freshly verified. No real appliance was accessed for this audit.

Privacy constraint: repository artifacts must use only synthetic fixtures and public vendor documentation. Do not include customer screenshots, traces, payloads, names, hostnames, addresses, identifiers, tokens, incident dates, or customer-derived values. Findings below are supported by source inspection and synthetic reproductions.

`POST /alertdefinitions/query` is a documented endpoint in the current official API. The mock did not invent that endpoint. Availability and authorization on older appliance versions need independent verification; a generic 403 does not establish the cause.

## Findings requiring immediate correction

### 1. Alerts broaden a requested start-time range; symptoms omit it entirely

Client lines 817–829 compute the requested alert window but transmit `startTime=0`, fetch pages with `limit=None`, filter locally, hydrate all matching resources, and only then slice the result. This implements **overlap**, not **started during** semantics. An earlier alert may legitimately overlap an incident, so replacing zero without defining the contract changes results.

Client lines 861–886 make symptoms worse: no time range is transmitted, all pages are collected, and filtering happens locally only when `active_only` is false. Explicit times are therefore ignored for active-only symptoms. The current official symptom-query schema supports `startTimeRange` and `cancelTimeRange`.

Fix: expose explicit started/overlap semantics shared by alerts and symptoms, resolve the window once, push supported filters to the server, and make pagination stop after enough matching rows. For saved queries, define the migration/default behavior explicitly. For overlap, verify a server-filtered strategy against the target API version; do not silently omit earlier active alerts or impose an arbitrary historical lookback. Handle end-only bounds consistently too: active-only alerts currently do not enter the time-filter branch when only `end_time` is supplied.

### 2. Timeouts can leave the connector fetching additional pages

`backend/app/ai/code_execution/code_execution.py:915` runs the client in a daemon thread and stops waiting after the query timeout. Its cancellation path targets registered database connections. This HTTP client registers no such connection and has no cooperative deadline/cancellation checks. Consequently its remaining requests can continue after the caller sees failure. The cancellation helper can even return `not_running` merely because this HTTP work is absent from its SQL registry.

Fix: propagate a per-execution deadline/cancellation signal into the HTTP client, check it before every page/chunk/enrichment call, cap each HTTP timeout by remaining time, and distinguish unsupported cancellation from confirmed completion. An in-flight HTTP request may need to finish or time out, but no further page should start. Avoid changing shared client-wide state when multiple queries run concurrently.

### 3. Top-N parses the wrong response structure

Client lines 751–760 expect `resourceStats[].stat-list.stat[]`. The official `resource-stat` schema instead has a single `stat` object. A populated response with that documented shape returns an empty DataFrame in the reproduction. Both the existing test and mock reproduce the client's incorrect shape (`test_aria_operations_client.py:382`, `mock_suite_api.py:1033`).

Fix the parser and fixtures. Then separately validate ranking semantics: using `data[-1]` and reranking chunks is not a verified implementation of a whole-window aggregate. The API's Top-N entry budget and group semantics also differ from a guaranteed N results per requested metric. Test multiple metrics, multiple samples, ties, and more than one resource batch against a known ranking.

### 4. Dynamic thresholds can be assigned to the wrong measurements

Client lines 1086–1108 associate threshold arrays with metric samples by index and ignore `dtTimestamps`. Official schemas define separate timestamps for thresholds. The reproduction supplies an additional earlier threshold timestamp; the client attaches the earlier bands to later metric samples.

Fix: align by timestamp with an explicit missing-value policy. Do not silently interpolate or forward-fill. Tests must include different timestamp sets, lengths, and missing bands.

Wide tables compound this: they advertise `dt`, send it upstream, then call `_flatten_stats(..., with_dt=False)` and discard every threshold (lines 771–796). Either expose a defined threshold-column format or explicitly reject this option and direct callers to the long metric table.

### 5. Search and pagination silently produce incomplete results

`alert_definitions` fetches at most `limit` definitions before applying `search` (lines 897–908). A matching definition beyond that prefix is missed; even a two-row response with limit=1 reproduces it. The documented GET parameters do not include a name search, so adding an undocumented `name=` parameter is not a validated fix. Scan filtered pages until enough matches or actual exhaustion, and expose incomplete coverage when a scan budget is reached.

Shared `_paged` (lines 385–402) stops when a response has fewer than the requested 1,000 rows, even if `pageInfo.totalCount` says more remain. A server returning effective pages of 100 yields only 100 of 200 rows. It also silently stops at 200 pages, treats an absent response collection as empty, and assumes exhaustion when totalCount is absent without considering continuation links.

Fix: endpoint-aware pagination using actual response metadata; distinguish exhaustion, requested result limit, scan limit, malformed response, and cancellation. Never claim a truncated result is a full count or full-catalog search.

## Coverage of all tables

| Table | Findings / required changes |
|---|---|
| `adapter_kinds` | Its basic mapping is consistent with the reviewed schema. Keep it as a cheap connectivity/discovery check; success does not certify other endpoints. Unknown filters should not be silently accepted. |
| `resource_kinds` | Each kind triggers a count request by default. Reaching `limit` only breaks the inner loop, so subsequent adapters still incur work. The declared primary key `key` is scoped to an adapter, not globally unique; use `(adapterKind,key)` or a qualified identifier and update joins. |
| `stat_keys` | Preserve dictionary metadata such as data type and instance information for query planning. It filters `search` before slicing, unlike alert definitions; reuse that semantic contract with paged inputs. No endpoint mismatch established here. |
| `resources` | Shared pagination defects apply. Documentation promises name lists, while current official resource-query documentation says only one name is supported. `name` and `regex` are mutually exclusive upstream but our input accepts both. Support multiple names through deliberate fan-out/deduplication or reject them. Add explicit support or validation for status/state/tag filters rather than silently ignoring them. Flattening only the first resourceStatusStates item loses multiple adapter-instance states. |
| `properties` | Resolves the full resource set, then sends one GET per object. The documented bulk latest-properties query can reduce round trips. Bound resource resolution, push selected property keys where supported, and apply matching-row limits while collecting. |
| `relationships` | Each visited node/direction fetches every related-resource page before applying the edge limit. Reaching the limit in one direction can still trigger the other direction. Bound page expansion, preserve deduplication/depth semantics, and report truncated graph coverage. Consider the bulk relationship endpoint only after verifying its exact response contract. |
| `metrics` | Threshold alignment bug above. Resource resolution can enumerate the estate before any metrics call. A flattened-row limit silently cuts series or excludes later resources; complete aggregate/RCA claims are unsafe. Bound resources, metric keys and time resolution, disclose partial series, and chunk without changing aggregation meaning. |
| `metrics_latest` | Can fetch every metric when keys are omitted. `currentOnly` is not exposed, so the latest historical sample can be presented as current even if collection stopped. Add explicit freshness semantics and validate sample/resource limits. |
| `metrics_topn` | Wrong response shape; unverified multi-sample/multi-key ranking; GET with up to 1,000 UUIDs can produce large URLs. Define and verify ranking rather than relying on the simplistic mock. |
| `alerts` | Historical scan, late limit/enrichment, ambiguous started/overlap semantics, and end-only inconsistency. Slicing before local sorting does not mean the globally earliest/latest N alerts; define server order or label the subset. |
| `symptoms` | Historical scan without upstream time range; active-only ignores explicit times. Missing explicit symptom-id, symptom-definition and stat-key query filters unnecessarily broaden searches. The prompt promises observed values/thresholds in `message`, but official docs say that field is usually relevant to event symptoms. Expose appropriate alarm/definition information and avoid that promise. |
| `contributing_symptoms` | Keeps only the first `symptomDefinitionsIds` entry and first condition severity; loses definition/condition relationships. The documented endpoint only populates SELF-defined symptoms, so an empty response cannot prove there were no contributing symptoms elsewhere. Preserve the relationships and document this coverage. |
| `alert_definitions` | Search-after-limit bug. Flattening keeps only first-state severity and discards condition trees, definition links, type/subtype and state structure, although the prompt promises operator thresholds/rules. Expose rules through structured fields or linked tables. Both GET and POST are documented in the current API; verify older-version compatibility independently. |
| `custom_groups` | Fetches all groups and then all members of each selected group before limiting. The API defaults `includePolicy=false`, but we expose a policy column without requesting policies. Expose group-id selection, request policy deliberately, and bound membership work. |
| `metrics::<Adapter>/<Kind>` | Defaults to every object of the kind and every stat key, collects all long rows, pivots everything, then calls `head(limit)`. It can consume large memory despite a tiny limit. `dt` is discarded; no-resource output omits the advertised metric columns; every discovered metric is declared float irrespective of its type. Require/select columns and scope, budget samples, preserve empty-result schema and actual types. |

## Cross-cutting contract and discovery fixes

- **Validate a typed query per table.** `_parse_spec` validates JSON/table only. Unsupported filters are silently ignored; `limit=0` becomes the default, negative limits slice unexpectedly, string `"false"` becomes true, and negative relative windows are accepted. Validate integers, booleans, timestamps, bounds, aliases, mutually exclusive fields and table-specific options before network requests. Do not introduce arbitrary retention claims from timestamp validation.
- **Make completeness visible to the agent.** Preserve DataFrame compatibility but deliver fetched/returned counts, pages, scope, ordering, time mode and truncation through the tool result/trace. DataFrame attrs alone are insufficient unless execution preserves and surfaces them. Distinguish empty, incomplete, unavailable and unsupported responses.
- **Stop hiding discovery failures.** `get_schemas` catches count failures as zero and can return all fixed tables after API errors. “Unknown/inaccessible” must not mean “no resources,” and advertised tables need capability status. The max-metric-table cap limits output after counting every kind; even zero still performs the count scan. Skip disabled discovery, cache inventory per identity, and budget/count lazily.
- **Correct misleading schema relationships.** The resources→resource_kinds relationship currently joins on kind key without adapter identity. Preserve composite identity in exposed keys. Audit dropped nested fields before describing a table as complete operator knowledge.
- **Retain diagnostic facts.** Replace hardcoded explanations for 403, 404 and XML with sanitized method/path/status/server detail. Log effective server filters and per-request timings without tokens, passwords or headers. No blanket 403 token-refresh loop or automatic broad-permission advice. Token `validity` parsing and refresh-on-401 are consistent with the reviewed auth schema; token expiry is not the confirmed cause here.
- **Fix prompt contracts.** Explain resource-name regex versus alert-name search; list each table's supported options; use computed dates instead of reusable 2025 epoch literals; remove fixed five-minute sampling assumptions where adapters can differ; forbid absence/retention claims from truncated data.
- **Version and capability evidence.** The old design explicitly required the target appliance's Swagger and live verification of six endpoints; the feedback loop says that was not completed. Do not describe mock success as appliance compatibility. Prefer verified endpoints and semantically equivalent fallbacks; never treat every 403 as proof of endpoint absence.

## Additional finding: successful retry reported as failure

A synthetic failed-then-successful query sequence exposes a mismatch between the final query result and the inspection status.

Code review found a concrete mechanism: `StreamingCodeExecutor.generate_and_execute_stream_v2` retains `code_and_error_messages` across retries and returns that history even after `executed_successfully=True` (`code_execution.py:1844`, `:1996`, `:2062`). `InspectDataTool` sets `success=False` whenever that list is nonempty (`inspect_data.py:345`), regardless of the successful final DataFrame. Its reported duration covers generation and all attempts, not just the final query (`inspect_data.py:287`, `:336`).

A real streaming-executor reproduction with only external HTTP and the LLM generator stubbed produced:

```text
FINAL_DF_ROWS 1
FINAL_LOG returned rows 1
RETAINED_ERRORS 1
FINAL_QUERY_HAS_ERROR False
INSPECT_DATA_WOULD_MARK_FAILED True
```

This proves a successful retry can be mislabeled as failure. The reproduction uses a synthetic initial HTTP 500 followed by success; recovered timeouts use the same retained-error retry path.

Fix: emit an explicit final execution status and attempt records; keep historical errors separately. `inspect_data` must use final status, not the existence of recovered errors. Keep each attempt's code, output, query timings and error together. Reset per-attempt logs so a later failed attempt cannot inherit a previous attempt's output. Report total tool duration separately from generation, queueing and final query duration. Audit other consumers of this executor contract too.

Earlier active alerts can legitimately appear under overlap semantics. A limited sample's min/max and zero name matches do not establish total retention or absence of an alert type.

## Proposed delivery sequence

1. Repair final-attempt status reporting, shared request validation, pagination/completeness and execution deadlines. Add sanitized outbound diagnostics.
2. Repair alerts/symptoms time modes, limits and enrichment. Define how legacy saved queries retain or migrate overlap semantics; new generated queries must choose explicitly. Compare identical requests from the BOW host and Postman.
3. Repair Top-N response parsing, threshold alignment, definition search, and contributing-definition preservation before trusting RCA outputs. Fix the mock independently from official response schemas.
4. Bound metric collection and graph/property/group fan-out; repair schema discovery and richer rule metadata.
5. Validate every table family on the target appliance with the same identity, including small/empty/paged results, unsupported endpoints, 401/403 and cancellation. A connectivity test alone is insufficient.

## Reproduction and observed results

Existing unit suite (without root database fixtures):

```sh
backend/.venv/bin/python -m pytest backend/tests/unit/test_aria_operations_client.py --confcutdir=backend/tests/unit -q
```

Observed: **32 passed in 0.49s**, despite the defects below. The HTTP-boundary harness below uses the real client and the existing fake transport; it requires no live credentials. Synthetic identities and values are unrelated to customer data.

Observed output:

```text
DT_TIMESTAMP_ALIGNMENT: metric timestamps [1000,2000] receive minima [2,8]; expected [8,18]
TOPN_OFFICIAL_RESPONSE: 0 rows; expected 1
DEFINITION_FILTER_AFTER_LIMIT: 0 matches; expected 1
RESOURCE_HEALTH: [85]; expected [85] (control check passed)
SERVER_SMALLER_PAGE: 100 rows, 1 request; expected 200 rows
SYMPTOM_REQUEST: {'activeOnly': False} (requested time bounds absent)
ALERT_REQUEST: {'activeOnly': False, 'startTimeRange': {'startTime': 0, 'endTime': 2000}}
WIDE_DT_COLUMNS: resourceId, resourceName, timestamp, cpu; request_dt=True
CONTRIBUTING_DEFINITIONS: ['d1']; expected both d1 and d2
```

The harness demonstrates local contract failures; it does not establish production token state, production throughput, or any real appliance's response shape/version. No fix has been applied, so no FAIL→PASS claim is made. Convert these cases into generalized regression tests when implementing fixes.

### Executable HTTP-boundary harness

From the repository root, save the following as `/tmp/vrops_review_repro.py` and run `backend/.venv/bin/python /tmp/vrops_review_repro.py`.

```python
import sys, importlib.util, json
from pathlib import Path
root=Path.cwd()
sys.path.insert(0,str(root/'backend'))
import app.data_sources.clients.aria_operations_client as m
spec=importlib.util.spec_from_file_location('aria_test_helpers',root/'backend/tests/unit/test_aria_operations_client.py')
t=importlib.util.module_from_spec(spec);spec.loader.exec_module(t)
def run(routes,query):
 fake=t._FakeRequests(routes);m.requests=fake
 client=m.AriaOperationsClient(url='https://mock.invalid',username='test',password='test')
 return client.execute_query(query),fake
stats={'values':[{'resourceId':'r1','stat-list':{'stat':[{'statKey':{'key':'cpu'},'timestamps':[1000,2000],'data':[10,20],'dtTimestamps':[500,1000,2000],'minThresholdData':[2,8,18],'maxThresholdData':[4,12,22]}]}}]}
res={'resourceList':[{'identifier':'r1','resourceKey':{'name':'R','resourceKindKey':'VM'},'resourceHealthValue':85}]}
base={('POST','/resources/query'):res}
df,f=run({**base,('POST','/resources/stats/query'):stats},{'table':'metrics','resource_id':'r1','stat_key':'cpu','start_time':500,'end_time':3000,'dt':True})
print('DT_TIMESTAMP_ALIGNMENT',df[['timestamp','dt_min','dt_max']].to_dict('records'),'expected minima=[8,18]')
top={'resourceStatGroups':[{'groupKey':'cpu','resourceStats':[{'resourceId':'r1','stat':{'statKey':{'key':'cpu'},'timestamps':[1000],'data':[10]}}]}]}
df,f=run({**base,('GET','/resources/stats/topn'):top},{'table':'metrics_topn','resource_id':'r1','stat_key':'cpu'})
print('TOPN_OFFICIAL_RESPONSE',len(df),'expected=1')
defs={'alertDefinitions':[{'id':'a','name':'unrelated'},{'id':'b','name':'needle'}]}
df,f=run({('POST','/alertdefinitions/query'):defs},{'table':'alert_definitions','search':'needle','limit':1})
print('DEFINITION_FILTER_AFTER_LIMIT',len(df),'expected=1')
df,f=run(base,{'table':'resources'})
print('RESOURCE_HEALTH',df.healthValue.tolist(),'expected=[85]')
def page(p,b):
 return {'resourceList':[{'identifier':str(i),'resourceKey':{}} for i in range(p['page']*100,(p['page']+1)*100)],'pageInfo':{'totalCount':200,'page':p['page'],'pageSize':100}}
df,f=run({('POST','/resources/query'):page},{'table':'resources','limit':200})
print('SERVER_SMALLER_PAGE',len(df),'expected=200, calls=',len(f.calls))
df,f=run({('POST','/symptoms/query'):{'symptom':[]}},{'table':'symptoms','start_time':1000,'end_time':2000})
print('SYMPTOM_REQUEST',f.calls[0][4])
df,f=run({('POST','/alerts/query'):{'alerts':[]}},{'table':'alerts','start_time':1000,'end_time':2000,'limit':1})
print('ALERT_REQUEST',f.calls[0][4])
df,f=run({**base,('POST','/resources/stats/query'):stats},{'table':'metrics::VMWARE/VM','stat_keys':['cpu'],'start_time':500,'end_time':3000,'dt':True})
print('WIDE_DT_COLUMNS',list(df.columns),'request_dt=',f.calls[-1][4].get('dt'))
con={'contributingSymptoms':[{'alertId':'a','contributingSymptoms':{'contributingSymptoms':[{'symptomId':'s','symptomDefinitionsIds':['d1','d2'],'alertConditions':[]}]}}]}
df,f=run({('GET','/alerts/contributingsymptoms'):con},{'table':'contributing_symptoms','alert_id':'a'})
print('CONTRIBUTING_DEFINITIONS',df.symptomDefinitionId.tolist(),'expected both d1,d2')
```

### Executable recovered-retry harness

Save as `/tmp/vrops_retry_repro.py`. From `backend/`, run `.venv/bin/python /tmp/vrops_retry_repro.py` (the backend settings loader expects that working directory).

```python
import asyncio, importlib.util, sys
from pathlib import Path
root=Path.cwd().parent;sys.path.insert(0,str(root/'backend'))
import app.data_sources.clients.aria_operations_client as m
from app.ai.code_execution.code_execution import StreamingCodeExecutor
from app.ai.schemas.codegen import CodeGenRequest, CodeGenContext
s=importlib.util.spec_from_file_location('helpers',root/'backend/tests/unit/test_aria_operations_client.py');t=importlib.util.module_from_spec(s);s.loader.exec_module(t)
m.requests=t._FakeRequests({('POST','/alerts/query'):[t._FakeResponse({'message':'first attempt failed'},500),t._FakeResponse({'alerts':[{'alertId':'a','startTimeUTC':1000,'status':'ACTIVE'}]})]})
client=m.AriaOperationsClient(url='https://mock.invalid',username='test',password='test')
async def generate(**kwargs):
 return '''def generate_df(ds_clients, excel_files):
    df = ds_clients["demo"].execute_query('{"table":"alerts","active_only":true}')
    print("returned rows", len(df))
    return df
'''
async def main():
 ex=StreamingCodeExecutor()
 req=CodeGenRequest(context=CodeGenContext(user_prompt='read alerts',schemas_excerpt='alerts'),retries=2)
 async for e in ex.generate_and_execute_stream_v2(request=req,ds_clients={'demo':client},excel_files=[],code_generator_fn=generate):
  if e['type']=='done':
   p=e['payload'];print('FINAL_DF_ROWS',len(p['df']));print('FINAL_LOG',p['execution_log'].strip());print('RETAINED_ERRORS',len(p['errors']));print('FINAL_QUERY_HAS_ERROR',any(x.get('error') for x in p['query_timings']));print('INSPECT_DATA_WOULD_MARK_FAILED',bool(p['errors']))
asyncio.run(main())
```

Downloaded official spec SHA-256: `39c3145f11cf83c57e0cb4f54572ea9dba88ca2461b5b020570e05bb8aa88f3e`.
