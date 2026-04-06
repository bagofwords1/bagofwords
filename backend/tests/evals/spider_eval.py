"""
Spider text-to-SQL eval driver for the BoW Completion API.

Pipeline (per question):
  1. POST /api/reports                           -> create a fresh report bound to the data source
  2. POST /api/reports/{id}/completions          -> ask the question
  3. Walk completion_blocks, take the LAST successful create_data tool_execution
     (handles agent self-corrections — earlier attempts may have failed/been revised)
  4. Pull predicted SQL from result_json.executed_queries[-1]
  5. Run gold SQL against the local SQLite file, compare result sets (execution match)
  6. Append a JSONL row with report_id / report_url / step_id for traceability

Assumptions:
  - Each Spider db_id is already registered as a BoW data source whose `name` == db_id.
  - Email/password auth (mirrors backend/bow-eval.py).
  - Default = report-per-question for clean-room eval (no context bleed, no
    in-report instruction accumulation). Use --report-per-db to share reports.

Usage:
  python backend/tests/evals/spider_eval.py \
      --bow-url http://localhost:8000 \
      --email admin@email.com --password password \
      --spider-dir backend/tests/evals/spider \
      --limit 50 \
      --out backend/tests/evals/spider_results.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("spider_eval")

# Optional fallback download URL (HuggingFace mirror of Spider). Override with --download-url.
DEFAULT_SPIDER_ZIP_URL = "https://huggingface.co/datasets/xlangai/spider/resolve/main/spider.zip"


# ---------------------------------------------------------------------------
# BoW API client
# ---------------------------------------------------------------------------
class BowClient:
    def __init__(self, base_url: str, email: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.s = requests.Session()
        self._login(email, password)
        self.org_id = self._first_org()

    def _login(self, email: str, password: str) -> None:
        r = self.s.post(
            f"{self.base_url}/api/auth/jwt/login",
            data={"username": email, "password": password},
            timeout=30,
        )
        r.raise_for_status()
        self.s.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

    def _first_org(self) -> str:
        r = self.s.get(f"{self.base_url}/api/organizations", timeout=30)
        r.raise_for_status()
        org_id = r.json()[0]["id"]
        self.s.headers["X-Organization-Id"] = str(org_id)
        return org_id

    def list_data_sources(self) -> list[dict]:
        r = self.s.get(f"{self.base_url}/api/data_sources", timeout=30)
        r.raise_for_status()
        return r.json()

    def create_report(self, title: str, data_source_id: str) -> dict:
        r = self.s.post(
            f"{self.base_url}/api/reports",
            json={"title": title, "widget": None, "files": [], "data_sources": [data_source_id]},
            timeout=60,
        )
        r.raise_for_status()
        return r.json()

    def create_completion(self, report_id: str, question: str, data_source_id: str) -> list[dict]:
        payload = {
            "prompt": {
                "content": question,
                "widget_id": None,
                "step_id": None,
                "mentions": [
                    {"name": "DATA SOURCES", "items": [{"id": data_source_id, "name": ""}]},
                    {"name": "TABLES", "items": []},
                    {"name": "FILES", "items": []},
                    {"name": "ENTITIES", "items": []},
                ],
                "mode": "chat",
            }
        }
        r = self.s.post(
            f"{self.base_url}/api/reports/{report_id}/completions",
            json=payload,
            params={"background": False},
            timeout=600,
        )
        r.raise_for_status()
        body = r.json()
        # Endpoint may return either a list or a {completions: [...]} object.
        if isinstance(body, dict) and "completions" in body:
            return body["completions"]
        return body


# ---------------------------------------------------------------------------
# Spider data loading
# ---------------------------------------------------------------------------
def ensure_spider_data(spider_dir: Path, download_url: str | None) -> None:
    questions_file = spider_dir / "spider.json"
    db_dir = spider_dir / "database"
    if questions_file.exists() and db_dir.exists():
        return
    if not download_url:
        sys.exit(
            f"Spider data missing. Expected {questions_file} and {db_dir}/.\n"
            f"Either populate the directory manually (see {spider_dir}/README.md) "
            f"or pass --download-url."
        )
    spider_dir.mkdir(parents=True, exist_ok=True)
    zip_path = spider_dir / "spider.zip"
    log.info("Downloading Spider from %s", download_url)
    urllib.request.urlretrieve(download_url, zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(spider_dir)
    # Many releases nest under spider/. Flatten.
    nested = spider_dir / "spider"
    if nested.exists():
        for item in nested.iterdir():
            item.rename(spider_dir / item.name)
        nested.rmdir()
    if not questions_file.exists():
        for cand in ("dev.json", "train_spider.json"):
            if (spider_dir / cand).exists():
                (spider_dir / cand).rename(questions_file)
                break


def load_questions(spider_dir: Path, limit: int | None) -> list[dict]:
    qs = json.loads((spider_dir / "spider.json").read_text())
    if limit:
        qs = qs[:limit]
    return qs


def sqlite_path(spider_dir: Path, db_id: str) -> Path:
    return spider_dir / "database" / db_id / f"{db_id}.sqlite"


# ---------------------------------------------------------------------------
# SQL execution + comparison
# ---------------------------------------------------------------------------
def run_sql(db_path: Path, sql: str) -> tuple[list[tuple] | None, str | None]:
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.text_factory = lambda b: b.decode("utf-8", errors="replace")
        try:
            cur = con.execute(sql)
            return cur.fetchall(), None
        finally:
            con.close()
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def exec_match(pred_rows: list[tuple], gold_rows: list[tuple], gold_sql: str) -> bool:
    if pred_rows is None or gold_rows is None:
        return False
    # Order matters only if gold has ORDER BY.
    if "order by" in gold_sql.lower():
        return pred_rows == gold_rows
    return sorted(map(repr, pred_rows)) == sorted(map(repr, gold_rows))


# ---------------------------------------------------------------------------
# Completion-blocks extraction
# ---------------------------------------------------------------------------
def extract_prediction(completions: list[dict]) -> dict:
    """
    Walk all system completions; return the LAST successful create_data
    tool_execution. Later blocks reflect agent self-corrections.
    """
    found: dict | None = None
    for comp in completions:
        for block in comp.get("completion_blocks") or []:
            te = block.get("tool_execution") or {}
            if te.get("tool_name") != "create_data" or not te.get("success"):
                continue
            rj = te.get("result_json") or {}
            sqls = rj.get("executed_queries") or []
            if not sqls:
                continue
            data = (rj.get("data") or {})
            rows = [tuple(row.values()) for row in (data.get("rows") or [])]
            found = {
                "pred_sql": sqls[-1].strip(),
                "pred_rows": rows,
                "step_id": te.get("created_step_id"),
                "tool_execution_id": te.get("id"),
            }
    return found or {"pred_sql": None, "pred_rows": None, "step_id": None, "tool_execution_id": None}


# ---------------------------------------------------------------------------
# Main eval loop
# ---------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bow-url", default="http://localhost:8000")
    ap.add_argument("--email", default=os.environ.get("BOW_EMAIL", "admin@email.com"))
    ap.add_argument("--password", default=os.environ.get("BOW_PASSWORD", "password"))
    ap.add_argument("--spider-dir", default="backend/tests/evals/spider", type=Path)
    ap.add_argument("--limit", type=int, default=None, help="Max questions to run")
    ap.add_argument("--out", default="backend/tests/evals/spider_results.jsonl", type=Path)
    ap.add_argument("--report-per-db", action="store_true",
                    help="Reuse one report per db_id (faster, but context can bleed)")
    ap.add_argument("--download-url", default=None,
                    help=f"Spider zip URL (e.g. {DEFAULT_SPIDER_ZIP_URL})")
    args = ap.parse_args()

    ensure_spider_data(args.spider_dir, args.download_url)
    questions = load_questions(args.spider_dir, args.limit)
    log.info("Loaded %d questions", len(questions))

    client = BowClient(args.bow_url, args.email, args.password)

    # db_id -> data_source_id (exact name match)
    ds_by_name = {ds["name"]: ds["id"] for ds in client.list_data_sources()}
    log.info("Found %d data sources", len(ds_by_name))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    out_f = args.out.open("a", encoding="utf-8")

    db_to_report: dict[str, str] = {}
    correct = 0
    attempted = 0
    skipped = 0

    for i, q in enumerate(questions):
        db_id = q["db_id"]
        question = q["question"]
        gold_sql = q["query"]

        ds_id = ds_by_name.get(db_id)
        if not ds_id:
            log.warning("[%d] skip — no data source named %r", i, db_id)
            skipped += 1
            continue

        db_path = sqlite_path(args.spider_dir, db_id)
        if not db_path.exists():
            log.warning("[%d] skip — sqlite missing at %s", i, db_path)
            skipped += 1
            continue

        # Reuse or create report.
        if args.report_per_db and db_id in db_to_report:
            report_id = db_to_report[db_id]
        else:
            report = client.create_report(title=f"spider:{db_id}:{i}", data_source_id=ds_id)
            report_id = report["id"]
            if args.report_per_db:
                db_to_report[db_id] = report_id

        report_url = f"{args.bow_url}/reports/{report_id}"
        t0 = time.time()
        error: str | None = None
        pred = {"pred_sql": None, "pred_rows": None, "step_id": None, "tool_execution_id": None}
        try:
            completions = client.create_completion(report_id, question, ds_id)
            pred = extract_prediction(completions)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            log.exception("[%d] completion failed", i)

        gold_rows, gold_err = run_sql(db_path, gold_sql)
        match = False
        if pred["pred_sql"] and gold_rows is not None and not error:
            match = exec_match(pred["pred_rows"] or [], gold_rows, gold_sql)

        attempted += 1
        if match:
            correct += 1

        row = {
            "idx": i,
            "db_id": db_id,
            "question": question,
            "gold_sql": gold_sql,
            "pred_sql": pred["pred_sql"],
            "exec_match": match,
            "n_pred_rows": len(pred["pred_rows"]) if pred["pred_rows"] is not None else None,
            "n_gold_rows": len(gold_rows) if gold_rows is not None else None,
            "report_id": report_id,
            "report_url": report_url,
            "step_id": pred["step_id"],
            "tool_execution_id": pred["tool_execution_id"],
            "error": error,
            "gold_sql_error": gold_err,
            "duration_s": round(time.time() - t0, 2),
        }
        out_f.write(json.dumps(row, ensure_ascii=False) + "\n")
        out_f.flush()
        log.info("[%d] %s db=%s acc=%d/%d (%.1f%%)",
                 i, "PASS" if match else "FAIL", db_id, correct, attempted,
                 100 * correct / max(1, attempted))

    out_f.close()
    log.info("Done. attempted=%d correct=%d skipped=%d acc=%.1f%%",
             attempted, correct, skipped, 100 * correct / max(1, attempted))


if __name__ == "__main__":
    main()
