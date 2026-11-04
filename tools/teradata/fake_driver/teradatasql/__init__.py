"""Shim: run the backend against the fake Teradata driver.

    PYTHONPATH=tools/teradata/fake_driver FAKE_TERADATA_STATS=/tmp/td-stats.json \
        tools/agent/boot_stack.sh

Entries on PYTHONPATH win over site-packages, so `import teradatasql` loads
this package, which executes backend/tests/mocks/fake_teradatasql.py in its
place. See tools/teradata/README.md.
"""
import pathlib

_FAKE = pathlib.Path(__file__).resolve().parents[4] / "backend" / "tests" / "mocks" / "fake_teradatasql.py"
exec(compile(_FAKE.read_text(), str(_FAKE), "exec"), globals())
