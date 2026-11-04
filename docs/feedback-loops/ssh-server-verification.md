# SSH setup: missing server key and raw validation error

A customer supplied host and login details but could not connect because the
server key was missing. The form required manual public-key entry and surfaced
a raw Pydantic error. The new flow discovers candidate keys without credentials,
shows fingerprints, and requires an explicit independent verification before trust.

## Validated cause and reproduction

`RemoteServerClient.__init__` directly propagated configuration validation errors;
connection-test services returned their raw string. The form offered no discovery
or trust action and displayed the required host-key textarea beside login details.

`test_missing_host_key_has_actionable_error_without_validator_details` fails
against the prior client: the result contains `1 validation error`, `Field required`
and a Pydantic URL. With the fix it passes and the message describes the next action.

```bash
cd backend
TESTING=true BOW_DATABASE_URL=sqlite+aiosqlite:////tmp/ssh-setup-test.db \
  uv run pytest tests/unit/test_ssh_host_keys.py \
  tests/e2e/rbac/test_ssh_host_keys.py tests/unit/test_remote_server_client.py \
  -q --disable-warnings
```

Observed: **42 passed**. The broader connector/policy/gateway/data-source suite
passed **152 tests with 2 credential-gated skips** in 42.48 seconds. It emitted
warnings; no full image build or Windows acceptance run was performed.

## Implementation and trust boundary

- `backend/app/services/ssh_host_keys.py`: validated host/port, resolve and check
  egress once, pin the keyscan subprocess to a validated address, bound time/output,
  validate wire-format public keys, return SHA-256 fingerprints with `trusted=false`.
- `backend/app/routes/connection.py`: authenticated, permission-gated discovery;
  credentials and unknown request fields are rejected. The route writes no state.
- `frontend/components/datasources/SSHServerVerification.vue`: pending candidates
  are separate from the saved key, explicit confirmation, destination invalidation,
  stale-response rejection, and readable saved fingerprint.
- `ConnectForm.vue`: main login fields, upload into the existing credential field,
  friendly preflight messages, and manual trusted-key entry under Advanced.
- All new UI/error keys are present in all ten locale catalogs.

Server key discovery is not authentication or verification. The administrator
must compare the fingerprint with a trusted source. Existing pinned SSH transport
verification remains mandatory. No automatic acceptance on first use or key change.

## Live verification

The discovery service was run against the existing authorized SSH alias. At least
one discovered key exactly matched the local trusted known_hosts record, and the
response remained untrusted. No login credentials were used by discovery and no
remote files were changed. Unit/API tests cover valid fingerprints, invalid keys,
malformed destinations, metadata denial, secret-field rejection, and unauthorized
and member requests denied before subprocess execution.

Browser verification exercised missing-key validation, invalid and valid synthetic
private-key upload, explicit trust, exact key submission, destination invalidation,
stale discovery rejection, manual Advanced entry, and Hebrew layout. Screenshots
and the flow recording are in `media/pr/ssh-server-verification/`.

After adding the connection-scoped discovery route, the final focused unit/API run
passed **11 tests** (320 warnings). The first scoped test attempted a real SSH
connection during fixture setup; reusing the external SSH executable fixture fixed
the test isolation. Both unauthorized members and authorized administrators were
checked against the scoped route.
