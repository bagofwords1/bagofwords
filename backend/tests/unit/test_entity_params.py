"""Identity-scoped entities — policy + resolution contracts (DB-free parts).

The e2e sandbox covers the full loop (promotion carries declarations,
load_entity resolves per viewer, EntityUserResult caching); these pin the
server-side invariants that must hold regardless.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.services.viewer_data_policy import entity_data_withheld


def _entity(**kw):
    base = dict(id="e1", owner_id="owner-1", parameters=None, data={"rows": [1]})
    base.update(kw)
    return SimpleNamespace(**base)


def test_identity_param_entity_withheld_from_non_owner():
    ent = _entity(parameters=[{"name": "assignee_email", "source": "identity"}])
    viewer = SimpleNamespace(id="viewer-2")
    # The identity-param check must short-circuit BEFORE any DB access —
    # db=None proves it (a DB touch would explode).
    assert asyncio.run(entity_data_withheld(None, ent, viewer)) is True


def test_identity_param_entity_visible_to_owner():
    ent = _entity(parameters=[{"name": "assignee_email", "source": "identity"}])
    owner = SimpleNamespace(id="owner-1")
    assert asyncio.run(entity_data_withheld(None, ent, owner)) is False


def test_input_param_entity_not_identity_withheld():
    ent = _entity(parameters=[{"name": "region", "source": "input"}])
    viewer = SimpleNamespace(id="viewer-2")
    # Falls through to the credential checks, which need the DB — a non-DB
    # call raising (rather than returning True) proves the identity branch
    # did NOT fire for input params.
    with pytest.raises(Exception):
        asyncio.run(entity_data_withheld(None, ent, viewer))


def test_entity_params_resolution_binds_run_user():
    from app.schemas.param_schema import parse_param_specs
    from app.ai.code_execution.query_params import resolve_param_values

    specs = parse_param_specs([
        {"name": "assignee_email", "type": "string", "source": "identity",
         "identity_binding": "viewer.email"},
        {"name": "status", "type": "string", "source": "input", "default": "open"},
    ])
    identity = SimpleNamespace(
        user_id="u2", email="sarah@example.com", profile_attributes={}, group_names=set()
    )
    out = resolve_param_values(specs, None, identity)
    assert out == {"assignee_email": "sarah@example.com", "status": "open"}
