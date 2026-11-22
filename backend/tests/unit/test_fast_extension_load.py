"""The verified-once httpfs load for FAST serving engines (option V).

With BOW_FAST_VERIFIED_EXTENSION_LOAD on, the process checks the extension
file once (owner, contents of its directory, hash, a signature-checked load)
and then lets each serving engine load that file without re-checking the
signature. These tests go through `trusted_httpfs`, the `extension` module's
functions and `FastQueryClient.execute_query`, with real DuckDB, real
extension files and real encrypted extracts. A directory reaches V only
through the `trusted_extension_dir` fixture (tests/fixtures/fast_extension.py).
"""

import ast
import dataclasses
import logging
import os
import shutil
import threading
from pathlib import Path

import duckdb
import pytest

from app.data_sources.fast import artifacts, extension
from app.data_sources.fast import fast_client as fast_client_module
from app.data_sources.fast.fast_client import FastQueryClient, FastRelation
from app.data_sources.fast.rls import Filter
from tests.fixtures.fast_extension import (
    extension_file,
    make_httpfs_dir,
    prepared_extensions_dir,  # noqa: F401 - fixture
    trusted_extension_dir,  # noqa: F401 - fixture
    write_hash_record,
)

BACKEND = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _fresh_verifier(monkeypatch):
    """Each test starts with a process that has not verified anything yet."""
    monkeypatch.setattr(extension, "_VERIFIER", extension._Verifier())
    monkeypatch.delenv(extension.SWITCH_ENV, raising=False)
    monkeypatch.delenv(extension.DIRECTORY_ENV, raising=False)


def _write_extract(path, key, rows=(("EMEA", 10), ("EMEA", 20), ("APAC", 30), ("AMER", 40))):
    con = artifacts.connect_encrypted(path, key)
    con.execute("CREATE TABLE sales (region VARCHAR, amount INTEGER)")
    con.executemany("INSERT INTO sales VALUES (?, ?)", list(rows))
    con.close()
    return str(path)


def _relation(path, key, name="sales", **kw):
    return FastRelation(name=name, artifact_path=str(path), artifact_key=key,
                        columns=[{"name": "region"}, {"name": "amount"}], **kw)


@pytest.fixture
def extract(tmp_path):
    key = artifacts.new_artifact_key()
    return _write_extract(tmp_path / "sales.duckdb", key), key


class _OpenCounter:
    """Counts the engines opened while it is installed (a spy, not a fake)."""

    def __init__(self, monkeypatch):
        self.count = 0
        real = duckdb.connect

        def counting(*a, **kw):
            self.count += 1
            return real(*a, **kw)

        monkeypatch.setattr(fast_client_module.duckdb, "connect", counting)


# --------------------------------------------------------------------- V2
# trusted_httpfs(directory): the unit seam.

def test_no_directory_is_not_trusted(tmp_path):
    assert extension.trusted_httpfs(None) is None
    assert extension.trusted_httpfs(str(tmp_path / "absent")) is None


def test_a_directory_without_the_file_is_not_trusted(tmp_path):
    assert extension.trusted_httpfs(str(tmp_path), require_foreign_owner=False) is None


def test_a_good_file_is_trusted(prepared_extensions_dir, tmp_path):
    path = make_httpfs_dir(prepared_extensions_dir, tmp_path / "ext")
    assert extension.trusted_httpfs(str(tmp_path / "ext"), require_foreign_owner=False) == path


def test_a_location_this_process_owns_is_refused(prepared_extensions_dir, tmp_path):
    directory = tmp_path / "ext"
    path = make_httpfs_dir(prepared_extensions_dir, directory)
    assert extension.trusted_httpfs(str(directory)) is None
    # Read-only is not enough: the owner may give write access back.
    for root, dirs, files in os.walk(directory):
        for f in files:
            os.chmod(os.path.join(root, f), 0o444)
    for root, dirs, files in os.walk(directory, topdown=False):
        os.chmod(root, 0o555)
    try:
        assert not os.access(path, os.W_OK)
        assert extension.trusted_httpfs(str(directory)) is None
    finally:
        for root, dirs, files in os.walk(directory):
            os.chmod(root, 0o755)


