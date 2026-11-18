"""Reversible tokens for the ``pseudonymize`` action.

A pseudonymized value reaches the model as a token such as
``<EMAIL_3fa9c21d>``. The token is an HMAC of the value, keyed per report, so:

* the same value always gets the same token inside a report — the model can
  refer to it consistently, parallel calls agree without coordination, and the
  prompt prefix stays stable for provider caching;
* tokens from two reports cannot be correlated, and a token says nothing about
  the value to anyone without the instance's encryption key.

The vault maps tokens back to values, and lives for one LLM call: the call
tokenizes everything it sends (the conversation is stored with real values, so
each call re-derives the same tokens), and everything the model produces in
reply — streamed text, reasoning, tool arguments — is restored at the LLM
chokepoint before anything else sees it. Tools run with the real values and
people who can open the report read them. Because tokens are derived, nothing
needs to be stored: there is no second copy of the private values at rest.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Lenient on purpose: models sometimes change the case or add spaces.
TOKEN_RE = re.compile(r"<\s*([A-Za-z][A-Za-z0-9_]{0,31})_([0-9a-fA-F]{12}|[0-9a-fA-F]{8})\s*>")
# Longest text that can still be the start of a token (for stream buffering).
MAX_TOKEN_CHARS = 64

MODEL_HINT = (
    "Values like <EMAIL_3fa9c21d> or <PERSON_8c1d02ab> are placeholders for private data. "
    "Treat a placeholder as the real value: it is replaced with the real value automatically before "
    "your tool calls, code and SQL run, and before the user reads your answer. So use placeholders "
    "directly and copy them exactly, for example WHERE Email = '<EMAIL_3fa9c21d>'. A placeholder "
    "stands for the whole original text (a full name is one placeholder), so compare it with the whole "
    "value, for example WHERE FirstName || ' ' || LastName = '<PERSON_8c1d02ab>'. Never ask the user "
    "for the value behind a placeholder, never say a value is hidden or redacted, and never invent or "
    "alter a placeholder."
)


def _canonical(label: str, digest: str) -> str:
    return f"<{label.upper()}_{digest.lower()}>"


class PiiVault:
    """Token ↔ value map for one report (or one call, when there is no report)."""

    def __init__(self, key: bytes, scope: str):
        self._key = key
        self._scope = scope
        self._values: Dict[str, str] = {}

    @classmethod
    def ephemeral(cls) -> "PiiVault":
        """A vault that lives for one call: tokens are consistent within it,
        restorable by whoever holds the object, and never stored."""
        import os
        return cls(os.urandom(32), "ephemeral")

    @property
    def has_tokens(self) -> bool:
        return bool(self._values)

    def token_for(self, label: str, value: str) -> str:
        digest = hmac.new(self._key, f"{self._scope}\0{label}\0{value}".encode("utf-8"), hashlib.sha256).hexdigest()
        token = _canonical(label, digest[:8])
        existing = self._values.get(token)
        if existing is not None and existing != value:
            # Two values in one call sharing a 32-bit digest (about one in four
            # billion per pair): give the newcomer a longer one.
            token = _canonical(label, digest[:12])
        self._values.setdefault(token, value)
        return token

    def _lookup(self, match: "re.Match") -> Optional[str]:
        return self._values.get(_canonical(match.group(1), match.group(2)))

    def restore(self, text: Any) -> Any:
        """Replace every known token in ``text`` with its value. Unknown
        tokens, and non-strings, are left as they are."""
        if not isinstance(text, str) or not self._values or "<" not in text:
            return text
        return TOKEN_RE.sub(lambda m: self._lookup(m) if self._lookup(m) is not None else m.group(0), text)

    def restore_json_fragment(self, fragment: Any) -> Any:
        """Like :meth:`restore`, for a piece of JSON text (streamed tool
        arguments): the value is inserted JSON-escaped."""
        if not isinstance(fragment, str) or not self._values or "<" not in fragment:
            return fragment

        def _sub(m: "re.Match") -> str:
            value = self._lookup(m)
            return m.group(0) if value is None else json.dumps(value, ensure_ascii=False)[1:-1]

        return TOKEN_RE.sub(_sub, fragment)

    def restore_deep(self, obj: Any) -> Any:
        """Restore every string inside nested dicts/lists (tool arguments)."""
        if isinstance(obj, str):
            return self.restore(obj)
        if isinstance(obj, dict):
            return {k: self.restore_deep(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.restore_deep(v) for v in obj]
        return obj


class StreamRestorer:
    """Restores tokens in text that arrives in chunks: a token split across two
    chunks is held back until it is complete."""

    def __init__(self, vault: Optional[PiiVault], *, json_fragment: bool = False):
        self._vault = vault
        self._json = json_fragment
        self._held = ""

    def feed(self, chunk: str) -> str:
        if self._vault is None or not self._vault.has_tokens or not isinstance(chunk, str):
            return chunk
        text = self._held + chunk
        self._held = ""
        cut = text.rfind("<")
        if cut != -1 and ">" not in text[cut:] and len(text) - cut <= MAX_TOKEN_CHARS:
            self._held = text[cut:]
            text = text[:cut]
        return self._vault.restore_json_fragment(text) if self._json else self._vault.restore(text)

    def flush(self) -> str:
        held, self._held = self._held, ""
        if not held or self._vault is None:
            return held
        return self._vault.restore_json_fragment(held) if self._json else self._vault.restore(held)


def _hmac_key() -> bytes:
    """Token key, derived from the instance's encryption key."""
    from app.settings.config import settings

    raw = settings.bow_config.encryption_key
    raw_bytes = raw.encode() if isinstance(raw, str) else raw
    return hashlib.sha256(b"bow-pii-vault\0" + raw_bytes).digest()


def vault_for(organization_id: Optional[str], report_id: Optional[str]) -> PiiVault:
    """The vault for one LLM call. Tokens are keyed by the report (else the
    organization), so every call in a report produces the same token for the
    same value."""
    scope = f"report:{report_id}" if report_id else (f"org:{organization_id}" if organization_id else None)
    if scope is None:
        return PiiVault.ephemeral()
    return PiiVault(_hmac_key(), scope)
