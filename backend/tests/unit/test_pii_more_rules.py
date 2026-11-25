"""Custom rule conditions (checksum, context words, exact terms), the built-in
rules that ship off, and Presidio's context-dependent entities.

What is promised:

* A custom rule's checksum / context words decide every use of the rule the
  same way: what the model is sent, block detection, merged policies, and
  display masking.
* A term list matches whole words, case-insensitively, longest first.
* Built-in rules added after policies existed are off until a policy turns
  them on, so an upgrade never redacts more on its own.
* A context-dependent Presidio entity needs its context word at the default
  sensitivity when its pattern is weak.
"""

import pytest

from app.ai.llm.pii import checks
from app.ai.llm.pii import policy as pol
from app.ai.llm.pii.builtin_rules import BUILTIN_PII_RULES
from app.ai.llm.pii.redactor import PiiPromptBlockedError, build_redactor
from app.schemas.organization_settings_schema import PiiProtectionConfig


def _cfg(rule: dict, **default) -> dict:
    return {"enabled": True, "custom_rules": [{"id": "r", "name": "Rule", "replacement": "[X]", **rule}],
            "policies": {"default": {"name": "Default", **default}}}


# --- checksums ------------------------------------------------------------------

@pytest.mark.parametrize("name, valid, invalid", [
    ("luhn", ["4111 1111 1111 1111", "4012-8888-8888-1881", "046 454 286"], ["4111 1111 1111 1112", "1234567812345678"]),
    ("iban", ["DE89 3704 0044 0532 0130 00", "GB82WEST12345698765432"], ["DE89 3704 0044 0532 0130 01", "XX00"]),
    ("il_id", ["000000018", "123456782", "39337423"], ["123456789", "000000000", "1234"]),
    ("aba", ["011000015", "021000021"], ["011000016", "12345678"]),
])
def test_checksums(name, valid, invalid):
    fn = checks.VALIDATORS[name]
    assert all(fn(v) for v in valid)
    assert not any(fn(v) for v in invalid)


def test_a_checksum_rule_redacts_only_values_that_pass_it_everywhere():
    cfg = _cfg({"patterns": [r"\b\d{9}\b"], "checksum": "il_id"})
    text = "ids 123456782 and 123456789"
    out, _ = build_redactor(cfg).apply(text)
    assert out == "ids [X] and 123456789"
    assert build_redactor(cfg).redact_display(text) == "ids [X] and 123456789"

    blocking = _cfg({"patterns": [r"\b\d{9}\b"], "checksum": "il_id"}, custom_overrides={"r": {"action": "block"}})
    build_redactor(blocking).apply("only 123456789 here")          # fails the checksum: not blocked
    with pytest.raises(PiiPromptBlockedError):
        build_redactor(blocking).apply("id 123456782")


def test_a_checksum_rule_holds_in_a_merged_policy():
    cfg = pol.normalize({**_cfg({"patterns": [r"\b\d{9}\b"], "checksum": "il_id"}),
                         "policies": {"default": {"name": "Default"},
                                      "agent": {"name": "Agent", "builtin_overrides": {"email": {"action": "mask"}}}}})
    merged = pol.merge_policies(cfg, ["default", "agent"])
    merged_cfg = {**cfg, "policies": {**cfg["policies"], "m": merged}}
    r = pol.compile_policy(merged_cfg, "m")
    r.resolve_overlaps = True
    out, _ = r.apply("ids 123456782 and 123456789")
    assert out == "ids [X] and 123456789"


# --- context words --------------------------------------------------------------

def test_context_words_must_be_near_the_match():
    cfg = _cfg({"patterns": [r"\b[A-Z]\d{7}\b"], "context": ["passport", "pasaporte"]})
    r = build_redactor(cfg)
    assert r.apply("Passport no. X1234567 issued")[0] == "Passport no. [X] issued"
    assert r.apply("SKU X1234567 in stock")[0] == "SKU X1234567 in stock"
    far = "passport" + " filler" * 20 + " X1234567"
    assert r.apply(far)[0] == far                                 # beyond the window
    assert r.apply("X1234567 (pasaporte)")[0] == "[X] (pasaporte)"  # after the value counts too
    assert r.apply("passports X1234567")[0] == "passports X1234567"  # whole words only


# --- exact terms ----------------------------------------------------------------

def test_terms_match_whole_words_case_insensitively_longest_first():
    cfg = _cfg({"terms": ["Project Falcon", "Falcon", "acme"]})
    out, result = build_redactor(cfg).apply("project falcon and FALCON for Acme, not Falconry or acmeX")
    assert out == "[X] and [X] for [X], not Falconry or acmeX"
    assert result.matches[0]["count"] == 3


def test_terms_are_literal_not_regex():
    out, _ = build_redactor(_cfg({"terms": ["a.b (x)"]})).apply("a.b (x) and aXb (x)")
    assert out == "[X] and aXb (x)"