def test_a_hash_that_does_not_match_is_refused(prepared_extensions_dir, tmp_path):
    directory = tmp_path / "ext"
    path = make_httpfs_dir(prepared_extensions_dir, directory)
    Path(path + ".sha256").write_text("0" * 64 + "\n")
    assert extension.trusted_httpfs(str(directory), require_foreign_owner=False) is None


def test_anything_besides_httpfs_in_the_directory_is_refused(prepared_extensions_dir, tmp_path):
    directory = tmp_path / "ext"
    path = make_httpfs_dir(prepared_extensions_dir, directory)
    shutil.copy2(extension_file(prepared_extensions_dir, "tpch"),
                 os.path.join(os.path.dirname(path), "tpch.duckdb_extension"))
    assert extension.trusted_httpfs(str(directory), require_foreign_owner=False) is None


def test_a_file_that_fails_the_signature_checked_load_is_refused(tmp_path):
    directory = tmp_path / "ext"
    path = extension_file(directory, "httpfs")
    os.makedirs(os.path.dirname(path))
    Path(path).write_bytes(b"not an extension " * 1024)
    write_hash_record(path)
    assert extension.trusted_httpfs(str(directory), require_foreign_owner=False) is None


# The process-wide decision: current(), its retry, single flight, re-hash.

def test_with_the_switch_off_nothing_is_verified(trusted_extension_dir, monkeypatch):
    monkeypatch.setenv(extension.SWITCH_ENV, "0")
    assert extension.current() is None
    assert extension.state()["status"] == "off"
    assert extension.state()["verifications"] == 0


def test_with_the_switch_on_a_good_file_is_trusted(trusted_extension_dir):
    assert extension.current() == trusted_extension_dir.path
    state = extension.state()
    assert state["status"] == "trusted"
    assert state["path"] == trusted_extension_dir.path
    assert state["verifications"] == 1


def test_one_log_line_per_change_of_state_not_per_call(trusted_extension_dir, caplog):
    caplog.set_level(logging.INFO, logger=extension.logger.name)
    for _ in range(5):
        assert extension.current() == trusted_extension_dir.path
    lines = [r for r in caplog.records if r.name == extension.logger.name]
    assert len(lines) == 1, [r.getMessage() for r in lines]
    assert "trusted" in lines[0].getMessage()


def test_a_file_replaced_after_verification_is_re_verified_at_once(trusted_extension_dir):
    t = trusted_extension_dir
    assert extension.current() == t.path
    replacement = t.path + ".new"
    shutil.copy2(t.path, replacement)
    os.replace(replacement, t.path)
    assert extension.current() == t.path
    assert extension.state()["verifications"] == 2


def test_unavailable_is_re_checked_after_60_seconds_and_not_before(trusted_extension_dir):
    t = trusted_extension_dir
    aside = t.path + ".aside"
    os.rename(t.path, aside)
    assert extension.current() is None
    assert extension.state()["status"] == "unavailable"
    os.rename(aside, t.path)
    t.clock.advance(59)
    assert extension.current() is None
    assert extension.state()["verifications"] == 1
    t.clock.advance(1)
    assert extension.current() == t.path
    assert extension.state()["verifications"] == 2


def test_the_hash_is_re_computed_every_five_minutes(trusted_extension_dir):
    t = trusted_extension_dir
    assert extension.current() == t.path
    first = extension.state()["hashed_at"]
    t.clock.advance(299)
    extension.current()
    assert extension.state()["hashed_at"] == first
    t.clock.advance(1)
    assert extension.current() == t.path
    assert extension.state()["hashed_at"] == first + 300


def test_a_re_hash_that_no_longer_matches_makes_v_unavailable(trusted_extension_dir, monkeypatch):
    t = trusted_extension_dir
    assert extension.current() == t.path
    # The file's bytes changed while its stat stayed the same (a file system
    # that kept the change time): only the re-hash can see it.
    record = extension._RECORDS[t.path]
    monkeypatch.setitem(extension._RECORDS, t.path, dataclasses.replace(record, sha256="0" * 64))
    t.clock.advance(300)
    assert extension.current() is None
    assert extension.state()["status"] == "unavailable"


