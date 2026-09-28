#!/usr/bin/env python3
"""Install the artifact app persistence demo (P11) through the real HTTP API.

Creates a NEW page artifact in an existing report that reuses the source
dashboard's visualizations and adds three declared collections (notes,
selections, highlights), then seeds one owner highlight and one owner note.
No LLM, no direct database access.

Safety: this is meant for the migrated COPY of a dev database. Before any
write, in every mode, the report must be titled "... (demo copy)"; that
marker only exists in the copy, so seeing it through the API proves the
backend is bound to the copy.

Run it with the backend's venv so httpx is available:

    cd backend && uv run python ../tools/agent/seed_app_persistence_demo.py \\
        --report-title "Country Revenue by Genre (demo copy)" \\
        --source-artifact-title "Revenue by Country"

Credentials come ONLY from the environment (never flags, never files):
    BOW_DEMO_OWNER_EMAIL / BOW_DEMO_OWNER_PASSWORD   report owner (required)
    BOW_DEMO_USER_EMAIL  / BOW_DEMO_USER_PASSWORD    optional second user; when
                                                     set, one note is seeded as them

Flags:
    --base-url               frontend (proxies /api) or backend URL (default http://localhost:3000)
    --report-title           exact report title (must end with " (demo copy)")
    --source-artifact-title  exact title of the artifact whose visualizations are reused
    --check-binding          only prove the binding (read-only), then exit
    --dry-run                resolve everything, print the plan, write nothing
    --force-new              create a new demo artifact even if one exists (required when the
                             existing demo declares different storage; it is never mutated)
    --note-country           country of the seeded notes (default USA)

Exit codes: 0 ok, 2 usage/credentials, 3 binding check failed, 4 not found,
5 API error, 6 the existing demo declares different storage (rerun with --force-new). Prints a JSON summary on success (never credentials or tokens).
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable, Optional, Tuple

import httpx

DEMO_TITLE = "Revenue by Country — with notes"
COPY_MARKER = " (demo copy)"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "app_persistence_demo.jsx"
RUNTIME_VERSION = 11

STORAGE = {
    "collections": {
        "notes": {
            "scope": "shared",
            "create": "members",
            "modify": "author",
            "fields": {
                "country": {"type": "string", "required": True, "max_length": 100},
                "text": {"type": "string", "required": True, "max_length": 2000},
            },
        },
        "selections": {
            "scope": "per_user",
            "fields": {"genre": {"type": "string", "default": None}},
        },
        "highlights": {
            "scope": "shared",
            "create": "owner",
            "modify": "owner",
            # Published: public-link visitors read the owner's highlights.
            "public_read": True,
            "fields": {
                "text": {"type": "string", "required": True, "max_length": 280},
            },
        },
    }
}

SEED_HIGHLIGHT = "Notes are live: add context per country, it stays with this dashboard."
SEED_OWNER_NOTE = "Owner note: check the top market against last quarter."
SEED_USER_NOTE = "Second user note: customer count looks low for the revenue here."


class DemoError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Pure helpers (unit tested in backend/tests/unit/test_app_persistence_demo_fixture.py)
# ---------------------------------------------------------------------------

def is_demo_copy_title(title: Optional[str]) -> bool:
    """True only for a non-empty title that ends with the copy marker."""
    return isinstance(title, str) and title.endswith(COPY_MARKER) and len(title.strip()) > len(COPY_MARKER.strip())


def render_code(template: str, revenue_id: str, customers_id: str) -> str:
    """Put the source visualization ids into the fixture."""
    return template.replace("__REVENUE_VIZ_ID__", revenue_id).replace("__CUSTOMERS_VIZ_ID__", customers_id)


def pick_visualizations(vizzes: Iterable[dict]) -> Tuple[str, str]:
    """(revenue viz id, customers viz id): by title, else by order."""
    vizzes = [v for v in vizzes if v.get("id")]
    if not vizzes:
        return "", ""
    rev = next((v for v in vizzes if re.search(r"revenue|sales", str(v.get("title") or ""), re.I)), None)
    cus = next((v for v in vizzes if re.search(r"customer", str(v.get("title") or ""), re.I) and v is not rev), None)
    rest = [v for v in vizzes if v is not rev and v is not cus]
    rev = rev or (rest.pop(0) if rest else None)
    cus = cus or (rest.pop(0) if rest else None)
    return (str(rev["id"]) if rev else "", str(cus["id"]) if cus else "")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _check(r: httpx.Response, what: str) -> dict:
    if r.status_code >= 400:
        raise DemoError(5, f"{what} failed: {r.status_code} {r.text[:300]}")
    return r.json()


def login(client: httpx.Client, email: str, password: str) -> str:
    """Same flow as tools/agent/seed_org.py."""
    r = client.post("/api/auth/jwt/login", data={"username": email, "password": password})
    if r.status_code != 200:
        # Never echo the credentials.
        raise DemoError(2, f"login failed: {r.status_code}")
    return r.json()["access_token"]


def auth(token: str, org_id: Optional[str] = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    if org_id:
        headers["X-Organization-Id"] = org_id
    return headers


def find_report(client: httpx.Client, token: str, title: str) -> Tuple[str, dict]:
    """(org id, report) for the owner's report with exactly this title."""
    orgs = _check(client.get("/api/organizations", headers=auth(token)), "list organizations")
    for org in orgs:
        r = client.get(
            "/api/reports",
            params={"search": title, "limit": 100, "filter": "my"},
            headers=auth(token, org["id"]),
        )
        if r.status_code != 200:
            continue
        for rep in r.json().get("reports", []):
            if rep.get("title") == title:
                return org["id"], rep
    raise DemoError(4, f"no report titled {title!r} is visible to the owner")


