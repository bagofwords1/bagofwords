"""Parameters work on an accelerated custom table, rendered for DuckDB.

Contract: an activated custom table is served by `FastQueryClient` (DuckDB
over the stored artifact). Generated code calling it with `params=` gets the
rows whose value equals the parameter, whatever quotes or backslashes the
value holds: the value is rendered as a DuckDB string literal. The client
stays unguarded at the wrapper (the engine is locked at its own dispatch).

The artifact is built from a real SQLite file by the real extractor, and the
call goes through `StreamingCodeExecutor`, in both sandbox modes.
"""
from __future__ import annotations

import sqlite3
import sys
from unittest.mock import patch

import pytest

from app.ai.code_execution.client_profiles import guard_profile
from app.ai.code_execution.code_execution import StreamingCodeExecutor
from app.ai.code_execution.query_params import renderer_for
from app.data_sources.clients.sqlite_client import SqliteClient
from app.data_sources.fast import artifacts, extractor
from app.data_sources.fast.fast_client import FastQueryClient, FastRelation

_LINUX = sys.platform.startswith("linux")
BACKSLASH = chr(92)
VALUES = ["it's", "a" + BACKSLASH + "b", "x" + BACKSLASH + "'y", "plain"]


@pytest.fixture(params=[
    "inprocess",
    pytest.param("subprocess", marks=pytest.mark.skipif(not _LINUX, reason="sandbox child needs Linux")),
])
def mode(request, monkeypatch):
    monkeypatch.setenv("BOW_CODE_SANDBOX", request.param)
    if request.param == "inprocess":
        # In-process mode is for local development only; CI runs with
        # ENVIRONMENT=production, so the test names the environment itself.
        monkeypatch.setenv("ENVIRONMENT", "development")
    return request.param


@pytest.fixture
def custom_table(tmp_path):
    source = tmp_path / "source.db"
    con = sqlite3.connect(source)
    con.execute("CREATE TABLE notes (id INTEGER, v TEXT)")
    # Decoys hold each value as a renderer that escaped it twice would write it.
    rows = [(i, v) for i, v in enumerate(VALUES, start=1)]
    decoys = [v.replace("'", "''").replace(BACKSLASH, BACKSLASH * 2) for v in VALUES]
    rows += [(10 + i, d) for i, d in enumerate(decoys) if d not in VALUES]
    con.executemany("INSERT INTO notes VALUES (?, ?)", rows)
    con.commit()
    con.close()
    with patch.object(artifacts, "_ARTIFACT_ROOT", tmp_path / "fast"):
        result = extractor.extract_to_artifact(SqliteClient(database=str(source)), "SELECT * FROM notes", "notes", "conn")
        yield FastQueryClient([FastRelation(
            name="notes", artifact_path=result.artifact_path, artifact_key=result.artifact_key,
            columns=[{"name": "id"}, {"name": "v"}],
        )])


def test_the_accelerated_client_renders_for_duckdb_and_is_not_guarded(custom_table):
    assert renderer_for(custom_table).name == "duckdb"
    assert guard_profile(custom_table) is None


@pytest.mark.parametrize("value", VALUES)
def test_params_on_an_accelerated_custom_table_return_the_matching_row(mode, custom_table, value):
    code = (
        "def generate_df(ds_clients, excel_files):\n"
        "    return ds_clients['fast'].execute_query("
        f"'SELECT id FROM notes WHERE v = :v ORDER BY id', params={{'v': {value!r}}})\n"
    )
    df, _, _ = StreamingCodeExecutor().execute_code(code=code, ds_clients={"fast": custom_table}, excel_files=[])
    assert df["id"].tolist() == [VALUES.index(value) + 1]
