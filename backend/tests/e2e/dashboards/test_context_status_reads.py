"""A status poll must not fetch complete query grids to count waiting inputs.

Observe real SQL at the driver boundary; the endpoint, ORM and summary writer
all run normally. The legacy case exercises releases written before summaries.
"""
import re

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine

from tests.e2e.dashboards.test_context_publish import (
    WAITING_TITLE, _copies, _status, _step_title, context_dash, drawn, spawned,  # noqa: F401
)


@pytest.mark.parametrize("legacy", [False, True])
def test_status_uses_bounded_summaries_and_preserves_legacy_waits(
        corpus, corpus_rows, test_client, context_dash, legacy):  # noqa: F811
    aid, release = context_dash
    copies = _copies(corpus_rows, release)
    label = _step_title(corpus_rows, copies[WAITING_TITLE]["step"])
    if legacy:
        # Only old workers can leave a stored result without its projection.
        corpus_rows.update("steps", copies[WAITING_TITLE]["step"], context_summary_json=None)
    full_reads = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selected = statement.split("FROM", 1)[0]
            if re.search(r"\bsteps\.data\b", selected):
                full_reads.append(statement)

    event.listen(Engine, "before_cursor_execute", observe)
    try:
        status = _status(test_client, corpus, aid)
    finally:
        event.remove(Engine, "before_cursor_execute", observe)
    assert status["published"]["steps_needing_input_labels"] == [label]
    assert len(full_reads) == int(legacy), "only a legacy row may need its full grid"
