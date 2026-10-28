"""Second wave of encrypted-at-rest columns: conversation text, agent context
snapshots, artifact content, applied params and entity code.

The contract is the one ``EncryptedJSON`` established (tests/unit/
test_encrypted_payloads.py) extended to ``Text``/``String`` columns:

* writes encrypt only while the gate is open;
* reads never consult the gate — stored ciphertext always decrypts, and a
  plaintext row written before the feature existed reads back untouched;
* the table may hold both kinds of row at once, and every reader (ORM *and*
  the SQL prefilters that look inside these columns) must cope with that.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Text, select
from sqlalchemy.dialects import postgresql, sqlite

from app.ee.encryption import (
    TEXT_ENVELOPE_PREFIX,
    EncryptedJSON,
    EncryptedString,
    EncryptedText,
    envelope_marker_expr,
    is_encrypted_envelope,
    is_encrypted_text,
    text_envelope_expr,
)
from app.ee.encryption import types as enc
from app.models.agent_execution import AgentExecution
from app.models.completion import Completion
from app.models.context_snapshot import ContextSnapshot
from app.models.entity import Entity
from app.models.report_context_state import ReportContextState
from app.models.step import Step
from app.models.step_user_result import StepUserResult
from tests.fixtures.encryption import seed_encryption_graph as _seed_graph


@pytest.fixture
def encrypting(monkeypatch):
    monkeypatch.setattr(enc, "encryption_active", lambda: True)
    return enc


@pytest.fixture
def not_encrypting(monkeypatch):
    monkeypatch.setattr(enc, "encryption_active", lambda: False)
    return enc


SQL = "SELECT region, SUM(revenue) AS revenue FROM sales WHERE region = 'EMEA' GROUP BY 1"
LONG_SQL = "\n".join(f"-- line {i}: revenue for EMEA customer {uuid.uuid4()}" for i in range(40))


# --------------------------------------------------------------------------
# EncryptedText / EncryptedString: the type contract
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value", ["", "x", SQL, LONG_SQL, "unicode ✓ שלום"], ids=["empty", "one-char", "sql", "long", "unicode"]
)
def test_text_round_trip_preserves_value(value):
    envelope = enc.encrypt_text(value)
    assert is_encrypted_text(envelope)
    assert enc.decrypt_text(envelope) == value


def test_text_envelope_hides_the_payload():
    envelope = enc.encrypt_text(LONG_SQL)
    assert "EMEA" not in envelope
    assert "revenue" not in envelope


def test_long_text_is_compressed_and_short_text_is_not():
    short = enc.encrypt_text("x").split("$")
    long = enc.encrypt_text(LONG_SQL).split("$")
    assert short[3] == "0"
    assert long[3] == "1"
    assert len(enc.encrypt_text(LONG_SQL)) < len(LONG_SQL)


@pytest.mark.parametrize("column_type", [EncryptedText, EncryptedString])
def test_text_bind_encrypts_only_when_active(column_type, encrypting):
    bound = column_type().process_bind_param(SQL, None)
    assert is_encrypted_text(bound)
    assert column_type().process_result_value(bound, None) == SQL


@pytest.mark.parametrize("column_type", [EncryptedText, EncryptedString])
def test_text_bind_passes_through_when_inactive(column_type, not_encrypting):
    assert column_type().process_bind_param(SQL, None) == SQL


def test_text_bind_preserves_none_and_non_strings(encrypting):
    t = EncryptedText()
    assert t.process_bind_param(None, None) is None
    # A plaintext column would hand a non-string to the driver unchanged.
    assert t.process_bind_param(42, None) == 42


def test_text_bind_does_not_double_wrap(encrypting):
    once = EncryptedText().process_bind_param(SQL, None)
    assert EncryptedText().process_bind_param(once, None) == once


def test_plaintext_text_rows_read_back_untouched(not_encrypting):
    assert EncryptedText().process_result_value(SQL, None) == SQL
    assert EncryptedText().process_result_value("$not quite an envelope", None) == "$not quite an envelope"


def test_stored_text_ciphertext_still_reads_when_the_feature_is_switched_off(not_encrypting):
    envelope = enc.encrypt_text(SQL)
    assert EncryptedText().process_result_value(envelope, None) == SQL


def test_unreadable_text_degrades_to_none_rather_than_raising():
    bogus = TEXT_ENVELOPE_PREFIX + "1$0$" + "gAAAAA" + "A" * 60
    assert EncryptedText().process_result_value(bogus, None) is None
    assert EncryptedText().process_result_value(TEXT_ENVELOPE_PREFIX + "garbage", None) is None


def test_ddl_types_are_unchanged():
    """Adopting the feature must not need a migration: the DDL type of each
    column stays what it was, so Alembic autogenerate sees nothing."""
    assert EncryptedText.impl is Text
    assert EncryptedString.impl.__name__ == "String"
    assert EncryptedJSON.impl.__name__ == "JSON"


# --------------------------------------------------------------------------
# SQL-side envelope detection, both dialects
# --------------------------------------------------------------------------

def _compile(expr, dialect):
    return str(expr.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))


def test_json_marker_expression_compiles_on_both_dialects():
    pg = _compile(envelope_marker_expr(Completion.prompt), postgresql.dialect())
    lite = _compile(envelope_marker_expr(Completion.prompt), sqlite.dialect())
    assert "->>" in pg and enc.ENVELOPE_MARKER in pg and "IS NOT NULL" in pg
    assert "JSON_EXTRACT" in lite.upper() and enc.ENVELOPE_MARKER in lite and "IS NOT NULL" in lite


def test_text_marker_expression_is_a_literal_prefix_like():
    """The prefix contains ``_``, a LIKE wildcard; the expression must escape
    it so ``$bowXenc$...`` plaintext is not flagged."""
    for dialect in (postgresql.dialect(), sqlite.dialect()):
        sql = _compile(text_envelope_expr(Entity.code), dialect)
        assert "LIKE" in sql.upper()
        assert "ESCAPE" in sql.upper()
        assert "bow" in sql and "enc$" in sql


# --------------------------------------------------------------------------
# Database round-trips through the real models
# --------------------------------------------------------------------------

PROMPT = {"content": "show revenue by region for EMEA", "mentions": []}
ANSWER = {"content": "EMEA revenue is 1,234,567", "reasoning": "..."}
CONTEXT_VIEW = {
    "static": {},
    "warm": {"widgets": {"items": [{"data_preview": "region | revenue\nEMEA | 1234567"}]}},
}
SUMMARY = {"goal": "EMEA revenue", "critical_context": ["EMEA revenue is 1,234,567"]}
PARAMS = {"region": "EMEA", "customer_id": "c-42"}


def _insert(model, defaults: dict, over: dict):
    """A Core INSERT with the row's required columns; ``over`` wins."""
    return model.__table__.insert().values(**{**defaults, **over})


