"""A viewer's identity resolved for a stamp is forgotten when its transaction ends.

A session keeps the identities it resolved, per (viewer, organization), in
session.info: the default-params read (viewer_data_policy) fills it as the
stamp does (viewer_result_freshness). Once the transaction ends, the next
read or stamp must resolve the viewer's groups and attributes again. That
holds however the process got there: a memo filled by the default-params
read in a process that never imported viewer_result_freshness is dropped
too. Run in a fresh interpreter, where nothing else has been imported yet.
"""
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]

FILL_THEN_COMMIT = """
import sys
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

import app.services.viewer_data_policy  # where the default-params read fills the memo

assert "app.services.viewer_result_freshness" not in sys.modules, "imported already"
session = Session(create_engine("sqlite://"))
session.execute(text("SELECT 1"))
session.info["_default_params_identity"] = {("viewer", "org"): "identity resolved before the change"}
session.commit()
print("dropped" if "_default_params_identity" not in session.info else "kept")
"""


def test_a_memo_filled_before_the_stamp_module_is_imported_is_dropped_at_transaction_end():
    done = subprocess.run([sys.executable, "-c", FILL_THEN_COMMIT], cwd=BACKEND, capture_output=True,
                          text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().splitlines()[-1] == "dropped", done.stdout[-2000:]