def test_two_threads_verifying_at_once_load_once(prepared_extensions_dir, tmp_path, monkeypatch):
    directory = tmp_path / "ext"
    path = make_httpfs_dir(prepared_extensions_dir, directory)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow_check():
        calls.append(1)
        entered.set()
        release.wait(10)
        return extension.trusted_httpfs(str(directory), require_foreign_owner=False)

    monkeypatch.setenv(extension.SWITCH_ENV, "1")
    monkeypatch.setattr(extension, "_VERIFIER", extension._Verifier(check=slow_check))
    results = {}
    first = threading.Thread(target=lambda: results.setdefault("first", extension.current()))
    first.start()
    assert entered.wait(10)
    # The second caller does not wait for the first: it gets the last decision.
    results["second"] = extension.current()
    release.set()
    first.join(10)
    assert results == {"first": path, "second": None}
    assert len(calls) == 1
    assert extension.state()["verifications"] == 1


# --------------------------------------------------------------------- V3

def test_a_failed_explicit_load_reopens_as_today_and_turns_v_off_for_60_seconds(
    trusted_extension_dir, extract, monkeypatch, caplog
):
    t = trusted_extension_dir
    assert extension.current() == t.path
    # Swap the file for garbage of the same size, times put back. (Swapped,
    # not rewritten in place: verification mapped the real file into this
    # process, and rewriting a mapped library under it crashes the process,
    # with or without V.) The inode and change time cannot be put back from a
    # test, so the record is moved to them: the stat matches, as it would for
    # someone who could.
    st = os.stat(t.path)
    swap = t.path + ".swap"
    Path(swap).write_bytes(b"\0" * st.st_size)
    os.replace(swap, t.path)
    os.utime(t.path, ns=(st.st_atime_ns, st.st_mtime_ns))
    now = os.stat(t.path)
    record = extension._RECORDS[t.path]
    monkeypatch.setitem(extension._RECORDS, t.path, dataclasses.replace(
        record, dev=now.st_dev, ino=now.st_ino, ctime_ns=now.st_ctime_ns))
    assert extension.unchanged(t.path)
    caplog.set_level(logging.INFO, logger=extension.logger.name)
    caplog.clear()
    opened = _OpenCounter(monkeypatch)

    df = FastQueryClient([_relation(*extract)]).execute_query("SELECT SUM(amount) AS s FROM sales")

    assert int(df.iloc[0]["s"]) == 100
    assert opened.count == 2
    assert extension.state()["status"] == "unavailable"
    lines = [r for r in caplog.records if r.name == extension.logger.name]
    assert len(lines) == 1, [r.getMessage() for r in lines]
    t.clock.advance(59)
    assert extension.current() is None


def test_a_changed_stat_at_open_reopens_as_today(trusted_extension_dir, extract, monkeypatch):
    t = trusted_extension_dir
    assert extension.current() == t.path
    os.utime(t.path, (1, 1))
    opened = _OpenCounter(monkeypatch)
    client = FastQueryClient([_relation(*extract)])
    con = client._open_engine(trusted_extension=t.path)
    try:
        assert con.execute("SELECT current_setting('allow_unsigned_extensions')").fetchone()[0] is False
    finally:
        con.close()
    assert opened.count == 1
    assert extension.state()["status"] == "unavailable"


def test_a_garbage_httpfs_on_todays_path_still_serves(extract, tmp_path, monkeypatch):
    home = tmp_path / "home"
    garbage = extension_file(home / ".duckdb" / "extensions", "httpfs")
    os.makedirs(os.path.dirname(garbage))
    Path(garbage).write_bytes(b"not an extension " * 4096)
    monkeypatch.setenv("HOME", str(home))
    probe = duckdb.connect(":memory:")
    try:
        where = probe.execute(
            "SELECT install_path FROM duckdb_extensions() WHERE extension_name = 'httpfs'"
        ).fetchone()[0]
    finally:
        probe.close()
    assert where == garbage  # DuckDB looks where the garbage is

    df = FastQueryClient([_relation(*extract)]).execute_query("SELECT SUM(amount) AS s FROM sales")
    assert int(df.iloc[0]["s"]) == 100


