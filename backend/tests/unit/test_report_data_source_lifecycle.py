"""Unit tests for the report data-source lifecycle filters.

A report's attached data sources are a creation-time snapshot; agents
disabled (or moved back to draft/development) afterwards stay on the
snapshot. These filters keep such agents out of report serialization
(``filter_live_data_sources``) and out of query execution / AI context
(``is_execution_live``), mirroring the lifecycle rules of
``get_active_data_sources``.
"""
import asyncio
from types import SimpleNamespace

from app.services.data_source_service import DataSourceService


def _ds(ds_id, publish_status="published", reliability_status="ok", is_active=True):
    return SimpleNamespace(
        id=ds_id,
        is_active=is_active,
        publish_status=publish_status,
        reliability_status=reliability_status,
    )


svc = DataSourceService()

# Precomputed _publish_visibility results: (is_gov, manage_ids, resolved).
NON_MANAGER = (False, set(), None)
GOVERNANCE = (True, set(), None)

USER = SimpleNamespace(id="user-1")
ORG = SimpleNamespace(id="org-1")


def _filter(data_sources, visibility, user=USER, org=ORG):
    return asyncio.run(
        svc.filter_live_data_sources(None, data_sources, user, org, visibility=visibility)
    )


class TestIsExecutionLive:
    def test_published_active_is_live(self):
        assert svc.is_execution_live(_ds("a"))

    def test_disabled_is_not_live(self):
        assert not svc.is_execution_live(_ds("a", publish_status="disabled"))

    def test_inactive_is_not_live(self):
        assert not svc.is_execution_live(_ds("a", is_active=False))

    def test_draft_and_development_remain_runnable(self):
        # Draft/development are per-user visibility concerns, not execution ones.
        assert svc.is_execution_live(_ds("a", publish_status="draft"))
        assert svc.is_execution_live(_ds("a", reliability_status="development"))

    def test_missing_lifecycle_fields_default_to_live(self):
        assert svc.is_execution_live(SimpleNamespace(id="a", is_active=True))


class TestFilterLiveDataSources:
    def test_disabled_dropped_for_everyone(self):
        ds = [_ds("a"), _ds("b", publish_status="disabled")]
        assert [d.id for d in _filter(ds, GOVERNANCE)] == ["a"]
        assert [d.id for d in _filter(ds, NON_MANAGER)] == ["a"]

    def test_draft_and_development_hidden_from_non_managers(self):
        ds = [
            _ds("a"),
            _ds("b", publish_status="draft"),
            _ds("c", reliability_status="development"),
            _ds("d", reliability_status="training"),
        ]
        assert [d.id for d in _filter(ds, NON_MANAGER)] == ["a", "d"]

    def test_managers_keep_their_drafts(self):
        ds = [_ds("a"), _ds("b", publish_status="draft"), _ds("c", reliability_status="development")]
        assert [d.id for d in _filter(ds, GOVERNANCE)] == ["a", "b", "c"]

    def test_per_ds_manage_grant_unlocks_draft(self):
        resolved = SimpleNamespace(
            has_resource_permission=lambda rtype, rid, perm: False
        )
        visibility = (False, {"b"}, resolved)
        ds = [_ds("a"), _ds("b", publish_status="draft")]
        assert [d.id for d in _filter(ds, visibility)] == ["a", "b"]

    def test_system_context_applies_only_user_independent_checks(self):
        # No user (scheduled/system runs): drop disabled/inactive, keep the rest.
        ds = [_ds("a"), _ds("b", publish_status="disabled"), _ds("c", publish_status="draft")]
        result = asyncio.run(svc.filter_live_data_sources(None, ds, None, None))
        assert [d.id for d in result] == ["a", "c"]

    def test_empty_and_none_inputs(self):
        assert _filter([], NON_MANAGER) == []
        assert asyncio.run(svc.filter_live_data_sources(None, None, None, None)) == []
