# Feedback Loop — authentication selection is ignored

Connection setup must preserve credential-level authentication settings without
forwarding the registry form selector as a driver's authentication argument.

## Root cause and review findings

The original parameter filtering removed `auth_type` after merging credentials,
forcing driver defaults. The initial PR retained the merged key, but review
correctly identified a regression: when credentials omit the key, the config's
form selector could replace the driver's default. Review also identified the
same original filtering bug in the personal-credential test path.

## Deterministic reproduction

With backend dependencies installed:

```sh
cd backend
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db .venv/bin/python -m pytest tests/unit/test_connection_auth_selection.py tests/unit/test_egress_callers.py -q
```

The regression matrix uses registered test drivers at the external driver
boundary. It exercises three explicit selections plus missing and empty settings,
four constructor signatures (explicit, forwarding, unsupported, opaque), and
four public surfaces (new connection, saved connection, runtime, personal test).
No live server or database is needed.

Observed evidence:

- Original 27-case regression against the pre-PR implementation: 18 failed,
  9 passed; initial fix: 27 passed.
- Expanded 80-case regression against the initial PR: 31 failed, 49 passed.
- Revised implementation plus egress caller checks: 106 passed.
- Mutation that bypasses signature filtering: 12 unsupported-argument cases
  fail; the 8 missing/empty-setting cases correctly remain green.

## Fix and context map

- `backend/app/data_sources/client_parameters.py`: remove the config form
  selector before merging credentials; missing or empty credential settings
  leave driver defaults intact. Centralize signature filtering, retain keyword
  forwarders, and tolerate TypeError/ValueError for opaque signatures.
- `backend/app/models/connection.py`: use the helpers for saved connections.
- `backend/app/services/data_source_service.py`: use the same merge policy for
  new tests, runtime clients, and catalog clients.
- `backend/app/services/user_data_source_credentials_service.py`: apply the
  policy to personal credential tests, including keyword-forwarding drivers.
- `backend/tests/unit/test_connection_auth_selection.py`: public-surface matrix.

Only legacy paths that previously discarded the config selector use this merge
policy. Tool-provider construction that deliberately consumes config-level
settings is outside this change. Existing full-argument filtering stays full;
paths that only filtered authentication continue to filter only that argument.

## Verification limits

No live credentials or server configuration were tested. The unsupported driver
uses its own fixed default, and mutation testing proves the compatibility checks
fail if unsupported arguments are forwarded. Passing against the old code is
expected for compatibility cases; the failure check is against a relevant fault.
