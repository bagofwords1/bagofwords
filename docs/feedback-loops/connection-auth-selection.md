# Feedback Loop — authentication selection is ignored

Connection setup can silently use a driver's default authentication mode even
when the user selects another mode. The regression checks preserve the selected
mode across new-connection testing, saved connections, and runtime construction.

## Root cause (validated)

`backend/app/services/data_source_service.py:3435` classified `auth_type` as
form metadata and removed it after merging credentials into configuration.
`backend/app/models/connection.py:205` did the same for saved connections.
The runtime construction paths also excluded this field. Drivers consequently
received no authentication selection and used their constructor defaults.

## Loop A — deterministic reproduction

From a checkout with backend dependencies installed:

```sh
cd backend
TESTING=true .venv/bin/python -m pytest tests/unit/test_connection_auth_selection.py -q
```

The tests replace only the external driver boundary. Three authentication modes
are exercised through three public construction surfaces with explicit,
forwarding, and unsupported constructor signatures. Credentials must override
form-level authentication metadata; clients without the parameter must continue
to construct successfully. No database or external service is required.

With the production files restored to their pre-fix versions and the regression
test retained: **18 failed, 9 passed**. Authentication-aware drivers received the
default instead of the selected mode. With the fix: **27 passed**.

## The fix

Preserve authentication selection when filtering runtime parameters. In the two
paths without general signature filtering, remove `auth_type` only when the
constructor accepts neither that argument nor arbitrary keyword arguments.
Existing signature-aware runtime paths retain the selection automatically.

## What this proves / regression notes

The selected authentication mode reaches compatible driver constructors, and
unsupported constructors remain compatible. This does not validate credentials
or authentication configuration on a live external server.
