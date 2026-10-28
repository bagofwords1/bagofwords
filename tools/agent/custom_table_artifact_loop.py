#!/usr/bin/env python3
"""Run the artifact lifetime regression suite in a disposable sandbox.

From backend: uv run python ../tools/agent/custom_table_artifact_loop.py
No credentials, live source, running app or production paths are used.
"""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

with tempfile.TemporaryDirectory(prefix="bow-artifact-loop-") as sandbox:
    backend = Path(__file__).resolve().parents[2] / "backend"
    os.environ["TESTING"] = "true"
    os.environ["TEST_DATABASE_URL"] = f"sqlite:///{sandbox}/app.db"
    os.environ["BOW_DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
    sys.path.insert(0, str(backend))
    os.chdir(backend)
    # Register the real ORM without starting application lifespan/schedulers.
    import main  # noqa: F401

    spec = importlib.util.spec_from_file_location(
        "artifact_lifecycle", backend / "tests/unit/test_fast_artifact_lifecycle.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    unittest.main(module=module, verbosity=2)
