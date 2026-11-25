"""The PII engine beyond pattern replace/block: detectors (names, locations),
masking, and reversible tokens that are restored in what the model writes.

Promises covered:

* pattern rules behave exactly as the engine they replaced (differential test
  against a reference copy of the old one-rule-after-another algorithm);
* a detector's spans are applied with each policy's options, never inside
  platform context, never over a pattern match, and its outage follows the
  policy (refuse, or pattern rules only);
* ``mask`` keeps the shape and the last four characters;
* ``pseudonymize`` gives a stable token per value, the model's output is
  restored — streamed text, split tokens, tool arguments (JSON-escaped while
  streaming), reasoning — and the display does not mask those rules;
* the HTTP detector turns every kind of failure into a DetectorError.
"""

import asyncio
import json
import random
import re

import httpx
import pytest

from app.ai.llm.pii import policy as pol
from app.ai.llm.pii.builtin_rules import BUILTIN_PII_RULES
from app.ai.llm.pii.context import pii_context
from app.ai.llm.pii.detectors import DetectedSpan, DetectorError, set_detector_for_tests
from app.ai.llm.pii.detectors.presidio_http import PresidioHttpDetector
from app.ai.llm.pii.redactor import (
    PiiDetectorUnavailableError,
    PiiPromptBlockedError,
    build_redactor,
    mask_value,
)
from app.ai.llm.pii.vault import PiiVault, StreamRestorer, vault_for

EMAIL = "jane.roe@contoso.com"


# --- pattern rules: identical to the previous engine -------------------------

def _reference_scan(config: dict, text: str):
    """The previous engine, verbatim in behavior: block rules counted on the
    original text, then each replace rule substituted in turn."""
    mode = config.get("mode", "replace")
    overrides = config.get("builtin_overrides") or {}
    rules = []
    for spec in BUILTIN_PII_RULES:
        ov = overrides.get(spec["id"], {})
        # Rules added later are off unless turned on: an existing config
        # behaves exactly as it did before they existed.
        if not ov.get("enabled", spec.get("enabled_by_default", True)):
            continue
        rules.append((spec["name"], ov.get("replacement") or spec["replacement"],
                      ov.get("action") if ov.get("action") in ("replace", "block") else mode,
                      [re.compile(p, re.IGNORECASE) for p in spec["patterns"]]))
    for raw in config.get("custom_rules") or []:
        if not raw.get("enabled", True):
            continue
        rules.append((raw["name"], raw.get("replacement") or "[REDACTED]",
                      raw.get("action") if raw.get("action") in ("replace", "block") else mode,
                      [re.compile(p, re.IGNORECASE) for p in raw["patterns"]]))
    blocked = sorted(name for name, _, action, pats in rules
                     if action == "block" and any(p.findall(text) for p in pats))
    if blocked:
        return ("blocked", blocked)
    for _name, replacement, action, pats in rules:
        if action == "block":
            continue
        for p in pats:
            text = p.sub(replacement, text)
    return ("text", text)


_FRAGMENTS = [
    "john@acme.com", "a.b+c@sub.example.co.uk", "415-555-1234", "(212) 555-0199", "+44 20 7946 0958",
    "4111 1111 1111 1111", "4111-1111-1111-1111", "078-05-1120", "10.0.0.12", "192.168.1.300",
    "DE89370400440532013000", "AKIAIOSFODNN7EXAMPLE", "EMP-1234", "E123456", "revenue", "Q3", "12345",
    " ", ", ", "\n", " and ", "(", ")", ":", "id=", "[REDACTED]", "x", "call ", "ssn ", "tel:",
]
_CONFIGS = [
    {"enabled": True, "mode": "replace"},
    {"enabled": True, "mode": "replace",
     "builtin_overrides": {"ipv4": {"enabled": False}, "email": {"replacement": "<EMAIL>"}, "us_ssn": {"action": "block"}},
     "custom_rules": [{"id": "emp", "name": "Employee ID", "patterns": [r"EMP-\d{4}", r"E\d{6}"], "replacement": "[EMP]"},
                      {"id": "grp", "name": "Groups", "patterns": [r"(\d{3})-(\d{2})"], "replacement": r"\2-\1"},
                      {"id": "red", "name": "Token eater", "patterns": [r"REDACTED_\w+"], "replacement": "X"}]},
    {"enabled": True, "mode": "replace",
     "custom_rules": [{"id": "empty", "name": "Empty", "patterns": [r"x*"], "replacement": "-"}]},
    {"enabled": True, "mode": "block", "builtin_overrides": {"phone": {"action": "replace"}}},
]


