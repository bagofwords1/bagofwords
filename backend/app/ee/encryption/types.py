# Enterprise payload encryption
# Licensed under the Business Source License 1.1
# See ENTERPRISE_LICENSE for details
"""``EncryptedJSON`` — a drop-in replacement for ``Column(JSON)`` that stores its
value as ciphertext.

Storage envelope
----------------
An encrypted value is persisted as a small, *valid JSON object*::

    {"__bow_enc__": 1, "z": 1, "v": "<fernet token>"}

Keeping the envelope valid JSON is deliberate: the column stays a ``JSON``
column on both PostgreSQL and SQLite, so adopting the feature needs **no schema
migration** and no table rewrite.

Compatibility contract
----------------------
Reads are *never* gated. Writes are.

* **Reading** an enveloped value always decrypts, regardless of licence or
  config. A licence that lapses, or an operator toggling the feature off, must
  never turn a customer's existing dashboards into unreadable ciphertext.
* **Reading** a plaintext value returns it untouched. Rows written before the
  feature was enabled keep working forever — there is no forced backfill.
* **Writing** encrypts only when the enterprise ``data_encryption`` feature is
  licensed *and* ``bow_config.data_encryption.enabled`` is true.

The two rules together make adoption and rollback a config toggle: existing rows
migrate lazily as they are rewritten, and nothing ever becomes unreadable.

Performance
-----------
Payloads are zlib-compressed before encryption (above a size floor). JSON result
rows are highly compressible, so an encrypted ``Step.data`` is typically several
times *smaller* on disk than the plaintext it replaces — the reduction in row
size and I/O offsets the cost of the cipher. See
``tests/unit/test_encrypted_json_perf.py``.
"""

from __future__ import annotations

import json
import logging
import zlib
from typing import Any, Optional

try:  # optional at import time so the module never hard-fails on a slim install
    import orjson as _orjson
except ImportError:  # pragma: no cover - orjson is a declared dependency
    _orjson = None

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import JSON
from sqlalchemy.types import TypeDecorator

logger = logging.getLogger(__name__)

# Envelope keys. Short by design: they are repeated on every encrypted row.
ENVELOPE_MARKER = "__bow_enc__"
_ENVELOPE_VERSION = 1
_ENVELOPE_PAYLOAD = "v"
_ENVELOPE_COMPRESSED = "z"

# Compress before encrypting only above this size. Below it the zlib header
# costs more than it saves, and tiny payloads are not where I/O time goes.
_COMPRESS_MIN_BYTES = 512
# Level 4, chosen by end-to-end measurement rather than by default (see
# tests/unit/test_encrypted_json_perf.py). It is the level at which BOTH the
# read and the write of an encrypted payload land at or below the plaintext
# baseline: the smaller ciphertext speeds decryption up by more than the extra
# compression costs, while writes still finish sooner thanks to orjson. Level 1
# leaves reads marginally slower; level 6 pays too much on writes.
_COMPRESS_LEVEL = 4

_ENTERPRISE_FEATURE = "data_encryption"

# One-shot latch for the ephemeral-key warning below.
_warned_ephemeral = False

# Fernet construction base64-decodes and splits the key on every call. These
# types sit on the hottest read paths in the app, so the instance is memoized
# against the key it was built from (and rebuilt if the key ever changes).
_fernet_cache: dict[str, Fernet] = {}


