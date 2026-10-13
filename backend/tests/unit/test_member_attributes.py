"""Pure rules for admin-set member attributes (services/member_attributes.py)."""

from types import SimpleNamespace

import pytest

from app.errors import AppError
from app.services.member_attributes import effective_member_attributes, normalize_custom_attributes


@pytest.mark.parametrize("raw,expected", [
    ({" qlik_user_id ": " dana "}, {"qlik_user_id": "dana"}),
    ({"a.b-c_d": "x"}, {"a.b-c_d": "x"}),
    ({"empty": "", "none": None, "kept": "v"}, {"kept": "v"}),
    ({"n": 42}, {"n": "42"}),
    (None, {}),
])
def test_values_are_trimmed_and_blanks_dropped(raw, expected):
    assert normalize_custom_attributes(raw) == expected


@pytest.mark.parametrize("raw", [
    {"1starts_with_digit": "x"},
    {"has space": "x"},
    {"": "x"},
    {"k" * 65: "x"},
    {"k": "v" * 257},
    {"k": ["list"]},
    {"k": {"nested": "dict"}},
    {f"k{i}": "v" for i in range(51)},
    ["not", "a", "mapping"],
])
def test_invalid_input_is_a_typed_400(raw):
    with pytest.raises(AppError) as exc:
        normalize_custom_attributes(raw)
    assert exc.value.status_code == 400
    assert exc.value.error_code == "member_attributes.invalid"


def test_admin_values_win_over_idp_synced_values():
    membership = SimpleNamespace(
        profile_attributes={"department": "Sales", "employeeId": "synced"},
        custom_attributes={"employeeId": "admin"},
    )
    assert effective_member_attributes(membership) == {"department": "Sales", "employeeId": "admin"}
    assert effective_member_attributes(None) == {}
    assert effective_member_attributes(SimpleNamespace(profile_attributes=None, custom_attributes=None)) == {}
