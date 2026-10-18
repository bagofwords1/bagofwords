"""Generic webhook adapter: the summary (used as the trigger run's chat title)
must be readable text whatever shape the sender gives each field."""
import pytest

from app.services.webhook_adapters.generic_adapter import GenericAdapter


def _summary(payload: dict) -> str:
    return GenericAdapter().normalize({}, payload)["summary"]


@pytest.mark.parametrize("field", ["type", "event", "action"])
def test_reference_object_event_key_uses_display_value(field):
    payload = {field: {"value": "id-123", "display_value": "alert"},
               "message": "Something happened"}
    s = _summary(payload)
    assert s == "alert: Something happened"
    assert "{" not in s and "id-123" not in s


@pytest.mark.parametrize("field", ["title", "name", "message"])
def test_reference_object_title_uses_display_value(field):
    payload = {"type": "alert", field: {"value": "id-456", "display_value": "Disk full"}}
    assert _summary(payload) == "alert: Disk full"


def test_object_without_display_value_falls_back_to_value():
    assert _summary({"type": {"value": "incident"}}) == "incident"


def test_empty_reference_object_is_skipped():
    payload = {"type": {"value": "", "display_value": ""}, "event": "push", "title": "x"}
    assert _summary(payload) == "push: x"


def test_plain_strings_unchanged():
    assert _summary({"type": "deploy", "title": "v1.2 shipped"}) == "deploy: v1.2 shipped"
    assert _summary({}) == "event"


def test_list_field_is_joined():
    assert _summary({"type": "tags", "title": ["a", {"display_value": "b"}]}) == "tags: a, b"
