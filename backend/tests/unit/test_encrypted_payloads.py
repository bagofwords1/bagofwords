"""Encryption at rest for analytical payloads (enterprise ``data_encryption``).

The contract these lock down (see app/ee/encryption/types.py):

* writes encrypt only when licensed, enabled, and the key is durable;
* reads decrypt unconditionally, so nothing already stored ever goes dark;
* plaintext rows written before the feature existed keep working forever.
"""

from __future__ import annotations

import json
import uuid
import zlib

import pytest
from sqlalchemy import JSON, select

from app.ee.encryption import types as enc
from app.ee.encryption import EncryptedJSON, is_encrypted_envelope
from app.models.step import Step
from app.models.tool_execution import ToolExecution


@pytest.fixture
def encrypting(monkeypatch):
    """Force the write gate open without a licence or config file."""
    monkeypatch.setattr(enc, "encryption_active", lambda: True)
    return enc


@pytest.fixture
def not_encrypting(monkeypatch):
    monkeypatch.setattr(enc, "encryption_active", lambda: False)
    return enc


SAMPLE = {
    "rows": [{"region": "EMEA", "revenue": 636269.0, "units": 5919}] * 40,
    "columns": [{"field": "region"}, {"field": "revenue"}],
    "info": {"total_rows": 40},
}


# --------------------------------------------------------------------------
# Envelope round-trip
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    SAMPLE,
    {},
    [],
    [1, 2, 3],
    {"nested": {"deep": [{"a": None, "b": True, "c": 1.5}]}},
    {"unicode": "café — 東京 — \u0000 edge"},
    {"big": "x" * 100_000},
])
def test_round_trip_preserves_value(value):
    envelope = enc.encrypt_value(value)
    assert is_encrypted_envelope(envelope)
    assert enc.decrypt_value(envelope) == value


def test_envelope_hides_the_payload():
    envelope = enc.encrypt_value(SAMPLE)
    blob = json.dumps(envelope)
    for secret in ("EMEA", "636269", "revenue", "total_rows"):
        assert secret not in blob


def test_envelope_is_valid_json_so_the_column_stays_json():
    """The whole point of the object envelope: no schema migration."""
    envelope = enc.encrypt_value(SAMPLE)
    assert json.loads(json.dumps(envelope)) == envelope
    assert set(envelope) <= {enc.ENVELOPE_MARKER, "v", "z"}


def test_large_payloads_are_compressed_and_small_ones_are_not():
    big = enc.encrypt_value(SAMPLE)
    small = enc.encrypt_value({"a": 1})
    assert big.get("z") == 1
    assert "z" not in small
    assert enc.decrypt_value(small) == {"a": 1}


def test_compression_makes_row_payloads_smaller_than_plaintext():
    plain = json.dumps(SAMPLE, separators=(",", ":"))
    cipher = json.dumps(enc.encrypt_value(SAMPLE), separators=(",", ":"))
    assert len(cipher) < len(plain)


def test_stdlib_json_fallback_matches_orjson(monkeypatch):
    """orjson is an optimization, never a behavior change."""
    fast = enc.encrypt_value(SAMPLE)
    monkeypatch.setattr(enc, "_orjson", None)
    slow = enc.encrypt_value(SAMPLE)
    assert enc.decrypt_value(fast) == enc.decrypt_value(slow) == SAMPLE


def test_non_string_dict_keys_coerce_like_plain_json():
    value = {1: "a", True: "b", 2.5: "c"}
    expected = json.loads(json.dumps(value))
    assert enc.decrypt_value(enc.encrypt_value(value)) == expected


# --------------------------------------------------------------------------
# The write gate
# --------------------------------------------------------------------------

def test_bind_encrypts_only_when_active(encrypting):
    bound = EncryptedJSON().process_bind_param(SAMPLE, None)
    assert is_encrypted_envelope(bound)


def test_bind_passes_through_when_inactive(not_encrypting):
    bound = EncryptedJSON().process_bind_param(SAMPLE, None)
    assert bound == SAMPLE


def test_bind_preserves_none_and_json_null(encrypting):
    col = EncryptedJSON()
    assert col.process_bind_param(None, None) is None
    assert col.process_bind_param(JSON.NULL, None) is JSON.NULL


