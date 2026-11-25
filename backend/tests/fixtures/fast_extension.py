"""DuckDB extension directories for the FAST verified-load tests.

Two things live here:

- `prepared_extensions_dir` (session): one directory holding `httpfs` and the
  extra signed extensions the lock test needs, installed but never loaded. In
  CI, `scripts/prepare_test_extensions.py` builds it once and exports its path
  as BOW_TEST_DUCKDB_EXTENSION_DIR; then a missing file fails the run. Without
  the variable the fixture builds it itself, under a file lock so that parallel
  workers prepare it once and the others wait, and skips (with the reason) when
  nothing can be installed here.
- `trusted_extension_dir`: the one route by which a test hands V a directory.
  It copies `httpfs` (and its `.info`) into a private directory, writes the
  hash record, and swaps the module's verifier for one that checks that
  directory with the owner rule off (a test cannot make a root-owned file) and
  an injected clock. Nothing else in the tests relaxes the owner rule, and
  nothing in the app can.
"""

import hashlib
import os
import shutil
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

import duckdb
import pytest
from filelock import FileLock

PREPARED_ENV = "BOW_TEST_DUCKDB_EXTENSION_DIR"
EXTRA_EXTENSIONS = ("tpch", "inet", "excel", "sqlite_scanner", "postgres_scanner", "azure")


def engine_layout() -> tuple[str, str]:
    """The (version, platform) sub-folders DuckDB uses inside an extension directory."""
    con = duckdb.connect(":memory:")
    try:
        version = con.execute("SELECT version()").fetchone()[0]
        platform = con.execute("SELECT platform FROM pragma_platform()").fetchone()[0]
    finally:
        con.close()
    return version, platform


def extension_file(directory, name: str) -> str:
    version, platform = engine_layout()
    return os.path.join(str(directory), version, platform, f"{name}.duckdb_extension")


def _installed_copy(name: str):
    """The file DuckDB already installed for `name` in its default directory, if any."""
    con = duckdb.connect(":memory:")
    try:
        row = con.execute(
            "SELECT install_path FROM duckdb_extensions() "
            "WHERE extension_name = ? AND installed",
            [name],
        ).fetchone()
    finally:
        con.close()
    path = row[0] if row else None
    return path if path and os.path.isfile(path) else None


def install(directory, names) -> None:
    """Install `names` into `directory`: from a local copy when there is one,
    else from DuckDB's repository (needs the network)."""
    con = duckdb.connect(":memory:", config={"extension_directory": str(directory)})
    try:
        for name in names:
            if os.path.isfile(extension_file(directory, name)):
                continue
            local = _installed_copy(name)
            if local:
                con.execute("INSTALL '" + local.replace("'", "''") + "'")
            else:
                con.execute(f"INSTALL {name}")
    finally:
        con.close()


def write_hash_record(extension_path: str) -> str:
    digest = hashlib.sha256(Path(extension_path).read_bytes()).hexdigest()
    Path(extension_path + ".sha256").write_text(digest + "\n")
    return digest


def _shared_root(tmp_path_factory) -> Path:
    base = tmp_path_factory.getbasetemp()
    # Under xdist every worker has its own base; their parent is shared.
    return base.parent if os.environ.get("PYTEST_XDIST_WORKER") else base


@pytest.fixture(scope="session")
def prepared_extensions_dir(tmp_path_factory) -> str:
    """A directory with httpfs and the extra extensions installed, not loaded."""
    wanted = ("httpfs",) + EXTRA_EXTENSIONS
    given = os.environ.get(PREPARED_ENV)
    if given:
        missing = [n for n in wanted if not os.path.isfile(extension_file(given, n))]
        assert not missing, f"{PREPARED_ENV}={given} lacks {missing}"
        return given
    root = _shared_root(tmp_path_factory)
    target = root / "fast-duckdb-extensions"
    with FileLock(str(root / "fast-duckdb-extensions.lock")):
        try:
            install(target, wanted)
        except Exception as e:  # noqa: BLE001 - reported as the skip reason
            pytest.skip(
                f"cannot install DuckDB extensions here ({type(e).__name__}: "
                f"{str(e).splitlines()[0][:160]}); set {PREPARED_ENV} or allow the network"
            )
    return str(target)


@dataclass
class FakeClock:
    now: float = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class TrustedDir:
    directory: str
    path: str
    clock: FakeClock
    verifier: object = field(repr=False)


def make_httpfs_dir(source_dir: str, target: Path) -> str:
    """A private copy of httpfs (file, `.info`, hash record) in DuckDB's layout."""
    src = extension_file(source_dir, "httpfs")
    dst = extension_file(target, "httpfs")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    if os.path.isfile(src + ".info"):
        shutil.copy2(src + ".info", dst + ".info")
    write_hash_record(dst)
    return dst


@pytest.fixture
def trusted_extension_dir(prepared_extensions_dir, tmp_path, monkeypatch) -> TrustedDir:
    from app.data_sources.fast import extension

    directory = tmp_path / "duckdb_httpfs"
    path = make_httpfs_dir(prepared_extensions_dir, directory)
    clock = FakeClock()
    verifier = extension._Verifier(
        check=partial(extension.trusted_httpfs, str(directory), require_foreign_owner=False),
        clock=clock,
    )
    monkeypatch.setenv(extension.SWITCH_ENV, "1")
    monkeypatch.setattr(extension, "_VERIFIER", verifier)
    return TrustedDir(directory=str(directory), path=path, clock=clock, verifier=verifier)
