# Feedback Loop — "GET /api/auth/entra/authorize → 500: certificate verify failed: Missing Authority Key Identifier"

A customer on bow 0.0.595 (Kubernetes, namespace `test-bow`) reported that every
Entra (Microsoft) login attempt returned 500. The pod log showed, per request:

```
httpx.ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Missing Authority Key Identifier (_ssl.c:1032)
httpx_oauth.clients.openid.OpenIDConfigurationError: [SSL: CERTIFICATE_VERIFY_FAILED] ...
INFO: "GET /api/auth/entra/authorize HTTP/1.1" 500 Internal Server Error
```

Rolling back below 0.0.590 made login work again. The claim validated here:
the image's move to Python 3.13 (0.0.590) made the app reject the certificate
chain presented by the customer's TLS-inspecting proxy, and an operator switch
that restores the pre-3.13 checks is enough to unblock them while the proxy's
CA is fixed.

## Root cause (validated)

1. **Python 3.13 verifies in OpenSSL strict X.509 mode by default.**
   `ssl.create_default_context()` now sets `VERIFY_X509_STRICT` (and
   `VERIFY_X509_PARTIAL_CHAIN`). Strict mode requires every certificate that is
   not self-signed to carry an Authority Key Identifier (AKI) extension;
   missing it raises `X509_V_ERR_MISSING_AUTHORITY_KEY_IDENTIFIER`, which is
   the exact text in the log. Python 3.12 and earlier never checked this.
   Validated: `ssl.create_default_context().verify_flags & ssl.VERIFY_X509_STRICT`
   is non-zero on the image's Python 3.13.14 and in the probe below.
2. **The chain the pod sees is not Microsoft's.** Microsoft's public chain for
   `login.microsoftonline.com` (DigiCert root → Microsoft Azure issuing CA →
   leaf) carries AKI on every cert, so the failure can only come from a
   certificate minted by something between the pod and Microsoft. The pod spec
   has a customer-added `merge-ca-bundle` init container (not in our Helm
   chart), i.e. a private CA is being trusted. Validated live: `openssl x509
   -ext authorityKeyIdentifier` on the cert returned to the pod for
   `login.microsoftonline.com:443` printed **no** `X509v3 Authority Key
   Identifier` line (customer ran it; see Loop B).
3. **The app has no place to hand in a TLS context.** OIDC discovery runs at
   request time in `backend/app/services/auth_providers.py:192`, inside
   httpx-oauth's `OpenID.__init__` (`httpx_oauth/clients/openid.py:51`), which
   opens a bare `httpx.Client()`. The Graph profile call at
   `backend/app/core/auth.py:620` and the LLM SDKs build their own clients the
   same way. They end in three different places, and the policy has to reach
   all three (found in review, see "Review findings" below):
   - `ssl.create_default_context` — httpx, httpx-oauth, the LLM SDKs, OTEL,
     ldap3;
   - `urllib3.util.ssl_.create_urllib3_context` — urllib3 2.x never calls the
     stdlib factory and ORs its own copy of `VERIFY_X509_STRICT`
     (`urllib3/util/ssl_.py:292-296`, 2.8.0). This is `requests`, so MSAL /
     azure-identity (Fabric, Azure Data Explorer, the Azure OpenAI Entra token
     provider) and ~30 `requests`-based connectors;
   - contexts cached at import — aiohttp builds `_SSL_CONTEXT_VERIFIED` once
     when `aiohttp.connector` is imported (`connector.py:928`, 3.14.5).
4. **Why it surfaced on Entra first:** login is the first outbound HTTPS call a
   fresh session makes. Behind the same proxy, LLM provider calls and any
   Graph lookups fail identically; the migrations and the database do not
   (libpq does not use strict mode and RDS has its own bundle).

Hypothesis, not validated: the proxy product. The customer has not yet named
it; the missing AKI is typical of a hand-generated CA uploaded to the proxy
rather than a vendor-generated one.

## Loop A — deterministic reproduction (no external services)

`tools/agent/tls_strict_x509_probe.py` builds a proxy-shaped chain in a temp
dir (self-signed CA with SKI, leaf for `localhost` signed by it **without**
AKI), serves `/.well-known/openid-configuration` from a local HTTPS server, and
fetches it once per policy through both client families: `httpx` (what
httpx-oauth and the LLM SDKs use) and `requests` (urllib3: MSAL and most
connectors).

