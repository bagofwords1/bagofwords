"""The migration chain has exactly one head.

Two heads make `alembic upgrade head` -- which start.sh runs on every boot --
fail, so a server upgraded to such a release does not come up. It happens
when two branches each add a migration on the same parent and both merge
(main, 2026-10-08: toolconfresp01 + dstexclcols01, fixed by mrgheads07).
"""
import pytest

from tests.fixtures.dashboard_corpus.alembic_at import MAIN_BASE_REVISION, script_directory


@pytest.mark.e2e
def test_migrations_have_a_single_head():
    heads = script_directory().get_heads()
    assert len(heads) == 1, f"alembic has {len(heads)} heads: {sorted(heads)}; add a merge revision"


@pytest.mark.e2e
def test_base_revision_is_an_ancestor_of_head():
    """The corpus is written at MAIN_BASE_REVISION; it must stay on the chain."""
    script = script_directory()
    head = script.get_current_head()
    ancestors = {rev.revision for rev in script.walk_revisions(base="base", head=head)}
    assert MAIN_BASE_REVISION in ancestors
