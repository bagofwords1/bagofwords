#!/usr/bin/env python3
"""Measure the planner's schema context for wide agents.

For each named agent (data source) this builds the schema context exactly the
way the planner does (``SchemaContextBuilder.build()`` with stats, then
``render_combined(top_k_per_ds=<org top_k_schema>, index_limit=1000)``), and
reports:

* characters, tokens (Anthropic ``count_tokens`` on the real model, when
  ANTHROPIC_KEY_FILE is set; otherwise chars/4 is reported as an estimate),
  number of ``<column`` entries rendered, per-table "N of M" when available;
* build and render wall time (median of --reps cold builds), and the
  end-to-end planner preparation time (``ContextHub.prime_static`` +
  ``refresh_warm`` + the planner's schema render, schema cache cleared);
* an excerpt of the widest table's rendered block, saved under --out (the
  full rendered context only with --full-out, it runs to ~500 KB).

Runs against the stack's database (same env as boot_stack.sh):

    cd backend && TESTING=true ENVIRONMENT=production TEST_DATABASE_URL=sqlite:///db/agent.db \
      uv run python ../tools/agent/wide_context_probe.py --label before --out ../docs/feedback-loops/wide-table-columns

Works on code with or without the column cap (new attributes are read with getattr).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

DEFAULT_AGENTS = ["Wide Postgres", "Wide DuckDB", "Wide Elastic"]


def count_tokens(text: str) -> tuple[int, str]:
    key_file = os.environ.get("ANTHROPIC_KEY_FILE")
    if not key_file or not Path(key_file).exists():
        return len(text) // 4, "estimate(chars/4)"
    import httpx

    key = Path(key_file).read_text().strip()
    r = httpx.post(
        "https://api.anthropic.com/v1/messages/count_tokens",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": os.environ.get("TOKEN_MODEL", "claude-haiku-5-5"),
              "messages": [{"role": "user", "content": text or " "}]},
        timeout=120,
    )
    if r.status_code != 200:
        return len(text) // 4, f"estimate(count_tokens {r.status_code})"
    return int(r.json()["input_tokens"]), "anthropic.count_tokens(claude-haiku-5-5)"


def widest_table_block(xml: str) -> tuple[str, str]:
    """Return (table name, its <table ...>...</table> block) for the table with
    the most rendered <column> entries."""
    best = ("", "", -1)
    for m in re.finditer(r"<table\b[^>]*name=\"([^\"]+)\"[^>]*>(.*?)</table>", xml, re.S):
        n = m.group(2).count("<column ")
        if n > best[2]:
            best = (m.group(1), m.group(0), n)
    return best[0], best[1]


def excerpt(block: str, head: int = 25, tail: int = 25) -> str:
    lines = block.splitlines()
    if len(lines) <= head + tail + 1:
        return block
    return "\n".join(lines[:head] + [f"... [{len(lines) - head - tail} lines elided by the probe] ..."] + lines[-tail:])


async def run(args) -> dict:
    sys.path.insert(0, os.getcwd())
    import main  # noqa: F401  full model registry, like the server boot
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.user import User
    from app.models.data_source import DataSource
    from app.ai.context.builders.schema_context_builder import SchemaContextBuilder
    from app.services.organization_settings_service import OrganizationSettingsService
    from app.ai.context import context_hub as hub_mod

    results: dict = {"label": args.label, "agents": {}}
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    async with async_session_maker() as db:
        org = (await db.execute(select(Organization))).scalars().first()
        user = (await db.execute(select(User).where(User.email == args.email))).scalars().first()
        settings = await OrganizationSettingsService().get_settings(db, org, user)
        top_k = int(settings.get_config("top_k_schema").value)
        results["top_k_schema"] = top_k
        for name in args.agents:
            ds = (await db.execute(
                select(DataSource).options(selectinload(DataSource.connections)).where(DataSource.name == name)
            )).scalars().first()
            if ds is None:
                print(f"skip {name}: not found")
                continue
            build_ms, render_ms = [], []
            xml = ""
            ctx = None
            for _ in range(args.reps):
                b = SchemaContextBuilder(db, [ds], org, None, user=user, organization_settings=settings)
                t0 = time.perf_counter()
                ctx = await b.build()
                t1 = time.perf_counter()
                xml = ctx.render_combined(top_k_per_ds=top_k, index_limit=1000)
                t2 = time.perf_counter()
                build_ms.append((t1 - t0) * 1000)
                render_ms.append((t2 - t1) * 1000)
            # End-to-end planner preparation: a fresh ContextHub, cold schema cache.
            prep_ms = []
            for _ in range(args.reps):
                hub_mod._SCHEMA_CACHE.clear()
                hub = hub_mod.ContextHub(db=db, organization=org, report=None, data_sources=[ds], user=user,
                                         organization_settings=settings)
                t0 = time.perf_counter()
                await hub.prime_static(args.prompt)
                await hub.refresh_warm()
                view = hub.get_view()
                _ = view.static.schemas.render_combined(top_k_per_ds=top_k, index_limit=1000)
                prep_ms.append((time.perf_counter() - t0) * 1000)

            tables = ctx.data_sources[0].tables if ctx and ctx.data_sources else []
            widest_total = max((len(t.columns or []) for t in tables), default=0)
            snap = ctx.get_usage_snapshot(top_k_per_ds=top_k).model_dump() if ctx else {}
            tokens, tok_method = count_tokens(xml)
            tname, tblock = widest_table_block(xml)
            safe = name.lower().replace(" ", "_")
            if args.full_out:
                full_dir = Path(args.full_out)
                full_dir.mkdir(parents=True, exist_ok=True)
                (full_dir / f"{args.label}_{safe}_context.xml").write_text(xml)
            (out_dir / f"{args.label}_{safe}_excerpt.xml").write_text(excerpt(tblock))
            used = (snap.get("data_sources") or [{}])[0].get("tables_used", [])
            results["agents"][name] = {
                "tables_total": len(tables),
                "widest_table_columns_in_catalog": widest_total,
                "context_chars": len(xml),
                "context_tokens": tokens,
                "token_method": tok_method,
                "columns_rendered": xml.count("<column "),
                "widest_rendered_table": tname,
                "widest_rendered_table_columns": tblock.count("<column "),
                "build_ms_median": round(statistics.median(build_ms), 1),
                "render_ms_median": round(statistics.median(render_ms), 1),
                "planner_prep_ms_median": round(statistics.median(prep_ms), 1),
                "snapshot_top_tables": [
                    {k: u.get(k) for k in ("name", "columns_count", "columns_total", "columns_shown",
                                           "column_selection_reason", "selection_reason") if k in u}
                    for u in used[:5]
                ],
            }
            print(json.dumps({name: results["agents"][name]}, indent=2))
    (out_dir / f"{args.label}_summary.json").write_text(json.dumps(results, indent=2))
    return results


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--agents", nargs="*", default=DEFAULT_AGENTS)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--full-out", default="", help="also write each full rendered context here (large; keep out of the repo)")
    ap.add_argument("--email", default=os.environ.get("BOW_ADMIN_EMAIL", "admin@example.com"))
    ap.add_argument("--prompt", default="Which hosts had the highest network retransmit rate last week?")
    asyncio.run(run(ap.parse_args()))
