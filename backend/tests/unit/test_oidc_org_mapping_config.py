"""`org_mapping` on an OIDC provider: accepted forms and load-time errors."""
import pytest
from pydantic import ValidationError

from app.settings.bow_config import OIDCProvider


def _provider(**options):
    return OIDCProvider(name="corp", issuer="https://idp.example", **options)


def _rules(provider):
    return [(r.group, r.organization, r.role) for r in provider.org_mapping]


def test_no_mapping_by_default():
    assert _provider().org_mapping == []
    assert _provider(org_mapping=None).org_mapping == []


def test_object_and_compact_forms_mean_the_same():
    group, org = "0f4c2a1e-1111-4222-8333-444455556666", "9b1d7c3a-aaaa-4bbb-8ccc-ddddeeeeffff"
    as_objects = _provider(org_mapping=[{"group": group, "organization": org, "role": "admin"}])
    as_strings = _provider(org_mapping=[f"{group}:{org}:admin"])
    assert _rules(as_objects) == _rules(as_strings) == [(group, org, "admin")]


def test_forms_can_be_mixed_and_role_defaults_to_member():
    provider = _provider(org_mapping=[
        "Finance Analysts:Finance:Member",
        {"group": "hr-all", "organization": "People"},
    ])
    assert _rules(provider) == [("Finance Analysts", "Finance", "member"), ("hr-all", "People", "member")]


def test_compact_form_splits_on_the_last_two_colons():
    provider = _provider(org_mapping=["urn:corp:groups:finance:Finance:admin"])
    assert _rules(provider) == [("urn:corp:groups:finance", "Finance", "admin")]


@pytest.mark.parametrize("entry", [
    "only-a-group",
    "group:organization",
    "group:organization:owner",
    ":organization:member",
    "group::member",
    {"group": "g", "organization": "o", "role": "superuser"},
    {"group": "g"},
    {"group": "g", "organization": "o", "rol": "admin"},
    42,
])
def test_invalid_entries_are_rejected_when_the_config_loads(entry):
    with pytest.raises(ValidationError):
        _provider(org_mapping=[entry])


def test_mapping_must_be_a_list():
    with pytest.raises(ValidationError):
        _provider(org_mapping="group:organization:member")


def test_provider_name_must_leave_room_for_the_membership_marker():
    long_name = "p" * 60
    assert OIDCProvider(name=long_name, issuer="https://idp.example").org_mapping == []
    with pytest.raises(ValidationError):
        OIDCProvider(name=long_name, issuer="https://idp.example", org_mapping=["g:o:member"])
