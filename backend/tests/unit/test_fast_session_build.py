"""The two halves of a FAST serving session: opening the engine, then building
and locking the session on it.

`connect()` is made of exactly these two steps. Later work (a pooled session, a
verified extension load) reuses them, so they are pinned here on their own:
the engine opens with today's settings, and a built session serves its
relations and is sealed before any agent SQL can run.
"""

import pytest

from app.data_sources.fast.fast_client import FastQueryClient, FastRelation


def _setting(con, name):
    return con.execute(f"SELECT current_setting('{name}')").fetchone()[0]


@pytest.fixture
def artifact(tmp_path):
    """A real encrypted extract with one four-row relation in it."""
    from app.data_sources.fast import artifacts

    key = artifacts.new_artifact_key()
    path = tmp_path / "probe.duckdb"
    con = artifacts.connect_encrypted(path, key)
    con.execute("CREATE TABLE sales (region VARCHAR, amount INTEGER)")
    con.executemany(
        "INSERT INTO sales VALUES (?, ?)",
        [("EMEA", 10), ("EMEA", 20), ("APAC", 30), ("AMER", 40)],
    )
    con.close()
    return str(path), key


def _relation(artifact, name="sales", **kw):
    path, key = artifact
    return FastRelation(name=name, artifact_path=path, artifact_key=key,
                        columns=[{"name": "region"}, {"name": "amount"}], **kw)


def test_an_opened_engine_has_the_default_settings_and_is_not_locked():
    open_engine = FastQueryClient._open_engine
    con = open_engine(FastQueryClient([]))
    try:
        assert _setting(con, "enable_external_access") is True
        assert _setting(con, "lock_configuration") is False
        assert con.execute("SELECT 1").fetchone()[0] == 1
    finally:
        con.close()


def test_extra_config_is_applied_to_the_opened_engine():
    open_engine = FastQueryClient._open_engine
    con = open_engine(FastQueryClient([]), {"threads": 1})
    try:
        assert int(_setting(con, "threads")) == 1
    finally:
        con.close()


def test_a_built_session_serves_its_relation_and_is_locked(artifact):
    open_engine = FastQueryClient._open_engine
    build_session = FastQueryClient._build_session
    client = FastQueryClient([])
    con = open_engine(client)
    try:
        attached = build_session(client, con, [_relation(artifact)])
        assert attached == 1
        assert con.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == 4
        assert _setting(con, "enable_external_access") is False
        assert _setting(con, "lock_configuration") is True
        assert _setting(con, "python_enable_replacements") is False
    finally:
        con.close()


def test_relations_without_an_artifact_are_not_counted_but_the_session_is_locked(
    artifact,
):
    open_engine = FastQueryClient._open_engine
    build_session = FastQueryClient._build_session
    client = FastQueryClient([])
    con = open_engine(client)
    try:
        attached = build_session(
            client,
            con,
            [
                FastRelation(name="empty", artifact_path=None, artifact_key=None,
                             columns=[]),
                _relation(artifact),
            ],
        )
        assert attached == 1
        assert _setting(con, "lock_configuration") is True
    finally:
        con.close()


def test_connect_opens_and_builds_through_the_two_steps(artifact, monkeypatch):
    open_engine = FastQueryClient._open_engine
    build_session = FastQueryClient._build_session
    calls = []

    def spy_open(self, *a, **kw):
        calls.append("open")
        return open_engine(self, *a, **kw)

    def spy_build(self, con, relations):
        calls.append(("build", len(relations)))
        return build_session(self, con, relations)

    monkeypatch.setattr(FastQueryClient, "_open_engine", spy_open)
    monkeypatch.setattr(FastQueryClient, "_build_session", spy_build)

    df = FastQueryClient([_relation(artifact)]).execute_query(
        "SELECT SUM(amount) AS s FROM sales"
    )
    assert int(df.iloc[0]["s"]) == 100
    assert calls == ["open", ("build", 1)]
