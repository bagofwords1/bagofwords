"""A real streamable-HTTP MCP server returning a LARGE, awkwardly-shaped result.

This is the oracle for the "execute_mcp result must reach write_csv / create_data"
feedback loop. It reproduces the shape that an ERP-style MCP tool actually returns
and that the tabular detector historically failed on:

  * the rows are NOT at a conventional envelope key (``data``/``results``/...) —
    they sit under a domain-specific key (``WorkOrdersMFG``),
  * a SECOND array field (``validationWarnings``) sits beside them, so a detector
    that only accepts a *sole* unconventional row-list candidate gives up,
  * the payload is large (hundreds of thousands of characters), so it can only
    travel to downstream tools as a materialized file, never inline,
  * records are wide and contain nulls, nested objects and mixed types, exactly
    like a real ERP row.

Run standalone::

    uv run python tests/mocks/bulk_orders_mcp_server.py --port 3335 --rows 500

Then point a ``type=mcp`` connection at ``http://localhost:3335/mcp``
(transport ``streamable_http``).
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import date, datetime, timedelta
from typing import Any, Optional

from mcp.server.fastmcp import Context, FastMCP

mcp = FastMCP("mock-mfg")

# Fixed so successive calls (and successive runs) return identical data — a
# feedback loop that compares before/after needs the payload to be stable.
_SEED = 20260728

_PLANNERS = [
    ("P-101", "Dana Whitfield"),
    ("P-102", "Marcus Oyelaran"),
    ("P-103", "Ingrid Sollberg"),
    ("P-104", "Tomas Reyes"),
    ("P-105", "Aiko Nakamura"),
]

_ITEMS = [
    ("FRM-7710", "Aluminium frame, 54cm, matte"),
    ("WHL-3320", "Wheelset 700c, sealed bearing"),
    ("DRV-1180", "Drivetrain assembly, 11-speed"),
    ("BRK-4405", "Hydraulic disc brake, front"),
    ("SDL-2210", "Saddle, gel-injected, unisex"),
    ("HBR-8890", "Handlebar, carbon, 420mm"),
    ("FRK-5560", "Suspension fork, 120mm travel"),
]

_WAREHOUSES = ["101", "102", "205", "310"]
_STATUSES = ["planned", "released", "in_process", "completed", "on_hold"]
_LINES = ["ASSY-A", "ASSY-B", "PAINT-1", "WELD-2"]


def _iso(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _stamp(d: date, hour: int) -> str:
    return datetime(d.year, d.month, d.day, hour, 5, 24).strftime("%Y-%m-%dT%H:%M:%SZ")


def _work_order(index: int, rng: random.Random, week_start: date) -> dict[str, Any]:
    """One wide ERP-style record: many fields, nulls, mixed types, nested bits."""
    planner_code, planner_name = _PLANNERS[index % len(_PLANNERS)]
    item_code, item_desc = _ITEMS[index % len(_ITEMS)]
    offset = index % 7
    planned = week_start + timedelta(days=offset)
    requested = planned + timedelta(days=rng.choice([0, 0, 1, 2]))
    started = planned - timedelta(days=rng.randint(1, 4))
    status = _STATUSES[index % len(_STATUSES)]
    completed = status == "completed"

    return {
        "orderNumber": f"WO{900000 + index}",
        "orderType": "production",
        "company": "3200",
        "item": item_code,
        "itemDescription": item_desc,
        "planner": planner_code,
        "plannerName": planner_name,
        "quantityOrdered": rng.randint(5, 480),
        "quantityCompleted": rng.randint(0, 120) if completed else 0,
        "quantityRejected": rng.choice([0, 0, 0, 1, 3]),
        "unitOfMeasure": "pcs",
        "warehouse": _WAREHOUSES[index % len(_WAREHOUSES)],
        "productionLine": _LINES[index % len(_LINES)],
        "plannedDeliveryDate": _iso(planned),
        "requestedDeliveryDate": _iso(requested),
        "productionStartDate": _stamp(started, 6),
        "actualProductionStartDate": _stamp(started, 7) if status != "planned" else None,
        "actualDeliveryDate": _stamp(planned, 15) if completed else None,
        "confirmedDeliveryDate": _iso(planned) if status != "planned" else None,
        "completionDate": _stamp(planned, 16) if completed else None,
        "actualClosingDate": None,
        "status": status,
        "definitivelyClosed": "no",
        "operationsBlocked": rng.choice(["no", "no", "yes"]),
        "backflushMaterials": "yes",
        "backflushHours": "no",
        "useActualCostPriceForReceiptPosting": "yes",
        "calculationOffice": f"C{1990 + (index % 12)}",
        "calculationDate": None,
        "costingReportsPrinted": ["no", "no", "no", "no"],
        "checklistPrinted": "original.doc",
        "cuttingListPrinted": "original.doc",
        "coveringNotePrinted": "no.action",
        "updateMethod": "no.action",
        "planningMethod": "forward",
        "lotCode": "",
        "referenceDate": _stamp(started, 8),
        "estimatedCostsAreFrozen": "no",
        "routingRevision": "",
        "containsFMFMaterial": "no",
        "containsOperationSteps": "no",
        "excludeFromWIPAutomationReceipt": "no",
        "readyForTransferToPurchase": "no",
        "handledByMES": "no",
        "mesMigrationStatus": "na",
        "faiRequiredForThisWorkOrder": "no",
        "dpas": "No",
        "lru": "",
        "modelItem": "",
        "activeForCollection": "yes",
        "pegData": [
            {
                "project": f"A{70 + (index % 30)}",
                "activity": f"PCA{7000 + index}",
                "distribution": "pegs",
            }
        ],
        "salesOrder": f"SO{40000 + (index * 3 % 9000)}" if index % 4 else None,
        "customerReference": f"CUST-{2200 + (index % 55)}",
        "priority": rng.choice([1, 2, 2, 3, 5]),
        "remarks": None if index % 5 else "expedite; customer escalation",
    }


def _build_payload(rows: int) -> dict[str, Any]:
    """The exact awkward envelope: rows at a domain key, warnings beside them."""
    rng = random.Random(_SEED)
    # Anchor the "next week" window so the data is stable and self-describing.
    week_start = date(2026, 8, 3)
    orders = [_work_order(i, rng, week_start) for i in range(rows)]
    return {
        "WorkOrdersMFG": orders,
        # A second list of records at the top level. This is what makes the row
        # list ambiguous to a "sole candidate" detector.
        "validationWarnings": [
            {
                "code": "W-1180",
                "severity": "info",
                "message": "3 orders have no confirmed delivery date",
            },
            {
                "code": "W-2245",
                "severity": "warning",
                "message": "Routing revision empty for 12 orders",
            },
        ],
        "queryWindow": {"from": "2026-08-03", "to": "2026-08-09"},
        "recordCount": len(orders),
        "truncated": False,
    }


_ROWS = 500


@mcp.tool(
    description=(
        "Query manufacturing work orders for a production week. Returns every "
        "work order scheduled in the window with planner, item, quantities and "
        "dates."
    ),
)
def query_work_orders_with_prompt(
    prompt: str,
    company: str = "",
    custom_metadata: Optional[dict] = None,
    ctx: Context = None,
) -> str:
    return json.dumps(_build_payload(_ROWS))


@mcp.tool(
    description=(
        "Return the free-text shift handover notes for the production week. "
        "Plain prose, no structure."
    ),
)
def get_shift_notes(company: str = "", ctx: Context = None) -> str:
    """A text result — the branch that used to produce no artifact at all."""
    paragraphs = [
        f"Shift handover, line {line}: shift lead reported the changeover ran "
        f"{minutes} minutes over plan. Torque calibration on station 4 was "
        "repeated after the first pass drifted out of tolerance. Material "
        "staging for the next run is complete; the pallet of FRM-7710 arrived "
        "short by two units and has been flagged to purchasing."
        for line, minutes in zip(_LINES, (12, 4, 27, 9))
    ]
    return "\n\n".join(paragraphs * 12)


@mcp.tool(
    description=(
        "Return the raw machine event log for the production week as plain text. "
        "One event per line, regular format, not delimited."
    ),
)
def get_machine_log(company: str = "", ctx: Context = None) -> str:
    """A text result with a REGULAR pattern.

    Deliberately not delimited — `pd.read_csv` cannot make a table of this, so
    the only route is reading the text and parsing it. That makes it the test
    for whether generated code can read a text file at all.
    """
    rng = random.Random(_SEED + 1)
    reasons = ["tool_change", "material_wait", "operator_break", "jam_clear", "qa_hold"]
    lines = []
    for i in range(240):
        line = _LINES[i % len(_LINES)]
        hour = 6 + (i % 12)
        reason = reasons[i % len(reasons)]
        lines.append(
            f"[2026-08-{3 + (i % 7):02d} {hour:02d}:{(i * 7) % 60:02d}] "
            f"{line} WO{900000 + (i % 430)} STOP {reason} {rng.randint(2, 45)}min"
        )
    return "\n".join(lines)


@mcp.tool(
    description="Return the current status of each production line. Small JSON object.",
)
def get_line_status(company: str = "", ctx: Context = None) -> str:
    """A small JSON result — under the preview cap, so it used to get no file."""
    return json.dumps({
        "asOf": "2026-08-03T06:00:00Z",
        "plantStatus": "running",
        "openIncidents": 0,
        "shiftPattern": "2x8",
    })


def main() -> None:
    global _ROWS
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=3335)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--rows", type=int, default=500)
    parser.add_argument(
        "--dump",
        action="store_true",
        help="Print the payload size and exit (no server).",
    )
    args = parser.parse_args()
    _ROWS = args.rows

    if args.dump:
        blob = json.dumps(_build_payload(args.rows))
        print(f"rows={args.rows} chars={len(blob):,}")
        return

    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.run(transport="streamable-http")  # served at http://host:port/mcp


if __name__ == "__main__":
    main()