@pytest.mark.parametrize("config_index", range(len(_CONFIGS)))
def test_pattern_rules_match_the_previous_engine_on_generated_text(config_index):
    config = _CONFIGS[config_index]
    redactor = build_redactor(config)
    rng = random.Random(1234 + config_index)
    for _ in range(400):
        text = "".join(rng.choice(_FRAGMENTS) for _ in range(rng.randint(1, 12)))
        expected = _reference_scan(config, text)
        try:
            out, _ = redactor.apply(text)
            got = ("text", out)
        except PiiPromptBlockedError as e:
            got = ("blocked", sorted(e.rule_names))
        assert got == expected, text


# --- mask ---------------------------------------------------------------------

@pytest.mark.parametrize("value, masked", [
    ("4111 1111 1111 1234", "**** **** **** 1234"),
    ("jane@contoso.com", "****@******o.com"),
    ("abcd", "****"),
    ("", ""),
])
def test_mask_keeps_shape_and_the_last_four(value, masked):
    out = mask_value(value)
    assert out == masked
    assert len(out) == len(value)
    kept = [c for c in value if c.isalnum()][-4:] if sum(c.isalnum() for c in value) > 4 else []
    assert [c for c in out if c.isalnum()] == kept


def test_a_mask_rule_masks_for_the_model_and_for_display():
    r = build_redactor({"enabled": True, "policies": {"default": {
        "builtin_overrides": {"credit_card": {"action": "mask"}}}}})
    out, result = r.apply("card 4111 1111 1111 1234 ok")
    assert "1234" in out and "4111" not in out
    assert {"action": "mask"}.items() <= next(m for m in result.matches if m["id"] == "credit_card").items()
    assert "4111" not in r.redact_display("card 4111 1111 1111 1234")


# --- detectors ------------------------------------------------------------------

class FakeDetector:
    """Finds the names it is told about; counts calls; can be made to fail."""
    name = "fake"

    def __init__(self, names=None, score=0.9):
        self.names = names or {"Dana Whitfield": "PERSON", "Lisbon": "LOCATION", "Jordan": "PERSON"}
        self.score = score
        self.calls = []
        self.fail = False

    def supported_entities(self):
        return ["PERSON", "LOCATION", "ORGANIZATION", "NRP"]

    def detect(self, text, entities, language=None):
        self.calls.append(text)
        if self.fail:
            raise DetectorError("detector down")
        spans = []
        for name, entity in self.names.items():
            if entity not in entities:
                continue
            for m in re.finditer(re.escape(name), text):
                spans.append(DetectedSpan(m.start(), m.end(), entity, self.score))
        return spans


@pytest.fixture
def detector():
    d = FakeDetector()
    set_detector_for_tests(d)
    yield d
    set_detector_for_tests(None, resolved=False)


def _policy(**default):
    return {"enabled": True, "policies": {"default": {"name": "Default", **default}}}


def _person_on(**opts):
    return _policy(detector_overrides={"PERSON": {"enabled": True, **opts}})


def test_detector_entities_are_off_until_a_policy_turns_them_on(detector):
    out, _ = build_redactor(_policy()).apply("Dana Whitfield lives in Lisbon")
    assert out == "Dana Whitfield lives in Lisbon" and detector.calls == []
    out, result = build_redactor(_person_on()).apply("Dana Whitfield lives in Lisbon")
    assert out == "[REDACTED_PERSON] lives in Lisbon"  # LOCATION is still off
    assert [(m["id"], m["count"]) for m in result.matches] == [("presidio.PERSON", 1)]