def _completion(cid, g, **over):
    return _insert(Completion, {
        "id": cid, "report_id": g["report"], "prompt": PROMPT, "completion": ANSWER,
        "status": "success", "model": "m", "turn_index": 0, "feedback_score": 0,
        "message_type": "ai_completion",
    }, over)


def _snapshot(sid, g, **over):
    return _insert(ContextSnapshot, {
        "id": sid, "agent_execution_id": g["execution"], "kind": "final",
        "context_view_json": CONTEXT_VIEW, "prompt_text": "show revenue by region for EMEA",
    }, over)


def _state(sid, g, **over):
    return _insert(ReportContextState, {
        "id": sid, "report_id": g["report"], "summary_json": SUMMARY,
    }, over)


def _execution(eid, g, **over):
    return _insert(AgentExecution, {
        "id": eid, "completion_id": g["completion"], "prompt_text": "show revenue by region for EMEA",
    }, over)


def _entity(eid, g, **over):
    return _insert(Entity, {
        "id": eid, "organization_id": g["org"], "owner_id": g["user"], "type": "model",
        "title": "t", "slug": "s-" + eid, "code": SQL, "applied_params": PARAMS, "status": "draft",
    }, over)


def _step(sid, g, **over):
    return _insert(Step, {
        "id": sid, "title": "t", "slug": sid, "status": "success", "prompt": "", "code": "",
        "data": {}, "description": "", "type": "table", "data_model": {}, "view": {},
        "applied_params": PARAMS, "widget_id": g["widget"],
    }, over)


