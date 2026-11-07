"""Hand a data source's notes on a query result to the planner.

A connector can attach a note to a result frame (``df.attrs["bow_notice"]``,
see ``code_execution.QUERY_NOTICE_ATTR``): the row limit cut the result off,
the request actually sent differs from the query as written, the rows were
already flattened. The query wrapper copies each note onto that query's timing
record, and this puts them on the tool observation as ``data_notices``, with a
pointer in the one-line ``summary``. Printed only into the run log, the note
was easy to miss: an agent read "5 rows" and told the user the 5 rows were all
of them.
"""
from typing import Any, Dict, List

from app.ai.code_execution.code_execution import collect_query_notices

DATA_NOTICES_KEY = "data_notices"

_SUMMARY_FLAG = (
    " The data source flagged this result; read data_notices before describing it"
    " (it may be incomplete, e.g. cut off at a row limit, or answer a rewritten query)."
)


def attach_data_notices(observation: Dict[str, Any], query_timings) -> List[str]:
    """Put the execution's source notices on ``observation``; return them."""
    notices = collect_query_notices(query_timings)
    if notices:
        observation[DATA_NOTICES_KEY] = notices
        summary = observation.get("summary")
        if isinstance(summary, str):
            observation["summary"] = summary + _SUMMARY_FLAG
    return notices