def demo_action(existing: Optional[dict], force_new: bool) -> str:
    """'create', 'reuse', or 'declaration_differs' for the latest demo artifact
    (a full artifact with ``content``). An installed demo whose storage
    differs from STORAGE is never changed in place: the caller stops and asks
    for --force-new."""
    if existing is None or force_new:
        return "create"
    if (existing.get("content") or {}).get("storage") != STORAGE:
        return "declaration_differs"
    return "reuse"


def latest_by_title(artifacts: list, title: str) -> Optional[dict]:
    same = [a for a in artifacts if a.get("title") == title and a.get("status", "completed") == "completed"]
    if not same:
        return None
    return max(same, key=lambda a: (a.get("created_at") or "", a.get("version") or 0))


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(description="Install the artifact app persistence demo (API only).")
    p.add_argument("--base-url", default="http://localhost:3000")
    p.add_argument("--report-title", default="Country Revenue by Genre (demo copy)")
    p.add_argument("--source-artifact-title", default="Revenue by Country")
    p.add_argument("--check-binding", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force-new", action="store_true")
    p.add_argument("--note-country", default="USA")
    args = p.parse_args(argv)

    email = os.environ.get("BOW_DEMO_OWNER_EMAIL")
    password = os.environ.get("BOW_DEMO_OWNER_PASSWORD")
    if not email or not password:
        print("set BOW_DEMO_OWNER_EMAIL and BOW_DEMO_OWNER_PASSWORD", file=sys.stderr)
        return 2
    user_email = os.environ.get("BOW_DEMO_USER_EMAIL")
    user_password = os.environ.get("BOW_DEMO_USER_PASSWORD")

    base = args.base_url.rstrip("/")
    client = httpx.Client(base_url=base, timeout=60)
    try:
        token = login(client, email, password)
        org_id, report = find_report(client, token, args.report_title)
        report_id = report["id"]

        # Binding proof: first thing after the read, before any write, in every mode.
        if not is_demo_copy_title(report.get("title")):
            print(
                f"BINDING CHECK FAILED: report title {report.get('title')!r} does not end with {COPY_MARKER!r}; "
                "the backend is not on the demo copy. Nothing was written.",
                file=sys.stderr,
            )
            return 3
        print(f"binding ok: report {report_id} is titled {report['title']!r}", file=sys.stderr)
        if args.check_binding:
            print(json.dumps({"binding": "ok", "report_id": report_id, "organization_id": org_id}))
            return 0

        h = auth(token, org_id)
        artifacts = _check(client.get(f"/api/artifacts/report/{report_id}", headers=h), "list artifacts")
        source = latest_by_title(artifacts, args.source_artifact_title)
        if not source:
            raise DemoError(4, f"no artifact titled {args.source_artifact_title!r} in the report")
        source_full = _check(client.get(f"/api/artifacts/{source['id']}", headers=h), "get source artifact")
        viz_ids = [str(v) for v in (source_full.get("content") or {}).get("visualization_ids") or []]
        vizzes = []
        for vid in viz_ids:
            r = client.get(f"/api/visualizations/{vid}", headers=h)
            vizzes.append({"id": vid, "title": r.json().get("title") if r.status_code == 200 else None})
        revenue_id, customers_id = pick_visualizations(vizzes)
        code = render_code(FIXTURE.read_text(encoding="utf-8"), revenue_id, customers_id)

        latest = None if args.force_new else latest_by_title(artifacts, DEMO_TITLE)
        existing = (
            _check(client.get(f"/api/artifacts/{latest['id']}", headers=h), "get demo artifact") if latest else None
        )
        action = demo_action(existing, args.force_new)
        if action == "declaration_differs":
            print(
                f"the existing demo artifact {existing.get('artifact_id')} declares different storage than this "
                "script (for example highlights.public_read); it is left unchanged. Rerun with --force-new to "
                "install a new demo artifact.",
                file=sys.stderr,
            )
            return 6
        plan = {
            "report_id": report_id,
            "source_artifact": {"version_id": source["id"], "artifact_id": source.get("artifact_id")},
            "visualizations": vizzes,
            "revenue_viz_id": revenue_id,
            "customers_viz_id": customers_id,
            "action": action,
        }
        if args.dry_run:
            print(json.dumps({"dry_run": True, **plan}, indent=2))
            return 0

        if action == "reuse":
            demo = existing
        else:
            demo = _check(
                client.post(
                    "/api/artifacts",
                    json={
                        "report_id": report_id,
                        "title": DEMO_TITLE,
                        "mode": "page",
                        "content": {
                            "code": code,
                            "visualization_ids": viz_ids,
                            "runtime_version": RUNTIME_VERSION,
                            "storage": STORAGE,
                        },
                    },
                    headers=h,
                ),
                "create demo artifact",
            )
        artifact_id = demo["artifact_id"]
        data_url = f"/api/artifacts/{artifact_id}/data"

        def ensure(headers: dict, collection: str, data: dict, match: str) -> str:
            items = _check(client.get(f"{data_url}/{collection}", headers=headers), f"list {collection}")["items"]
            if any(i.get("data", {}).get("text") == match for i in items):
                return "exists"
            _check(client.post(f"{data_url}/{collection}", json={"data": data}, headers=headers), f"seed {collection}")
            return "created"

        seeded = {
            "highlight": ensure(h, "highlights", {"text": SEED_HIGHLIGHT}, SEED_HIGHLIGHT),
            "owner_note": ensure(h, "notes", {"country": args.note_country, "text": SEED_OWNER_NOTE}, SEED_OWNER_NOTE),
        }
        if user_email and user_password:
            user_h = auth(login(client, user_email, user_password))
            try:
                seeded["user_note"] = ensure(
                    user_h, "notes", {"country": args.note_country, "text": SEED_USER_NOTE}, SEED_USER_NOTE,
                )
            except DemoError as exc:
                # Usually the report's artifact visibility does not include the
                # second user yet; the demo still works for the owner.
                seeded["user_note"] = f"skipped: {exc}"

        print(json.dumps({
            **plan,
            "action": "reused" if action == "reuse" else "created",
            "artifact_id": artifact_id,
            "version_id": demo["id"],
            "seeded": seeded,
            "urls": {"report": f"{base}/reports/{report_id}", "public": f"{base}/r/{report_id}"},
        }, indent=2))
        print(f"demo artifact_id={artifact_id}", file=sys.stderr)
        return 0
    except DemoError as exc:
        print(str(exc), file=sys.stderr)
        return exc.code
    except httpx.HTTPError as exc:
        print(f"cannot reach {base}: {exc.__class__.__name__}", file=sys.stderr)
        return 5
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
