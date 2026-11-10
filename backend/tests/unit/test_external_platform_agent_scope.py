"""Unit tests for agent-scope narrowing and channel-level resolution.

``ExternalPlatformManager._apply_agent_scope`` is the rule that decides which
agents an external-platform instance — or one of its channels — exposes. The
invariants it must keep:

* no scope configured -> the input is returned unchanged (existing behavior);
* a scope narrows to the intersection, never widens;
* original order is preserved;
* a channel's own scope wins over the ``""`` connection default, which in turn
  wins over "unscoped"; a message with no routing channel (WhatsApp / DM) uses
  the connection default, exactly as before.

``ExternalPlatformService.resolve_scope_agent_ids`` implements that precedence.
"""
import pytest

from app.services.external_platform_manager import ExternalPlatformManager
from app.services.external_platform_service import ExternalPlatformService


class _FakeScopeService:
    """Stands in for ExternalPlatformService over a channel->ids map.

    ``scopes`` maps a channel key to the agent ids scoped there; ``""`` is the
    connection default.
    """

    def __init__(self, scopes):
        self._scopes = {k: set(v) for k, v in scopes.items()}

    async def get_scope_agent_ids(self, db, platform_id, external_channel_id=""):
        return set(self._scopes.get(external_channel_id or "", set()))

    async def resolve_scope_agent_ids(self, db, platform_id, routing_channel_id=None):
        if routing_channel_id:
            channel = self._scopes.get(routing_channel_id)
            if channel:
                return set(channel)
        return set(self._scopes.get("", set()))


def _manager(scopes):
    mgr = ExternalPlatformManager()
    mgr.platform_service = _FakeScopeService(scopes)
    return mgr


# --- narrowing invariants (unchanged behavior via the "" connection default) ---

@pytest.mark.asyncio
async def test_empty_scope_returns_input_unchanged():
    mgr = _manager({"": []})
    out = await mgr._apply_agent_scope(db=None, platform_id="p1", data_source_ids=["a", "b", "c"])
    assert out == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_scope_narrows_to_intersection_preserving_order():
    # Scope lists more than the user has; result is only the overlap, in the
    # user's original order.
    mgr = _manager({"": {"c", "a", "z"}})
    out = await mgr._apply_agent_scope(
        db=None, platform_id="p1", data_source_ids=["a", "b", "c"]
    )
    assert out == ["a", "c"]


@pytest.mark.asyncio
async def test_scope_never_widens_beyond_input():
    # An agent in scope that the user cannot access is never added.
    mgr = _manager({"": {"x", "y"}})
    out = await mgr._apply_agent_scope(
        db=None, platform_id="p1", data_source_ids=["a", "b"]
    )
    assert out == []


# --- channel-level resolution ---

@pytest.mark.asyncio
async def test_channel_scope_overrides_connection_default():
    # #sales is mapped to "a"; the connection default is "b". A message in
    # #sales must resolve to "a" only.
    mgr = _manager({"": {"b"}, "C_SALES": {"a"}})
    out = await mgr._apply_agent_scope(
        db=None, platform_id="p1", data_source_ids=["a", "b"], routing_channel_id="C_SALES"
    )
    assert out == ["a"]


@pytest.mark.asyncio
async def test_unmapped_channel_falls_back_to_connection_default():
    mgr = _manager({"": {"b"}, "C_SALES": {"a"}})
    out = await mgr._apply_agent_scope(
        db=None, platform_id="p1", data_source_ids=["a", "b"], routing_channel_id="C_OTHER"
    )
    assert out == ["b"]


@pytest.mark.asyncio
async def test_no_default_and_unmapped_channel_is_unscoped():
    # Only #sales is mapped and there is no connection default -> a message in
    # any other channel is unscoped (today's behavior).
    mgr = _manager({"C_SALES": {"a"}})
    out = await mgr._apply_agent_scope(
        db=None, platform_id="p1", data_source_ids=["a", "b"], routing_channel_id="C_OTHER"
    )
    assert out == ["a", "b"]


@pytest.mark.asyncio
async def test_none_routing_channel_uses_connection_default():
    # WhatsApp / DM path: no routing channel -> the "" default applies, which is
    # exactly the pre-channel per-instance behavior.
    mgr = _manager({"": {"a"}})
    out = await mgr._apply_agent_scope(
        db=None, platform_id="p1", data_source_ids=["a", "b"], routing_channel_id=None
    )
    assert out == ["a"]


# --- resolution precedence in the real service ---

@pytest.mark.asyncio
async def test_service_resolution_precedence(monkeypatch):
    svc = ExternalPlatformService()
    canned = {"": {"d"}, "C_SALES": {"s"}}

    async def fake_get(db, platform_id, external_channel_id=""):
        return set(canned.get(external_channel_id or "", set()))

    monkeypatch.setattr(svc, "get_scope_agent_ids", fake_get)

    # channel with its own rows -> channel rows
    assert await svc.resolve_scope_agent_ids(None, "p1", "C_SALES") == {"s"}
    # channel without rows -> connection default
    assert await svc.resolve_scope_agent_ids(None, "p1", "C_OTHER") == {"d"}
    # no routing channel -> connection default
    assert await svc.resolve_scope_agent_ids(None, "p1", None) == {"d"}
