# Enterprise payload encryption
# Licensed under the Bag of Words Enterprise License
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

See ``types.EncryptedJSON`` for the storage envelope and the backward- and
forward-compatibility contract.
"""

from app.ee.encryption.types import (
    ENVELOPE_MARKER,
    EncryptedJSON,
    encryption_active,
    envelope_marker_sql,
    is_encrypted_envelope,
)

__all__ = [
    "ENVELOPE_MARKER",
    "EncryptedJSON",
    "encryption_active",
    "envelope_marker_sql",
    "is_encrypted_envelope",
]
