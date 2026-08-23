"""Payload encryption must not cost throughput on the paths that carry rows.

``Step.data`` is read on every report load and written on every query run, so
"secure but slower" would be a real regression. Two properties keep it honest:

* the ciphertext is *smaller* than the plaintext it replaces (compressed before
  encryption), so the database stores and ships less;
* serialization uses orjson, which is enough faster than the stdlib encoder that
  it pays for the compression and the cipher.

Thresholds here are deliberately loose — this guards against an order-of-
magnitude regression (a wrong compression level, a per-call Fernet
construction), not against CI noise.
"""

from __future__ import annotations

import json
import random
import string
import time

import pytest

from app.ee.encryption import types as enc


def _dataset(n_rows: int) -> dict:
    rnd = random.Random(17)
    rows = [
        {
            "id": i,
            "region": rnd.choice(["EMEA", "APAC", "North America", "LATAM"]),
            "product": "".join(rnd.choices(string.ascii_lowercase, k=12)),
            "revenue": round(rnd.random() * 10000, 2),
            "units": rnd.randint(1, 500),
            "month": f"2026-{rnd.randint(1, 12):02d}",
        }
        for i in range(n_rows)
    ]
    return {"rows": rows, "columns": [{"field": k} for k in rows[0]],
            "info": {"total_rows": n_rows}}


def _best_of(fn, rounds: int = 5) -> float:
    """Best-of-N wall time. Best-of resists scheduler noise on shared CI."""
    fn()  # warm caches (Fernet instance, allocator)
    return min(_timed(fn) for _ in range(rounds))


def _timed(fn) -> float:
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


@pytest.fixture(scope="module")
def data():
    return _dataset(5000)


def test_ciphertext_is_smaller_than_plaintext(data):
    """Compression before encryption is what buys back the cipher's cost."""
    plain = json.dumps(data, separators=(",", ":"))
    cipher = json.dumps(enc.encrypt_value(data), separators=(",", ":"))
    ratio = len(cipher) / len(plain)
    assert ratio < 0.6, f"encrypted payload is {ratio:.2f}x plaintext; compression regressed"


def test_encrypt_is_not_slower_than_plain_serialization(data):
    plain = _best_of(lambda: json.dumps(data, separators=(",", ":")).encode())
    encrypted = _best_of(lambda: enc.encrypt_value(data))
    assert encrypted < plain * 2.0, (
        f"encrypt {encrypted*1000:.1f}ms vs plain dumps {plain*1000:.1f}ms"
    )


def test_decrypt_is_not_slower_than_plain_parsing(data):
    raw = json.dumps(data, separators=(",", ":")).encode()
    envelope = enc.encrypt_value(data)
    plain = _best_of(lambda: json.loads(raw))
    decrypted = _best_of(lambda: enc.decrypt_value(envelope))
    assert decrypted < plain * 2.0, (
        f"decrypt {decrypted*1000:.1f}ms vs plain loads {plain*1000:.1f}ms"
    )


def test_fernet_instance_is_reused_across_calls():
    """Rebuilding Fernet per row would dominate the cost of small payloads."""
    first = enc._get_fernet()
    assert first is not None
    assert enc._get_fernet() is first


def test_disabled_encryption_adds_no_work(monkeypatch, data):
    """With the feature off, bind must be a type check and nothing more."""
    monkeypatch.setattr(enc, "encryption_active", lambda: False)
    column = enc.EncryptedJSON()
    passthrough = _best_of(lambda: column.process_bind_param(data, None))
    # A single isinstance + dict membership test: microseconds, not milliseconds.
    assert passthrough < 0.001, f"inactive bind took {passthrough*1000:.3f}ms"