def _dumps(value: Any) -> bytes:
    """Serialize a payload to JSON bytes.

    orjson is several times faster than the stdlib encoder, and it is what lets
    an encrypted write finish sooner than the plaintext write it replaces.
    ``OPT_NON_STR_KEYS`` matches the stdlib's coercion of non-string dict keys so
    an encrypted column accepts exactly what the plaintext column accepted.

    One deliberate difference: orjson writes non-finite floats (``NaN``,
    ``Infinity`` — which reach ``Step.data`` from pandas) as ``null``, where the
    stdlib emits the bare literals. The literals are not valid JSON, and today a
    payload containing them fails the INSERT outright on PostgreSQL, so
    normalizing to ``null`` widens what can be stored rather than narrowing it.
    """
    if _orjson is not None:
        try:
            return _orjson.dumps(value, option=_orjson.OPT_NON_STR_KEYS)
        except TypeError:
            # The stdlib encoder accepts a few shapes orjson rejects; never let
            # the faster path narrow what a column can store.
            pass
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _loads(raw: bytes) -> Any:
    """Parse payload bytes back.

    Falls back to the stdlib because the two parsers do not accept the same
    input: ``json.dumps`` emits bare ``NaN``/``Infinity`` (which orjson rejects)
    for the non-finite floats that reach ``Step.data`` from pandas. Anything
    ``_dumps`` can write, this must be able to read — a payload that encrypts
    but will not decrypt is data loss.
    """
    if _orjson is not None:
        try:
            return _orjson.loads(raw)
        except Exception:
            pass
    return json.loads(raw.decode("utf-8"))


def _get_fernet() -> Optional[Fernet]:
    """The Fernet built from ``bow_config.encryption_key``, or None if unusable.

    Deliberately the *same* key that protects data-source credentials: one key
    for an operator to pin, back up and rotate, not two.
    """
    from app.settings.config import settings

    key = getattr(settings.bow_config, "encryption_key", None)
    if not key:
        return None
    cached = _fernet_cache.get(key)
    if cached is None:
        try:
            cached = Fernet(key)
        except Exception as exc:
            logger.error("Invalid encryption_key; payload encryption unavailable: %s", exc)
            return None
        _fernet_cache.clear()
        _fernet_cache[key] = cached
    return cached


def encryption_active() -> bool:
    """Whether *new writes* should be encrypted.

    Requires both the enterprise licence feature and the config toggle. Never
    consulted on the read path — see the module docstring.
    """
    global _warned_ephemeral
    try:
        from app.settings.config import settings

        config = getattr(settings.bow_config, "data_encryption", None)
        if config is not None and not config.enabled:
            return False

        from app.ee import license as ee_license

        if not ee_license.has_feature(_ENTERPRISE_FEATURE):
            return False

        # Checked last, so the warning below only reaches instances that would
        # otherwise be encrypting.
        from app.settings.bow_config import encryption_key_is_ephemeral

        if encryption_key_is_ephemeral():
            # No BOW_ENCRYPTION_KEY was supplied, so the key was invented at
            # startup and dies with the process. Encrypting under it would make
            # every snapshot written this run unreadable after a restart — and
            # unlike a credential, an analytical payload cannot simply be
            # re-entered. Stay in plaintext and say why, loudly, once.
            if not _warned_ephemeral:
                _warned_ephemeral = True
                logger.warning(
                    "Payload encryption is licensed and enabled but "
                    "BOW_ENCRYPTION_KEY is not set, so the key is regenerated "
                    "on every restart. Writing plaintext instead of data this "
                    "process alone could read. Set BOW_ENCRYPTION_KEY to turn "
                    "encryption on."
                )
            return False
    except Exception as exc:
        # A misconfigured licence or config must not fail the write. Falling
        # back to plaintext keeps the row readable; falling back to ciphertext
        # with a broken key would not.
        logger.warning("Could not resolve payload encryption state: %s", exc)
        return False

    return _get_fernet() is not None


def is_encrypted_envelope(value: Any) -> bool:
    """True when ``value`` is a stored ciphertext envelope rather than payload.

    Requires the token as well as the marker, so a (wildly improbable) plaintext
    payload carrying a ``__bow_enc__`` key is still read as the payload it is
    rather than failing to decrypt. ``envelope_marker_sql`` tests only the
    marker; a row it over-flags simply takes the ORM path and comes back as
    plaintext, so the two stay consistent.
    """
    return (
        isinstance(value, dict)
        and ENVELOPE_MARKER in value
        and isinstance(value.get(_ENVELOPE_PAYLOAD), str)
    )


