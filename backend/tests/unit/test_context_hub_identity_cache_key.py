"""Fix validation: the process-wide schema/instruction caches are identity-aware.

`SchemaContextBuilder` serves the CALLER's per-user overlay for `user_required`
sources, so the built section is user-specific. `ContextHub.prime_static` caches
that built section process-wide; before the fix the key was
(org, ds_ids, build_id) with no identity, so whichever user warmed the cache had
their table list served to every other user in the same org for the whole TTL
(300s) — a restricted user inherited a broader user's table names, and a broader
user could silently inherit a narrower view.

Run:
    cd backend && python -m pytest tests/unit/test_context_hub_identity_cache_key.py -v
"""
from types import SimpleNamespace

from app.ai.context.context_hub import ContextHub


def _hub(user_id, auth_policy):
    """A ContextHub shell — only the fields the key helper reads."""
    hub = ContextHub.__new__(ContextHub)
    hub.user = SimpleNamespace(id=user_id) if user_id else None
    hub.data_sources = [
        SimpleNamespace(
            id="ds-1",
            connections=[SimpleNamespace(auth_policy=auth_policy)],
        )
    ]
    return hub


def test_user_required_keys_differ_per_user():
    a = _hub("user-a", "user_required")._schema_identity_key()
    b = _hub("user-b", "user_required")._schema_identity_key()
    assert a != b, "two users must not share a cached identity-scoped schema section"
    assert "user-a" in a and "user-b" in b


def test_system_only_sources_share_one_entry():
    a = _hub("user-a", "system_only")._schema_identity_key()
    b = _hub("user-b", "system_only")._schema_identity_key()
    assert a == b == "system", (
        "non-delegated sources serve the same canonical catalog to everyone, so "
        "they should keep sharing a single cache entry"
    )


def test_mixed_connections_are_identity_scoped():
    hub = ContextHub.__new__(ContextHub)
    hub.user = SimpleNamespace(id="user-a")
    hub.data_sources = [
        SimpleNamespace(id="ds-1", connections=[SimpleNamespace(auth_policy="system_only")]),
        SimpleNamespace(id="ds-2", connections=[SimpleNamespace(auth_policy="user_required")]),
    ]
    assert hub._schema_identity_key() == "user:user-a"


def test_fails_safe_to_per_user_when_connections_unavailable():
    """If the connections can't be inspected (not loaded), key on the user:
    correct, just a lower hit rate — never share by accident."""
    class _Boom:
        id = "ds-1"

        @property
        def connections(self):
            raise RuntimeError("lazy load outside greenlet")

    hub = ContextHub.__new__(ContextHub)
    hub.user = SimpleNamespace(id="user-a")
    hub.data_sources = [_Boom()]
    assert hub._schema_identity_key() == "user:user-a"


def test_anonymous_caller_does_not_share_with_named_users():
    anon = _hub(None, "user_required")._schema_identity_key()
    named = _hub("user-a", "user_required")._schema_identity_key()
    assert anon != named