def test_detector_options_score_replacement_and_block(detector):
    assert build_redactor(_person_on(min_score=0.95)).apply("ask Dana Whitfield")[0] == "ask Dana Whitfield"
    assert build_redactor(_person_on(replacement="<NAME>")).apply("ask Dana Whitfield")[0] == "ask <NAME>"
    with pytest.raises(PiiPromptBlockedError) as exc:
        build_redactor(_person_on(action="block")).apply("ask Dana Whitfield")
    assert exc.value.rule_names == ["Person name"]


def test_a_detector_never_runs_on_platform_context_even_when_the_policy_redacts_it(detector):
    """A column or table called Jordan must reach the model: only pattern
    rules apply to context, whatever the policy says."""
    schema = pii_context(f'<column name="Jordan"/> contact {EMAIL}')
    r = build_redactor(_policy(redact_context=True, detector_overrides={"PERSON": {"enabled": True}}))
    out, _ = r.apply(schema + " and Jordan asked")
    assert '<column name="Jordan"/>' in out          # detector skipped the context
    assert EMAIL not in out                          # pattern rules did not
    assert out.endswith(" and [REDACTED_PERSON] asked")


def test_a_pattern_rule_wins_over_a_detector_span_it_overlaps(detector):
    detector.names = {"jane.roe": "PERSON", "Dana Whitfield": "PERSON"}
    out, _ = build_redactor(_person_on()).apply(f"{EMAIL} wrote to Dana Whitfield at 415-555-1234")
    assert out == "[REDACTED_EMAIL] wrote to [REDACTED_PERSON] at [REDACTED_PHONE]"


def test_overlapping_detector_spans_keep_the_longest(detector):
    detector.names = {"Dana": "PERSON", "Dana Whitfield": "PERSON"}
    assert build_redactor(_person_on()).apply("Dana Whitfield")[0] == "[REDACTED_PERSON]"


def test_detector_results_are_cached_by_content_but_failures_are_not(detector):
    r = build_redactor(_person_on(), )
    r.apply("ask Dana Whitfield")
    r.apply("ask Dana Whitfield")
    assert len(detector.calls) == 1
    detector.fail = True
    with pytest.raises(PiiDetectorUnavailableError):
        r.apply("a new text about Dana Whitfield")
    detector.fail = False
    assert r.apply("a new text about Dana Whitfield")[0] == "a new text about [REDACTED_PERSON]"