def encrypt_value(value: Any) -> Any:
    """Wrap a JSON-serializable value into a ciphertext envelope.

    Returns the value unchanged if encryption is unavailable, so a caller can
    use this unconditionally.
    """
    fernet = _get_fernet()
    if fernet is None:
        return value
    try:
        raw = _dumps(value)
    except (TypeError, ValueError) as exc:
        # Not JSON-serializable: a plaintext JSON column would have failed on
        # this value too. Hand it back and let the normal error surface.
        logger.warning("Value is not JSON-serializable; storing unencrypted: %s", exc)
        return value

    compressed = False
    if len(raw) >= _COMPRESS_MIN_BYTES:
        raw = zlib.compress(raw, _COMPRESS_LEVEL)
        compressed = True

    envelope = {
        ENVELOPE_MARKER: _ENVELOPE_VERSION,
        _ENVELOPE_PAYLOAD: fernet.encrypt(raw).decode("ascii"),
    }
    if compressed:
        envelope[_ENVELOPE_COMPRESSED] = 1
    return envelope


def decrypt_value(envelope: dict) -> Any:
    """Unwrap a ciphertext envelope produced by :func:`encrypt_value`."""
    fernet = _get_fernet()
    token = envelope.get(_ENVELOPE_PAYLOAD)
    if fernet is None or not isinstance(token, str):
        raise InvalidToken("no usable encryption key for stored payload")

    raw = fernet.decrypt(token.encode("ascii"))
    if envelope.get(_ENVELOPE_COMPRESSED):
        raw = zlib.decompress(raw)
    return _loads(raw)


class EncryptedJSON(TypeDecorator):
    """A ``JSON`` column whose value is encrypted at rest.

    Accepts the same constructor arguments as ``sqlalchemy.JSON`` (notably
    ``none_as_null``), which are forwarded to the underlying impl.
    """

    impl = JSON
    cache_ok = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # TypeDecorator does not inherit this from its impl. Plain JSON marks
        # an explicit ``None`` as a meaningful value (stored as JSON null,
        # column defaults suppressed) unless ``none_as_null=True``; without
        # this line, ``step.data = None`` on a column with ``default=dict``
        # silently comes back as ``{}`` instead of ``None``.
        self.should_evaluate_none = self.impl_instance.should_evaluate_none

    def process_bind_param(self, value, dialect):
        if value is None or value is JSON.NULL:
            return value
        if is_encrypted_envelope(value):
            # Already an envelope (e.g. copied straight from another row).
            return value
        if not encryption_active():
            return value
        return encrypt_value(value)

    def process_result_value(self, value, dialect):
        # Ungated on purpose: stored ciphertext must stay readable even when the
        # feature is switched off or the licence lapses.
        if not is_encrypted_envelope(value):
            return value
        try:
            return decrypt_value(value)
        except (InvalidToken, zlib.error, ValueError) as exc:
            # Almost always a changed/lost BOW_ENCRYPTION_KEY. Surface it loudly
            # but return None rather than raising: one unreadable snapshot
            # should degrade a single widget, not fail the whole report load.
            logger.error(
                "Failed to decrypt stored payload — is BOW_ENCRYPTION_KEY the "
                "one it was written with? %s",
                exc,
            )
            return None


def envelope_marker_sql(column: str, *, postgres: bool) -> str:
    """A SQL expression that is non-NULL exactly when ``column`` holds ciphertext.

    Several hot paths project fields out of these JSON documents in SQL rather
    than hydrating multi-megabyte payloads in Python. SQL cannot see inside an
    envelope, so those queries select this marker alongside their projection and
    re-read the few enveloped rows through the ORM (where the column type
    decrypts them). Detecting per row — rather than switching wholesale on the
    config flag — keeps the projection correct in every state, including the
    mixed table left behind by enabling the feature and later disabling it.
    """
    if postgres:
        return f"{column}->'{ENVELOPE_MARKER}'"
    return f"json_extract({column}, '$.{ENVELOPE_MARKER}')"
