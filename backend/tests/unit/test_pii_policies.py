"""PII policies: what each LLM provider receives, and what stays out of scope.

Promises covered (each through public surfaces — the redactor, the policy
config functions, the prompt builder — never private helpers):

* an org that never touches policies gets exactly the redaction it had
  (golden output pinned from the pre-policy redactor, through every way the
  config can arrive: stored as-is, written by an old client, read back);
* each provider gets its assigned policy; Exempt gets nothing redacted; an
  assignment to a policy that no longer exists falls back to Default;
* instructions, schemas and other platform context are left as they are,
  while the user's prompt and the data are redacted — and the context markers
  never survive into what is sent;
* writes that would be quietly repaired on read are rejected instead;
* a block is classified as ``pii_blocked``, which is neither retried nor
  eligible for fallback.
"""

import json
import pathlib

import pytest

from app.ai.llm.pii import policy as pol
from app.ai.llm.pii.context import CONTEXT_END, CONTEXT_START, pii_context, split_segments, strip_markers
from app.ai.llm.pii.redactor import PiiPromptBlockedError, build_redactor
from app.schemas.organization_settings_schema import PiiProtectionConfig

GOLDEN = json.loads(
    (pathlib.Path(__file__).parent.parent / "fixtures" / "pii_redaction_golden.json").read_text()
)
EMAIL = "jane.roe@contoso.com"


def _run(redactor, text):
    if redactor is None:
        return {"result": strip_markers(text), "blocked": None}
    try:
        out, _ = redactor.apply(text)
        return {"result": out, "blocked": None}
    except PiiPromptBlockedError as e:
        return {"result": None, "blocked": sorted(e.rule_names)}


def _expected(case):
    return {"result": case["result"], "blocked": case["blocked"]}


# --- an org that never touches policies is unchanged ----------------------

@pytest.mark.parametrize("case", GOLDEN["cases"], ids=lambda c: f"{c['config']}:{c['text'][:20]}")
def test_single_rule_set_redacts_exactly_as_before(case):
    legacy = GOLDEN["configs"][case["config"]]
    # 1. stored in the old shape and compiled as it is
    assert _run(build_redactor(legacy), case["text"]) == _expected(case)
    # 2. written by an old client onto an empty config
    written = pol.apply_update({}, legacy)
    assert _run(pol.compile_config(written).for_provider("any-provider"), case["text"]) == _expected(case)
    # 3. saved and read back through the settings schema (new shape + mirrors)
    stored = PiiProtectionConfig(**legacy).model_dump()
    assert _run(pol.compile_config(stored).for_display(), case["text"]) == _expected(case)
    assert _run(build_redactor(stored), case["text"]) == _expected(case)


def test_the_old_fields_read_back_from_the_default_policy():
    legacy = GOLDEN["configs"]["custom_and_overrides"]
    stored = PiiProtectionConfig(**legacy).model_dump()
    assert stored["mode"] == "replace"
    assert stored["builtin_overrides"]["ipv4"] == {"enabled": False}
    rules = {r["id"]: r for r in stored["custom_rules"]}
    assert rules["emp"]["patterns"] == [r"EMP-\d{4}", r"E\d{6}"]
    assert rules["off"]["enabled"] is False
    assert set(stored["policies"]) == {"default"}


def test_two_custom_rules_sharing_an_id_both_stay_active():
    cfg = {"enabled": True, "custom_rules": [
        {"id": "dup", "name": "A", "patterns": ["AAA-\\d+"], "replacement": "[A]"},
        {"id": "dup", "name": "B", "patterns": ["BBB-\\d+"], "replacement": "[B]"},
    ]}
    out, _ = build_redactor(cfg).apply("AAA-1 BBB-2")
    assert out == "[A] [B]"


# --- each provider gets its policy ----------------------------------------

def _v2(**extra):
    return {
        "enabled": True,
        "custom_rules": [{"id": "emp", "name": "Employee ID", "patterns": [r"EMP-\d{4}"], "replacement": "[EMP]"}],
        "policies": {
            "default": {"name": "Default", "mode": "replace"},
            "strict": {"name": "Strict", "mode": "block"},
            "light": {"name": "Email only", "mode": "replace",
                      "builtin_overrides": {k: {"enabled": False} for k in pol.BUILTIN_RULE_IDS if k != "email"},
                      "custom_overrides": {"emp": {"enabled": False}}},
        },
        "provider_policies": {"p-strict": "strict", "p-light": "light", "p-vpc": "exempt"},
        **extra,
    }


def test_each_provider_gets_the_policy_assigned_to_it():
    compiled = pol.compile_config(_v2())
    text = f"mail {EMAIL} call 415-555-1234 id EMP-1234"

    out, _ = compiled.for_provider("p-default-by-omission").apply(text)
    assert EMAIL not in out and "415-555-1234" not in out and "[EMP]" in out

    out, _ = compiled.for_provider("p-light").apply(text)
    assert EMAIL not in out and "415-555-1234" in out and "EMP-1234" in out

    with pytest.raises(PiiPromptBlockedError) as exc:
        compiled.for_provider("p-strict").apply(text)
    assert exc.value.policy_id == "strict" and exc.value.policy_name == "Strict"

    assert compiled.for_provider("p-vpc") is None  # exempt: sent as is