```bash
cd backend
TESTING=true BOW_DATABASE_URL=sqlite:////tmp/probe.db \
  uv run python ../tools/agent/tls_strict_x509_probe.py
```

Before the fix the module under test did not exist, so the equivalent is the
strict line alone, which is Python 3.13's unconditional behaviour:

```
python            3.13.14
strict_x509=true  httpx FAIL [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Missing Authority Key Identifier (_ssl.c:1032) | requests FAIL SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Missing Authority Key Identifier (_ssl.c:1032)'
```

That is the customer's error, byte for byte, with no proxy and no network, on
both client stacks.

The regression tests live in `backend/tests/unit/test_tls_strict_x509.py`
and assert the general invariant, not the Entra scenario: with the switch off,
every context built through plain `ssl`, through `httpx.create_ssl_context`
(httpx-oauth's and the SDKs' path), through the shared LLM context, through
`urllib3.util.ssl_.create_urllib3_context` (requests / MSAL) and the context
aiohttp cached at import has the strict flag cleared, while `verify_mode`
stays `CERT_REQUIRED` and `check_hostname` stays on; with the switch on
(default) all of those match the interpreter's own flags; applying the policy
twice does not stack and switching back restores every path; the env var
relaxes only on an explicit `false/0/no/off` and a typo keeps strict mode.

```bash
cd backend
TESTING=true BOW_DATABASE_URL=sqlite:////tmp/probe.db \
  uv run pytest tests/unit/test_tls_strict_x509.py -q
# 15 passed
```

Can-fail checks, each restored before commit: the stdlib flag-clearing line
replaced by `pass` → 2 fail; the urllib3 line removed → 1 fails; the cached-
context patch removed → 1 fails.

## Loop B — live confirmation (customer's cluster)

Run from inside the `bowapp` container. No credentials involved.

```bash
# Who issued the cert the pod sees, and does it carry AKI?
kubectl exec -n test-bow <pod> -c bowapp -- sh -c \
  'openssl s_client -connect login.microsoftonline.com:443 </dev/null 2>/dev/null \
   | openssl x509 -noout -issuer -ext authorityKeyIdentifier'
# Observed: no "X509v3 Authority Key Identifier" line  -> proxy-minted cert without AKI

# Is strict mode on in this interpreter?
kubectl exec -n test-bow <pod> -c bowapp -- python -c \
  "import ssl; print(ssl.create_default_context().verify_flags & ssl.VERIFY_X509_STRICT)"
# Observed on 0.0.595: non-zero. Expected after the fix with BOW_TLS_STRICT_X509=false: 0
```

`env | grep -i proxy` in the pod showed no `HTTPS_PROXY`, so the interception
is transparent (network-level); `NO_PROXY` cannot route around it.

## The fix

Commits `d8419b388` (initial) and the review follow-up on
`fix/tls-strict-x509-opt-out`.

- `backend/app/settings/bow_config.py` — `security.tls_strict_x509: bool`,
  default `true`, env override `BOW_TLS_STRICT_X509`. Only an explicit
  `false/0/no/off` relaxes; anything else, including a typo, stays strict.
