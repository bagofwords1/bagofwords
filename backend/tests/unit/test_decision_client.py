"""Decision-model wire formats: the internal (OpenAI Decisions) shape maps to
System One and back without losing questions, options or probabilities.

Pure functions only — no HTTP, no DB. The HTTP path is covered end-to-end in
tests/e2e/test_decision_models.py against tests/mocks/fake_decision_server.py.
"""
import pytest

from app.ai.llm.decisions import (
    DecisionError,
    answers_from_openai,
    answers_from_systemone,
    build_request_body,
    validate_questions,
)


def _questions(option_count: int):
    return [
        {"type": "predicate", "name": "is_urgent", "instructions": "Is it urgent?"},
        {
            "type": "choice", "name": "pick",
            "instructions": "Which one?",
            "choices": [
                {"value": f"opt-{i}", "description": (f"option {i}" if i % 2 else None)}
                for i in range(option_count)
            ],
        },
        {
            "type": "score", "name": "level", "instructions": "How much?",
            "levels": [{"label": "low"}, {"label": "mid"}, {"label": "high"}],
        },
    ]


@pytest.mark.parametrize("option_count", [2, 5, 11])
def test_systemone_body_carries_every_question_and_option(option_count):
    qs = _questions(option_count)
    body = build_request_body("systemone", "jev-1.14.0", "some state", qs)

    assert body["model"] == "jev-1.14.0"
    assert body["state"] == "some state"
    assert set(body["questions"]) == {"is_urgent", "pick", "level"}
    assert body["questions"]["is_urgent"]["type"] == "noul"
    criteria = body["questions"]["pick"]["criteria"]
    assert list(criteria) == [f"opt-{i}" for i in range(option_count)]
    assert body["questions"]["level"]["criteria"] == ["low", "mid", "high"]


def test_openai_body_is_the_internal_shape_unchanged():
    qs = _questions(3)
    body = build_request_body("openai", "gpt-6-luna", "some input", qs)
    assert body == {"model": "gpt-6-luna", "input": "some input", "questions": qs}


@pytest.mark.parametrize("option_count", [2, 4])
def test_systemone_answers_round_trip_to_internal_shape(option_count):
    qs = _questions(option_count)
    top = f"opt-{option_count - 1}"
    probs = {f"opt-{i}": 0.0 for i in range(option_count)}
    probs[top] = 0.93
    raw = {"answers": {
        "is_urgent": {"type": "noul", "noul": 0.81},
        "pick": {"type": "choice", "choice": top, "confidence": 0.9, "probabilities": probs},
        "level": {"type": "score", "score": 1.2, "confidence": 0.7,
                  "legend": {"0": "low", "1": "mid", "2": "high"},
                  "probabilities": {"0": 0.1, "1": 0.6, "2": 0.3}},
    }}
    by_name = {a["name"]: a for a in answers_from_systemone(raw, qs)}

    assert by_name["is_urgent"] == {"type": "predicate", "name": "is_urgent", "probability": 0.81}
    assert by_name["pick"]["choice"] == top
    assert {p["value"] for p in by_name["pick"]["probabilities"]} == set(probs)
    assert by_name["level"]["score"] == pytest.approx(1.2)
    assert [p["label"] for p in by_name["level"]["probabilities"]] == ["low", "mid", "high"]


def test_missing_systemone_answer_becomes_refusal():
    qs = _questions(2)
    by_name = {a["name"]: a for a in answers_from_systemone({"answers": {}}, qs)}
    assert all(a["type"] == "refusal" for a in by_name.values())
    assert set(by_name) == {"is_urgent", "pick", "level"}


def test_openai_answers_are_keyed_by_name_and_keep_refusals():
    qs = _questions(2)
    raw = {"answers": [
        {"type": "refusal", "name": "is_urgent"},
        {"type": "choice", "name": "pick", "choice": "opt-1", "confidence": 1.0,
         "probabilities": [{"value": "opt-0", "probability": 0.0}, {"value": "opt-1", "probability": 1.0}]},
    ]}
    by_name = {a["name"]: a for a in answers_from_openai(raw, qs)}
    assert by_name["is_urgent"]["type"] == "refusal"
    assert by_name["pick"]["choice"] == "opt-1"
    # A question the provider skipped entirely is reported as a refusal too.
    assert by_name["level"]["type"] == "refusal"


def test_question_count_above_model_limit_is_rejected_before_sending():
    qs = [{"type": "predicate", "name": f"q{i}", "instructions": "?"} for i in range(5)]
    validate_questions(qs, max_questions=5, question_types=("predicate",))
    with pytest.raises(DecisionError):
        validate_questions(qs, max_questions=4, question_types=("predicate",))


def test_unsupported_question_type_is_rejected_before_sending():
    qs = _questions(2)
    with pytest.raises(DecisionError):
        validate_questions(qs, max_questions=100, question_types=("predicate", "choice"))