def test_a_long_text_is_scanned_to_the_end(detector):
    from app.ai.llm.pii.detectors import CHUNK_CHARS
    text = ("row of data\n" * (CHUNK_CHARS // 6)) + "last line: Dana Whitfield"
    out, _ = build_redactor(_person_on()).apply(text)
    assert out.endswith("last line: [REDACTED_PERSON]")
    assert len(detector.calls) > 1


@pytest.mark.parametrize("mode", ["block", "regex_only"])
def test_a_detector_outage_follows_the_policy(detector, mode):
    detector.fail = True
    r = build_redactor(_policy(on_detector_error=mode, detector_overrides={"PERSON": {"enabled": True}}))
    text = f"Dana Whitfield <{EMAIL}>"
    if mode == "block":
        with pytest.raises(PiiDetectorUnavailableError) as exc:
            r.apply(text)
        assert "detector is unavailable" in str(exc.value) and EMAIL not in str(exc.value)
    else:
        out, result = r.apply(text)
        assert out == "Dana Whitfield <[REDACTED_EMAIL]>"   # pattern rules still applied
        assert result.detector_failed is True


def test_no_detector_on_the_instance_is_an_outage_not_a_silent_pass():
    set_detector_for_tests(None)
    try:
        with pytest.raises(PiiDetectorUnavailableError):
            build_redactor(_person_on()).apply("Dana Whitfield")
    finally:
        set_detector_for_tests(None, resolved=False)


def test_display_masking_is_pattern_rules_only(detector):
    r = build_redactor(_person_on())
    assert r.redact_display(f"Dana Whitfield {EMAIL}") == "Dana Whitfield [REDACTED_EMAIL]"
    assert detector.calls == []


def test_invalid_detector_options_are_rejected_on_write():
    for bad in ({"PERSON": {"min_score": 2}}, {"PERSON": {"action": "shred"}}, {"ALIEN": {"enabled": True}}):
        with pytest.raises(pol.PolicyValidationError):
            pol.validate_update({"policies": {"default": {"detector_overrides": bad}}}, {})
    with pytest.raises(pol.PolicyValidationError):
        pol.validate_update({"policies": {"default": {"on_detector_error": "ignore"}}}, {})


# --- the HTTP detector ---------------------------------------------------------

def _http_detector(handler):
    return PresidioHttpDetector("http://presidio.test", transport=httpx.MockTransport(handler), timeout=1)


def test_http_detector_speaks_the_presidio_analyze_api():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=[
            {"entity_type": "PERSON", "start": 4, "end": 18, "score": 0.91},
            {"entity_type": "EMAIL_ADDRESS", "start": 0, "end": 3, "score": 1.0},   # not asked for
            {"entity_type": "PERSON", "start": 90, "end": 99, "score": 0.9},         # out of range
        ])

    spans = _http_detector(handler).detect("ask Dana Whitfield", ["PERSON"])
    assert seen["url"] == "http://presidio.test/analyze"
    assert seen["body"] == {"text": "ask Dana Whitfield", "language": "en", "entities": ["PERSON"]}
    assert spans == [DetectedSpan(4, 18, "PERSON", 0.91)]


@pytest.mark.parametrize("handler", [
    lambda request: httpx.Response(500, text="boom"),
    lambda request: httpx.Response(200, text="not json"),
    lambda request: httpx.Response(200, json=[{"start": "x"}]),
    lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused")),
    lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow")),
], ids=["http-500", "not-json", "bad-shape", "unreachable", "timeout"])
def test_http_detector_failures_are_detector_errors(handler):
    with pytest.raises(DetectorError) as exc:
        _http_detector(handler).detect("ask Dana Whitfield", ["PERSON"])
    assert "Dana" not in str(exc.value)


def test_http_detector_offers_only_the_entities_the_service_serves():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        # The official image's answer: a mix of entities we offer and some we don't.
        return httpx.Response(200, json=["EMAIL_ADDRESS", "CREDIT_CARD", "PERSON", "UK_NHS", "DATE_TIME"])

    d = _http_detector(handler)
    assert sorted(d.supported_entities()) == ["CREDIT_CARD", "PERSON", "UK_NHS"]
    d.supported_entities()
    assert calls == ["http://presidio.test/supportedentities?language=en"]   # asked once


@pytest.mark.parametrize("handler, cached", [
    (lambda request: httpx.Response(404), True),                                   # a service without the endpoint
    (lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused")), False),  # down: ask again later
], ids=["no-endpoint", "unreachable"])
def test_http_detector_without_an_entity_list_offers_the_ner_entities(handler, cached):
    calls = []

    def counting(request):
        calls.append(1)
        return handler(request)

    d = _http_detector(counting)
    assert d.supported_entities() == ["PERSON", "LOCATION", "ORGANIZATION", "NRP"]
    d.supported_entities()
    assert len(calls) == (1 if cached else 2)


def test_detector_status_lists_validated_identifiers_the_detector_supports():
    from app.ai.llm.pii.detectors import detector_status

    class _Detector(FakeDetector):
        def supported_entities(self):
            return ["PERSON", "CREDIT_CARD", "IBAN_CODE"]

    set_detector_for_tests(_Detector())
    try:
        entities = {e["id"]: e for e in detector_status()["entities"]}
    finally:
        set_detector_for_tests(None, resolved=False)
    assert set(entities) == {"PERSON", "CREDIT_CARD", "IBAN_CODE"}
    assert entities["PERSON"]["kind"] == "ner" and entities["CREDIT_CARD"]["kind"] == "pattern"


def test_a_validated_identifier_is_redacted_like_any_detector_entity():
    # CREDIT_CARD from the detector, with the built-in regex rule off: the
    # detector's span is what gets replaced.
    det = FakeDetector(names={"4111 1111 1111 1111": "CREDIT_CARD"}, score=1.0)
    set_detector_for_tests(det)
    try:
        r = build_redactor(_policy(
            builtin_overrides={"credit_card": {"enabled": False}},
            detector_overrides={"CREDIT_CARD": {"enabled": True}},
        ))
        out, result = r.apply("card 4111 1111 1111 1111 on file")
    finally:
        set_detector_for_tests(None, resolved=False)
    assert out == "card [REDACTED_CC] on file"
    assert [(m["id"], m["count"]) for m in result.matches] == [("presidio.CREDIT_CARD", 1)]


# One valid value per validated identifier (public test values from each
# scheme's documentation / Presidio's own tests).
VALIDATED_SAMPLES = {
    "CREDIT_CARD": "4111 1111 1111 1111",
    "IBAN_CODE": "DE89 3704 0044 0532 0130 00",
    "ABA_ROUTING_NUMBER": "011000015",
    "CRYPTO": "1BoatSLRHtKNngkdXEeobR76b53LETtpyT",
    "US_NPI": "1234567893",
    "MEDICAL_LICENSE": "BB1388568",
    "UK_NHS": "943 476 5919",
    "ES_NIF": "55555555K",
    "ES_NIE": "X1234567L",
    "IT_FISCAL_CODE": "RSSMRA85T10A562S",
    "DE_TAX_ID": "86095742719",
    "PL_PESEL": "44051401359",
    "FI_PERSONAL_IDENTITY_CODE": "131052-308T",
    "SE_PERSONNUMMER": "811228-9874",
    "TR_NATIONAL_ID": "10000000146",
    "AU_TFN": "876 543 210",
    "AU_ABN": "51 824 753 556",
    "AU_MEDICARE": "2123 45670 1",
    "IN_AADHAAR": "2345 6789 0124",
    "KR_RRN": "900101-1234568",
    "ZA_ID_NUMBER": "8001015009087",
    "IT_VAT_CODE": "01114601006",
    "DE_VAT_ID": "DE136695976",
}


# Where the last digit is not the check digit (Medicare: the trailing digit is
# the card's issue number), the value with a wrong check digit.
BROKEN_CHECKSUM = {"AU_MEDICARE": "2123 45671 1"}


def test_every_validated_identifier_has_a_sample():
    from app.ai.llm.pii.detectors.catalog import PATTERN_ENTITIES
    assert {e["id"] for e in PATTERN_ENTITIES} == set(VALIDATED_SAMPLES)


@pytest.fixture(scope="module")
def presidio_patterns_only():
    """The real in-process detector, without loading a spaCy model: only the
    pattern recognizers are exercised."""
    pytest.importorskip("presidio_analyzer")
    spacy = pytest.importorskip("spacy")
    from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
    from presidio_analyzer.nlp_engine import SpacyNlpEngine

    from app.ai.llm.pii.detectors.presidio_inproc import PresidioInprocDetector, _pattern_recognizers

    class _Blank(SpacyNlpEngine):
        def load(self):
            self.nlp = {"en": spacy.blank("en")}

    nlp = _Blank(models=[{"lang_code": "en", "model_name": "blank"}])
    nlp.load()
    registry = RecognizerRegistry(supported_languages=["en"])
    for _, cls in _pattern_recognizers():
        registry.add_recognizer(cls(supported_language="en"))
    d = PresidioInprocDetector()
    d._analyzer = AnalyzerEngine(nlp_engine=nlp, registry=registry, supported_languages=["en"])
    return d


@pytest.mark.parametrize("entity", sorted(VALIDATED_SAMPLES))
def test_validated_identifier_is_found_and_a_broken_checksum_is_not(presidio_patterns_only, entity):
    from app.ai.llm.pii.detectors.catalog import DEFAULT_MIN_SCORE

    value = VALIDATED_SAMPLES[entity]
    assert entity in presidio_patterns_only.supported_entities()
    text = f"row 7: {value} ."
    hits = [s for s in presidio_patterns_only.detect(text, [entity]) if s.score >= DEFAULT_MIN_SCORE]
    assert [text[s.start:s.end] for s in hits] == [value]

    # Change one digit: same shape, checksum no longer holds.
    i = max(i for i, ch in enumerate(value) if ch.isdigit())
    broken = BROKEN_CHECKSUM.get(entity) or value[:i] + str((int(value[i]) + 1) % 10) + value[i + 1:]
    text = f"row 7: {broken} ."
    assert [s for s in presidio_patterns_only.detect(text, [entity]) if s.score >= DEFAULT_MIN_SCORE] == []


# --- reversible tokens ------------------------------------------------------------

def _pseudo_policy():
    return _policy(builtin_overrides={"email": {"action": "pseudonymize"}})


def test_a_value_gets_one_stable_token_per_report_and_restores():
    r = build_redactor(_pseudo_policy())
    v1, v2 = vault_for("org", "report-1"), vault_for("org", "report-1")
    out1, _ = r.apply(f"mail {EMAIL} then {EMAIL} and bob@x.io", v1)
    out2, _ = r.apply(f"again {EMAIL}", v2)
    token = re.search(r"<EMAIL_[0-9a-f]{8}>", out1).group(0)
    assert EMAIL not in out1 and out1.count(token) == 2
    assert token in out2                                   # same report → same token
    assert token not in r.apply(f"mail {EMAIL}", vault_for("org", "report-2"))[0]   # other report → other token
    assert v1.restore(out1) == f"mail {EMAIL} then {EMAIL} and bob@x.io"


def test_restore_is_lenient_about_case_and_spaces_and_leaves_unknown_tokens():
    v = vault_for("org", "r")
    token = v.token_for("EMAIL", EMAIL)
    digest = token[7:-1]
    assert v.restore(f"to <email_{digest.upper()}> and < EMAIL_{digest} >") == f"to {EMAIL} and {EMAIL}"
    assert v.restore("keep <EMAIL_00000000> and <b>bold</b>") == "keep <EMAIL_00000000> and <b>bold</b>"


def test_other_rules_do_not_match_inside_a_token():
    r = build_redactor(_policy(
        builtin_overrides={"email": {"action": "pseudonymize"}},
        custom_rules=[]) | {"custom_rules": [{"id": "hex", "name": "Hex", "patterns": [r"[0-9a-f]{8}"], "replacement": "[HEX]"}]})
    v = vault_for("org", "r")
    out, _ = r.apply(f"{EMAIL} deadbeef", v)
    assert re.fullmatch(r"<EMAIL_[0-9a-f]{8}> \[HEX\]", out)
    assert v.restore(out) == f"{EMAIL} [HEX]"


def test_two_values_colliding_on_a_short_digest_stay_distinct(monkeypatch):
    import app.ai.llm.pii.vault as vault_mod

    class _Collide:
        def __init__(self, tail):
            self._tail = tail

        def hexdigest(self):
            return "abcdef12" + self._tail

    tails = iter(["0001" + "0" * 52, "0002" + "0" * 52])
    monkeypatch.setattr(vault_mod.hmac, "new", lambda *a, **k: _Collide(next(tails)))
    v = PiiVault(b"k", "s")
    a, b = v.token_for("EMAIL", "a@x.io"), v.token_for("EMAIL", "b@x.io")
    assert a != b and v.restore(f"{a} {b}") == "a@x.io b@x.io"


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 64])
def test_a_token_split_across_stream_chunks_is_restored(size):
    v = vault_for("org", "r")
    token = v.token_for("EMAIL", EMAIL)
    text = f"Send it to {token}, then cc {token}. Is 3 < 5 and a<b? <unfinished"
    restorer = StreamRestorer(v)
    out = "".join(restorer.feed(text[i:i + size]) for i in range(0, len(text), size)) + restorer.flush()
    assert out == f"Send it to {EMAIL}, then cc {EMAIL}. Is 3 < 5 and a<b? <unfinished"


