"""Unit tests for LLM-judge gating on the small-default model.

The judge must only run when the org has a small-default model that is
distinct from the regular default. A small default is almost always set —
provider creation flags the first enabled model as both default and small
default — so the gate requires ``is_small_default and not is_default`` on
the resolved model: a same-as-default small model (both flags on one row)
and resolution's silent fallback to the regular default are both rejected.
"""
import types

from app.ai.agents.judge.judge import judge_model_allowed


def _model(*, small=False, default=False):
    m = types.SimpleNamespace()
    m.is_small_default = small
    m.is_default = default
    return m


def test_no_model_disallows_judge():
    assert judge_model_allowed(None) is False


def test_fallback_default_model_disallows_judge():
    # What get_default_model(is_small=True) returns when no small default
    # exists: the regular default. The judge must not run on it.
    assert judge_model_allowed(_model(default=True)) is False


def test_distinct_small_default_model_allows_judge():
    assert judge_model_allowed(_model(small=True)) is True


def test_small_default_same_as_regular_default_disallows_judge():
    # Provider creation flags the first enabled model as BOTH default and
    # small default. That means the org has no separate small model, so the
    # judge must not run on it.
    assert judge_model_allowed(_model(small=True, default=True)) is False


def test_model_without_flag_attribute_disallows_judge():
    assert judge_model_allowed(object()) is False


def test_agent_scoring_gate_requires_small_default():
    from app.ai.agent_v2 import AgentV2

    setting_on = types.SimpleNamespace(value=True)

    def fake_agent(*, setting, report_type, small_model):
        settings = types.SimpleNamespace(get_config=lambda key: setting)
        return types.SimpleNamespace(
            organization_settings=settings,
            report_type=report_type,
            small_model=small_model,
        )

    # All conditions met → judge runs.
    agent = fake_agent(setting=setting_on, report_type="regular", small_model=_model(small=True))
    assert AgentV2._llm_judgement_enabled(agent) is True

    # No small default (fallback resolved the big default) → judge skipped.
    agent = fake_agent(setting=setting_on, report_type="regular", small_model=_model(default=True))
    assert AgentV2._llm_judgement_enabled(agent) is False

    # Small default is the same model as the regular default → judge skipped.
    agent = fake_agent(setting=setting_on, report_type="regular", small_model=_model(small=True, default=True))
    assert AgentV2._llm_judgement_enabled(agent) is False

    # No small model at all → judge skipped.
    agent = fake_agent(setting=setting_on, report_type="regular", small_model=None)
    assert AgentV2._llm_judgement_enabled(agent) is False

    # Setting off → judge skipped even with a small default.
    setting_off = types.SimpleNamespace(value=False)
    agent = fake_agent(setting=setting_off, report_type="regular", small_model=_model(small=True))
    assert AgentV2._llm_judgement_enabled(agent) is False

    # Non-regular report → judge skipped.
    agent = fake_agent(setting=setting_on, report_type="dashboard", small_model=_model(small=True))
    assert AgentV2._llm_judgement_enabled(agent) is False