def test_conditions_survive_normalize_and_the_settings_schema():
    raw = _cfg({"patterns": [r"\d+"], "terms": [" Falcon ", "Falcon", ""], "checksum": "luhn",
                "context": ["card", "card"]})
    cfg = PiiProtectionConfig(**raw).model_dump()
    rule = cfg["custom_rules"][0]
    assert (rule["terms"], rule["checksum"], rule["context"]) == (["Falcon"], "luhn", ["card"])
    assert pol.normalize({**raw, "custom_rules": [{**raw["custom_rules"][0], "checksum": "nope"}]})[
        "custom_rules"][0]["checksum"] is None


# --- built-in rules that ship off ----------------------------------------------------

NEW_BUILTINS = {
    "ipv6": ("host 2001:db8:85a3::8a2e:370:7334 up", "at 12:30:45 std::vector"),
    "mac_address": ("nic 00:1A:2B:3C:4D:5E", "ratio 00:1A"),
    "il_id": ("id 123456782", "id 123456789"),
    "jwt": ("Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N", "eyJ alone"),
    "private_key": ("-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----", "-----BEGIN PUBLIC KEY-----"),
    "api_key": ("key ghp_" + "A1" * 18, "task sk-short"),
    "url_credentials": ("postgres://app:s3cret@localhost:5432/x", "https://db.internal/a:b@c"),
}


def test_every_new_builtin_has_a_sample():
    off = {r["id"] for r in BUILTIN_PII_RULES if r.get("enabled_by_default") is False}
    assert off == set(NEW_BUILTINS)


@pytest.mark.parametrize("rid", sorted(NEW_BUILTINS))
def test_a_new_builtin_is_off_until_turned_on(rid):
    hit, miss = NEW_BUILTINS[rid]
    # A policy saved before the rule existed: no override for it.
    assert build_redactor({"enabled": True}).apply(hit)[0] == hit
    on = build_redactor({"enabled": True, "policies": {"default": {"builtin_overrides": {rid: {"enabled": True}}}}})
    out, result = on.apply(hit)
    assert out != hit and [m["id"] for m in result.matches] == [rid]
    assert on.apply(miss)[0] == miss


def test_merging_policies_keeps_a_new_builtin_off_unless_one_turns_it_on():
    cfg = pol.normalize({"enabled": True, "policies": {
        "default": {"name": "Default"}, "a": {"name": "A"},
        "b": {"name": "B", "builtin_overrides": {"jwt": {"enabled": True}}}}})
    assert pol.merge_policies(cfg, ["default", "a"])["builtin_overrides"]["jwt"] == {"enabled": False}
    assert pol.merge_policies(cfg, ["default", "b"])["builtin_overrides"]["jwt"]["enabled"] is True


# --- Presidio entities that depend on context -------------------------------------------

@pytest.fixture(scope="module")
def presidio_with_model():
    pytest.importorskip("presidio_analyzer")
    pytest.importorskip("en_core_web_sm")
    from app.ai.llm.pii.detectors.presidio_inproc import PresidioInprocDetector
    return PresidioInprocDetector()


@pytest.mark.parametrize("entity, value, context", [
    ("US_PASSPORT", "912803456", "passport number"),
    ("US_BANK_NUMBER", "123456789012", "checking account"),
    ("US_DRIVER_LICENSE", "D1234567", "driver license"),
])
def test_a_weak_context_entity_needs_its_context_word(presidio_with_model, entity, value, context):
    from app.ai.llm.pii.detectors.catalog import default_min_score

    floor = default_min_score(entity)
    assert floor == 0.4

    def found(text):
        return [text[s.start:s.end] for s in presidio_with_model.detect(text, [entity]) if s.score >= floor]

    assert found(f"row 12: {value} ok") == []
    assert found(f"the {context} is {value}") == [value]


def test_context_entities_use_their_own_default_sensitivity():
    cfg = pol.normalize({"enabled": True, "policies": {"default": {
        "detector_overrides": {"US_PASSPORT": {"enabled": True}, "PERSON": {"enabled": True}}}}})
    rules = {r.entity: r.min_score for r in pol._resolve_detector_rules(cfg["policies"]["default"])}
    assert rules == {"US_PASSPORT": 0.4, "PERSON": 0.7}
    merged = pol.merge_policies(cfg, ["default", "default"])
    assert merged["detector_overrides"]["US_PASSPORT"]["min_score"] == 0.4


def test_context_words_match_whole_words_only(presidio_with_model):
    """Presidio's default substring matching lets "us" (a US passport context
    word) hit inside "customer"; the detector matches whole words."""
    from app.ai.llm.pii.detectors.catalog import default_min_score

    text = "Customer ID 123456782, order 123456789. Duplicate D1234567."
    spans = presidio_with_model.detect(text, ["US_PASSPORT", "US_DRIVER_LICENSE"])
    assert [s for s in spans if s.score >= default_min_score(s.entity)] == []
