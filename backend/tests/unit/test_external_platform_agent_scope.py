"""Unit tests for per-instance agent-scope narrowing.

``ExternalPlatformManager._apply_agent_scope`` is the rule that decides which
agents an external-platform instance exposes. The invariants it must keep:

* no scope configured -> the input is returned unchanged (existing behavior);
* a scope narrows to the intersection, never widens;
* original order is preserved.
"""
import pytest

from app.services.external_platform_manager import ExternalPlatformManager


class _FakeScopeService:
    def __init__(self, scope):
        self._scope = set(scope)

    async def get_scope_agent_ids(self, db, platform_id):
        return set(self._scope)


def _manager(scope):
    mgr = ExternalPlatformManager()
    mgr.platform_service = _FakeScopeService(scope)
    return mgr


@pytest.mark.asyncio
async def test_empty_scope_returns_input_unchanged():
    mgr = _manager(scope=[])
    ids = ["a", "b", "c"]
    out = await mgr._apply_agent_scope(db=None, platform_id="p1", data_source_ids=ids)
    assert out == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_scope_narrows_to_intersection_preserving_order():
    # Scope lists more than the user has; result is only the overlap, in the
    # user's original order.
    mgr = _manager(scope={"c", "a", "z"})
    out = await mgr._apply_agent_scope(
        db=None, platform_id="p1", data_source_ids=["a", "b", "c"]
    )
    assert out == ["a", "c"]


@pytest.mark.asyncio
async def test_scope_never_widens_beyond_input():
    # An agent in scope that the user cannot access is never added.
    mgr = _manager(scope={"x", "y"})
    out = await mgr._apply_agent_scope(
        db=None, platform_id="p1", data_source_ids=["a", "b"]
    )
    assert out == []
