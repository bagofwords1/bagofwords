#!/usr/bin/env python3
"""Install the DuckDB extensions the FAST extension tests use, once.

The lock test (tests/unit/test_fast_extension_lock.py) needs httpfs and six
more signed extensions installed but not loaded; the load tests need httpfs.
This puts all of them in one directory, in DuckDB's own layout, so that no
test worker installs anything. Each one is copied from DuckDB's default
directory when it is already installed there, else fetched from DuckDB's
extension repository.

Prints the directory and, under GitHub Actions, exports it to later steps as
BOW_TEST_DUCKDB_EXTENSION_DIR. With that variable set, the tests fail rather
than skip when a file is missing.

Usage: prepare_test_extensions.py [DIRECTORY]   (default: ./.duckdb-test-extensions)
"""
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from tests.fixtures.fast_extension import (  # noqa: E402
    EXTRA_EXTENSIONS,
    PREPARED_ENV,
    extension_file,
    install,
)


def main(argv: list[str]) -> int:
    target = Path(argv[1] if len(argv) > 1 else ".duckdb-test-extensions").resolve()
    names = ("httpfs",) + EXTRA_EXTENSIONS
    install(target, names)
    missing = [n for n in names if not os.path.isfile(extension_file(target, n))]
    if missing:
        print(f"missing after install: {missing}", file=sys.stderr)
        return 1
    for name in names:
        print(extension_file(target, name))
    env_file = os.environ.get("GITHUB_ENV")
    if env_file:
        with open(env_file, "a", encoding="utf-8") as fh:
            fh.write(f"{PREPARED_ENV}={target}\n")
    print(f"{PREPARED_ENV}={target}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
