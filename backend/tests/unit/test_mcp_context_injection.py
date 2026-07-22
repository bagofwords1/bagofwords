"""Unit tests for per-user MCP context forwarding (headers + custom_metadata).

Covers the promised invariants of `app.services.mcp_context_injection`:
  * whitelist-only source resolution (unknown expressions never resolve)
  * static-value interpolation (identity concatenated into a literal)
  * locked vs ai merge semantics against model-supplied arguments
  * on_missing policy (empty / omit / block)
  * headers omit empties; static + injected headers merge
  * admin-locked metadata fields are stripped from the model-facing schema
"""

import pytest

from app.services.mcp_context_injection import (
    IdentityContext,
    apply_metadata_injection,
    build_plan,
    filter_locked_from_schema,
    resolve_source,
)


def _ctx(**kw):
    base = dict(
        email="Beni.Klein@elbitsystems.com",
        name="Beni Klein",
        user_id="usr_8f21c0",
        role="analyst",
        attributes={"employeeId": "dp28376", "department": "Avionics"},
    )
    base.update(kw)
    return IdentityContext(**base)


# --------------------------------------------------------------------------- #
# source resolution + whitelist safety
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "source,expected",
    [
        ("user.email", "Beni.Klein@elbitsystems.com"),
        ("user.name", "Beni Klein"),
        ("user.id", "usr_8f21c0"),
        ("membership.role", "analyst"),
        ("membership.attr:employeeId", "dp28376"),
        ("membership.attr:department", "Avionics"),
        ("static:BagOfWords", "BagOfWords"),
    ],
)
def test_resolve_source_whitelist(source, expected):
    assert resolve_source(source, _ctx()) == expected


@pytest.mark.parametrize(
    "source",
    [
        "user.password",            # not whitelisted
        "membership.attr:secret",   # attribute not present
        "connection.credentials",   # not a real source
        "__class__",                # no arbitrary attribute access
        "",                         # empty
    ],
)
def test_unknown_or_absent_sources_do_not_resolve(source):
    assert resolve_source(source, _ctx()) is None


def test_static_interpolation_concatenates_identity():
    # The `_client_full_userId: "elbit_nt\\dp28376"` case from the report.
    out = resolve_source(r"static:elbit_nt\{membership.attr:employeeId}", _ctx())
    assert out == r"elbit_nt\dp28376"


def test_static_interpolation_of_missing_attr_yields_empty_token():
    out = resolve_source("static:prefix-{membership.attr:missing}", _ctx())
    assert out == "prefix-"


# --------------------------------------------------------------------------- #
# metadata merge: locked vs ai
# --------------------------------------------------------------------------- #

def _meta_config(fields, argument_key="custom_metadata"):
    return {"metadata_injection": {"argument_key": argument_key, "fields": fields}}


def test_locked_field_overrides_model_value():
    cfg = _meta_config([
        {"name": "_client_userId", "source": "membership.attr:employeeId", "mode": "locked"},
    ])
    plan = build_plan(cfg, _ctx())
    args = {"prompt": "p", "custom_metadata": {"_client_userId": "hacked_by_model"}}
    apply_metadata_injection(args, plan)
    assert args["custom_metadata"]["_client_userId"] == "dp28376"


def test_ai_field_fills_only_when_absent():
    cfg = _meta_config([
        {"name": "department", "source": "membership.attr:department", "mode": "ai"},
    ])
    plan = build_plan(cfg, _ctx())

    # model omitted it -> server fills
    a = {"prompt": "p"}
    apply_metadata_injection(a, plan)
    assert a["custom_metadata"]["department"] == "Avionics"

    # model provided it -> model wins
    b = {"prompt": "p", "custom_metadata": {"department": "Marine"}}
    apply_metadata_injection(b, plan)
    assert b["custom_metadata"]["department"] == "Marine"


def test_injection_creates_bag_and_respects_argument_key():
    cfg = _meta_config(
        [{"name": "u", "source": "user.email", "mode": "locked"}],
        argument_key="context",
    )
    plan = build_plan(cfg, _ctx())
    args = {"prompt": "p"}
    apply_metadata_injection(args, plan)
    assert args["context"]["u"] == "Beni.Klein@elbitsystems.com"
    assert "custom_metadata" not in args


# --------------------------------------------------------------------------- #
# on_missing policy
# --------------------------------------------------------------------------- #