def _user_result(rid, g, **over):
    return _insert(StepUserResult, {
        "id": rid, "step_id": g["step"], "user_id": g["user"], "organization_id": g["org"],
        # (step, user, fingerprint) is unique; key it on the row so two
        # rows for one viewer can coexist.
        "status": "success", "data": {}, "executed_as": "viewer", "params_fingerprint": rid,
        "applied_params": PARAMS,
    }, over)


# (insert builder, table, column name, expected value, sensitive substring)
CASES = [
    pytest.param(_completion, Completion, "prompt", PROMPT, "EMEA", id="completion.prompt"),
    pytest.param(_completion, Completion, "completion", ANSWER, "1,234,567", id="completion.completion"),
    pytest.param(_snapshot, ContextSnapshot, "context_view_json", CONTEXT_VIEW, "1234567", id="context_snapshot.context_view_json"),
    pytest.param(_snapshot, ContextSnapshot, "prompt_text", "show revenue by region for EMEA", "EMEA", id="context_snapshot.prompt_text"),
    pytest.param(_state, ReportContextState, "summary_json", SUMMARY, "1,234,567", id="report_context_state.summary_json"),
    pytest.param(_execution, AgentExecution, "prompt_text", "show revenue by region for EMEA", "EMEA", id="agent_execution.prompt_text"),
    pytest.param(_entity, Entity, "code", SQL, "EMEA", id="entity.code"),
    pytest.param(_entity, Entity, "applied_params", PARAMS, "c-42", id="entity.applied_params"),
    pytest.param(_step, Step, "applied_params", PARAMS, "c-42", id="step.applied_params"),
    pytest.param(_user_result, StepUserResult, "applied_params", PARAMS, "c-42", id="step_user_result.applied_params"),
]


@pytest.mark.db
@pytest.mark.asyncio
@pytest.mark.parametrize("insert, model, column, expected, secret", CASES)
async def test_column_is_ciphertext_on_disk_and_plaintext_through_the_orm(
    insert, model, column, expected, secret, encrypting
):
    from app.dependencies import async_session_maker

    table = model.__table__
    row_id = str(uuid.uuid4())
    async with async_session_maker() as db:
        g = await _seed_graph(db)
        await db.execute(insert(row_id, g))
        await db.commit()

    async with async_session_maker() as db:
        raw = (await db.execute(
            select(table.c[column].cast(Text)).where(table.c.id == row_id)
        )).scalar()
        assert secret not in raw, f"{column} must be ciphertext on disk"
        assert enc.ENVELOPE_MARKER in raw or raw.startswith(TEXT_ENVELOPE_PREFIX)

        value = (await db.execute(
            select(table.c[column]).where(table.c.id == row_id)
        )).scalar()
        assert value == expected, "ORM read must return the decrypted value"

    async with async_session_maker() as db:
        await db.execute(table.delete().where(table.c.id == row_id))
        await db.commit()


