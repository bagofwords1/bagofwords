"""Phase 2 eval harness: parametrize pytest over backend/evals/suites/*.yaml.

The tests here drive the in-product eval feature end-to-end: they POST each
YAML to ``/api/tests/suites/import``, start a run via
``/api/tests/runs/batch``, poll ``/api/tests/runs/{id}/results`` until
terminal, and assert ``status == "pass"`` per case.

Everything is gated behind ``@pytest.mark.evals`` and requires a real LLM
credential (``OPENAI_API_KEY_TEST``) because the agent actually runs.
"""

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

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
    create_anthropic_provider_and_models,
    install_demo_data_source,
):
    """Seed a fresh org with: admin user, LLM provider, and the committed
    chinook demo data source (surfaced in the product as "Music Store").

    Chooses the provider based on which ``*_API_KEY_TEST`` env var is set:
    - ``ANTHROPIC_API_KEY_TEST``  → Claude 4.6 Sonnet (preferred)
    - ``OPENAI_API_KEY_TEST``     → GPT-5.4

    Fails fast (skip) if neither is set or Chinook is missing.
    """
    has_anthropic = bool(os.getenv("ANTHROPIC_API_KEY_TEST"))
    has_openai = bool(os.getenv("OPENAI_API_KEY_TEST"))
    if not (has_anthropic or has_openai):
        pytest.skip(
            "No LLM key set for evals — set ANTHROPIC_API_KEY_TEST or "
            "OPENAI_API_KEY_TEST."
        )
    if not CHINOOK_DB_PATH.exists():
        pytest.skip(f"Chinook demo db missing at {CHINOOK_DB_PATH}")

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    if has_anthropic:
        create_anthropic_provider_and_models(user_token=token, org_id=org_id)
    else:
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
def run_case_and_wait(test_client):
    """Create + execute a run for the given case ids, consume the SSE
    stream until ``run.finished``, then return final results.

    Why streaming: Starlette's ``TestClient`` tears down its event loop
    between requests, which kills any ``asyncio.create_task`` spawned by
    the `background=True` path in ``POST /runs/batch``. The streaming
    endpoint keeps a single long-lived request alive for the full agent
    execution, so the event loop stays up and multi-turn threading
    works. In production either path is fine.
    """
    terminal_statuses = {"pass", "fail", "error", "stopped", "success"}

    def _run(case_ids: List[str], *, user_token: str, org_id: str,
             timeout_s: float = 300.0) -> List[Dict[str, Any]]:
        headers = {
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        }

        # 1. Create the run (placeholder results, status="init").
        create_resp = test_client.post(
            "/api/tests/runs",
            json={"case_ids": case_ids, "trigger_reason": "eval"},
            headers=headers,
        )
        assert create_resp.status_code == 200, create_resp.text
        run = create_resp.json()
        run_id = run["id"]
        print(f"[eval] run_id={run_id[:8]} created — streaming…", flush=True)

        # 2. Stream execution end-to-end.
        started = time.time()
        stream_headers = {**headers, "Accept": "text/event-stream"}
        with test_client.stream(
            "POST",
            f"/api/tests/runs/{run_id}/stream",
            headers=stream_headers,
            timeout=timeout_s,
        ) as resp:
            assert resp.status_code == 200, resp.text
            current_event = None
            for raw in resp.iter_lines():
                line = raw if isinstance(raw, str) else (raw.decode() if raw else "")
                line = line.strip()
                if not line:
                    continue
                if line.startswith("event: "):
                    current_event = line[len("event: "):].strip()
                    continue
                if line.startswith("data: "):
                    payload = line[len("data: "):]
                    try:
                        envelope = json.loads(payload) if payload else {}
                    except Exception:
                        envelope = {}
                    # The `data:` line carries the full SSEEvent; its own
                    # payload is the nested ``data`` dict.
                    data = envelope.get("data") or {}
                    elapsed = time.time() - started
                    if current_event == "completion.started":
                        print(
                            f"[eval] t+{elapsed:5.1f}s  turn={data.get('turn_index')} "
                            f"result={str(data.get('result_id',''))[:8]} "
                            f"started",
                            flush=True,
                        )
                    elif current_event == "result.update":
                        print(
                            f"[eval] t+{elapsed:5.1f}s  result={str(data.get('result_id',''))[:8]} "
                            f"status={data.get('status')}",
                            flush=True,
                        )
                    elif current_event == "completion.finished":
                        print(
                            f"[eval] t+{elapsed:5.1f}s  completion finished "
                            f"status={data.get('status')}",
                            flush=True,
                        )
                    elif current_event == "completion.error":
                        print(
                            f"[eval] t+{elapsed:5.1f}s  completion ERROR: "
                            f"{data.get('error')}",
                            flush=True,
                        )
                    elif current_event == "run.finished":
                        print(
                            f"[eval] t+{elapsed:5.1f}s  run finished "
                            f"status={data.get('status')}",
                            flush=True,
                        )
                        break

        # 3. Fetch final results. There is a race between
        # ``run.finished`` and result-status persistence (agent error
        # branch + evaluator commit both use short-lived sessions), so
        # briefly retry until all results are terminal.
        settle_deadline = time.time() + 30.0
        results: List[Dict[str, Any]] = []
        last_non_terminal_tick = 0
        while time.time() < settle_deadline:
            res_resp = test_client.get(
                f"/api/tests/runs/{run_id}/results", headers=headers,
            )
            assert res_resp.status_code == 200, res_resp.text
            results = res_resp.json()
            if results and all(r.get("status") in terminal_statuses for r in results):
                break
            # Surface every ~2s so the user can tell the harness is still
            # waiting for persistence rather than hung.
            last_non_terminal_tick += 1
            if last_non_terminal_tick % 4 == 0:
                non_terminal = [
                    (r.get("id", "?")[:8], r.get("status"))
                    for r in results if r.get("status") not in terminal_statuses
                ]
                print(
                    f"[eval] settle: waiting on {non_terminal}",
                    flush=True,
                )
            time.sleep(0.5)

        # 4. Trailing tool trace per result (best-effort).
        try:
            status_resp = test_client.get(
                f"/api/tests/runs/{run_id}/status", headers=headers,
            )
            if status_resp.status_code == 200:
                data = status_resp.json()
                for item in (data.get("results") or []):
                    rid = (item.get("result") or {}).get("id", "?")
                    tools: List[str] = []
                    for comp in (item.get("completions") or []):
                        for block in (comp.get("completion_blocks") or []):
                            te = block.get("tool_execution") or {}
                            name = te.get("tool_name")
                            if name:
                                tools.append(name)
                    if tools:
                        print(
                            f"[eval] result={str(rid)[:8]} "
                            f"tools={' → '.join(tools)}",
                            flush=True,
                        )
        except Exception:
            pass

        for r in results:
            assert r.get("status") in terminal_statuses, (
                f"result {r.get('id')} did not reach terminal state: "
                f"{r.get('status')}"
            )
        return results

    return _run
