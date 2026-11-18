"""What a connection keeps when its auth method is exclusive.

For every connector that declares its methods exclusive, credentials saved
under one method and then switched to another must come out holding only the
new method's fields (plus shared `oauth_*` sign-in overrides), and nothing the
new method needs may be lost. A connection that names no method keeps all of
its fields. The e2e tests in tests/e2e/test_connection_auth_method_switch.py
drive the same rule through the API for representative connectors.
"""

from __future__ import annotations

import pytest

from app.schemas.data_source_registry import REGISTRY, credentials_for_auth_method


def _accepts_extras(schema) -> bool:
    return (getattr(schema, "model_config", None) or {}).get("extra") == "allow"


def _switches():
    for ds_type, entry in sorted(REGISTRY.items()):
        options = entry.credentials_auth
        if not options.exclusive_methods:
            continue
        system = {n: v for n, v in options.by_auth.items() if "system" in (v.scopes or [])}
        for new, new_variant in system.items():
            for old, old_variant in system.items():
                if old != new:
                    yield pytest.param(ds_type, old_variant.schema, new, new_variant.schema, id=f"{ds_type}:{old}->{new}")


SWITCHES = list(_switches())


def test_connectors_with_known_wrong_branch_bugs_are_exclusive():
    # Their clients choose a method by which fields are present; see the
    # e2e tests for the observable failure each one had.
    for ds_type in ("MSSQL", "neo4j", "trino", "snowflake", "elasticsearch", "aws_redshift", "druid", "bigquery"):
        assert REGISTRY[ds_type].credentials_auth.exclusive_methods, ds_type


@pytest.mark.parametrize("ds_type,old_schema,new,new_schema", SWITCHES)
def test_switching_method_keeps_only_what_the_new_method_owns(ds_type, old_schema, new, new_schema):
    old_fields = {name: f"old-{name}" for name in old_schema.model_fields}
    new_fields = {name: f"new-{name}" for name in new_schema.model_fields}
    saved = {**old_fields, **new_fields}

    kept = credentials_for_auth_method(ds_type, new, saved)

    # Nothing the new method declares is lost, and its values win.
    assert {k: kept.get(k) for k in new_fields} == new_fields
    if _accepts_extras(new_schema):
        assert kept == saved
    else:
        assert set(kept) <= set(new_fields) | {k for k in saved if k.startswith("oauth_")}


@pytest.mark.parametrize("ds_type,old_schema,new,new_schema", SWITCHES)
def test_a_connection_that_names_no_method_keeps_its_fields(ds_type, old_schema, new, new_schema):
    """Without a saved method only a deployment-identity marker goes: that
    method must be chosen by name, where the operator's switch is checked."""
    options = REGISTRY[ds_type].credentials_auth
    system = [v for v in options.by_auth.values() if "system" in (v.scopes or [])]
    ungated = {f for v in system if not v.requires_security_flag for f in v.schema.model_fields}
    saved = {name: "x" for name in {*old_schema.model_fields, *new_schema.model_fields}}

    kept = credentials_for_auth_method(ds_type, None, saved)

    assert {k: v for k, v in saved.items() if k in ungated or k.startswith("oauth_")} == kept
