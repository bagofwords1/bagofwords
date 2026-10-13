# Feedback Loop — alert-definition POST denied by access policy

Date: 2026-10-13

Reported behavior: "currently it all works well there besides this POST (due to
access) and we need to switch to GET". This change makes the `alert_definitions`
table use `GET /suite-api/api/alertdefinitions` directly. It preserves the
connector's filters and result shape without depending on POST query access.

## Root cause (validated)

Before the change, `backend/app/data_sources/clients/aria_operations_client.py:936`
unconditionally called `POST alertdefinitions/query`. There was no GET fallback.
The HTTP boundary reproduction denies this POST with 403 and permits GET;
all 26 parameterized cases failed with:

```text
RuntimeError: Aria Operations authorization failed (403) for POST alertdefinitions/query.
26 failed, 53 deselected
```

This validates the connector behavior under the reported restriction. It does
not independently identify the customer's appliance/proxy permission policy.

## Documented contract

- [vRealize Operations GET alert definitions](https://developer.broadcom.com/xapis/vmware-vrealize-operations-api/latest/api/alertdefinitions/get/)
- [VCF Operations GET alert definitions](https://developer.broadcom.com/xapis/vcf-operations-api/latest/suite-api/api/alertdefinitions/get/)

GET supports scalar `adapterKind` and `resourceKind`, multiple `id` values, and
`page`/`pageSize`. IDs must not be combined with kind parameters. The response
contains `alertDefinitions` and `pageInfo`. The docs warn that unsatisfied
criteria can return all definitions, so the client also enforces filters locally.

## Loop A — deterministic reproduction and verification

From a Python 3.12 environment with the backend development dependencies installed:

```bash
cd backend
uv run pytest tests/unit/test_aria_operations_client.py -k read_only_access -q --tb=short --disable-warnings
uv run pytest tests/unit/test_aria_operations_client.py tests/unit/test_aria_mock_contract.py -q --tb=short --disable-warnings
```

Locally executed with `.venv/bin/python -m pytest` using the existing backend
environment. The regression test is
`test_alert_definitions_read_only_access_preserves_filters_and_pagination`.
It stubs only the external HTTP boundary: POST query is forbidden, GET follows
the documented parameter contract, and pages contain at most one definition.
It covers unfiltered reads, scalar/list kind filters, scalar/list IDs, combined
ID/kind filters, case-insensitive search, no matches, and limits of 1 and 10.

Observed after the fix:

```text
81 passed, 230 warnings in 0.25s
```

## The fix

`backend/app/data_sources/clients/aria_operations_client.py:926` now sends GET
with URL query parameters and no request body. Single-kind filters and IDs
are sent to the server where supported. Multi-kind and combined ID/kind filters
are applied locally; all filters run before the result limit across pages.
The flattened columns and nested `states` JSON are unchanged.

## What this proves / regression notes

The HTTP-boundary regression fails before the fix and passes afterward. The
full connector suite and in-process FastAPI simulator contract checks pass.
Authentication and other connector endpoints are unchanged. Multi-kind reads
may fetch more definitions because GET accepts only one kind per parameter.

The simulator is synthetic, not an appliance compatibility certificate. No real
vROps appliance was available for this verification. On the target appliance,
run the same user queries using its existing connection identity, check that
alert definitions load, and confirm requests use GET rather than POST query.
The earlier connector feedback-loop document's GET-fallback claim was inaccurate
for alert definitions; this change uses GET directly rather than a fallback.
