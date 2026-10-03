#!/usr/bin/env python3
"""Customer/simulator read-only acceptance runner. Report contains no row data.

Run from backend with PYTHONPATH=. and credentials in environment variables.
Only explicit queries in the local input JSON are executed. No broad crawl.
"""

import argparse
import json
import os
from pathlib import Path
from app.data_sources.clients.netapp_ontap_client import (
    NetAppOntapClient,
    OntapQueryError,
)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--queries", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--simulator", action="store_true")
    args = p.parse_args()
    queries = json.loads(args.queries.read_text())
    if not isinstance(queries, list) or not 1 <= len(queries) <= 30:
        p.error("Provide 1–30 explicit bounded queries.")
    client = NetAppOntapClient(
        url=os.environ["NETAPP_URL"],
        username=os.environ["NETAPP_USERNAME"],
        password=os.environ["NETAPP_PASSWORD"],
        ca_certificate=os.environ.get("NETAPP_CA_PEM"),
        allow_http=args.simulator,
    )
    report = {
        "environment": "synthetic_api" if args.simulator else "customer_appliance",
        "contract": "9.14.1",
        "checks": [],
    }
    try:
        client.test_connection()
        for query in queries:
            result = {"table": query.get("table")}
            try:
                frame = client.execute_query(query)
                result.update(
                    status="pass",
                    rows=len(frame),
                    columns=list(frame.columns),
                    complete=True,
                )
            except OntapQueryError as e:
                result.update(status="fail", code=e.code, http_status=e.status)
            report["checks"].append(result)
        report["coverage"] = client.coverage_report()
    except OntapQueryError as e:
        report["connection_error"] = {"code": e.code, "http_status": e.status}
    finally:
        client.close()
    # Deliberately omit URL, usernames, query values, object IDs and result rows.
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    args.output.chmod(0o600)
    passed = sum(x["status"] == "pass" for x in report["checks"])
    print(
        f"{passed}/{len(queries)} queries passed ({report['environment']}); report: {args.output}"
    )
    return 0 if passed == len(queries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
