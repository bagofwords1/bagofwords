"""Run the performance regressions against the reviewed implementation.

Only this process loads the old function definitions; checkout files are never
changed. Run from backend with its test environment/dependencies configured.
"""
import ast
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "backend"))
BASE = "48e712425"


class BeforeReview:
    loaded = False

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_call(self, item):
        if self.loaded:
            return
        self.loaded = True
        for module, names in {
            "app.services.dashboard_publish": ["waiting_step_labels"],
            "app.services.dashboard_runs": ["_reader_input_wait", "_run_copy", "viewer_run"],
            "app.routes.dashboard": ["_has_own_run"],
        }.items():
            target = importlib.import_module(module)
            path = "backend/" + module.replace(".", "/") + ".py"
            source = subprocess.check_output(["git", "show", f"{BASE}:{path}"], text=True)
            for node in ast.parse(source).body:
                if isinstance(node, ast.AsyncFunctionDef) and node.name in names:
                    exec(compile(ast.get_source_segment(source, node), f"{BASE}:{path}", "exec"), target.__dict__)


raise SystemExit(pytest.main([
    "tests/e2e/dashboards/test_context_status_reads.py",
    "tests/e2e/dashboards/test_context_dependency_reads.py",
    "tests/e2e/dashboards/test_viewer_export_read_budget.py",
    "--db=sqlite", "-q", "--tb=short", "--disable-warnings", "--show-capture=no",
], plugins=[BeforeReview()]))
