"""The driver registry: backend types, their forms, and lazy loading."""
import sys

import pytest

from app.services.sandbox.drivers import registry
from app.services.sandbox.drivers.base import ALL_CAPABILITIES, BackendContext, SandboxDriver, SandboxDriverError
from app.settings.config import settings


def test_the_fake_type_is_offered_only_for_tests_or_by_explicit_opt_in(monkeypatch):
    assert "fake" in {t.name for t in registry.backend_types()}  # the suite runs under TESTING

    monkeypatch.setattr(settings, "TESTING", False)
    monkeypatch.delenv("BOW_AGENT_SANDBOXES_FAKE_DRIVER", raising=False)
    assert "fake" not in {t.name for t in registry.backend_types()}
    assert "docker-ssh" in {t.name for t in registry.backend_types()}
    # Not listed means not usable either: an existing row cannot reach it.
    with pytest.raises(SandboxDriverError) as refused:
        registry.create_driver("fake", BackendContext(backend_id="b", name="b", instance_id="i"))
    assert refused.value.code == "unknown_type"

    monkeypatch.setenv("BOW_AGENT_SANDBOXES_FAKE_DRIVER", "1")
    assert "fake" in {t.name for t in registry.backend_types()}


def test_unknown_types_are_refused():
    with pytest.raises(SandboxDriverError) as refused:
        registry.get_backend_type("kubernetes")
    assert refused.value.code == "unknown_type"


def test_a_type_describes_its_own_form_and_capabilities():
    described = registry.describe_type("fake")
    assert described["name"] == "fake"
    assert "strong_isolation" in described["config_schema"]["properties"]
    assert "api_token" in described["credentials_schema"]["properties"]
    assert set(described["capabilities"]) <= ALL_CAPABILITIES and described["capabilities"]


def test_config_is_validated_and_normalised_against_the_types_schema():
    assert registry.validate_config("fake", {})["strong_isolation"] is True
    assert registry.validate_config("fake", {"strong_isolation": False})["strong_isolation"] is False
    with pytest.raises(SandboxDriverError) as refused:
        registry.validate_config("fake", {"no_such_setting": 1})
    assert refused.value.code == "invalid_config"
    assert "no_such_setting" in refused.value.message


def test_credential_errors_never_echo_the_input():
    with pytest.raises(SandboxDriverError) as refused:
        registry.validate_credentials("fake", {"api_token": {"nested": "hunter2-secret"}})
    assert refused.value.code == "invalid_credentials"
    assert "hunter2" not in refused.value.message


def test_empty_credentials_are_dropped_and_one_time_ones_are_marked():
    assert registry.validate_credentials("fake", {"api_token": "t", "one_time_password": ""}) == {"api_token": "t"}
    assert registry.transient_credential_fields("fake") == ["one_time_password"]


def test_drivers_are_built_from_the_registry_and_implement_the_interface():
    driver = registry.create_driver("fake", BackendContext(backend_id="b", name="b", instance_id="i"))
    assert isinstance(driver, SandboxDriver)
    assert driver.capabilities() <= ALL_CAPABILITIES
    assert [s.key for s in driver.prepare_steps()]


def test_only_safe_driver_state_is_public():
    info = registry.public_info("fake", {"isolation": "strong", "owner": "inst", "internal_secret": "x"})
    assert info == {"isolation": "strong", "owner": "inst"}


def test_listing_types_does_not_load_any_driver():
    for name in [m for m in sys.modules if m.startswith("app.services.sandbox.drivers.docker_ssh")]:
        del sys.modules[name]
    registry.backend_types()
    registry.get_backend_type("docker-ssh")
    assert not [m for m in sys.modules if m.startswith("app.services.sandbox.drivers.docker_ssh")]