def test_display_uses_the_default_policy_whatever_providers_are_assigned():
    compiled = pol.compile_config(_v2())
    assert compiled.for_display().policy_id == "default"


def test_an_assignment_to_a_deleted_policy_falls_back_to_default():
    cfg = _v2()
    del cfg["policies"]["strict"]
    compiled = pol.compile_config(cfg)
    assert compiled.policy_id_for_provider("p-strict") == "default"
    assert "p-strict" not in pol.normalize(cfg)["provider_policies"]


def test_disabled_protection_redacts_for_no_provider():
    compiled = pol.compile_config(_v2(enabled=False))
    assert compiled.for_provider("p-strict") is None and compiled.for_display() is None


def test_an_old_client_write_lands_on_default_and_keeps_other_policies():
    cfg = pol.apply_update(_v2(), {"mode": "block", "builtin_overrides": {"email": {"enabled": False}}})
    assert cfg["policies"]["default"]["mode"] == "block"
    assert cfg["policies"]["default"]["builtin_overrides"] == {"email": {"enabled": False}}
    assert cfg["policies"]["strict"]["mode"] == "block" and cfg["policies"]["light"]["name"] == "Email only"
    assert cfg["provider_policies"]["p-light"] == "light"


def test_an_old_client_exempt_list_only_touches_exempt_assignments():
    cfg = pol.apply_update(_v2(), {"exempt_provider_ids": ["p-other"]})
    assert cfg["provider_policies"] == {"p-strict": "strict", "p-light": "light", "p-other": "exempt"}


def test_bow_152_exemptions_become_exempt_assignments():
    cfg = pol.normalize({"enabled": True, "exempt_provider_ids": ["p1", "p2"]})
    assert cfg["provider_policies"] == {"p1": "exempt", "p2": "exempt"}
    assert set(cfg["policies"]) == {"default"}


# --- context is left alone, prompt and data are redacted -------------------

def test_context_regions_are_left_as_they_are_and_markers_never_survive():
    r = pol.compile_config(_v2()).for_provider("p-default")
    instructions = pii_context(f"<instructions>Exclude test accounts like {EMAIL}</instructions>")
    schema = pii_context('<column name="Email" sample="ops@corp.io"/>')
    text = f"{instructions}\n{schema}\n<user_prompt>email {EMAIL} please</user_prompt>"
    out, result = r.apply(text)
    assert f"Exclude test accounts like {EMAIL}" in out
    assert 'sample="ops@corp.io"' in out
    assert f"email {EMAIL} please" not in out
    assert CONTEXT_START not in out and CONTEXT_END not in out
    assert sum(m["count"] for m in result.matches) == 1


def test_a_block_rule_ignores_context_but_still_sees_the_prompt():
    r = pol.compile_config(_v2()).for_provider("p-strict")
    out, _ = r.apply(pii_context(f"route errors to {EMAIL}") + " what is revenue?")
    assert EMAIL in out
    with pytest.raises(PiiPromptBlockedError):
        r.apply(pii_context("route errors to ops") + f" mail {EMAIL}")


def test_a_policy_can_also_redact_context():
    cfg = _v2()
    cfg["policies"]["default"]["redact_context"] = True
    out, _ = pol.compile_config(cfg).for_provider("p").apply(pii_context(f"instr {EMAIL}") + " ask")
    assert EMAIL not in out and CONTEXT_START not in out


@pytest.mark.parametrize("text, expected", [
    ("plain", [(False, "plain")]),
    (f"a{CONTEXT_START}ctx{CONTEXT_END}b", [(False, "a"), (True, "ctx"), (False, "b")]),
    # unterminated: the rest stays in scope (fail-closed)
    (f"a{CONTEXT_START}never closed {EMAIL}", [(False, f"anever closed {EMAIL}")]),
    # a stray end is dropped
    (f"a{CONTEXT_END}b", [(False, "ab")]),
    # regions are flat: an inner start is dropped
    (f"{CONTEXT_START}x{CONTEXT_START}y{CONTEXT_END}z", [(True, "xy"), (False, "z")]),
])
def test_context_segments_fail_closed(text, expected):
    assert split_segments(text) == expected


def test_a_truncated_context_region_is_redacted():
    r = pol.compile_config(_v2()).for_provider("p")
    out, _ = r.apply(f"{CONTEXT_START}instructions cut off by truncation {EMAIL}")
    assert EMAIL not in out and CONTEXT_START not in out


# --- the planner marks context, not the prompt or the data ------------------

