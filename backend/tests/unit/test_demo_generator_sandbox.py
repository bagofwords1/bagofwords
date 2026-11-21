"""The demo dataset generator runs model-written code in the code sandbox.

The contract: the generator function is executed outside the API process, so
it cannot read or change anything that lives there, and the dataset it
produces is the same one the function describes (same seed, same frames).
The LLM is the only stubbed boundary.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date

import pandas as pd
import pytest

from app.schemas.demo_dataset_schema import DemoDatasetSpec
from app.services.demo_data.generator import DemoDatasetGenerator, TableGenerationError, run_generator

TODAY = date(2026, 10, 8)
START, END = pd.Timestamp("2025-01-01"), pd.Timestamp("2025-12-31 23:59:59")


def _run(coro):
    return asyncio.run(coro)


def _spec():
    return DemoDatasetSpec(
        name="Demo – Fleet", domain="logistics",
        date_range_start="2025-01-01", date_range_end="2026-06-30",
        tables=[
            {"name": "drivers", "description": "one row per driver", "row_count": 25,
             "columns": [
                 {"name": "driver_id", "type": "integer", "primary_key": True},
                 {"name": "name", "type": "text"},
                 {"name": "tags", "type": "json", "nullable": True},
             ]},
            {"name": "trips", "description": "one row per trip", "row_count": 90,
             "columns": [
                 {"name": "trip_id", "type": "text", "primary_key": True},
                 {"name": "driver_id", "type": "integer", "references": "drivers.driver_id"},
                 {"name": "started_at", "type": "datetime"},
                 {"name": "km", "type": "real"},
                 {"name": "night", "type": "boolean"},
                 {"name": "note", "type": "text", "nullable": True},
             ]},
        ],
        agents=[{"name": "Ops", "icon": "🚚", "tables": ["drivers", "trips"]}],
    )


DRIVERS = """```python
def generate(n, rng, tables, start, end):
    return pd.DataFrame({
        'driver_id': np.arange(1, n + 1),
        'name': ['driver-' + str(i) for i in rng.integers(100, 999, size=n)],
        # Differently shaped values in one column, and gaps.
        'tags': [{'tier': 'gold'} if i % 4 == 0 else ({'regions': ['n', 's'], 'k': i} if i % 4 == 1 else None)
                 for i in range(n)],
    })
```"""

TRIPS = """```python
def generate(n, rng, tables, start, end):
    drivers = tables['drivers']['driver_id'].to_numpy()
    secs = rng.integers(0, int((end - start).total_seconds()), size=n)
    return pd.DataFrame({
        'trip_id': ['T-' + format(i, '05d') for i in range(n)],
        'driver_id': rng.choice(drivers, size=n),
        'started_at': start + pd.to_timedelta(secs, unit='s'),
        'km': np.round(rng.gamma(2.0, 40.0, size=n), 1),
        'night': rng.random(n) < 0.2,
        'note': [None if i % 5 else 7 * i for i in range(n)],
    })
```"""


async def _inference(system, prompt):
    return DRIVERS if "TABLE TO GENERATE: drivers" in prompt else TRIPS


def _generate():
    return _run(DemoDatasetGenerator(_inference, today=TODAY).generate(_spec()))


def test_same_seed_gives_the_same_dataset_as_running_the_function_directly(monkeypatch):
    """The debug-only in-process mode is the reference: it calls the function
    with nothing in between."""
    sandboxed = _generate()
    monkeypatch.setenv("BOW_CODE_SANDBOX", "inprocess")
    reference = _generate()

    assert set(sandboxed.frames) == {"drivers", "trips"}
    for name, expected in reference.frames.items():
        pd.testing.assert_frame_equal(sandboxed.frames[name], expected)
    assert sandboxed.frames["trips"]["driver_id"].isin(sandboxed.frames["drivers"]["driver_id"]).all()
    # And it is repeatable.
    again = _generate()
    for name, frame in sandboxed.frames.items():
        pd.testing.assert_frame_equal(again.frames[name], frame)


def test_generator_code_cannot_change_state_in_the_api_process():
    """Code the static rules accept still runs somewhere else."""
    code = (
        "def generate(n, rng, tables, start, end):\n"
        "    pd.options.display.max_rows = 4242\n"
        "    tables['parent']['v'] = -1\n"
        "    return pd.DataFrame({'a': np.arange(n), 'rows': [pd.options.display.max_rows] * n})\n"
    )
    before = pd.options.display.max_rows
    assert before != 4242
    parent = pd.DataFrame({"v": [1, 2, 3]})
    try:
        out = _run(run_generator(code, n=3, seed=1, frames={"parent": parent}, start=START, end=END))
        # It ran (the frame came back with what the code saw) ...
        assert out["rows"].tolist() == [4242] * 3 and out["a"].tolist() == [0, 1, 2]
        # ... but not here.
        assert pd.options.display.max_rows == before
        assert parent["v"].tolist() == [1, 2, 3]
    finally:
        pd.options.display.max_rows = before


def test_generator_code_does_not_see_the_api_process_environment(monkeypatch):
    monkeypatch.setenv("BOW_TEST_ONLY_SETTING", "visible-in-the-api-process")
    code = (
        "def generate(n, rng, tables, start, end):\n"
        "    import os\n"
        "    env = os.environ\n"
        "    return pd.DataFrame({'seen': [env.get('BOW_TEST_ONLY_SETTING', '')], 'child': [env.get('BOW_SANDBOX_CHILD', '')]})\n"
    )
    # The static rules would refuse this probe; the property under test is the
    # process boundary behind them.
    monkeypatch.setattr("app.services.demo_data.generator.validate_generator_code", lambda code: None)
    out = _run(run_generator(code, n=1, seed=1, frames={}, start=START, end=END))
    assert out["seen"].tolist() == [""]
    assert out["child"].tolist() == ["1"]


def test_code_the_static_rules_refuse_is_never_started(tmp_path):
    marker = tmp_path / "ran.txt"
    code = (
        "```python\n"
        "def generate(n, rng, tables, start, end):\n"
        f"    open({str(marker)!r}, 'w').write('x')\n"
        "    return pd.DataFrame({'driver_id': np.arange(1, n + 1), 'name': ['a'] * n, 'tags': [None] * n})\n"
        "```"
    )
    calls = []

    async def inference(system, prompt):
        calls.append(prompt)
        return code

    with pytest.raises(TableGenerationError) as exc:
        _run(DemoDatasetGenerator(inference, today=TODAY, max_attempts=2).generate(_spec()))
    assert len(calls) == 2
    assert not marker.exists()
    assert "open" in exc.value.message


def test_a_generator_that_never_finishes_is_stopped_and_reported():
    code = (
        "def generate(n, rng, tables, start, end):\n"
        "    while True:\n"
        "        n += 1\n"
    )
    with pytest.raises(Exception) as exc:
        _run(run_generator(code, n=1, seed=1, frames={}, start=START, end=END, timeout_s=2))
    assert "longer than 2s" in str(exc.value)


def test_a_failure_inside_the_sandbox_reaches_the_retry_prompt_with_its_line():
    bad = """```python
def generate(n, rng, tables, start, end):
    ids = np.arange(1, n + 1)
    lookup = {'a': 1}
    value = lookup['missing_key']
    return pd.DataFrame({'driver_id': ids, 'name': ['a'] * n, 'tags': [None] * n})
```"""
    prompts = []

    async def inference(system, prompt):
        prompts.append(prompt)
        if "TABLE TO GENERATE: drivers" in prompt:
            return bad if sum("TABLE TO GENERATE: drivers" in p for p in prompts) == 1 else DRIVERS
        return TRIPS

    result = _run(DemoDatasetGenerator(inference, today=TODAY).generate(_spec()))
    assert {t.name: t.attempts for t in result.tables}["drivers"] == 2
    retry = [p for p in prompts if "previous attempt failed" in p][0]
    assert "KeyError" in retry and "missing_key" in retry
    assert "line 4" in retry and "lookup['missing_key']" in retry


def test_non_dataframe_results_are_reported_as_before():
    code = "def generate(n, rng, tables, start, end):\n    return [1, 2, 3]\n"
    with pytest.raises(Exception) as exc:
        _run(run_generator(code, n=3, seed=1, frames={}, start=START, end=END))
    assert "expected a pandas DataFrame" in str(exc.value) and "list" in str(exc.value)
