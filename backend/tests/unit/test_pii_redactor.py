"""Unit tests for the PII redaction engine (app.ai.llm.pii).

Contract under test:
  * built-in rules detect and replace the entity classes they promise,
  * one rule may carry several patterns (any match wins) under one token,
  * replace mode swaps matches; block mode refuses when anything matches,
  * per-org overrides can disable a built-in or change its token,
  * an invalid custom regex is rejected at validation time and never crashes
    redaction at runtime,
  * the match summary carries no raw matched values (no PII in telemetry),
  * a disabled / empty config yields no redactor (cheap no-op path).
"""

import re

import pytest

from app.ai.llm.pii.builtin_rules import BUILTIN_PII_RULES, builtin_rule_ids
from app.ai.llm.pii.redactor import (
    PiiPromptBlockedError,
    build_redactor,
    validate_pattern,
)


def _replace(text, **cfg):
    cfg.setdefault("enabled", True)
    cfg.setdefault("mode", "replace")
    redactor = build_redactor(cfg)
    assert redactor is not None
    out, result = redactor.apply(text)
    return out, result


# --- built-in detection ----------------------------------------------------

@pytest.mark.parametrize(
    "sample, secret, rule_id",
    [
        ("contact jane.roe+tag@example.co.uk now", "jane.roe+tag@example.co.uk", "email"),
        ("ssn is 078-05-1120 ok", "078-05-1120", "us_ssn"),
        ("card 4111 1111 1111 1111 done", "4111 1111 1111 1111", "credit_card"),
        ("call +44 20 7946 0958", "7946 0958", "phone"),
        ("host 192.168.10.254 up", "192.168.10.254", "ipv4"),
        ("key AKIAIOSFODNN7EXAMPLE leaked", "AKIAIOSFODNN7EXAMPLE", "aws_access_key"),
    ],
)
def test_builtin_rule_redacts_its_entity(sample, secret, rule_id):
    out, result = _replace(sample)
    assert secret not in out, f"{rule_id} left the secret in place: {out!r}"
    assert any(m["id"] == rule_id for m in result.matches)


def test_all_builtin_patterns_compile():
    for spec in BUILTIN_PII_RULES:
        assert spec["patterns"], f"{spec['id']} has no patterns"
        for pat in spec["patterns"]:
            re.compile(pat)  # raises if invalid


# --- multiple patterns per rule -------------------------------------------

def test_single_rule_matches_via_any_of_its_patterns():
    # One logical rule, two distinct real-world shapes; both must redact to the
    # same token under one enable switch.
    cfg = {
        "custom_rules": [
            {
                "id": "empid",
                "name": "Employee ID",
                "patterns": [r"EMP-\d{4}", r"E\d{6}"],
                "replacement": "[EMP]",
                "enabled": True,
            }
        ],
        # keep built-ins out of the way for a focused assertion
        "builtin_overrides": {rid: {"enabled": False} for rid in builtin_rule_ids()},
    }
    out, result = _replace("ids EMP-1234 and E987654 here", **cfg)
    assert out == "ids [EMP] and [EMP] here"
    assert result.matches[0]["count"] == 2


# --- replace vs block ------------------------------------------------------

def test_block_mode_raises_when_pii_present():
    redactor = build_redactor({"enabled": True, "mode": "block"})
    with pytest.raises(PiiPromptBlockedError):
        redactor.apply("email me at a@b.com")


def test_block_mode_passes_clean_text_through():
    redactor = build_redactor({"enabled": True, "mode": "block"})
    out, result = redactor.apply("nothing sensitive here")
    assert out == "nothing sensitive here"
    assert not result.redacted


def test_invalid_mode_falls_back_to_replace():
    redactor = build_redactor({"enabled": True, "mode": "nonsense"})
    assert redactor.mode == "replace"


# --- per-org overrides -----------------------------------------------------

def test_override_can_disable_a_builtin():
    out, _ = _replace(
        "reach me at a@b.com",
        builtin_overrides={"email": {"enabled": False}},
    )
    assert "a@b.com" in out  # email rule disabled -> left intact


def test_override_can_change_replacement_token():
    out, _ = _replace(
        "reach me at a@b.com",
        builtin_overrides={"email": {"replacement": "<email>"}},
    )
    assert "<email>" in out and "a@b.com" not in out


# --- validation & runtime robustness --------------------------------------

def test_validate_pattern_rejects_bad_regex_and_accepts_good():
    assert validate_pattern("(unclosed") is not None
    assert validate_pattern("") is not None
    assert validate_pattern(r"\d{3}-\d{4}") is None


def test_bad_custom_pattern_is_skipped_not_crashing():
    # A rule with one broken and one valid pattern still redacts via the valid
    # one instead of raising.
    cfg = {
        "custom_rules": [
            {
                "id": "mix",
                "name": "Mixed",
                "patterns": ["(unclosed", r"TOKEN-\d+"],
                "replacement": "[X]",
                "enabled": True,
            }
        ],
        "builtin_overrides": {rid: {"enabled": False} for rid in builtin_rule_ids()},
    }
    out, _ = _replace("here TOKEN-42 there", **cfg)
    assert out == "here [X] there"


# --- telemetry must not carry raw values ----------------------------------

def test_match_summary_contains_no_raw_values():
    _, result = _replace("email a@b.com ssn 078-05-1120")
    assert result.matches
    for m in result.matches:
        assert set(m.keys()) == {"id", "name", "count", "action"}
        # defensively ensure no value field slipped in
        assert "a@b.com" not in str(m)
        assert "078-05-1120" not in str(m)


# --- per-rule action (replace vs block) ------------------------------------

def test_per_rule_block_overrides_global_replace():
    # Global default is replace, but SSN is set to block. An SSN in the text
    # must refuse the whole request while other entities would just replace.
    cfg = {"enabled": True, "mode": "replace",
           "builtin_overrides": {"us_ssn": {"action": "block"}}}
    redactor = build_redactor(cfg)
    # email only -> replaced, no block
    out, result = redactor.apply("email a@b.com")
    assert "[REDACTED_EMAIL]" in out
    assert not result.blocked_rules
    # ssn present -> blocked (block wins over the replace rules)
    with pytest.raises(PiiPromptBlockedError):
        redactor.apply("ssn 078-05-1120 and email a@b.com")


def test_per_rule_replace_overrides_global_block():
    # Global default is block, but email is downgraded to replace.
    cfg = {"enabled": True, "mode": "block",
           "builtin_overrides": {"email": {"action": "replace"},
                                 **{rid: {"enabled": False} for rid in builtin_rule_ids() if rid not in ("email", "phone")}}}
    redactor = build_redactor(cfg)
    # email only -> replaced, not blocked
    out, result = redactor.apply("email a@b.com")
    assert "[REDACTED_EMAIL]" in out and not result.blocked_rules
    # phone inherits the global block -> refused
    with pytest.raises(PiiPromptBlockedError):
        redactor.apply("call 415-555-1234")


def test_block_detection_reports_the_blocking_rule():
    cfg = {"enabled": True, "mode": "block"}
    redactor = build_redactor(cfg)
    with pytest.raises(PiiPromptBlockedError) as exc:
        redactor.apply("ssn 078-05-1120")
    assert "US Social Security Number" in exc.value.rule_names


# --- no-op path ------------------------------------------------------------

@pytest.mark.parametrize("cfg", [
    None,
    {},
    {"enabled": False},
    {"enabled": True, "builtin_overrides": {rid: {"enabled": False} for rid in builtin_rule_ids()}},
])
def test_no_redactor_when_disabled_or_no_rules(cfg):
    assert build_redactor(cfg) is None