def test_the_planner_prompt_leaves_instructions_and_schema_but_redacts_prompt_and_files():
    from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
    from app.schemas.ai.planner import PlannerInput

    pi = PlannerInput(
        user_message=f"what did {EMAIL} buy?",
        instructions=f"<instructions>Never include internal@corp.io rows</instructions>",
        schemas_combined='<table name="Customer"><column name="Email"/></table>',
        files_context="<files>uploaded.csv: name,email\nBob,bob@files.io</files>",
    )
    built = PromptBuilderV3.build(pi)
    text = built.system + "\n" + "\n".join(
        m["content"] if isinstance(m["content"], str) else json.dumps(m["content"]) for m in built.messages
    )
    out, _ = pol.compile_config(_v2()).for_provider("p").apply(text)
    assert "internal@corp.io" in out          # instructions: context
    assert 'column name="Email"' in out       # schema: context
    assert EMAIL not in out                   # the user's prompt: redacted
    assert "bob@files.io" not in out          # uploaded file text: data, redacted
    assert CONTEXT_START not in out and CONTEXT_END not in out


# --- writes are validated ---------------------------------------------------

@pytest.mark.parametrize("update", [
    {"policies": {"strict": {"name": "Strict"}}},                                   # no Default
    {"policies": {"default": {}, "Bad Id": {"name": "x"}}},                         # id not a slug
    {"policies": {"default": {}, "exempt": {"name": "x"}}},                         # reserved id
    {"policies": {"default": {}, "a": {"name": ""}}},                               # unnamed
    {"policies": {"default": {}, "a": {"name": "Same"}, "b": {"name": "same"}}},    # duplicate name
    {"policies": {"default": {}, "a": {"name": "Exempt"}}},                         # looks like Exempt
    {"policies": {"default": {"mode": "shred"}}},                                   # bad mode
    {"policies": {"default": {"builtin_overrides": {"email": {"action": "x"}}}}},   # bad action
    {"policies": {"default": {}}, "provider_policies": {"p": "nope"}},             # unknown policy
    {"provider_policies": {"p": "nope"}},                                           # unknown (stored)
    {"exempt_provider_ids": "p1"},                                                  # not a list
    {"exempt_provider_ids": ["p1", 7]},                                             # not ids
])
def test_invalid_writes_are_rejected(update):
    with pytest.raises(pol.PolicyValidationError):
        pol.validate_update(update, _v2())


def test_valid_writes_pass():
    pol.validate_update(_v2(), {})
    pol.validate_update({"provider_policies": {"p": "strict", "q": "exempt", "r": "default"}}, _v2())


# --- audit diff -----------------------------------------------------------

def test_audit_entries_name_policies_and_assignment_changes_never_patterns():
    old = pol.normalize(_v2())
    new_cfg = _v2()
    new_cfg["policies"]["strict"]["mode"] = "replace"
    del new_cfg["policies"]["light"]
    new_cfg["policies"]["fresh"] = {"name": "Fresh"}
    new_cfg["provider_policies"] = {"p-strict": "fresh", "p-vpc": "exempt"}
    entries = pol.diff_for_audit(old, pol.normalize(new_cfg))
    by_action = {}
    for e in entries:
        by_action.setdefault(e["action"], []).append(e["details"])
    assert by_action["pii_protection.policy_created"] == [{"policy_id": "fresh", "policy_name": "Fresh"}]
    assert by_action["pii_protection.policy_deleted"] == [{"policy_id": "light", "policy_name": "Email only"}]
    assert by_action["pii_protection.policy_updated"][0]["changed"] == ["mode"]
    changes = by_action["pii_protection.provider_policies_updated"][0]["changes"]
    assert {"provider_id": "p-strict", "from": "strict", "to": "fresh"} in changes
    assert {"provider_id": "p-light", "from": "light", "to": "default"} in changes
    assert "EMP" not in json.dumps(entries)


# --- a block is its own error ----------------------------------------------

def test_a_block_is_classified_pii_blocked_and_is_neither_retried_nor_fallen_back():
    from app.ai.llm.errors import classify
    from app.ai.llm.fallback import FALLBACK_ELIGIBLE_CODES
    from app.ai.llm.llm import _RETRYABLE_CODES

    err = PiiPromptBlockedError(["Email address"], policy_id="strict", policy_name="Strict")
    err.attach(model="gpt-public", policy_id="strict", policy_name="Strict")
    wrapped = RuntimeError("stream failed")
    wrapped.__cause__ = err
    for exc in (err, wrapped):
        out = classify(exc, provider="openai", model="gpt-public").to_dict()
        assert out["code"] == "pii_blocked"
        assert out["rules"] == ["Email address"] and out["policy"] == "Strict"
        assert "gpt-public" in out["summary"] and out["provider_message"] == ""
    assert "pii_blocked" not in FALLBACK_ELIGIBLE_CODES
    assert "pii_blocked" not in _RETRYABLE_CODES