def test_on_missing_empty_sets_blank_string():
    cfg = _meta_config([
        {"name": "user_id", "source": "membership.attr:notThere", "mode": "locked", "on_missing": "empty"},
    ])
    plan = build_plan(cfg, _ctx())
    args = {}
    apply_metadata_injection(args, plan)
    assert args["custom_metadata"]["user_id"] == ""


def test_on_missing_omit_drops_key():
    cfg = _meta_config([
        {"name": "user_id", "source": "membership.attr:notThere", "mode": "locked", "on_missing": "omit"},
    ])
    plan = build_plan(cfg, _ctx())
    args = {}
    apply_metadata_injection(args, plan)
    assert "user_id" not in args.get("custom_metadata", {})


def test_on_missing_block_signals_blocking_missing():
    cfg = _meta_config([
        {"name": "user_id", "source": "membership.attr:notThere", "mode": "locked", "on_missing": "block"},
    ])
    plan = build_plan(cfg, _ctx())
    assert "user_id" in plan.blocking_missing


def test_present_value_never_blocks():
    cfg = _meta_config([
        {"name": "user_id", "source": "membership.attr:employeeId", "mode": "locked", "on_missing": "block"},
    ])
    plan = build_plan(cfg, _ctx())
    assert plan.blocking_missing == []


# --------------------------------------------------------------------------- #
# headers
# --------------------------------------------------------------------------- #

def test_header_injection_resolves_and_omits_empties():
    cfg = {
        "headers": {"X-Static": "fixed"},
        "header_injection": [
            {"header": "X-User-Email", "source": "user.email"},
            {"header": "X-Missing", "source": "membership.attr:notThere"},
        ],
    }
    plan = build_plan(cfg, _ctx())
    assert plan.headers["X-User-Email"] == "Beni.Klein@elbitsystems.com"
    assert plan.headers["X-Static"] == "fixed"          # static preserved
    assert "X-Missing" not in plan.headers              # empty header omitted


def test_dynamic_header_overrides_static_same_name():
    cfg = {
        "headers": {"X-User-Email": "placeholder"},
        "header_injection": [{"header": "X-User-Email", "source": "user.email"}],
    }
    plan = build_plan(cfg, _ctx())
    assert plan.headers["X-User-Email"] == "Beni.Klein@elbitsystems.com"


def test_no_spec_produces_empty_plan():
    plan = build_plan({"server_url": "http://x/mcp", "transport": "streamable_http"}, _ctx())
    assert not plan.has_headers and not plan.has_metadata


# --------------------------------------------------------------------------- #
# schema hiding
# --------------------------------------------------------------------------- #

_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string"},
        "company": {"type": "string"},
        "custom_metadata": {
            "type": "object",
            "properties": {
                "_client_userId": {"type": "string"},
                "_client_full_userId": {"type": "string"},
                "department": {"type": "string"},
            },
            "required": ["_client_userId"],
        },
    },
    "required": ["prompt"],
}


def test_locked_fields_hidden_ai_fields_kept():
    cfg = _meta_config([
        {"name": "_client_userId", "source": "membership.attr:employeeId", "mode": "locked"},
        {"name": "_client_full_userId", "source": "static:x", "mode": "locked"},
        {"name": "department", "source": "membership.attr:department", "mode": "ai"},
    ])
    filtered = filter_locked_from_schema(_SCHEMA, cfg)
    meta_props = filtered["properties"]["custom_metadata"]["properties"]
    assert "_client_userId" not in meta_props        # locked -> hidden
    assert "_client_full_userId" not in meta_props    # locked -> hidden
    assert "department" in meta_props                 # ai -> visible
    # locked field also removed from required
    assert "_client_userId" not in filtered["properties"]["custom_metadata"].get("required", [])
    # top-level args untouched
    assert "prompt" in filtered["properties"] and "company" in filtered["properties"]


def test_filter_is_nondestructive_to_input():
    cfg = _meta_config([{"name": "_client_userId", "source": "static:x", "mode": "locked"}])
    filter_locked_from_schema(_SCHEMA, cfg)
    # original schema still has the field
    assert "_client_userId" in _SCHEMA["properties"]["custom_metadata"]["properties"]


def test_no_locked_fields_returns_schema_unchanged():
    cfg = _meta_config([{"name": "department", "source": "membership.attr:department", "mode": "ai"}])
    filtered = filter_locked_from_schema(_SCHEMA, cfg)
    assert filtered["properties"]["custom_metadata"]["properties"].keys() == \
        _SCHEMA["properties"]["custom_metadata"]["properties"].keys()