def test_bind_does_not_double_wrap(encrypting):
    once = EncryptedJSON().process_bind_param(SAMPLE, None)
    twice = EncryptedJSON().process_bind_param(once, None)
    assert twice == once
    assert enc.decrypt_value(twice) == SAMPLE


def test_gate_closed_without_licence(monkeypatch):
    monkeypatch.setattr("app.settings.bow_config.encryption_key_is_ephemeral", lambda: False)
    monkeypatch.setattr("app.ee.license.has_feature", lambda f: False)
    assert enc.encryption_active() is False


def test_gate_closed_when_config_disabled(monkeypatch):
    from app.settings.config import settings

    monkeypatch.setattr(settings.bow_config.data_encryption, "enabled", False)
    assert enc.encryption_active() is False


def test_gate_closed_when_key_is_ephemeral(monkeypatch):
    """An invented key dies with the process; encrypting under it would make
    every snapshot written this run unreadable after a restart."""
    monkeypatch.setattr("app.ee.license.has_feature", lambda f: True)
    monkeypatch.setattr("app.settings.bow_config.encryption_key_is_ephemeral", lambda: True)
    assert enc.encryption_active() is False


def test_gate_open_when_licensed_enabled_and_key_is_durable(monkeypatch):
    monkeypatch.setattr("app.ee.license.has_feature", lambda f: True)
    monkeypatch.setattr("app.settings.bow_config.encryption_key_is_ephemeral", lambda: False)
    assert enc.encryption_active() is True


# --------------------------------------------------------------------------
# Backward and forward compatibility
# --------------------------------------------------------------------------

def test_plaintext_rows_read_back_untouched(not_encrypting):
    """Rows written before the feature existed must keep working."""
    col = EncryptedJSON()
    for legacy in (SAMPLE, {}, [], None, {"rows": []}):
        assert col.process_result_value(legacy, None) == legacy


def test_stored_ciphertext_still_reads_when_the_feature_is_switched_off(not_encrypting):
    """Turning the toggle off must not strand data already written."""
    envelope = enc.encrypt_value(SAMPLE)
    assert EncryptedJSON().process_result_value(envelope, None) == SAMPLE


def test_unreadable_payload_degrades_to_none_rather_than_raising():
    """A rotated/lost key should blank one widget, not fail the whole report."""
    envelope = {enc.ENVELOPE_MARKER: 1, "v": "gAAAAABnot-a-real-token"}
    assert EncryptedJSON().process_result_value(envelope, None) is None


def test_corrupt_compressed_payload_degrades_to_none(monkeypatch):
    envelope = enc.encrypt_value(SAMPLE)
    monkeypatch.setattr(enc, "decrypt_value", _raise(zlib.error("bad stream")))
    assert EncryptedJSON().process_result_value(envelope, None) is None


def _raise(exc):
    def _fn(*_a, **_k):
        raise exc
    return _fn


# --------------------------------------------------------------------------
# Database round-trip through the real models
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_step_data_round_trips_through_the_database(encrypting):
    from app.dependencies import async_session_maker

    step_id = str(uuid.uuid4())
    async with async_session_maker() as db:
        await db.execute(Step.__table__.insert().values(
            id=step_id, title="enc", slug=step_id, status="success", prompt="",
            code="", data=SAMPLE, description="", type="table", data_model={},
            view={}, widget_id="w-" + step_id, context_summary_json=None,
        ))
        await db.commit()

    async with async_session_maker() as db:
        stored = (await db.execute(
            select(Step.__table__.c.data).where(Step.__table__.c.id == step_id)
        )).scalar()
        assert stored == SAMPLE, "ORM read must return the decrypted payload"

        raw = (await db.execute(
            select(Step.__table__.c.data.cast(__import__("sqlalchemy").Text))
            .where(Step.__table__.c.id == step_id)
        )).scalar()
        assert enc.ENVELOPE_MARKER in raw, "the stored bytes must be ciphertext"
        assert "EMEA" not in raw

    async with async_session_maker() as db:
        await db.execute(Step.__table__.delete().where(Step.__table__.c.id == step_id))
        await db.commit()