def test_streamed_tool_arguments_are_restored_as_valid_json():
    v = vault_for("org", "r")
    token = v.token_for("NAME", 'Dana "D" O\\Neil')
    fragment = json.dumps({"sql": f"select * from t where name = '{token}'"})
    restorer = StreamRestorer(v, json_fragment=True)
    out = "".join(restorer.feed(fragment[i:i + 4]) for i in range(0, len(fragment), 4)) + restorer.flush()
    assert json.loads(out) == {"sql": "select * from t where name = 'Dana \"D\" O\\Neil'"}
    assert v.restore_deep({"a": [f"x {token}", 3, {"b": token}]}) == {"a": ['x Dana "D" O\\Neil', 3, {"b": 'Dana "D" O\\Neil'}]}


def test_display_does_not_mask_what_is_only_hidden_from_the_model():
    r = build_redactor(_pseudo_policy())
    assert r.redact_display(f"{EMAIL} 415-555-1234") == f"{EMAIL} [REDACTED_PHONE]"


def test_the_event_stream_restores_text_reasoning_and_tool_arguments():
    from app.ai.llm.llm import _restore_event_stream
    from app.ai.llm.types import (
        MessageStopEvent,
        ReasoningCompleteEvent,
        ReasoningDeltaEvent,
        TextDeltaEvent,
        ToolUseCompleteEvent,
        ToolUseInputDeltaEvent,
        ToolUseStartEvent,
    )

    v = vault_for("org", "r")
    token = v.token_for("EMAIL", EMAIL)
    half = len(token) // 2
    events = [
        ReasoningDeltaEvent(text=f"user wants {token[:half]}"),
        ReasoningDeltaEvent(text=f"{token[half:]} looked up"),
        ReasoningCompleteEvent(text=f"user wants {token} looked up"),
        TextDeltaEvent(text=f"Looking up {token[:half]}"),
        TextDeltaEvent(text=token[half:]),
        ToolUseStartEvent(id="t1", name="create_data"),
        ToolUseInputDeltaEvent(id="t1", partial_json='{"sql": "where email = \'' + token[:half]),
        ToolUseInputDeltaEvent(id="t1", partial_json=token[half:] + "'\"}"),
        ToolUseCompleteEvent(id="t1", name="create_data", input={"sql": f"where email = '{token}'"}),
        MessageStopEvent(stop_reason="tool_use"),
    ]

    async def source():
        for e in events:
            yield e

    async def collect():
        return [e async for e in _restore_event_stream(source(), v)]

    out = asyncio.run(collect())
    text = "".join(e.text for e in out if isinstance(e, TextDeltaEvent))
    reasoning = "".join(e.text for e in out if isinstance(e, ReasoningDeltaEvent))
    partial = "".join(e.partial_json for e in out if isinstance(e, ToolUseInputDeltaEvent))
    complete = next(e for e in out if isinstance(e, ToolUseCompleteEvent))
    assert text == f"Looking up {EMAIL}"
    assert reasoning == f"user wants {EMAIL} looked up"
    assert next(e for e in out if isinstance(e, ReasoningCompleteEvent)).text == f"user wants {EMAIL} looked up"
    assert json.loads(partial) == {"sql": f"where email = '{EMAIL}'"}
    assert complete.input == {"sql": f"where email = '{EMAIL}'"}
    assert token not in json.dumps([getattr(e, "__dict__", {}) for e in out], default=str)
    # order is preserved: text is flushed before the tool call starts
    kinds = [type(e).__name__ for e in out]
    assert kinds.index("ToolUseStartEvent") > max(i for i, k in enumerate(kinds) if k == "TextDeltaEvent")
    assert kinds[-1] == "MessageStopEvent"
