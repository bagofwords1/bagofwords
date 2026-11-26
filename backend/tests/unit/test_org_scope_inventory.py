"""Every mapped model is placed relative to the organization boundary.

A model either has an organization column (checked by the query guard), or is
listed in ``app.core.org_scope_inventory`` as reached through a parent, as
deployment-wide, or as still awaiting a decision. A new model that is none of
these fails here until someone classifies it.
"""
import importlib

import main  # noqa: F401 — registers the mappers the app imports at startup

from app.core.org_scope_guard import org_columns_of
from app.core.org_scope_inventory import GLOBAL, ORG_ROOT, PARENT_SCOPED, UNCLASSIFIED_REVIEW
from app.models.base import Base


# These model modules are imported only by the code path that uses them, so
# which mappers exist would otherwise depend on what ran earlier in the process.
for _module in ("email_delivery_claim", "file_index", "login_exchange_code",
                "pending_blob_delete", "report_context_state", "report_view"):
    importlib.import_module(f"app.models.{_module}")


def _mappers():
    return {mapper.class_.__name__: mapper for mapper in Base.registry.mappers}


def _org_scoped():
    return {
        name for name, mapper in _mappers().items()
        if name != ORG_ROOT and org_columns_of(mapper.local_table)
    }


def test_every_model_without_an_organization_column_is_classified():
    listed = set(PARENT_SCOPED) | GLOBAL | set(UNCLASSIFIED_REVIEW) | {ORG_ROOT}
    unclassified = sorted(set(_mappers()) - _org_scoped() - listed)
    assert not unclassified, (
        f"{unclassified} have no organization column and are not classified. Add an "
        "organization_id, or list each in app/core/org_scope_inventory.py as "
        "PARENT_SCOPED (naming the parent), GLOBAL, or UNCLASSIFIED_REVIEW."
    )


def test_the_inventory_lists_each_model_once_and_only_real_unscoped_models():
    groups = [set(PARENT_SCOPED), set(GLOBAL), set(UNCLASSIFIED_REVIEW)]
    listed = set().union(*groups)
    assert sum(len(group) for group in groups) == len(listed), "a model is listed twice"
    assert listed <= set(_mappers()), sorted(listed - set(_mappers()))
    # A stale entry would keep describing a model as unchecked after it gained
    # an organization column and came under the query guard.
    assert not listed & _org_scoped(), sorted(listed & _org_scoped())


def test_each_parent_scoped_model_reaches_an_organization_through_its_parents():
    mappers = _mappers()
    org_scoped = _org_scoped()

    for child, parents in PARENT_SCOPED.items():
        assert parents, child
        referenced = {fk.column.table.name for fk in mappers[child].local_table.foreign_keys}
        for parent in parents:
            assert parent in mappers, f"{child}: unknown parent {parent}"
            assert mappers[parent].local_table.name in referenced, (
                f"{child} has no foreign key to {parent}"
            )

    def reaches_organization(name, seen=()):
        if name in org_scoped or name == ORG_ROOT:
            return True
        if name in seen or name not in PARENT_SCOPED:
            return False
        return all(reaches_organization(p, seen + (name,)) for p in PARENT_SCOPED[name])

    dangling = sorted(name for name in PARENT_SCOPED if not reaches_organization(name))
    assert not dangling, f"parent chain does not end at an organization-scoped model: {dangling}"
