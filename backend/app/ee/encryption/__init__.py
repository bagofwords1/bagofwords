# Enterprise payload encryption
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details
"""Application-level encryption for row-heavy analytical payloads.

Data-source *credentials* have always been encrypted at rest (see
``Connection.encrypt_credentials``). The query **results** those credentials
produce — ``Step.data``, ``Entity.data``, ``ToolExecution.result_json`` and the
bounded context summaries derived from them — were stored as plaintext JSON.
For customers whose warehouses hold regulated data, that snapshot is often more
sensitive than the credential that fetched it.

This package encrypts those payloads with the *same* Fernet key already used for
credentials (``bow_config.encryption_key``), so operators have exactly one key to
protect, back up, and rotate.

A second wave extended the same protection to the columns that *restate* those
rows: conversation text (``Completion.prompt``/``completion``), the agent's
persisted context (``ContextSnapshot.context_view_json`` — which carries data
previews and tool observations — and ``ReportContextState.summary_json``),
generated artifact content, ``applied_params`` and ``Entity.code``. ``Text`` and
``String`` columns use ``EncryptedText``/``EncryptedString``, a prefixed-string
envelope with the same contract.

See ``types.EncryptedJSON`` for the storage envelope and the backward- and
forward-compatibility contract. Any query that filters or projects *inside*
one of these columns in SQL must route enveloped rows to a Python check:
``envelope_marker_expr`` / ``text_envelope_expr`` flag them per row.
"""

from app.ee.encryption.types import (
    ENVELOPE_MARKER,
    TEXT_ENVELOPE_PREFIX,
    EncryptedJSON,
    EncryptedString,
    EncryptedText,
    encryption_active,
    envelope_marker_expr,
    envelope_marker_sql,
    is_encrypted_envelope,
    is_encrypted_text,
    text_envelope_expr,
)

__all__ = [
    "ENVELOPE_MARKER",
    "TEXT_ENVELOPE_PREFIX",
    "EncryptedJSON",
    "EncryptedString",
    "EncryptedText",
    "encryption_active",
    "envelope_marker_expr",
    "envelope_marker_sql",
    "is_encrypted_envelope",
    "is_encrypted_text",
    "text_envelope_expr",
]
