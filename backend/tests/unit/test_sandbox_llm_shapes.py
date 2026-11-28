"""`llm` in generated code, as the code sees it (sandbox/namespace.py LLMHelper).

Contract, whatever the trusted side answers:
- one answer per input row, aligned to the input's index (Series, DataFrame,
  list) or a single value for a single string;
- each distinct value is asked about once per call; missing values (None, NaN)
  are never sent and come back empty;
- answers that came back empty stay empty (None / NaN / False), never a
  made-up value;
- what reaches the trusted side is plain JSON-able data.

The trusted side is a recording stand-in here; tests/e2e/rbac/test_sandbox_llm.py
covers it for real.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from app.ai.code_execution.sandbox.namespace import LLMHelper


class _Trusted:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def __call__(self, op, payload):
        json.dumps(payload)  # must cross the pipe as JSON
        self.calls.append((op, payload))
        answers = [self.answer(op, v) for v in payload["values"]]
        return {"answers": answers, "model": {"record_id": "r1", "model_id": "m1", "kind": "chat"},
                "sent": len(answers), "cached": 0, "requests": 1}


def _label(op, v):
    return None if v == "??" else {"a": v.split()[0], "p": 0.9}


def test_answers_align_to_a_series_index_and_each_distinct_value_is_asked_once():
    t = _Trusted(_label)
    s = pd.Series(["red car", "blue sky", "red car", None, np.nan, "??"], index=list("abcdef"))
    out = LLMHelper(t).classify(s, ["red", "blue"])
    assert list(out.index) == list("abcdef")
    assert out.tolist() == ["red", "blue", "red", None, None, None]
    (op, payload), = t.calls
    assert op == "classify" and sorted(payload["values"]) == sorted(["red car", "blue sky", "??"])
    assert payload["rows"] == 6


def test_a_dataframe_is_asked_about_row_by_row_as_labeled_text():
    t = _Trusted(lambda op, v: {"a": v, "p": None})
    df = pd.DataFrame({"title": ["A", "B"], "body": ["x", None]}, index=[7, 9])
    out = LLMHelper(t).generate(df, "summarize")
    assert list(out.index) == [7, 9]
    assert out[7] == "title: A\nbody: x" and out[9] == "title: B"


def test_a_single_string_gets_a_single_answer():
    t = _Trusted(_label)
    assert LLMHelper(t).classify("blue whale", ["red", "blue"]) == "blue"


def test_filter_uses_probability_against_the_threshold_and_empty_means_false():
    answers = {"a": {"a": True, "p": 0.6}, "b": {"a": True, "p": 0.3}, "c": None, "d": {"a": True, "p": None}}
    t = _Trusted(lambda op, v: answers[v])
    h = LLMHelper(t)
    s = pd.Series(["a", "b", "c", "d"])
    assert h.filter(s, "cond").tolist() == [True, False, False, True]
    assert h.filter(s, "cond", threshold=0.7).tolist() == [False, False, False, True]
    p = h.filter(s, "cond", with_proba=True)
    assert list(p.columns) == ["match", "p"] and p["p"].tolist()[:2] == [0.6, 0.3]


def test_score_is_float_with_nan_for_no_answer_and_extract_has_one_column_per_field():
    t = _Trusted(lambda op, v: None if v == "?" else ({"a": 3.0, "p": None} if op == "score"
                                                      else {"a": {"name": v.upper(), "n": 1}, "p": None}))
    h = LLMHelper(t)
    sc = h.score(["x", "?"], "how")
    assert sc.dtype == float and sc[0] == 3.0 and np.isnan(sc[1])
    ex = h.extract(pd.Series(["ab", "?"]), {"name": str, "n": "int"})
    assert list(ex.columns) == ["name", "n"]
    assert ex.iloc[0].tolist() == ["AB", 1] and ex.iloc[1].isna().all()
    # Types given as Python types reach the trusted side as type names.
    assert t.calls[-1][1]["spec"]["fields"] == {"name": "str", "n": "int"}


def test_nothing_is_sent_when_every_value_is_missing():
    t = _Trusted(_label)
    out = LLMHelper(t).classify(pd.Series([None, np.nan]), ["a", "b"])
    assert out.tolist() == [None, None] and t.calls == []


def test_a_dict_of_values_is_refused():
    with pytest.raises(TypeError):
        LLMHelper(_Trusted(_label)).classify({"a": 1}, ["a", "b"])
