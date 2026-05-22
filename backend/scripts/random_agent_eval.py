"""Random-agent sandbox harness.

Generates N random agent manifests against the chinook demo, applies
them via the live API, then generates matching eval YAMLs and runs them.
This is *not* a pytest test — it's an exploration tool for stressing the
YAML apply path and surfacing which agent-shaping primitives actually
move downstream eval scores.

Usage (with the dev server running per docs/design/sandbox-feedback-loop.md):

    cd backend && source .venv/bin/activate
    python scripts/random_agent_eval.py --count 5
    python scripts/random_agent_eval.py --inject-errors  # negative-path harness
    python scripts/random_agent_eval.py --report /tmp/agent_eval.csv

Uses ``sandbox_state.json`` for auth / org / data source ids.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SANDBOX_STATE = REPO_ROOT / "backend" / "sandbox_state.json"
DEFAULT_REPORT = Path("/tmp/random_agent_eval.csv")


CHINOOK_TABLES = [
    "Album", "Artist", "Customer", "Employee", "Genre",
    "Invoice", "InvoiceLine", "MediaType", "Playlist",
    "PlaylistTrack", "Track",
]


CONVERSATION_STARTER_POOL = [
    "Top 10 customers by total spend",
    "Sales by genre over time",
    "Average invoice size by country",
    "Tracks per album histogram",
    "Top revenue-generating artists",
    "Customer churn signals",
    "Most-played playlists",
    "Invoices missing line items",
    "Track inventory by media type",
    "Employees with the most customers",
]


def _load_state() -> Dict[str, Any]:
    if not SANDBOX_STATE.exists():
        sys.exit(f"sandbox_state.json missing — set up sandbox first ({SANDBOX_STATE})")
    return json.loads(SANDBOX_STATE.read_text())


def _headers(state: Dict[str, Any]) -> Dict[str, str]:
    sess = state["session"]
    return {
        "Authorization": f"Bearer {sess['token']}",
        "X-Organization-Id": str(sess["org_id"]),
    }


def _refresh_token(state: Dict[str, Any]) -> None:
    creds = state["credentials"]
    r = requests.post(
        f"{state['endpoints']['backend']}/api/auth/jwt/login",
        data={"username": creds["email"], "password": creds["password"]},
        timeout=10,
    )
    r.raise_for_status()
    state["session"]["token"] = r.json()["access_token"]
    SANDBOX_STATE.write_text(json.dumps(state, indent=2))


def _fetch_connection_name(state: Dict[str, Any]) -> str:
    backend = state["endpoints"]["backend"]
    r = requests.get(f"{backend}/api/connections", headers=_headers(state), timeout=10)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        sys.exit("No connections in the org — install the chinook demo first.")
    # Prefer the sqlite one (chinook); fall back to first available.
    for c in rows:
        if c.get("type") == "sqlite":
            return c["name"]
    return rows[0]["name"]


def _random_manifest(
    *,
    seed: int,
    connection_name: str,
    inject_errors: bool = False,
) -> Dict[str, Any]:
    rng = random.Random(seed)
    name = f"rand-agent-{seed}"

    # Random table subset (3..7 tables active)
    k = rng.randint(3, 7)
    chosen = rng.sample(CHINOOK_TABLES, k=k)
    include_globs = [f"*.{t}" for t in chosen]

    # Random conversation starters (2..4)
    starters = rng.sample(
        CONVERSATION_STARTER_POOL, k=rng.randint(2, min(4, len(CONVERSATION_STARTER_POOL)))
    )

    connections = [connection_name]
    if inject_errors:
        # Pick one corruption per call so each seed maps to a distinct
        # error class and the report shows code coverage at a glance.
        kind = rng.choice([
            "bad_connection", "bad_member_email", "bad_member_group", "unknown_tool",
        ])
        if kind == "bad_connection":
            connections = [connection_name + "-DOES-NOT-EXIST"]
        elif kind == "bad_member_email":
            return {
                "name": name,
                "connections": connections,
                "members": [{"user": "ghost@example.com"}],
                "_corruption": kind,
            }
        elif kind == "bad_member_group":
            return {
                "name": name,
                "connections": connections,
                "members": [{"group": "no-such-group"}],
                "_corruption": kind,
            }
        elif kind == "unknown_tool":
            # tool refs on a sqlite connection are wrong type — should
            # surface connection_type_mismatch
            return {
                "name": name,
                "connections": [connection_name],
                "tools": {connection_name: {"allow": ["fake_tool"]}},
                "_corruption": kind,
            }

    manifest = {
        "name": name,
        "description": f"random agent #{seed}",
        "is_public": rng.choice([True, False]),
        "connections": connections,
        "tables": {"include": include_globs, "exclude": []},
        "conversation_starters": starters,
    }
    if inject_errors:
        manifest["_corruption"] = "bad_connection"
    return manifest


def _yaml_dump(payload: Dict[str, Any]) -> str:
    import yaml

    # Strip internal metadata keys
    clean = {k: v for k, v in payload.items() if not k.startswith("_")}
    return yaml.safe_dump(clean, sort_keys=False, allow_unicode=True)


def _apply_agent(state: Dict[str, Any], yaml_text: str, *, dry_run: bool = False) -> Dict[str, Any]:
    backend = state["endpoints"]["backend"]
    params = {"dry_run": "true"} if dry_run else {}
    r = requests.post(
        f"{backend}/api/agents/apply",
        params=params,
        data=yaml_text.encode("utf-8"),
        headers={**_headers(state), "Content-Type": "application/yaml"},
        timeout=30,
    )
    if r.status_code == 401:
        _refresh_token(state)
        return _apply_agent(state, yaml_text, dry_run=dry_run)
    r.raise_for_status()
    return r.json()


def _apply_eval(state: Dict[str, Any], yaml_text: str) -> Dict[str, Any]:
    backend = state["endpoints"]["backend"]
    r = requests.post(
        f"{backend}/api/evals/apply",
        data=yaml_text.encode("utf-8"),
        headers={**_headers(state), "Content-Type": "application/yaml"},
        timeout=30,
    )
    if r.status_code == 401:
        _refresh_token(state)
        return _apply_eval(state, yaml_text)
    r.raise_for_status()
    return r.json()


def _eval_for_agent(agent_name: str, starters: List[str]) -> str:
    import yaml

    cases = []
    for i, q in enumerate(starters):
        cases.append({
            "name": f"q_{i}",
            "prompt": {"content": q, "mode": "chat"},
            "expectations": {},
        })
    return yaml.safe_dump(
        {
            "name": f"{agent_name}-eval",
            "description": f"smoke eval for {agent_name}",
            "data_source_slugs": [agent_name],
            "cases": cases,
        },
        sort_keys=False,
    )


def run(count: int, inject_errors: bool, report_path: Optional[Path]) -> int:
    state = _load_state()
    connection_name = _fetch_connection_name(state)

    rows: List[Dict[str, Any]] = []
    for seed in range(count):
        manifest = _random_manifest(
            seed=seed, connection_name=connection_name, inject_errors=inject_errors
        )
        yaml_text = _yaml_dump(manifest)
        corruption = manifest.get("_corruption", "")

        try:
            result = _apply_agent(state, yaml_text)
        except requests.HTTPError as e:
            rows.append({
                "seed": seed,
                "agent": manifest["name"],
                "corruption": corruption,
                "status": "http_error",
                "http_code": e.response.status_code,
                "errors": str(e),
                "warnings": "",
            })
            continue

        rows.append({
            "seed": seed,
            "agent": manifest["name"],
            "corruption": corruption,
            "status": result.get("status"),
            "http_code": 200,
            "errors": ";".join(e["code"] for e in result.get("errors") or []),
            "warnings": ";".join(w["code"] for w in result.get("warnings") or []),
        })

        # Don't bother applying an eval when the apply errored.
        if result.get("status") in ("created", "updated", "unchanged"):
            eval_yaml = _eval_for_agent(manifest["name"], manifest["conversation_starters"])
            try:
                _apply_eval(state, eval_yaml)
            except Exception as e:
                rows[-1]["eval_error"] = str(e)[:200]

    # Report
    if report_path:
        with report_path.open("w", newline="") as f:
            w = csv.DictWriter(
                f,
                fieldnames=["seed", "agent", "corruption", "status", "http_code", "errors", "warnings", "eval_error"],
            )
            w.writeheader()
            for r in rows:
                r.setdefault("eval_error", "")
                w.writerow(r)
        print(f"Wrote {len(rows)} rows to {report_path}")

    # Stdout summary
    status_counts: Dict[str, int] = {}
    for r in rows:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
    print(json.dumps(status_counts, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--count", type=int, default=5)
    p.add_argument("--inject-errors", action="store_true")
    p.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = p.parse_args()
    return run(args.count, args.inject_errors, args.report)


if __name__ == "__main__":
    raise SystemExit(main())