@pytest.mark.asyncio
async def test_mixed_plaintext_and_encrypted_rows_both_read(encrypting, monkeypatch):
    """The state a customer is in immediately after enabling the feature."""
    from app.dependencies import async_session_maker

    plain_id, enc_id = str(uuid.uuid4()), str(uuid.uuid4())

    def _insert(step_id):
        return ToolExecution.__table__.insert().values(
            id=step_id, agent_execution_id="ae-" + step_id, tool_name="create_data",
            status="success", success=True, result_json=SAMPLE, arguments_json={},
        )

    monkeypatch.setattr(enc, "encryption_active", lambda: False)
    async with async_session_maker() as db:
        await db.execute(_insert(plain_id))
        await db.commit()

    monkeypatch.setattr(enc, "encryption_active", lambda: True)
    async with async_session_maker() as db:
        await db.execute(_insert(enc_id))
        await db.commit()

    async with async_session_maker() as db:
        rows = dict((await db.execute(
            select(ToolExecution.id, ToolExecution.result_json)
            .where(ToolExecution.id.in_([plain_id, enc_id]))
        )).all())
        assert rows[plain_id] == SAMPLE
        assert rows[enc_id] == SAMPLE

        from sqlalchemy import Text
        raw = dict((await db.execute(
            select(ToolExecution.__table__.c.id,
                   ToolExecution.__table__.c.result_json.cast(Text))
            .where(ToolExecution.__table__.c.id.in_([plain_id, enc_id]))
        )).all())
        assert enc.ENVELOPE_MARKER not in raw[plain_id]
        assert enc.ENVELOPE_MARKER in raw[enc_id]

        await db.execute(ToolExecution.__table__.delete().where(
            ToolExecution.__table__.c.id.in_([plain_id, enc_id])))
        await db.commit()


def test_marker_without_a_token_is_treated_as_plaintext():
    """A payload that merely carries the marker key is not an envelope.

    ``envelope_marker_sql`` tests only the marker, so it can over-flag such a
    row into the ORM path; it must come back as the payload it is.
    """
    lookalike = {enc.ENVELOPE_MARKER: 1, "rows": [{"region": "EMEA"}]}
    assert not is_encrypted_envelope(lookalike)
    assert EncryptedJSON().process_result_value(lookalike, None) == lookalike


def test_non_finite_floats_normalize_to_null():
    """pandas puts NaN/Infinity into Step.data; they must survive the trip.

    They normalize to ``null`` — not valid-JSON literals PostgreSQL would
    reject on a plaintext column — and must never make a payload unreadable.
    """
    value = {"rows": [{"a": float("nan"), "b": float("inf"), "c": 1.5}]}
    assert enc.decrypt_value(enc.encrypt_value(value)) == {
        "rows": [{"a": None, "b": None, "c": 1.5}]
    }


def test_stdlib_written_payload_with_nan_is_still_readable(monkeypatch):
    """If _dumps ever falls back to the stdlib, _loads must still parse it.

    ``json.dumps`` emits bare ``NaN``, which orjson's parser rejects — reading
    must fall back too, or the payload would encrypt and never decrypt.
    """
    monkeypatch.setattr(enc, "_orjson", None)
    envelope = enc.encrypt_value({"a": float("nan")})
    monkeypatch.undo()
    decoded = enc.decrypt_value(envelope)
    assert decoded["a"] != decoded["a"], "expected NaN back from the stdlib path"


def test_explicit_none_is_stored_as_null_not_column_default():
    """Writing ``data = None`` explicitly must stay None on read-back.

    Plain ``JSON`` marks an explicit None as a meaningful value
    (``should_evaluate_none``), so SQLAlchemy stores JSON null instead of
    applying the column's ``default=dict``. TypeDecorator does not inherit
    that flag from its impl; without propagating it, a seeded
    ``Step(data=None)`` on the ``default=dict`` column silently came back as
    ``{}`` (caught by tests/e2e/test_report_refresh_on_view.py).
    """
    from sqlalchemy import JSON

    assert enc.EncryptedJSON().should_evaluate_none is JSON().should_evaluate_none
    assert (
        enc.EncryptedJSON(none_as_null=True).should_evaluate_none
        is JSON(none_as_null=True).should_evaluate_none
    )
