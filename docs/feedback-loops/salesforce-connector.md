# Feedback Loop — Salesforce connector: "only 5 objects indexed, sandbox/OAuth unsupported"

The Salesforce connector had gone unexercised for a long time. This loop
validates three claims about the pre-change connector and proves the fix:

1. Schema indexing only ever returned **5 hardcoded objects**
   (Account/Contact/Opportunity/Lead/Case) — a real org's custom objects
   (`*__c`) and other standard objects were invisible to the agent.
2. The `sandbox` / `domain` config fields were **captured but never forwarded**
   to `simple_salesforce`, so sandbox and My-Domain logins silently hit
   production.
3. There was **no OAuth path** — only username + password + security token.

The fix adds the OAuth 2.0 **JWT Bearer** flow (Connected App), an
**access-token** path, **dynamic object discovery** (standard + custom), field
type/FK mapping, and a `MAX_ROWS` pagination cap.

## Root cause (validated)

- `backend/app/data_sources/clients/salesforce_client.py:39` (pre-change) —
  `get_schemas` iterated a literal list `["Account","Contact","Opportunity","Lead","Case"]`.
- `salesforce_client.py:10-18` (pre-change) — `__init__` stored `domain` (and
  didn't even store `sandbox`), but the `sf` property called
  `Salesforce(username=…, password=…, security_token=…)` with no `domain`
  kwarg, so both config fields were dead.
- `backend/app/schemas/data_source_registry.py:514` (pre-change) — the only
  auth variant was `userpass`; `client_path=None` relied on the dynamic-import
  fallback.

## Loop A — deterministic reproduction (no external services)

`simple_salesforce.Salesforce` is mocked; no live org or network.

### Before (on `HEAD` prior to the fix)

Restore the old client into a temp module and observe the failure:

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"; mkdir -p db
git show HEAD~1:backend/app/data_sources/clients/salesforce_client.py > /tmp/old_salesforce_client.py
uv run python - <<'PY'
import importlib.util
spec = importlib.util.spec_from_file_location("old_sf", "/tmp/old_salesforce_client.py")
old = importlib.util.module_from_spec(spec); spec.loader.exec_module(old)
cap = {}
class FakeSFType:
    def describe(self): return {"fields":[{"name":"Id","type":"id"}]}
class FakeSF:
    def __init__(self, **kw): cap.update(kw)
    def __getattr__(self, n): return FakeSFType()
old.Salesforce = FakeSF
c = old.SalesforceClient(username="u", password="p", security_token="t", domain="login", sandbox=True)
names = [t.name for t in c.get_schemas()]
print("indexed objects:", names)
print("custom object discoverable? ->", "Widget__c" in names)
print("sandbox forwarded? domain kwarg =", cap.get("domain", "<MISSING>"))
PY
```

Observed FAIL:

```
indexed objects: ['Account', 'Contact', 'Opportunity', 'Lead', 'Case']
custom object discoverable? -> False
sandbox forwarded? domain kwarg = <MISSING>
```

### After (current code) — the same invariants encoded as regression tests

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/unit/test_salesforce_client.py -q
```

Observed PASS (`24 passed`). The load-bearing assertions:

- `TestDiscovery::test_dynamic_discovery_includes_custom_excludes_noise` —
  a custom object (`Widget__c`) **is** indexed; `*Share`/`*History`,
  deprecated, custom-setting, and non-queryable objects are excluded; the set
  is no longer the hardcoded five.
- `TestAuthMode::test_userpass_sandbox_routes_to_test` — `sandbox=True` now
  forwards `domain="test"` to `simple_salesforce`.
- `TestJwtBearer::test_jwt_exchange_and_construct` — the JWT Bearer grant signs
  a real RS256 assertion, exchanges it at
  `/services/oauth2/token`, and constructs `Salesforce(instance_url=, session_id=)`
  from the response.

### Generic connector suites still green

```bash
uv run pytest tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q   # 11 passed
```

## Loop B — live confirmation (optional, real org)

Only needed to prove the JWT handshake against a real Salesforce. Use a free
**Developer Edition** org + a Connected App with a certificate, admin
pre-authorized for the user. Secrets via env vars only — never commit them.

```bash
# SF_CONSUMER_KEY, SF_PRIVATE_KEY (PEM), SF_USERNAME set in the environment
cd backend && export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run python - <<'PY'
import os
from app.data_sources.clients.salesforce_client import SalesforceClient
c = SalesforceClient(consumer_key=os.environ["SF_CONSUMER_KEY"],
                     private_key=os.environ["SF_PRIVATE_KEY"],
                     username=os.environ["SF_USERNAME"])
print(c.test_connection())
print("objects:", len(c.get_schemas()))
print(c.execute_query("SELECT Id, Name FROM Account LIMIT 5"))
PY
```

## The fix

- `salesforce_client.py` — rewritten: `_auth_mode` selects jwt / access-token /
  userpass; `_jwt_access()` runs the `urn:ietf:params:oauth:grant-type:jwt-bearer`
  exchange; `_login_url()`/`_sf_domain()` honor `sandbox`/`domain`;
  `_discover_object_names()` enumerates queryable standard + custom objects
  (noise/deprecated/custom-setting filtered, `MAX_INDEX_OBJECTS` cap logged);
  `get_schema()` maps field types and builds FKs from `referenceTo`;
  `execute_query()` paginates up to `MAX_ROWS`.
- `schemas/data_sources/configs.py` — new `SalesforceJWTCredentials`
  (consumer_key / private_key / username); `SalesforceConfig` gains `objects`.
- `schemas/data_source_registry.py` — `jwt` + `userpass` system-scoped variants,
  explicit `client_path`.

## What this proves / regression notes

The loop demonstrates the connector now indexes a real org's objects (not a
demo five), routes sandbox/My-Domain logins correctly, and authenticates via
JWT Bearer OAuth. Pre-existing unrelated warnings (`datetime.utcnow`
deprecations, Pydantic v2 `.dict()`) appear in the suite output and reproduce
with these changes stashed — they are not introduced here.