def test_a_serving_engine_with_v_has_the_exact_config_and_httpfs_loaded(
    trusted_extension_dir, extract
):
    t = trusted_extension_dir
    assert extension.current() == t.path
    client = FastQueryClient([_relation(*extract)])
    con = client._open_engine(trusted_extension=t.path)
    try:
        got = con.execute(
            "SELECT current_setting('allow_unsigned_extensions'), "
            "current_setting('autoinstall_known_extensions'), "
            "current_setting('autoload_known_extensions'), "
            "current_setting('allow_community_extensions'), "
            "current_setting('extension_directory')"
        ).fetchone()
        loaded = con.execute(
            "SELECT loaded FROM duckdb_extensions() WHERE extension_name = 'httpfs'"
        ).fetchone()[0]
    finally:
        con.close()
    assert got == (True, False, False, False, t.directory)
    assert loaded is True


def test_execute_query_reads_the_decision_once_and_opens_one_engine(
    trusted_extension_dir, extract, monkeypatch
):
    t = trusted_extension_dir
    asked = []
    real_current = extension.current

    def spy():
        asked.append(1)
        return real_current()

    monkeypatch.setattr(extension, "current", spy)
    seen = []
    real_open = FastQueryClient._open_engine

    def spy_open(self, *a, **kw):
        seen.append(kw.get("trusted_extension"))
        return real_open(self, *a, **kw)

    monkeypatch.setattr(FastQueryClient, "_open_engine", spy_open)
    df = FastQueryClient([_relation(*extract)]).execute_query("SELECT COUNT(*) AS n FROM sales")
    assert int(df.iloc[0]["n"]) == 4
    assert asked == [1]
    assert seen == [t.path]


# --------------------------------------------------------------------- V4

@pytest.fixture(params=["v_off", "v_on"])
def v_mode(request):
    if request.param == "v_on":
        t = request.getfixturevalue("trusted_extension_dir")
        assert extension.current() == t.path
    return request.param


def test_a_path_with_a_single_quote_attaches_and_serves(v_mode, tmp_path):
    key = artifacts.new_artifact_key()
    written = _write_extract(tmp_path / "x.duckdb", key)
    folder = tmp_path / "it's here"
    folder.mkdir()
    path = str(folder / "x.duckdb")
    shutil.move(written, path)
    df = FastQueryClient([_relation(path, key)]).execute_query("SELECT SUM(amount) AS s FROM sales")
    assert int(df.iloc[0]["s"]) == 100


PLANTED_KEY = "planted+key/not=url-safe"


def _no_trace_of(secret, caplog, exc=None):
    for r in caplog.records:
        assert secret not in r.getMessage()
        assert secret not in repr(r.__dict__)
    if exc is not None:
        assert secret not in str(exc)
        assert secret not in repr(exc)


