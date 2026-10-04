"""write_csv saves the DataFrame the generated code returns.

Generated code runs in the sandbox, whose working directory is a private
scratch dir and which may not write under uploads/. The CSV must therefore be
written on the trusted side from the returned frame; code that only returns a
frame (and writes no file) has to produce a CSV with exactly that data.
"""
from __future__ import annotations

import os

import pandas as pd
import pytest

from app.ai.tools.implementations import write_csv as write_csv_module

_ROWS = 37

_GENERATED_CODE = f"""
def generate_df(ds_clients, excel_files):
    df = pd.DataFrame({{
        "region": ["north", "south", "east"] * {_ROWS // 3} + ["west"] * {_ROWS % 3},
        "amount": [i * 7 % 23 for i in range({_ROWS})],
    }})
    print(df.head())
    return df
"""


class _FakeCoder:
    """LLM boundary: returns fixed code and records the prompt it was given."""

    prompts: list = []

    def __init__(self, **_kwargs):
        pass

    async def generate_transform_code(self, **kwargs):
        _FakeCoder.prompts.append(kwargs.get("prompt") or "")
        return _GENERATED_CODE


class _FakeDb:
    def __init__(self):
        self.added = []

    def add(self, obj):
        obj.id = obj.id or "file-1"
        self.added.append(obj)

    async def flush(self):
        pass

    async def execute(self, *_args, **_kwargs):
        pass


@pytest.mark.asyncio
async def test_write_csv_saves_the_returned_dataframe(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs(os.path.join("uploads", "files"))
    monkeypatch.setattr("app.ai.agents.coder.coder.Coder", _FakeCoder)

    async def _no_audit(*_args, **_kwargs):
        pass

    monkeypatch.setattr(write_csv_module, "log_tool_audit", _no_audit)
    _FakeCoder.prompts.clear()
    db = _FakeDb()
    runtime_ctx = {"settings": None, "db": db, "ds_clients": {}, "excel_files": []}

    events = [
        e async for e in write_csv_module.WriteCsvTool().run_stream(
            {"user_prompt": "Build a table of sales by region", "title": "Sales"}, runtime_ctx
        )
    ]

    end = events[-1].payload
    assert end["output"]["success"], end["output"].get("error_message")
    assert end["observation"]["row_count"] == _ROWS
    saved = pd.read_csv(db.added[0].path)
    assert list(saved.columns) == ["region", "amount"]
    assert len(saved) == _ROWS
    assert saved["amount"].tolist() == [i * 7 % 23 for i in range(_ROWS)]
    # The coder is no longer asked to write the file itself.
    assert _FakeCoder.prompts and all("to_csv" not in p for p in _FakeCoder.prompts)
