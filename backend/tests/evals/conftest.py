"""Phase 2 eval harness: parametrize pytest over backend/evals/suites/*.yaml.

The tests here drive the in-product eval feature end-to-end: they POST each
YAML to ``/api/tests/suites/import``, start a run via
``/api/tests/runs/batch``, poll ``/api/tests/runs/{id}/results`` until
terminal, and assert ``status == "pass"`` per case.

Everything is gated behind ``@pytest.mark.evals`` and requires a real LLM
credential (``OPENAI_API_KEY_TEST``) because the agent actually runs.
"""

import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import List, Tuple

import pytest
import yaml


# Resolve once at import; parametrize is evaluated at collection time.
SUITES_DIR = Path(__file__).resolve().parents[2] / "evals" / "suites"


def _load_all_yaml_cases() -> List[Tuple[str, str, str]]:
    """Return ``(yaml_path, suite_name, case_name)`` triples for every case.

    Reads each YAML file only far enough to discover case names — the
    service is the real validator at import time.
    """
    if not SUITES_DIR.exists():
        return []
    out: List[Tuple[str, str, str]] = []
    for path in sorted(SUITES_DIR.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text()) or {}
        except Exception:
            continue
        suite_name = raw.get("name") or path.stem
        for case in raw.get("cases") or []:
            name = (case or {}).get("name")
            if name:
                out.append((str(path), suite_name, name))
    return out


ALL_EVAL_CASES = _load_all_yaml_cases()


def pytest_collection_modifyitems(config, items):
    """Auto-apply ``evals`` marker to everything under tests/evals/."""
    evals_root = Path(__file__).resolve().parent
    for item in items:
        try:
            test_path = Path(str(item.fspath)).resolve()
        except Exception:
            continue
        try:
            test_path.relative_to(evals_root)
        except ValueError:
            continue
        item.add_marker(pytest.mark.evals)


@pytest.fixture
def eval_demo_sqlite_db():
    """Deterministic sqlite with users + orders — matches prompts in
    backend/evals/suites/sanity_*.yaml."""
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        db_path = f.name
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                total_amount REAL NOT NULL,
                order_date TEXT NOT NULL
            );
            """
        )
        users = [
            (i, f"user{i}@example.com", f"User {i}", "2025-01-01")
            for i in range(1, 21)
        ]
        conn.executemany("INSERT INTO users VALUES (?, ?, ?, ?)", users)

        # 10 orders per month for 12 months of 2025 = 120 rows, total revenue
        # deterministic per month so sanity_dashboards has a stable target.
        orders = []
        order_id = 1
        for month in range(1, 13):
            for i in range(10):
                user_id = (order_id % 20) + 1
                amount = 100.0 + (order_id % 7) * 25.0
                orders.append((order_id, user_id, amount, f"2025-{month:02d}-15"))
                order_id += 1
        conn.executemany(
            "INSERT INTO orders VALUES (?, ?, ?, ?)", orders
        )
        conn.commit()
    finally:
        conn.close()
    yield db_path
    try:
        os.unlink(db_path)
    except OSError:
        pass


@pytest.fixture
def eval_env(
    create_user, login_user, whoami,
    create_llm_provider_and_models,
    create_data_source, eval_demo_sqlite_db,
):
    """Seed a fresh org with: admin user, LLM provider, eval_demo sqlite DS.

    Returns a dict with ``token`` and ``org_id`` so tests can reuse it.
    """
    if not os.getenv("OPENAI_API_KEY_TEST"):
        pytest.skip("OPENAI_API_KEY_TEST not set; skipping eval harness tests")

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    create_llm_provider_and_models(user_token=token, org_id=org_id)
    create_data_source(
        name="eval_demo",
        type="sqlite",
        config={"database": eval_demo_sqlite_db},
        credentials={},
        user_token=token,
        org_id=org_id,
    )
    return {"token": token, "org_id": org_id}


@pytest.fixture
def wait_for_run(test_client):
    """Poll ``/api/tests/runs/{run_id}/results`` until all results are
    terminal or the timeout fires."""
    terminal = {"pass", "fail", "error", "stopped", "success"}

    def _wait(run_id: str, *, user_token: str, org_id: str,
              timeout_s: float = 240.0, poll_s: float = 3.0):
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }
        deadline = time.time() + timeout_s
        last = None
        while time.time() < deadline:
            resp = test_client.get(
                f"/api/tests/runs/{run_id}/results", headers=headers,
            )
            assert resp.status_code == 200, resp.json()
            results = resp.json()
            last = results
            if results and all(r["status"] in terminal for r in results):
                return results
            time.sleep(poll_s)
        raise TimeoutError(
            f"run {run_id} did not reach terminal state in {timeout_s}s; "
            f"last: {last}"
        )

    return _wait
