"""Unit tests for per-user MCP context forwarding (headers + custom_metadata).

Covers the promised invariants of `app.services.mcp_context_injection`:
  * whitelist-only source resolution (unknown expressions never resolve)
  * static-value interpolation (identity concatenated into a literal)
  * locked vs ai merge semantics against model-supplied arguments
  * on_missing policy (empty / omit / block)
  * headers omit empties; static + injected headers merge
  * admin-locked metadata fields are stripped from the model-facing schema
  * dynamic params: an admin allow-list {key: default}; the caller's
    per-question metadata fills listed keys only, defaults fill the rest
    (null = omit), a field row always wins, keys are hidden from the model,
    and a malformed allow-list is rejected
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
        email="jane.doe@example.com",
        name="Jane Doe",
        user_id="usr_8f21c0",
        role="analyst",
        attributes={"employeeId": "u12345", "department": "Engineering"},
    )
    base.update(kw)
    return IdentityContext(**base)


# --------------------------------------------------------------------------- #
# source resolution + whitelist safety
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "source,expected",
    [
        ("user.email", "jane.doe@example.com"),
        ("user.name", "Jane Doe"),
        ("user.id", "usr_8f21c0"),
        ("membership.role", "analyst"),
        ("membership.attr:employeeId", "u12345"),
        ("membership.attr:department", "Engineering"),
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
    # The `_client_full_userId: "corp_nt\\u12345"` case from the report.
    out = resolve_source(r"static:corp_nt\{membership.attr:employeeId}", _ctx())
    assert out == r"corp_nt\u12345"


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
    assert args["custom_metadata"]["_client_userId"] == "u12345"


def test_ai_field_fills_only_when_absent():
    cfg = _meta_config([
        {"name": "department", "source": "membership.attr:department", "mode": "ai"},
    ])
    plan = build_plan(cfg, _ctx())

    # model omitted it -> server fills
    a = {"prompt": "p"}
    apply_metadata_injection(a, plan)
    assert a["custom_metadata"]["department"] == "Engineering"

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
    assert args["context"]["u"] == "jane.doe@example.com"
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
    assert plan.headers["X-User-Email"] == "jane.doe@example.com"
    assert plan.headers["X-Static"] == "fixed"          # static preserved
    assert "X-Missing" not in plan.headers              # empty header omitted


def test_dynamic_header_overrides_static_same_name():
    cfg = {
        "headers": {"X-User-Email": "placeholder"},
        "header_injection": [{"header": "X-User-Email", "source": "user.email"}],
    }
    plan = build_plan(cfg, _ctx())
    assert plan.headers["X-User-Email"] == "jane.doe@example.com"


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


# --------------------------------------------------------------------------- #
# dynamic params (allow-list on the connection, values from the request)
# --------------------------------------------------------------------------- #

from app.services.mcp_context_injection import validate_dynamic_params  # noqa: E402
from app.utils.request_metadata import validate_request_metadata  # noqa: E402

_ALLOWED = {"chat_id": None, "message_id": None, "env": "prod", "tags": []}


def _dyn_config(allowed=_ALLOWED, fields=(), enabled=True, argument_key="custom_metadata"):
    return {"metadata_injection": {
        "argument_key": argument_key,
        "fields": list(fields),
        "dynamic_params_enabled": enabled,
        "dynamic_params": allowed,
    }}


def _inject(cfg, request_metadata, args=None):
    args = {"prompt": "p"} if args is None else args
    apply_metadata_injection(args, build_plan(cfg, _ctx(), request_metadata))
    return args


def test_sent_values_fill_allowed_keys_with_their_json_types():
    sent = {"chat_id": "c-77", "message_id": 912, "env": "staging", "tags": ["a", {"b": True}]}
    assert _inject(_dyn_config(), sent)["custom_metadata"] == sent


def test_unsent_keys_take_their_default_and_null_defaults_are_omitted():
    cm = _inject(_dyn_config(), {"chat_id": "c-77"})["custom_metadata"]
    assert cm == {"chat_id": "c-77", "env": "prod", "tags": []}
    assert "message_id" not in cm


def test_keys_not_in_the_allow_list_are_dropped():
    cm = _inject(_dyn_config(), {"chat_id": "c-77", "not_listed": "x"})["custom_metadata"]
    assert "not_listed" not in cm and cm["chat_id"] == "c-77"


def test_no_request_metadata_still_sends_defaults():
    assert _inject(_dyn_config(), None)["custom_metadata"] == {"env": "prod", "tags": []}


def test_toggle_off_sends_nothing_from_request_or_defaults():
    plan = build_plan(_dyn_config(enabled=False), _ctx(), {"chat_id": "c-77"})
    assert not plan.has_metadata
    assert "custom_metadata" not in _inject(_dyn_config(enabled=False), {"chat_id": "c-77"})


def test_empty_allow_list_lets_nothing_through():
    plan = build_plan(_dyn_config(allowed={}), _ctx(), {"chat_id": "c-77"})
    assert not plan.has_metadata


@pytest.mark.parametrize("mode", ["locked", "ai"])
def test_fixed_field_wins_over_an_allowed_key(mode):
    cfg = _dyn_config(allowed={"department": "default-dept", "chat_id": None},
                      fields=[{"name": "department", "source": "membership.attr:department", "mode": mode}])
    cm = _inject(cfg, {"department": "from-caller", "chat_id": "c-77"})["custom_metadata"]
    assert cm == {"department": "Engineering", "chat_id": "c-77"}


def test_dynamic_values_override_model_values():
    args = {"prompt": "p", "custom_metadata": {"chat_id": "set-by-model", "note": "kept"}}
    cm = _inject(_dyn_config(allowed={"chat_id": None}), {"chat_id": "c-77"}, args)["custom_metadata"]
    assert cm == {"chat_id": "c-77", "note": "kept"}


def test_values_are_not_interpolated():
    cm = _inject(_dyn_config(allowed={"literal": "{user.email}"}), None)["custom_metadata"]
    assert cm == {"literal": "{user.email}"}


def test_dynamic_params_respect_argument_key():
    args = _inject(_dyn_config(allowed={"chat_id": None}, argument_key="context"), {"chat_id": "c-77"})
    assert args["context"] == {"chat_id": "c-77"} and "custom_metadata" not in args


def test_injection_does_not_mutate_the_callers_nested_object():
    shared = {"note": "from model"}
    args = _inject(_dyn_config(allowed={"chat_id": None}), {"chat_id": "c-77"},
                   {"prompt": "p", "custom_metadata": shared})
    assert shared == {"note": "from model"}
    assert args["custom_metadata"] == {"note": "from model", "chat_id": "c-77"}


def test_allowed_keys_are_hidden_from_the_model_schema_unless_a_row_owns_them():
    cfg = _dyn_config(allowed={"_client_full_userId": None, "department": None},
                      fields=[{"name": "department", "source": "membership.attr:department", "mode": "ai"}])
    meta_props = filter_locked_from_schema(_SCHEMA, cfg)["properties"]["custom_metadata"]["properties"]
    assert "_client_full_userId" not in meta_props  # allowed dynamic param -> hidden like locked
    assert "department" in meta_props               # owned by an ai row -> stays visible


def test_disabled_allow_list_is_not_hidden_from_the_model():
    cfg = _dyn_config(allowed={"_client_full_userId": None}, enabled=False)
    meta_props = filter_locked_from_schema(_SCHEMA, cfg)["properties"]["custom_metadata"]["properties"]
    assert "_client_full_userId" in meta_props


@pytest.mark.parametrize("allowed", [{"a": 1}, {}, None])
def test_valid_allow_list_passes_validation(allowed):
    validate_dynamic_params(_dyn_config(allowed=allowed))


@pytest.mark.parametrize(
    "allowed, reason",
    [
        ("not-an-object", "a JSON object"),
        (["a"], "a JSON object"),
        ({"": 1}, "keys must not be empty"),
        ({"blob": "x" * (8 * 1024)}, "the limit is"),
    ],
)
def test_invalid_allow_list_is_rejected(allowed, reason):
    with pytest.raises(ValueError, match=reason):
        validate_dynamic_params(_dyn_config(allowed=allowed))


@pytest.mark.parametrize("value", [{"a": 1}, {}, None, {"blob": "x" * (8 * 1024 - 20)}])
def test_valid_request_metadata_passes(value):
    assert validate_request_metadata(value) == value


@pytest.mark.parametrize(
    "value, reason",
    [({"": 1}, "must not be empty"), ({"blob": "x" * (8 * 1024)}, "the limit is")],
)
def test_invalid_request_metadata_is_rejected(value, reason):
    with pytest.raises(ValueError, match=reason):
        validate_request_metadata(value)
