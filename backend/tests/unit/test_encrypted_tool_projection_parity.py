"""Conversation history must read identically with and without encryption.

``ToolExecution.result_json`` holds whole datasets, so history projects a small
digest out of it *in SQL* rather than hydrating megabytes. SQL cannot see inside
an encrypted payload, so those rows are hydrated through the ORM and projected
in Python instead.

Two projections, one contract: these tests pin the Python side to the shape the
SQL side produces, so switching encryption on cannot quietly change what the
planner sees.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Text, select

from app.ai.context.builders.message_context_builder import (
    MessageContextBuilder,
    _project_create_data_result,
    _project_read_query_result,
)
from app.dependencies import async_session_maker
from app.ee.encryption import types as enc
from app.models.tool_execution import ToolExecution


CREATE_DATA_RESULT = {
    "success": True,
    "query_id": "q-1",
    "stats": {"rows": 3},
    "data_model": {"columns": [{"name": "region"}]},
    "view": {"type": "bar"},
    "created_visualization_ids": ["v-1"],
    "codegen_ms": 120,
    "execution_ms": 45,
    "data": {
        "columns": [{"field": "region"}, {"field": "revenue"}],
        "rows": [
            {"region": "EMEA", "revenue": 636269.0},
            {"region": "APAC", "revenue": 573129.0},
            {"region": "LATAM", "revenue": 551309.0},
        ],
    },
}

READ_QUERY_RESULT = {
    "success": True,
    "results": [
        {"query_id": "q-1", "visualization_id": "v-1", "title": "By region",
         "data_preview": {"columns": ["region"], "rows": [{"region": "EMEA"}]}},
        {"query_id": "q-2", "title": "By month", "error": None},
    ],
    "errors": None,
}


async def _insert(db, result_json, *, tool_name, encrypted):
    tid = str(uuid.uuid4())
    original = enc.encryption_active
    enc.encryption_active = (lambda: encrypted)
    try:
        await db.execute(ToolExecution.__table__.insert().values(
            id=tid, agent_execution_id="ae-" + tid, tool_name=tool_name,
            status="success", success=True, arguments_json={},
            result_json=result_json,
        ))
        await db.commit()
    finally:
        enc.encryption_active = original
    return tid


async def _cleanup(db, ids):
    await db.execute(ToolExecution.__table__.delete().where(
        ToolExecution.__table__.c.id.in_(ids)))
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name,result,projector,loader", [
    ("create_data", CREATE_DATA_RESULT, _project_create_data_result,
     "_load_create_data_result_projections"),
    ("read_query", READ_QUERY_RESULT, _project_read_query_result,
     "_load_read_query_result_projections"),
])
async def test_encrypted_row_projects_like_the_plaintext_sql_path(
    tool_name, result, projector, loader
):
    """Same input, one row plaintext and one encrypted: same digest out."""
    async with async_session_maker() as db:
        plain_id = await _insert(db, result, tool_name=tool_name, encrypted=False)
        enc_id = await _insert(db, result, tool_name=tool_name, encrypted=True)

        # Only the encrypted row should be opaque to SQL.
        raw = dict((await db.execute(
            select(ToolExecution.__table__.c.id,
                   ToolExecution.__table__.c.result_json.cast(Text))
            .where(ToolExecution.__table__.c.id.in_([plain_id, enc_id]))
        )).all())
        assert enc.ENVELOPE_MARKER not in raw[plain_id]
        assert enc.ENVELOPE_MARKER in raw[enc_id]

        builder = MessageContextBuilder(db, None, None)
        projections = await getattr(builder, loader)([plain_id, enc_id])

        assert set(projections) == {plain_id, enc_id}, (
            "the encrypted row must not drop out of the projection"
        )
        assert projections[enc_id] == projections[plain_id], (
            "encryption changed what conversation history sees"
        )
        await _cleanup(db, [plain_id, enc_id])


def test_create_data_projection_bounds_rows_to_the_first():
    """The digest must never carry the whole dataset into the prompt."""
    projected = _project_create_data_result(CREATE_DATA_RESULT)
    preview = projected["data_preview"]
    assert preview["rows"] == [CREATE_DATA_RESULT["data"]["rows"][0]]
    assert preview["row_count"] == 3
    assert "data" not in projected


def test_read_query_projection_falls_back_to_top_level_fields():
    """Mirrors the SQL COALESCE(item, top-level) behavior."""
    projected = _project_read_query_result(
        {"success": False, "query_id": "top", "error": "boom", "results": None}
    )
    assert projected["success"] is False
    assert projected["results"] == [
        {"query_id": "top", "visualization_id": None, "title": None,
         "data_preview": None, "error": "boom"}
    ]


def test_projections_tolerate_junk():
    assert _project_create_data_result(None) == {}
    assert _project_read_query_result(None) == {
        "success": None, "results": [], "errors": None
    }
