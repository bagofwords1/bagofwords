# Teradata stand-in for sandbox runs

No Teradata server can run in a sandbox or CI (Vantage Express is a licensed VM
image; ClearScape Analytics needs an account), so the Teradata connector is
verified against a fake of the `teradatasql` driver:
`backend/tests/mocks/fake_teradatasql.py`. It is DuckDB-backed and reproduces the
driver and database behaviour the connector depends on (parameter validation,
error text, DBC dictionary views, HELP COLUMN, cancel). The unit tests load it
directly; this folder lets the full app stack load it too.

```bash
PYTHONPATH="$PWD/tools/teradata/fake_driver" FAKE_TERADATA_STATS=/tmp/td-stats.json \
  tools/agent/boot_stack.sh
```

Then add a **Teradata Vantage** connection with:

| Field | Value |
|---|---|
| Host | `td.example.com` (`unreachable.invalid` simulates a DNS failure) |
| Database | `sales, finance, hr` |
| User / Password | `bow_reader` / `Reader#2026` |

What the fake holds: `sales.orders`, `sales.customers`, `sales.order_events`
(no primary index), `sales.v_monthly_revenue` (a view), `sales.slow_scan` (every
scan runs until cancelled), `finance.product_sales`, `finance.v_broken` (a view
over a dropped table), and `hr.salaries`, which the user cannot read.
`FAKE_TERADATA_QVCI=1` turns on DBC.ColumnsQV(X).

`/tmp/td-stats.json` counts logons, logoffs, requests, cancels and any write that
reached the driver, so a run can be checked from outside the backend process.
