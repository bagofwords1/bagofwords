#!/usr/bin/env python3
"""Seed the running sandbox for the embedded-chat feedback loop — through the
real API only (plus the sqlite read of invite tokens that seed_org.py does).

Creates, idempotently where it can:
  * two mapped members (alice E-1001, bob E-1002) and one unmapped member
    (carol) in the admin's org, with the ``employee_id`` member attribute;
  * MCP enabled + an "ERP orders" agent on the local echo MCP server with
    identity forwarding (locked ``employee_id`` from the member attribute,
    dynamic ``workflow_id`` / ``erp_user`` from request metadata, an
    ``X-Employee-Id`` header), made public so members can use it;
  * the stub LLM as the org default model;
  * one embed for the ERP host, with the private key written to
    ``--key-file`` and a JSON summary to ``--out``.

Run after tools/agent/boot_stack.sh + seed_org.py:
  cd backend && uv run python ../tools/agent/embed/seed_embed.py
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys

import httpx

HERE = pathlib.Path(__file__).resolve()
ROOT = HERE.parents[3]

MEMBERS = {
    "alice@erp.example": ("Alice Nguyen", {"employee_id": "E-1001"}),
    "bob@erp.example": ("Bob Okafor", {"employee_id": "E-1002"}),
    "carol@erp.example": ("Carol Diaz", None),  # registered member, no mapping
}


def invite_token(db_path: str, email: str):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT invite_token FROM memberships WHERE email = ? AND user_id IS NULL "
                           "ORDER BY created_at DESC LIMIT 1", (email,)).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument("--email", default="admin@example.com")
    p.add_argument("--password", default="Password123!")
    p.add_argument("--echo-url", default="http://127.0.0.1:3333/mcp")
    p.add_argument("--stub-url", default="http://127.0.0.1:9099/v1")
    p.add_argument("--host-origins", default="http://127.0.0.1:4173,http://localhost:4173")
    p.add_argument("--issuer", default="erp-sandbox")
    p.add_argument("--db-path", default=str(ROOT / "backend" / "db" / "agent.db"))
    p.add_argument("--key-file", default="/tmp/bow-agent/embed-private.pem")
    p.add_argument("--out", default="/tmp/bow-agent/embed.json")
    args = p.parse_args()

    c = httpx.Client(base_url=args.base_url, timeout=60, trust_env=False)

    def must(r, what):
        if r.status_code >= 300:
            sys.exit(f"{what} failed: {r.status_code} {r.text[:400]}")
        return r.json() if r.content else None

    tok = must(c.post("/api/auth/jwt/login", data={"username": args.email, "password": args.password}), "admin login")["access_token"]
    org = must(c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}), "orgs")[0]["id"]
    H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}
    summary = {"org_id": org, "members": {}}

    # ── members ───────────────────────────────────────────────────────────
    def by_email():
        rows = must(c.get(f"/api/organizations/{org}/members", headers=H), "members")
        return {(m.get("email") or (m.get("user") or {}).get("email")): m for m in rows}

    existing = by_email()
    for email, (name, attrs) in MEMBERS.items():
        m = existing.get(email)
        if not m:
            must(c.post(f"/api/organizations/{org}/members", json={"organization_id": org, "email": email, "role": "member"}, headers=H), f"invite {email}")
            token = invite_token(args.db_path, email)
            r = c.post("/api/auth/register", json={"name": name, "email": email, "password": "Password123!", "invite_token": token})
            if r.status_code not in (201, 400):
                sys.exit(f"register {email}: {r.status_code} {r.text[:300]}")
            existing = by_email()
            m = existing[email]
        if attrs is not None:
            must(c.put(f"/api/organizations/{org}/members/{m['id']}", json={"custom_attributes": attrs}, headers=H), f"attributes {email}")
        summary["members"][email] = {"membership_id": m["id"], "attributes": attrs, "password": "Password123!"}

    # ── MCP agent on the echo server ──────────────────────────────────────
    for key in ("mcp_enabled", "enable_mcp_tools"):
        c.put("/api/organization/settings", json={"config": {key: {"value": True}}}, headers=H)
    agents = must(c.get("/api/data_sources?show_all=true", headers=H), "data sources")
    ds = next((a for a in agents if a["name"] == "ERP orders"), None)
    mcp_config = {
        "server_url": args.echo_url,
        "transport": "streamable_http",
        "header_injection": [{"header": "X-Employee-Id", "source": "membership.attr:employee_id"}],
        "metadata_injection": {
            "argument_key": "custom_metadata",
            "fields": [
                {"name": "employee_id", "source": "membership.attr:employee_id", "mode": "locked", "on_missing": "block"},
                {"name": "user_email", "source": "user.email", "mode": "locked"},
            ],
            "dynamic_params_enabled": True,
            "dynamic_params": {"workflow_id": None, "erp_user": None},
        },
    }
    if ds is None:
        ds = must(c.post("/api/data_sources", json={
            "name": "ERP orders", "type": "mcp", "config": mcp_config, "credentials": {}, "auth_policy": "system_only",
            "generate_summary": False, "generate_conversation_starters": False, "generate_ai_rules": False,
        }, headers=H), "create mcp data source")
        ds = must(c.get(f"/api/data_sources/{ds['id']}", headers=H), "read data source")
    conn_id = next(x["id"] for x in ds["connections"])
    c.put(f"/api/connections/{conn_id}", json={"config": mcp_config}, headers=H)
    must(c.post(f"/api/connections/{conn_id}/refresh-tools", headers=H), "refresh tools")
    must(c.put(f"/api/data_sources/{ds['id']}", json={"is_public": True}, headers=H), "publish agent")
    summary["agent"] = {"id": ds["id"], "connection_id": conn_id}

    # ── stub LLM as default ───────────────────────────────────────────────
    models = must(c.get("/api/llm/models", headers=H), "models")
    stub = [m for m in models if m.get("model_id") == "stub"]
    if not stub:
        must(c.post("/api/llm/providers", json={
            "name": "Stub", "provider_type": "custom",
            "credentials": {"base_url": args.stub_url, "api_key": "sk-stub"},
            "models": [{"model_id": "stub", "name": "Stub", "is_custom": True, "is_enabled": True}],
        }, headers=H), "stub provider")
        stub = [m for m in must(c.get("/api/llm/models", headers=H), "models") if m.get("model_id") == "stub"]
    for m in stub:
        c.post(f"/api/llm/models/{m['id']}/set_default", headers=H)
        c.post(f"/api/llm/models/{m['id']}/set_default", params={"small": True}, headers=H)
    summary["model_id"] = stub[0]["id"]

    # ── the embed ─────────────────────────────────────────────────────────
    embeds = must(c.get("/api/embeds", headers=H), "embeds")
    runtime = {"data_source_ids": [ds["id"]], "model_id": stub[0]["id"], "tools": {"web_search": False}, "follow_ups": False}
    branding = {
        "display_name": "Northwind Assistant", "accent_color": "#0f766e", "theme": "auto",
        "greeting": "Hi! Ask me about production orders, incidents and inventory — I only see what you're allowed to see.",
        "suggestions": ["What are my open production orders?", "Summarize this incident", "Which lines are behind schedule?"],
    }
    current = next((e for e in embeds if e["name"] == "Northwind ERP"), None)
    if current is None:
        created = must(c.post("/api/embeds", json={
            "name": "Northwind ERP", "allowed_origins": args.host_origins.split(","), "issuer": args.issuer,
            "subject_attribute": "employee_id", "runtime": runtime, "branding": branding,
        }, headers=H), "create embed")
        private_key = created["private_key"]
    else:
        created = must(c.put(f"/api/embeds/{current['id']}", json={
            "revision": current["revision"], "allowed_origins": args.host_origins.split(","), "issuer": args.issuer,
            "subject_attribute": "employee_id", "runtime": runtime, "branding": branding, "enabled": True,
        }, headers=H), "update embed")
        rotated = must(c.post(f"/api/embeds/{created['id']}/rotate-key", headers=H), "rotate key")
        private_key = rotated["private_key"]
    # Fresh threads for a fresh loop: retire the employees' previous embed
    # conversations (sandbox only — there is deliberately no API for this).
    conn = sqlite3.connect(args.db_path)
    try:
        conn.execute("UPDATE reports SET deleted_at = CURRENT_TIMESTAMP WHERE embed_id = ? AND deleted_at IS NULL",
                     (created["id"],))
        conn.commit()
    finally:
        conn.close()
    pathlib.Path(args.key_file).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(args.key_file).write_text(private_key)
    summary["embed"] = {"id": created["id"], "public_id": created["public_id"], "issuer": args.issuer,
                        "origins": args.host_origins.split(","), "key_file": args.key_file}
    summary["admin"] = {"email": args.email, "password": args.password}
    pathlib.Path(args.out).write_text(json.dumps(summary, indent=2))
    print(json.dumps({k: v for k, v in summary.items() if k != "members"} | {"members": list(summary["members"])}, indent=2))


if __name__ == "__main__":
    main()