@pytest.mark.db
@pytest.mark.asyncio
@pytest.mark.parametrize("insert, model, column, expected, secret", CASES)
async def test_plaintext_and_encrypted_rows_read_identically(
    insert, model, column, expected, secret, monkeypatch
):
    """The state every customer is in right after enabling the feature: rows
    written before it are plaintext, rows written after are ciphertext, and
    nothing may tell them apart through the ORM."""
    from app.dependencies import async_session_maker

    table = model.__table__
    plain_id, enc_id = str(uuid.uuid4()), str(uuid.uuid4())

    monkeypatch.setattr(enc, "encryption_active", lambda: False)
    async with async_session_maker() as db:
        g = await _seed_graph(db)
        await db.execute(insert(plain_id, g))
        await db.commit()

    monkeypatch.setattr(enc, "encryption_active", lambda: True)
    async with async_session_maker() as db:
        # ReportContextState is one row per report, so the second row needs
        # its own parent graph.
        g2 = await _seed_graph(db) if model is ReportContextState else g
        await db.execute(insert(enc_id, g2))
        await db.commit()

    # Read with the feature OFF again: ciphertext must still decrypt.
    monkeypatch.setattr(enc, "encryption_active", lambda: False)
    async with async_session_maker() as db:
        rows = dict((await db.execute(
            select(table.c.id, table.c[column]).where(table.c.id.in_([plain_id, enc_id]))
        )).all())
        assert rows[plain_id] == expected
        assert rows[enc_id] == expected

        raw = dict((await db.execute(
            select(table.c.id, table.c[column].cast(Text)).where(table.c.id.in_([plain_id, enc_id]))
        )).all())
        assert secret in raw[plain_id], "the pre-existing row is untouched plaintext"
        assert secret not in raw[enc_id]

        # The SQL-side marker singles out exactly the encrypted row, so
        # prefilters can route it to a Python check instead of missing it.
        col = getattr(model, column)
        marker = text_envelope_expr(col) if isinstance(expected, str) else envelope_marker_expr(col)
        flagged = {r[0] for r in (await db.execute(
            select(table.c.id).where(table.c.id.in_([plain_id, enc_id]), marker)
        )).all()}
        assert flagged == {enc_id}

    async with async_session_maker() as db:
        await db.execute(table.delete().where(table.c.id.in_([plain_id, enc_id])))
        await db.commit()


@pytest.mark.db
@pytest.mark.asyncio
async def test_none_applied_params_stay_null(encrypting):
    """``applied_params`` is ``none_as_null``: an explicit None must land as SQL
    NULL, not as an encrypted JSON null, or ``IS NULL`` filters break."""
    from app.dependencies import async_session_maker

    sid = str(uuid.uuid4())
    async with async_session_maker() as db:
        g = await _seed_graph(db)
        await db.execute(_step(sid, g, applied_params=None))
        await db.commit()
    async with async_session_maker() as db:
        null_rows = (await db.execute(
            select(Step.__table__.c.id).where(
                Step.__table__.c.id == sid, Step.__table__.c.applied_params.is_(None)
            )
        )).all()
        assert [r[0] for r in null_rows] == [sid]
        assert (await db.execute(
            select(Step.__table__.c.applied_params).where(Step.__table__.c.id == sid)
        )).scalar() is None
    async with async_session_maker() as db:
        await db.execute(Step.__table__.delete().where(Step.__table__.c.id == sid))
        await db.commit()


@pytest.mark.db
@pytest.mark.asyncio
async def test_orm_object_round_trip_rewrites_plaintext_as_ciphertext(monkeypatch):
    """Lazy migration: a plaintext row rewritten after the feature is on
    becomes ciphertext, with the value unchanged either way."""
    from app.dependencies import async_session_maker

    eid = str(uuid.uuid4())
    monkeypatch.setattr(enc, "encryption_active", lambda: False)
    async with async_session_maker() as db:
        g = await _seed_graph(db)
        await db.execute(_entity(eid, g))
        await db.commit()

    monkeypatch.setattr(enc, "encryption_active", lambda: True)
    new_sql = SQL + " -- revised"
    async with async_session_maker() as db:
        entity = await db.get(Entity, eid)
        assert entity.code == SQL
        entity.code = new_sql
        await db.commit()

    async with async_session_maker() as db:
        raw = (await db.execute(
            select(Entity.__table__.c.code.cast(Text)).where(Entity.__table__.c.id == eid)
        )).scalar()
        assert raw.startswith(TEXT_ENVELOPE_PREFIX)
        assert (await db.get(Entity, eid)).code == new_sql
        await db.execute(Entity.__table__.delete().where(Entity.__table__.c.id == eid))
        await db.commit()


def test_is_encrypted_envelope_and_text_do_not_cross_match():
    assert not is_encrypted_envelope(enc.encrypt_text("x"))
    assert not is_encrypted_text(enc.encrypt_value({"a": 1}))
