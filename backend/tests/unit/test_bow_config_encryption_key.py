"""Contract for how BowConfig resolves ``encryption_key``.

Resolution order: explicit literal in the YAML → ``BOW_ENCRYPTION_KEY`` env
var → generated key (with a warning). The env var must win regardless of how
the YAML expresses "not set" (omitted, empty, ``${BOW_ENCRYPTION_KEY}``
placeholder), because that is the channel start.sh and k8s Secrets use.
"""
import logging

import pytest
from cryptography.fernet import Fernet

from app.settings.bow_config import BowConfig, generate_fernet_key

ENV_KEY = generate_fernet_key()
YAML_KEY = generate_fernet_key()

UNSET_FORMS = {
    "omitted": {},
    "empty": {"encryption_key": ""},
    "null": {"encryption_key": None},
    "placeholder": {"encryption_key": "${BOW_ENCRYPTION_KEY}"},
}


@pytest.fixture
def warnings_logged(monkeypatch):
    """Records every logging.Logger.warning call, independent of handlers,
    levels or logging.disable() that the app's logging setup may apply."""
    calls: list[str] = []
    monkeypatch.setattr(logging.Logger, "warning", lambda self, msg, *a, **k: calls.append(str(msg)))
    return calls


def _assert_valid_fernet(key: str) -> None:
    token = Fernet(key).encrypt(b"probe")
    assert Fernet(key).decrypt(token) == b"probe"


@pytest.mark.parametrize("yaml_fields", UNSET_FORMS.values(), ids=UNSET_FORMS.keys())
def test_env_var_is_used_when_yaml_does_not_set_a_key(monkeypatch, warnings_logged, yaml_fields):
    monkeypatch.setenv("BOW_ENCRYPTION_KEY", ENV_KEY)
    cfg = BowConfig(**yaml_fields)
    assert cfg.encryption_key == ENV_KEY
    assert "generating a random" not in " ".join(warnings_logged)


@pytest.mark.parametrize("yaml_fields", UNSET_FORMS.values(), ids=UNSET_FORMS.keys())
def test_key_is_generated_with_warning_when_nothing_is_set(monkeypatch, warnings_logged, yaml_fields):
    monkeypatch.delenv("BOW_ENCRYPTION_KEY", raising=False)
    first = BowConfig(**yaml_fields)
    second = BowConfig(**yaml_fields)
    _assert_valid_fernet(first.encryption_key)
    assert first.encryption_key != second.encryption_key, "generated keys are per-process"
    assert "generating a random" in " ".join(warnings_logged)


def test_explicit_yaml_literal_wins_over_env_var(monkeypatch, warnings_logged):
    monkeypatch.setenv("BOW_ENCRYPTION_KEY", ENV_KEY)
    cfg = BowConfig(encryption_key=YAML_KEY)
    assert cfg.encryption_key == YAML_KEY
    assert "generating a random" not in " ".join(warnings_logged)


def test_whitespace_around_env_var_is_stripped(monkeypatch):
    monkeypatch.setenv("BOW_ENCRYPTION_KEY", f"  {ENV_KEY}\n")
    assert BowConfig().encryption_key == ENV_KEY
