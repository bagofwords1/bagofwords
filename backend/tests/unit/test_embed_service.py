"""Pure rules of embedded chat, without HTTP or a database:

* ``decode_assertion``: the RS256 trusted-host assertion contract (algorithm
  allowlist, issuer/audience, time window, required claims);
* ``normalize_origin``: what counts as an allowed framing origin;
* ``effective_tools`` / ``bounded_context``: the thread settings an embed
  forces and the size of host context it accepts.

The HTTP flows (sessions, threads, isolation) live in tests/e2e/test_embedded_chat.py.
"""
import time
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest

from app.errors import AppError
from app.schemas.embed_schema import normalize_origin
from app.services.embed_service import (
    ASSERTION_MAX_LIFETIME,
    FORCED_OFF_TOOLS,
    EmbedService,
    bounded_context,
    effective_tools,
    generate_keypair,
)


@pytest.fixture(scope="module")
def keys():
    private_pem, public_pem, fingerprint = generate_keypair()
    return SimpleNamespace(private=private_pem, public=public_pem, fingerprint=fingerprint)


@pytest.fixture
def embed(keys):
    return SimpleNamespace(public_id="emb_" + uuid.uuid4().hex[:20], issuer="erp", public_key=keys.public, enabled=True)


def _claims(embed, **over):
    now = int(time.time())
    base = {"iss": embed.issuer, "aud": embed.public_id, "sub": "E-1", "iat": now, "exp": now + 300, "jti": uuid.uuid4().hex}
    base.update(over)
    return base


def test_generate_keypair_yields_pem_pair_and_stable_fingerprint():
    private_pem, public_pem, fingerprint = generate_keypair()
    assert private_pem.startswith("-----BEGIN PRIVATE KEY-----")
    assert public_pem.startswith("-----BEGIN PUBLIC KEY-----")
    assert len(fingerprint) == 32 and fingerprint == generate_keypair.__globals__["_hash"](public_pem)[:32]


def test_valid_assertion_decodes_to_subject_and_window(embed, keys):
    token = jwt.encode(_claims(embed, sub=" E-42 "), keys.private, algorithm="RS256")
    out = EmbedService.decode_assertion(embed, token)
    assert out["sub"] == "E-42" and out["jti"]
    assert isinstance(out["iat"], datetime) and out["exp"] - out["iat"] == timedelta(seconds=300)


@pytest.mark.parametrize("variant, reason", [
    ({"iss": "someone-else"}, "issuer"),
    ({"aud": "emb_other"}, "audience"),
    # 60 s of leeway is allowed; two minutes past exp is not.
    ({"exp": int(time.time()) - 120, "iat": int(time.time()) - 400}, "expired"),
    ({"exp": int(time.time()) + int(ASSERTION_MAX_LIFETIME.total_seconds()) + 600}, "lifetime"),
    ({"iat": int(time.time()) + 900, "exp": int(time.time()) + 1200}, "not_yet_valid"),
])
def test_claim_violations_are_typed(embed, keys, variant, reason):
    token = jwt.encode(_claims(embed, **variant), keys.private, algorithm="RS256")
    with pytest.raises(AppError) as info:
        EmbedService.decode_assertion(embed, token)
    assert info.value.error_code == "embed.assertion_invalid"
    assert info.value.params.get("reason") == reason


@pytest.mark.parametrize("missing", ["sub", "jti", "exp", "iat", "iss", "aud"])
def test_every_required_claim_is_required(embed, keys, missing):
    claims = _claims(embed)
    del claims[missing]
    token = jwt.encode(claims, keys.private, algorithm="RS256")
    with pytest.raises(AppError) as info:
        EmbedService.decode_assertion(embed, token)
    assert info.value.error_code == "embed.assertion_invalid"


def test_other_keys_and_other_algorithms_are_refused(embed, keys):
    other_private, _, _ = generate_keypair()
    wrong_key = jwt.encode(_claims(embed), other_private, algorithm="RS256")
    unsigned = jwt.encode(_claims(embed), None, algorithm="none")
    for token in (wrong_key, unsigned, "garbage.token.value"):
        with pytest.raises(AppError) as info:
            EmbedService.decode_assertion(embed, token)
        assert info.value.error_code == "embed.assertion_invalid"


@pytest.mark.parametrize("raw, expected", [
    ("https://erp.example.com", "https://erp.example.com"),
    ("https://ERP.Example.com/", "https://erp.example.com"),
    ("https://erp.example.com:443", "https://erp.example.com"),
    ("http://localhost:4173", "http://localhost:4173"),
    ("https://portal.example.com:8443", "https://portal.example.com:8443"),
])
def test_origins_normalize_to_scheme_host_port(raw, expected):
    assert normalize_origin(raw) == expected


@pytest.mark.parametrize("raw", [
    "erp.example.com", "ftp://erp.example.com", "https://erp.example.com/path", "https://erp.example.com/?q=1",
    "https://user:pw@erp.example.com", "https://", "", "javascript:alert(1)",
])
def test_non_origins_are_rejected(raw):
    with pytest.raises(ValueError):
        normalize_origin(raw)


def test_forced_off_tools_cannot_be_switched_back_on():
    tools = effective_tools({"tools": {"clarify": True, "create_artifact": True, "web_search": False, "not_a_switch": False}})
    assert all(tools[k] is False for k in FORCED_OFF_TOOLS)
    assert tools["web_search"] is False
    assert "not_a_switch" not in tools
    assert effective_tools(None) == {k: False for k in FORCED_OFF_TOOLS}


def test_host_context_is_bounded_json_object():
    assert bounded_context(None) is None
    assert bounded_context({"recordId": "INC-1"}) == {"recordId": "INC-1"}
    with pytest.raises(AppError) as info:
        bounded_context({"blob": "x" * 5000})
    assert info.value.error_code == "embed.context_too_large"
    with pytest.raises(AppError):
        bounded_context(["not", "an", "object"])  # type: ignore[arg-type]
