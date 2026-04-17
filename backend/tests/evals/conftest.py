"""Phase 2 eval harness: parametrize pytest over backend/evals/suites/*.yaml.

The tests here drive the in-product eval feature end-to-end: they POST each
YAML to ``/api/tests/suites/import``, start a run via
``/api/tests/runs/batch``, poll ``/api/tests/runs/{id}/results`` until
terminal, and assert ``status == "pass"`` per case.

Everything is gated behind ``@pytest.mark.evals`` and requires a real LLM
credential (``OPENAI_API_KEY_TEST``) because the agent actually runs.
"""

import os
import time
from pathlib import Path
from typing import List, Tuple

import pytest
import yaml


CHINOOK_DB_PATH = (
    Path(__file__).resolve().parents[2] / "demo-datasources" / "chinook.sqlite"
)


# Resolve once at import; parametrize is evaluated at collection time.
SUITES_DIR = Path(__file__).resolve().parent / "suites"


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
def eval_env(
    create_user, login_user, whoami,
    create_llm_provider_and_models,
    install_demo_data_source,
):
    """Seed a fresh org with: admin user, LLM provider, and the committed
    chinook demo data source (surfaced in the product as "Music Store").

    Returns a dict with ``token``, ``org_id`` and the installed data source
    id. Sanity YAMLs reference chinook tables (Album, Artist, Invoice,
    InvoiceLine, Customer, …) via ``data_source_slugs: ["Music Store"]``.
    """
    if not os.getenv("OPENAI_API_KEY_TEST"):
        pytest.skip("OPENAI_API_KEY_TEST not set; skipping eval harness tests")
    if not CHINOOK_DB_PATH.exists():
        pytest.skip(f"Chinook demo db missing at {CHINOOK_DB_PATH}")

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    create_llm_provider_and_models(user_token=token, org_id=org_id)
    result = install_demo_data_source(
        demo_id="chinook", user_token=token, org_id=org_id,
    )
    return {
        "token": token,
        "org_id": org_id,
        "data_source_id": result["data_source_id"],
    }


@pytest.fixture
def wait_for_run(test_client):
    """Poll ``/api/tests/runs/{run_id}/results`` until all results are
    terminal or the timeout fires.

    Prints every status transition to stdout so ``pytest -s`` surfaces
    progress (individual result ``status`` plus a trailing summary of
    tool calls observed so far).
    """
    terminal = {"pass", "fail", "error", "stopped", "success"}

    def _wait(run_id: str, *, user_token: str, org_id: str,
              timeout_s: float = 240.0, poll_s: float = 3.0):
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }
        deadline = time.time() + timeout_s
        last_statuses: Dict[str, str] = {}
        started = time.time()

        print(f"[eval] run={run_id} polling every {poll_s:g}s "
              f"(timeout {timeout_s:g}s)…", flush=True)

        while time.time() < deadline:
            resp = test_client.get(
                f"/api/tests/runs/{run_id}/results", headers=headers,
            )
            assert resp.status_code == 200, resp.json()
            results = resp.json()

            # Emit a line when any result's status changes.
            for r in results:
                rid = r["id"]
                status = r.get("status") or ""
                if last_statuses.get(rid) != status:
                    last_statuses[rid] = status
                    elapsed = time.time() - started
                    print(
                        f"[eval] t+{elapsed:5.1f}s  result={rid[:8]} "
                        f"status={status}",
                        flush=True,
                    )

            if results and all(r["status"] in terminal for r in results):
                # Peek at the run-status endpoint for a trailing summary of
                # tool calls per result — helpful when a case unexpectedly
                # passes/fails.
                try:
                    status_resp = test_client.get(
                        f"/api/tests/runs/{run_id}/status", headers=headers,
                    )
                    if status_resp.status_code == 200:
                        data = status_resp.json()
                        for item in (data.get("results") or []):
                            rid = item.get("result", {}).get("id", "?")
                            tool_names: List[str] = []
                            for comp in (item.get("completions") or []):
                                for block in (comp.get("completion_blocks") or []):
                                    te = block.get("tool_execution") or {}
                                    name = te.get("tool_name")
                                    if name:
                                        tool_names.append(name)
                            if tool_names:
                                print(
                                    f"[eval] result={str(rid)[:8]} "
                                    f"tools={' → '.join(tool_names)}",
                                    flush=True,
                                )
                except Exception:
                    pass
                return results
            time.sleep(poll_s)
        raise TimeoutError(
            f"run {run_id} did not reach terminal state in {timeout_s}s; "
            f"last: {last_statuses}"
        )

    return _wait