def test_a_key_that_is_not_base64url_skips_an_unfiltered_relation(v_mode, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    good_key = artifacts.new_artifact_key()
    good = _write_extract(tmp_path / "good.duckdb", good_key)
    # DuckDB itself would accept this key; the check refuses it before ATTACH.
    planted = _write_extract(tmp_path / "planted.duckdb", PLANTED_KEY, rows=(("X", 1),))
    client = FastQueryClient([
        _relation(planted, PLANTED_KEY, name="planted"),
        _relation(good, good_key, name="sales"),
    ])
    df = client.execute_query("SELECT SUM(amount) AS s FROM sales")
    assert int(df.iloc[0]["s"]) == 100
    with pytest.raises(Exception) as missing:
        client.execute_query("SELECT * FROM planted")
    assert "planted" in str(missing.value)
    assert any(r.getMessage() == "fast.key_check.refused" for r in caplog.records)
    _no_trace_of(PLANTED_KEY, caplog, missing.value)


def test_a_key_that_is_not_base64url_refuses_a_row_filtered_session(v_mode, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    planted = _write_extract(tmp_path / "planted.duckdb", PLANTED_KEY)
    rel = _relation(planted, PLANTED_KEY, rls_filter=Filter(column="region", allowed_values=["EMEA"]))
    with pytest.raises(RuntimeError) as refused:
        FastQueryClient([rel]).execute_query("SELECT COUNT(*) FROM sales")
    assert "sales" in str(refused.value)
    _no_trace_of(PLANTED_KEY, caplog, refused.value)


@pytest.mark.parametrize("key", [
    "abc' , READ_ONLY); SELECT 'x",
    "a" * 15,
    "a" * 44 + "===",
    "a" * 44 + "=\n",
    "",
])
def test_the_key_check_refuses_values_outside_the_alphabet(key, tmp_path):
    good_key = artifacts.new_artifact_key()
    good = _write_extract(tmp_path / "good.duckdb", good_key)
    client = FastQueryClient([
        _relation(good, key, name="planted"),
        _relation(good, good_key, name="sales"),
    ])
    with client.connect() as con:
        names = {r[0] for r in con.execute("SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall()}
    assert names == {"sales"}


def test_keys_made_the_products_way_pass_the_check(tmp_path):
    keys = [artifacts.new_artifact_key() for _ in range(200)]
    assert all(fast_client_module._KEY_FORMAT.fullmatch(k) for k in keys)


# --------------------------------------------------------------------- V6

DIFFERENTIAL = [
    "SELECT region, SUM(amount) AS s FROM sales GROUP BY region ORDER BY region",
    "SELECT * FROM sales ORDER BY amount",
    "SELECT COUNT(*) AS n, AVG(amount) AS a FROM sales WHERE region <> 'APAC'",
    "SELECT region, amount, rank() OVER (ORDER BY amount DESC) AS r FROM sales ORDER BY r",
]


def test_results_with_v_equal_results_without(extract, request):
    client = FastQueryClient([_relation(*extract)])
    before = [client.execute_query(q) for q in DIFFERENTIAL]
    t = request.getfixturevalue("trusted_extension_dir")
    assert extension.current() == t.path
    after = [client.execute_query(q) for q in DIFFERENTIAL]
    for a, b in zip(before, after):
        assert a.equals(b)
        assert list(a.dtypes) == list(b.dtypes)


# --------------------------------------------------------------------- V7

def _app_sources():
    for path in (BACKEND / "app").rglob("*.py"):
        yield path, path.read_text(encoding="utf-8")


def test_no_production_path_relaxes_the_owner_rule():
    places = []
    for path, src in _app_sources():
        if "require_foreign_owner" not in src:
            continue
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.keyword) and node.arg == "require_foreign_owner":
                places.append(f"{path}: passed as an argument")
            if isinstance(node, ast.arg) and node.arg == "require_foreign_owner":
                places.append(f"{path}: parameter")
            if isinstance(node, ast.Name) and node.id == "require_foreign_owner":
                places.append(f"{path}: used")
    rel = "app/data_sources/fast/extension.py"
    assert places, "the owner rule's parameter is gone"
    assert all(p.startswith(str(BACKEND / rel)) for p in places), places
    assert not [p for p in places if "argument" in p], places
    tree = ast.parse((BACKEND / rel).read_text(encoding="utf-8"))
    owners = [
        n.name for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
        and any(a.arg == "require_foreign_owner" for a in n.args.kwonlyargs + n.args.args)
    ]
    assert owners == ["trusted_httpfs"]


def test_only_two_environment_names_are_read_for_this_and_only_in_extension_py():
    found = {}
    for path, src in _app_sources():
        for name in set(__import__("re").findall(r"BOW_(?:FAST|DUCKDB)_[A-Z_]+", src)):
            found.setdefault(name, set()).add(str(path.relative_to(BACKEND)))
    assert found == {
        "BOW_FAST_VERIFIED_EXTENSION_LOAD": {"app/data_sources/fast/extension.py"},
        "BOW_DUCKDB_EXTENSION_DIR": {"app/data_sources/fast/extension.py"},
    }


def test_the_unsigned_flag_is_set_only_in_open_engine():
    hits = []
    for path, src in _app_sources():
        if "allow_unsigned_extensions" in src:
            hits.append(str(path.relative_to(BACKEND)))
    assert hits == ["app/data_sources/fast/fast_client.py"]
    src = (BACKEND / "app/data_sources/fast/fast_client.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    holders = [
        n.name for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
        and "allow_unsigned_extensions" in (ast.get_source_segment(src, n) or "")
    ]
    assert holders == ["_open_engine"]


def test_every_switch_set_still_refuses_a_directory_the_test_user_owns(
    prepared_extensions_dir, tmp_path, monkeypatch
):
    directory = tmp_path / "ext"
    make_httpfs_dir(prepared_extensions_dir, directory)
    for name in [n for n in os.environ if n.startswith(("BOW_FAST_", "BOW_DUCKDB_"))]:
        monkeypatch.delenv(name)
    for name in (
        "BOW_FAST_VERIFIED_EXTENSION_LOAD", "BOW_DUCKDB_EXTENSION_DIR",
        "BOW_FAST_TRUST_ANY_OWNER", "BOW_FAST_REQUIRE_FOREIGN_OWNER",
        "BOW_DUCKDB_ALLOW_UNSIGNED", "BOW_FAST_EXTENSION_OWNER_CHECK",
    ):
        value = str(directory) if name.endswith("_DIR") else "1"
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("BOW_FAST_REQUIRE_FOREIGN_OWNER", "0")
    monkeypatch.setenv("BOW_FAST_EXTENSION_OWNER_CHECK", "false")
    assert extension.directory() == str(directory)
    assert extension.current() is None
    assert extension.state()["status"] == "unavailable"


# --------------------------------------------------------------------- V8
# The writer, through its own route: no V fixture.

def _httpfs_of(con):
    return con.execute(
        "SELECT loaded, install_path FROM duckdb_extensions() WHERE extension_name = 'httpfs'"
    ).fetchone()


@pytest.mark.parametrize("switch", ["0", "1"])
def test_the_writer_loads_httpfs_signature_checked_from_the_directory(
    switch, prepared_extensions_dir, tmp_path, monkeypatch
):
    directory = tmp_path / "ext"
    path = make_httpfs_dir(prepared_extensions_dir, directory)
    monkeypatch.setenv(extension.DIRECTORY_ENV, str(directory))
    monkeypatch.setenv(extension.SWITCH_ENV, switch)
    key = artifacts.new_artifact_key()
    con = artifacts.connect_encrypted(tmp_path / "w.duckdb", key)
    try:
        assert _httpfs_of(con) == (True, path)
        assert con.execute("SELECT current_setting('allow_unsigned_extensions')").fetchone()[0] is False
        con.execute("CREATE TABLE t AS SELECT 7 AS x")
    finally:
        con.close()
    check = artifacts.connect_encrypted(tmp_path / "w.duckdb", key, read_only=True)
    try:
        assert check.execute("SELECT x FROM t").fetchone()[0] == 7
    finally:
        check.close()


@pytest.mark.parametrize("case", ["unset", "absent", "no_file", "garbage"])
def test_the_writer_opens_as_today_when_the_directory_cannot_serve(case, tmp_path, monkeypatch):
    directory = tmp_path / "ext"
    if case == "absent":
        monkeypatch.setenv(extension.DIRECTORY_ENV, str(directory))
    elif case == "no_file":
        directory.mkdir()
        monkeypatch.setenv(extension.DIRECTORY_ENV, str(directory))
    elif case == "garbage":
        path = extension_file(directory, "httpfs")
        os.makedirs(os.path.dirname(path))
        Path(path).write_bytes(b"not an extension " * 1024)
        monkeypatch.setenv(extension.DIRECTORY_ENV, str(directory))
    key = artifacts.new_artifact_key()
    con = artifacts.connect_encrypted(tmp_path / "w.duckdb", key)
    try:
        con.execute("CREATE TABLE t AS SELECT 7 AS x")
        assert con.execute("SELECT current_setting('extension_directory')").fetchone()[0] != str(directory)
    finally:
        con.close()
    check = artifacts.connect_encrypted(tmp_path / "w.duckdb", key, read_only=True)
    try:
        assert check.execute("SELECT x FROM t").fetchone()[0] == 7
    finally:
        check.close()


def test_directory_is_the_variable_when_it_names_a_directory(tmp_path, monkeypatch):
    assert extension.directory() is None
    monkeypatch.setenv(extension.DIRECTORY_ENV, str(tmp_path / "absent"))
    assert extension.directory() is None
    monkeypatch.setenv(extension.DIRECTORY_ENV, str(tmp_path))
    assert extension.directory() == str(tmp_path)
