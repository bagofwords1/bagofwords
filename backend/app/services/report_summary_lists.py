"""Fold a report's submit_list executions into one summary item per list."""
from __future__ import annotations

from typing import Any, Iterable, Optional, Tuple

from app.schemas.report_summary_schema import SummaryListItem, SummaryListSubmission


def aggregate_list_submissions(
    executions: Iterable[Tuple[str, str, Optional[dict[str, Any]]]],
) -> list[SummaryListItem]:
    """``executions`` are ``(tool_execution_id, completion_id, result_json)`` in
    conversation order. Rejected submissions saved nothing and are skipped.
    Items keep the order in which each list was first written to.
    """
    items: dict[str, SummaryListItem] = {}
    row_action: dict[str, dict[str, str]] = {}  # list_id -> row_id -> net action
    for te_id, completion_id, rj in executions:
        rj = rj or {}
        list_id = rj.get("list_id")
        if not rj.get("success") or not list_id:
            continue
        list_id = str(list_id)
        item = items.get(list_id)
        if item is None:
            item = items[list_id] = SummaryListItem(
                list_id=list_id,
                list_name=rj.get("list_name") or "",
                data_source_id=str(rj.get("data_source_id") or ""),
            )
            row_action[list_id] = {}
        item.submissions.append(
            SummaryListSubmission(tool_execution_id=str(te_id), message_id=str(completion_id))
        )
        actions = row_action[list_id]
        for row in rj.get("rows") or []:
            row_id = row.get("row_id")
            action = row.get("action")
            if not row_id or action not in ("inserted", "updated"):
                continue
            # A row this conversation added stays "added" whatever follows.
            if actions.get(str(row_id)) != "inserted":
                actions[str(row_id)] = action

    for list_id, item in items.items():
        actions = row_action[list_id].values()
        item.inserted = sum(1 for a in actions if a == "inserted")
        item.updated = sum(1 for a in actions if a == "updated")
    return list(items.values())