- `backend/app/core/tls.py` — `configure_process_tls(strict_x509=...)`. When
  off it (a) swaps `ssl.create_default_context` for a wrapper that clears
  `VERIFY_X509_STRICT`, (b) sets `urllib3.util.ssl_.VERIFY_X509_STRICT` to 0
  so urllib3's own builder stops ORing it in (the global is read at call
  time), (c) clears the flag in place on contexts libraries cached at import
  (aiohttp's `_SSL_CONTEXT_VERIFIED`), and (d) drops the shared httpx
  context cache. Idempotent and reversible; the original factory is captured
  once at import. Logs a WARNING so the relaxed mode is visible in every pod
  log.
- `backend/main.py` — applied as the first app import, before the route
  modules pull in aiohttp. Loading `app.settings.config` that early imports
  httpx only, nothing that builds a context.
- `bow-config.yaml` — commented `security.tls_strict_x509` example.

Loop A after the fix:

```
python            3.13.14
strict_x509=true  httpx FAIL [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Missing Authority Key Identifier (_ssl.c:1032) | requests FAIL SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Missing Authority Key Identifier (_ssl.c:1032)'
strict_x509=false httpx PASS HTTP 200 | requests PASS HTTP 200
RESULT OK
```

Startup wiring, checked by importing the app the way uvicorn does (the three
lines the reviewer used to find the gaps):

```bash
cd backend && BOW_TLS_STRICT_X509=false TESTING=true BOW_DATABASE_URL=sqlite:////tmp/probe.db \
  uv run python -c "
import main, ssl, urllib3.util.ssl_ as u, aiohttp.connector as ac
S = ssl.VERIFY_X509_STRICT
print('ssl.create_default_context strict:', bool(ssl.create_default_context().verify_flags & S))
print('urllib3 create_urllib3_context:   ', bool(u.create_urllib3_context().verify_flags & S))
print('aiohttp cached default ctx:       ', bool(ac._SSL_CONTEXT_VERIFIED.verify_flags & S))"
# tls.strict_x509_disabled: outbound TLS uses pre-Python-3.13 X.509 checks ...
# ssl.create_default_context strict: False
# urllib3 create_urllib3_context:    False
# aiohttp cached default ctx:        False
```

### Review findings (PR #1453)

The first revision only wrapped `ssl.create_default_context`, and the PR text
claimed that covered "every outbound HTTPS client". The reviewer checked out
the branch and showed two gaps, both confirmed against the installed
libraries and against a real tenant through an inspecting proxy:

1. **urllib3 stayed strict.** urllib3 2.x builds its own contexts and ORs its
   own copy of the flag. Login would have worked while every
   `requests`/MSAL-based data source (Fabric, Power BI, Azure Data Explorer,
   SharePoint on-prem, …) stayed broken.
2. **aiohttp had already cached its context.** The hook ran at `main.py:129`,
   after ~120 lines of imports had loaded aiohttp. The earlier "no library
   builds a context at import" note was wrong: the grep only looked for
   `from ssl import create_default_context`, and aiohttp calls it through the
   module.
3. **Unknown env values relaxed.** `"flase"` turned strict mode off.

All three are fixed above; the probe gained a `requests` leg and the tests
gained urllib3 and aiohttp cases so the class of gap, not just the two
instances, is covered.

### Rollout for the affected customer

```yaml
# Helm values
extraEnv:
  - name: BOW_TLS_STRICT_X509
    value: "false"
```

Their rollback also matters: 0.0.595 had already migrated the database to
`submsg02` (added in 0.0.592), and the older image failed with
`Can't locate revision identified by 'submsg02'`. If `alembic_version` was
pointed at `mrgheads10` to let the old image start, set it back to `submsg02`
before the fixed image starts, or Alembic will try to recreate
`sub_agent_messages`. Both migrations are additive, so nothing was dropped.

Remove the env var once the proxy CA carries AKI, or once
`login.microsoftonline.com` / `graph.microsoft.com` are on the proxy's
SSL-inspection bypass list. The proxy-side fix is the real one; this switch is
the bridge.

### Alternatives considered

- **No code change: `sitecustomize.py` via ConfigMap + `PYTHONPATH`.** Same
  monkeypatch injected at interpreter start. Works, was offered first, rejected
  by the customer in favour of a supported switch in the image.
- **Pass a context per client.** Not reachable: httpx-oauth opens its client
  inside `__init__`, and the LLM SDKs, MSAL and OTEL exporters each build
  their own.
- **`NO_PROXY`.** Only helps with an explicit proxy; this one is transparent.

## What this proves / regression notes

- Python 3.13 + any CA-issued certificate without AKI = the reported error,
  independent of Entra, the proxy vendor, or the network. The probe is the
  minimal reproduction and doubles as the operator's diagnostic.
- `security.tls_strict_x509=false` reaches every client path the app uses
  (stdlib factory, urllib3's builder, import-time caches) and changes nothing
  but the strict-extension flag: chains are still validated against the trust
  store and hostnames still checked (asserted in the tests).
- A library that builds and holds its own context before `main.py`'s first
  line, or copies the flag value rather than reading it, would still be
  strict. None is known; the reviewer's three-line check above is the way to
  find one.
- Default behaviour is unchanged; existing deployments see no difference.
- Unrelated: unit tests in this repo need `BOW_DATABASE_URL` set when run
  outside CI because `configs/bow-config.dev.yaml` reads it from the
  environment; `tests/unit/test_db_pool_config.py` fails the same way with
  the change stashed.
