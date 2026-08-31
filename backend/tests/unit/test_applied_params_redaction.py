"""`applied_params` must not carry an identity across a reader boundary.

The values a snapshot was materialized with include, for an identity-derived
param, an identity that is not the reader's. `code` and `data` are already
gated on that boundary; redact_applied_params gates this one.

Since identity-derived params also taint the step (identity_taint), a reader
who would see such a value is normally withheld before redaction is reached.
That makes the name-stripping branch defense in depth — the two mechanisms are
computed independently, so it must keep working on its own. These tests
exercise it directly rather than through a route.
"""
import pytest

from app.services.viewer_data_policy import redact_applied_params

SPECS = [
    {"name": "Region", "type": "string", "source": "input"},
    {"name": "Viewer", "type": "string", "source": "identity",
     "identity_binding": "viewer.email"},
    {"name": "Dept", "type": "string", "source": "input_identity_default",
     "identity_binding": "viewer.profile_attributes.department"},
]

APPLIED = {
    "Region": "EMEA",
    "Viewer": "owner@example.com",
    "Dept": "finance",
}


def test_withheld_reader_gets_nothing():
    assert redact_applied_params(APPLIED, SPECS, withheld=True) is None


def test_identity_sources_are_stripped_and_ordinary_params_kept():
    assert redact_applied_params(APPLIED, SPECS, withheld=False) == {
        "Region": "EMEA"
    }


def test_input_identity_default_is_stripped_too():
    """After resolution a client-supplied value is indistinguishable from the
    identity fallback, so this fails closed."""
    out = redact_applied_params(APPLIED, SPECS, withheld=False)
    assert "Dept" not in out
    assert "finance" not in str(out)


def test_all_identity_params_collapses_to_none_not_empty_dict():
    """None keeps the field absent rather than shipping {}."""
    only_identity = [s for s in SPECS if s["source"] != "input"]
    applied = {"Viewer": "owner@example.com", "Dept": "finance"}
    assert redact_applied_params(applied, only_identity, withheld=False) is None


def test_no_declared_params_passes_values_through():
    assert redact_applied_params({"Region": "EMEA"}, None, withheld=False) == {
        "Region": "EMEA"
    }


@pytest.mark.parametrize("applied", [None, {}])
def test_empty_applied_params_is_none(applied):
    assert redact_applied_params(applied, SPECS, withheld=False) is None


def test_unparseable_spec_rows_do_not_lose_the_identity_names():
    """parse_param_specs skips garbage rows. A malformed row must not take a
    real identity spec down with it."""
    specs = [{"name": "!!bad name!!", "source": "input"}] + SPECS
    assert redact_applied_params(APPLIED, specs, withheld=False) == {
        "Region": "EMEA"
    }
