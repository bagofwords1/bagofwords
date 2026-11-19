"""auth.superuser_emails / BOW_SUPERUSER_EMAILS parsing.

Contract: a comma-separated env value or a YAML list; emails are trimmed,
lowercased and de-duplicated; a non-empty env value replaces the YAML list;
unset or blank means "no configured list".
"""
import pytest

from app.settings.bow_config import AuthConfig, BowConfig


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch):
    monkeypatch.delenv("BOW_SUPERUSER_EMAILS", raising=False)


def test_unset_means_no_configured_list():
    assert AuthConfig().superuser_emails == []


@pytest.mark.parametrize("blank", ["", "   ", " , ,, "])
def test_blank_env_value_means_no_configured_list(monkeypatch, blank):
    monkeypatch.setenv("BOW_SUPERUSER_EMAILS", blank)
    assert AuthConfig().superuser_emails == []


def test_env_value_is_split_trimmed_lowercased_and_deduplicated(monkeypatch):
    monkeypatch.setenv("BOW_SUPERUSER_EMAILS", " Root@Example.com ,ops@example.com,, ROOT@example.com ")
    assert AuthConfig().superuser_emails == ["root@example.com", "ops@example.com"]


def test_yaml_list_and_yaml_string_are_normalized():
    assert AuthConfig(superuser_emails=[" A@Example.com", "b@example.com "]).superuser_emails == [
        "a@example.com", "b@example.com",
    ]
    assert AuthConfig(superuser_emails="A@Example.com, b@example.com").superuser_emails == [
        "a@example.com", "b@example.com",
    ]
    assert AuthConfig(superuser_emails=None).superuser_emails == []


def test_env_value_replaces_the_yaml_list(monkeypatch):
    monkeypatch.setenv("BOW_SUPERUSER_EMAILS", "env@example.com")
    assert AuthConfig(superuser_emails=["yaml@example.com"]).superuser_emails == ["env@example.com"]


def test_blank_env_value_keeps_the_yaml_list(monkeypatch):
    monkeypatch.setenv("BOW_SUPERUSER_EMAILS", "  ")
    assert AuthConfig(superuser_emails=["yaml@example.com"]).superuser_emails == ["yaml@example.com"]


def test_existing_config_files_without_the_key_still_load(monkeypatch):
    monkeypatch.setenv("BOW_SUPERUSER_EMAILS", "root@example.com")
    config = BowConfig(auth={"mode": "hybrid"}, encryption_key="")
    assert config.auth.mode == "hybrid"
    assert config.auth.superuser_emails == ["root@example.com"]
