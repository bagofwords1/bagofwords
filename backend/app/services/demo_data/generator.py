"""Demo dataset generator: spec -> validated DataFrames -> SQLite file.

How rows get made
-----------------
The approved spec (app/schemas/demo_dataset_schema.py) is generated table by
table, parents before children. For each table the org's *small default* model
writes one pandas/numpy function::

    def generate(n, rng, tables, start, end) -> pd.DataFrame

which runs in the same sandbox rules as every other generated code path
(``validate_python_code``: no file/network/process access) plus a ban on
DataFrame file sinks (``to_csv`` & co.), on the shared code-exec pool, under a
timeout. ``tables`` holds the already generated tables, which is how child
rows reference real parent keys and honor cross-table rules ("refunds only for
delivered orders").

The model never writes the file. Its frame is coerced to the spec's types and
checked (columns, nulls, unique primary key, foreign keys resolve, no
accidental future dates, row caps). Any failure goes back to the model as a
short error and it retries, up to ``max_attempts``. Only the backend writes
SQLite, with real PRIMARY KEY / FOREIGN KEY constraints, at a path the server
chooses.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Awaitable, Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from app.schemas.demo_dataset_schema import (
    MAX_ROWS_PER_TABLE,
    DemoColumn,
    DemoDatasetSpec,
    DemoTable,
    generation_order,
    primary_key,
    table_map,
)

logger = logging.getLogger(__name__)

# (system, prompt) -> model text
InferenceFn = Callable[[str, str], Awaitable[str]]
# Called with a small progress dict; must not raise.
ProgressFn = Callable[[Dict[str, Any]], None]

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_EXEC_TIMEOUT_S = 90.0

# DataFrame/ndarray methods that write to the filesystem (or a DB handle). The
# shared validator only blocks *readers*; a generator has no reason to persist.
FORBIDDEN_SINK_ATTRS = frozenset({
    "to_csv", "to_parquet", "to_sql", "to_excel", "to_pickle", "to_feather",
    "to_hdf", "to_stata", "to_orc", "to_html", "to_latex", "to_xml", "to_clipboard",
    "to_markdown", "tofile", "save", "savez", "savez_compressed", "savetxt", "dump",
})

# Words in a column's description/hint that make future dates legitimate.
_FUTURE_OK_RE = re.compile(r"\b(due|expir|expected|scheduled|planned|forecast|future|renewal|next|end_date|valid_to|until)", re.I)


class TableGenerationError(Exception):
    """A table could not be generated within the attempt budget."""

    def __init__(self, table: str, message: str, attempts: int, code: Optional[str] = None):
        super().__init__(f"{table}: {message}")
        self.table = table
        self.message = message
        self.attempts = attempts
        self.code = code  # last attempt's generator, for diagnosis


class _ValidationError(Exception):
    pass


@dataclass
class TableResult:
    name: str
    rows: int
    attempts: int
    warnings: List[str] = field(default_factory=list)
    code: Optional[str] = None  # the generator that produced the rows


@dataclass
class GenerationResult:
    frames: Dict[str, pd.DataFrame]
    tables: List[TableResult]
    seconds: float

    @property
    def total_rows(self) -> int:
        return sum(t.rows for t in self.tables)

    @property
    def warnings(self) -> List[str]:
        return [f"{t.name}: {w}" for t in self.tables for w in t.warnings]


# ---------------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You write one Python function that generates realistic mock data for ONE table of a demo database.

Contract (exact signature, nothing else is called):

def generate(n, rng, tables, start, end):
    # n:      target row count (int). Return about n rows unless the table's logic fixes the count (e.g. one row per device per hour).
    # rng:    numpy.random.Generator — use it for ALL randomness (reproducible). Never use the random module or np.random.seed.
    # tables: dict of already-generated tables {name: pandas.DataFrame}. Take foreign-key values ONLY from these frames.
    # start, end: pandas.Timestamp bounds of the dataset's time range.
    # return: pandas.DataFrame with EXACTLY the listed columns, in order.

Available names: pd, np (pre-imported). You may also import math, datetime, json, string, itertools.
Forbidden: files, network, OS access, eval/exec/open, writing anything to disk, printing large output.

Realism rules:
- Primary key: unique, non-null (integers 1..N, or prefixed strings like 'EMP-00042').
- Foreign keys: sample existing parent keys from tables['parent']['pk'].to_numpy(); respect the business logic in the notes (e.g. only delivered orders get refunds) by filtering the parent frame first. Use realistic skew (some customers order far more than others), not uniform.
- Numbers: skewed distributions (lognormal / gamma / poisson), sensible ranges and rounding (money to 2 decimals). Derived columns are computed, never random (total = quantity * unit_price).
- Time: timestamps within [start, end] — NEVER in the future unless the column is explicitly a due/expected/scheduled date. Add seasonality, weekday/hour patterns, growth trends, and incidents/spikes where the notes ask.
- Categories: realistic weighted mixes (rng.choice(values, size=n, p=weights)), never all equal.
- Text: names, products, log messages etc. from in-code vocab lists and templates combined with rng; plausible but FICTIONAL (no real people, no valid card numbers or real tax IDs).
- Nulls only in nullable columns, at a realistic rate.
- Events/logs/metrics: irregular event timestamps (sorted), regular metric intervals, correlated signals.
- Vectorize with numpy/pandas; avoid Python loops over more than ~5,000 items.
- pandas Index / RangeIndex objects are immutable: build keys and value arrays with numpy (np.arange, rng.choice) and call .copy() / .astype(float) before assigning into them; never assign into df.index, df.columns or a .unique() result.
- Self-reference (e.g. manager_id -> this table's id): create the id array first, then pick each row's parent from ids of earlier rows with rng; top-level rows get None (use a float array with np.nan, or a pandas "Int64" array with pd.NA).

Reply with ONLY one ```python code block containing the imports (if any) and the generate function."""


def _col_line(c: DemoColumn) -> str:
    bits = [f"{c.name} {c.type}"]
    if c.primary_key:
        bits.append("PRIMARY KEY")
    if c.references:
        bits.append(f"-> {c.references}")
    bits.append("NULLABLE" if c.nullable else "NOT NULL")
    line = "  - " + " ".join(bits)
    if c.description:
        line += f" — {c.description}"
    if c.generation_hint:
        line += f" [hint: {c.generation_hint}]"
    return line


def _frame_digest(name: str, df: pd.DataFrame, table: Optional[DemoTable]) -> str:
    pk = primary_key(table) if table else None
    lines = [f"tables['{name}']: {len(df):,} rows; columns: " + ", ".join(
        f"{c} ({str(df[c].dtype)})" for c in df.columns
    )]
    if pk is not None and pk.name in df.columns:
        s = df[pk.name]
        lines.append(f"  primary key {pk.name}: e.g. {', '.join(map(str, s.head(3).tolist()))}")
    for c in df.columns:
        s = df[c]
        if s.dtype == object and s.nunique(dropna=True) <= 12:
            vals = s.dropna().value_counts().head(8).index.tolist()
            if vals:
                lines.append(f"  {c} values: {', '.join(map(str, vals))}")
    try:
        sample = df.head(2).to_dict(orient="records")
        lines.append(f"  sample: {json.dumps(sample, default=str)[:600]}")
    except Exception:
        pass
    return "\n".join(lines)


def build_table_prompt(
    spec: DemoDatasetSpec,
    table: DemoTable,
    frames: Dict[str, pd.DataFrame],
    *,
    previous_code: Optional[str] = None,
    error: Optional[str] = None,
) -> str:
    tmap = table_map(spec)
    parts: List[str] = []
    parts.append(f"Dataset: {spec.name} — domain: {spec.domain}")
    if spec.description:
        parts.append(f"About: {spec.description}")
    parts.append(f"Time range: {spec.date_range_start} to {spec.date_range_end}")
    if spec.realism_notes:
        parts.append("Realism notes (whole dataset):\n" + "\n".join(f"- {n}" for n in spec.realism_notes))
    others = [t for t in spec.tables if t.name != table.name]
    if others:
        parts.append("Other tables in the dataset: " + "; ".join(
            f"{t.name} ({t.description[:80]})" for t in others
        ))
    parts.append(
        f"TABLE TO GENERATE: {table.name} — {table.description}\n"
        f"Target rows n = {table.row_count}\nColumns, in order:\n"
        + "\n".join(_col_line(c) for c in table.columns)
    )
    available = [n for n in frames]
    if available:
        parts.append("Already generated (available in `tables`):\n" + "\n".join(
            _frame_digest(n, frames[n], tmap.get(n)) for n in available
        ))
    self_ref = [c for c in table.columns if c.references and c.references.split(".", 1)[0] == table.name]
    if self_ref:
        parts.append(
            "Self-reference: " + ", ".join(c.name for c in self_ref)
            + " must point at primary keys of rows in THIS frame (build the keys first, then pick referenced rows from them; leave top-level rows NULL)."
        )
    if previous_code and error:
        parts.append(
            "Your previous attempt failed. Fix it and return the full corrected function.\n"
            f"Error:\n{error[:1500]}\n\nPrevious code:\n```python\n{previous_code[:8000]}\n```"
        )
    return "\n\n".join(parts)


_CODE_BLOCK_RE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)


def extract_code(text: str) -> str:
    text = text or ""
    blocks = _CODE_BLOCK_RE.findall(text)
    if blocks:
        with_fn = [b for b in blocks if "def generate" in b]
        return (with_fn or blocks)[0].strip()
    return text.strip()


# ---------------------------------------------------------------------------
# Sandboxed execution
# ---------------------------------------------------------------------------

def validate_generator_code(code: str) -> None:
    """Shared sandbox rules + no file sinks. Raises UnsafePythonError."""
    from app.ai.code_execution.code_execution import UnsafePythonError, validate_python_code

    validate_python_code(code)
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise _ValidationError(f"SyntaxError: {e}") from e
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_SINK_ATTRS:
            raise UnsafePythonError(f"Forbidden call '.{node.attr}()': generators must not write files")
        if isinstance(node, ast.Import) or isinstance(node, ast.ImportFrom):
            mod = (node.module if isinstance(node, ast.ImportFrom) else None) or ""
            names = [mod] if mod else [a.name for a in node.names]
            for n in names:
                root = (n or "").split(".")[0]
                if root == "random":
                    raise UnsafePythonError("Use rng (numpy Generator) instead of the random module")
    if not any(isinstance(n, ast.FunctionDef) and n.name == "generate" for n in tree.body):
        raise _ValidationError("No top-level `def generate(n, rng, tables, start, end)` found")


def _failing_line(exc: BaseException, code: Optional[str]) -> str:
    """' (line N: <source>)' for the deepest frame inside the generator, so a
    retry sees which statement failed instead of only the exception text."""
    import traceback
    if not code:
        return ""
    lines = code.splitlines()
    lineno = None
    for fr in traceback.extract_tb(exc.__traceback__):
        if fr.filename == "<demo_generator>":
            lineno = fr.lineno
    if not lineno or lineno > len(lines):
        return ""
    return f" (line {lineno}: {lines[lineno - 1].strip()[:200]})"


def _run_generator_sync(code: str, n: int, seed: int, frames: Dict[str, pd.DataFrame],
                        start: pd.Timestamp, end: pd.Timestamp) -> Any:
    namespace: Dict[str, Any] = {"pd": pd, "np": np}
    exec(compile(code, "<demo_generator>", "exec"), namespace)
    fn = namespace.get("generate")
    if not callable(fn):
        raise _ValidationError("`generate` is not a function")
    rng = np.random.default_rng(seed)
    # Copies: a generator mutating a parent frame must not corrupt it.
    view = {k: v.copy() for k, v in frames.items()}
    return fn(n, rng, view, start, end)


async def run_generator(code: str, *, n: int, seed: int, frames: Dict[str, pd.DataFrame],
                        start: pd.Timestamp, end: pd.Timestamp,
                        timeout_s: float = DEFAULT_EXEC_TIMEOUT_S) -> Any:
    from app.ai.code_execution.code_execution import _CODE_EXEC_POOL

    validate_generator_code(code)
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(_CODE_EXEC_POOL, _run_generator_sync, code, n, seed, frames, start, end)
    try:
        return await asyncio.wait_for(fut, timeout=timeout_s)
    except asyncio.TimeoutError as e:
        raise _ValidationError(
            f"Generation took longer than {int(timeout_s)}s — vectorize with numpy instead of Python loops"
        ) from e


# ---------------------------------------------------------------------------
# Validation / coercion
# ---------------------------------------------------------------------------

def _examples(values, k: int = 3) -> str:
    return ", ".join(map(str, list(values)[:k]))


def coerce_and_validate(
    raw: Any,
    table: DemoTable,
    frames: Dict[str, pd.DataFrame],
    spec: DemoDatasetSpec,
    *,
    today: Optional[date] = None,
) -> tuple[pd.DataFrame, List[str]]:
    """Return a frame with exactly the spec columns in SQLite-ready types, or
    raise _ValidationError with a message the model can act on."""
    warnings: List[str] = []
    today = today or date.today()
    if not isinstance(raw, pd.DataFrame):
        raise _ValidationError(f"generate() returned {type(raw).__name__}, expected a pandas DataFrame")
    df = raw.reset_index(drop=True)
    wanted = [c.name for c in table.columns]
    missing = [c for c in wanted if c not in df.columns]
    if missing:
        raise _ValidationError(f"Missing columns: {', '.join(missing)}. Return exactly: {', '.join(wanted)}")
    extra = [c for c in df.columns if c not in wanted]
    if extra:
        warnings.append(f"dropped extra columns {', '.join(map(str, extra))}")
    df = df[wanted].copy()

    n = len(df)
    if n == 0:
        raise _ValidationError("Returned 0 rows")
    if n > MAX_ROWS_PER_TABLE:
        raise _ValidationError(f"Returned {n:,} rows; the limit is {MAX_ROWS_PER_TABLE:,}. Lower the volume")
    if n < 0.5 * table.row_count or n > 2 * table.row_count:
        warnings.append(f"{n:,} rows (target {table.row_count:,})")

    future_cutoff = pd.Timestamp(today) + pd.Timedelta(days=1)
    for c in table.columns:
        s = df[c.name]
        try:
            if c.type == "integer":
                num = pd.to_numeric(s, errors="raise")
                if np.isinf(num.astype("float64")).any():
                    raise ValueError("infinite values")
                df[c.name] = num.round().astype("Int64")
            elif c.type == "real":
                num = pd.to_numeric(s, errors="raise").astype("float64")
                if np.isinf(num).any():
                    raise ValueError("infinite values")
                df[c.name] = num
            elif c.type == "boolean":
                mapped = s.map(lambda v: None if v is None or (isinstance(v, float) and np.isnan(v)) else (
                    1 if str(v).strip().lower() in ("1", "true", "t", "yes", "y") else 0
                ))
                df[c.name] = pd.array(mapped, dtype="Int64")
            elif c.type in ("date", "datetime"):
                ts = pd.to_datetime(s, errors="raise")
                try:
                    if getattr(ts.dt, "tz", None) is not None:
                        ts = ts.dt.tz_localize(None)
                except Exception:
                    pass
                future = ts[ts > future_cutoff]
                if len(future) and not _FUTURE_OK_RE.search(f"{c.name} {c.description} {c.generation_hint or ''}"):
                    raise _ValidationError(
                        f"Column {c.name}: {len(future)} values are in the future (e.g. {_examples(future)}); "
                        f"keep timestamps <= end ({spec.date_range_end})"
                    )
                fmt = "%Y-%m-%d" if c.type == "date" else "%Y-%m-%d %H:%M:%S"
                df[c.name] = ts.dt.strftime(fmt).where(ts.notna(), None)
            elif c.type == "json":
                df[c.name] = s.map(lambda v: None if v is None or (isinstance(v, float) and np.isnan(v))
                                   else (json.dumps(v, default=str) if isinstance(v, (dict, list)) else str(v)))
            else:  # text
                df[c.name] = s.map(lambda v: None if v is None or (isinstance(v, float) and np.isnan(v)) else str(v))
        except _ValidationError:
            raise
        except Exception as e:
            raise _ValidationError(f"Column {c.name} ({c.type}): cannot convert — {e}") from e

        nulls = int(df[c.name].isna().sum())
        if nulls and not c.nullable:
            raise _ValidationError(f"Column {c.name} has {nulls} NULLs but is NOT NULL")

    pk = primary_key(table)
    if pk is not None:
        s = df[pk.name]
        dups = s[s.duplicated()]
        if len(dups):
            raise _ValidationError(f"Primary key {pk.name} has {len(dups)} duplicates (e.g. {_examples(dups.unique())})")

    for c in table.columns:
        if not c.references:
            continue
        ref_table, ref_col = c.references.split(".", 1)
        parent = df if ref_table == table.name else frames.get(ref_table)
        if parent is None or ref_col not in parent.columns:
            raise _ValidationError(f"Column {c.name}: parent {c.references} is not available")
        parent_keys = parent[ref_col]
        child = df[c.name].dropna()
        bad = child[~child.isin(set(parent_keys.dropna().tolist()))]
        if len(bad):
            raise _ValidationError(
                f"Foreign key {c.name} -> {c.references}: {len(bad)} values have no parent row "
                f"(e.g. {_examples(bad.unique())}). Sample from tables['{ref_table}']['{ref_col}']"
            )
    return df, warnings


# ---------------------------------------------------------------------------
# SQLite writer
# ---------------------------------------------------------------------------

_SQL_TYPES = {
    "integer": "INTEGER", "real": "REAL", "text": "TEXT", "date": "DATE",
    "datetime": "DATETIME", "boolean": "BOOLEAN", "json": "JSON",
}


def write_sqlite(path: str, spec: DemoDatasetSpec, frames: Dict[str, pd.DataFrame]) -> int:
    """Write all tables with PK/FK constraints and FK indexes. Atomic: builds
    a temp file next to ``path`` and renames it. Returns the file size."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    conn = sqlite3.connect(tmp)
    try:
        cur = conn.cursor()
        # Enforcement stays off while loading: a self-reference (manager_id ->
        # employees) may point at a row inserted later in the same table. The
        # constraints are declared, and foreign_key_check below verifies every
        # row once the load is complete.
        cur.execute("PRAGMA foreign_keys = OFF")
        cur.execute("PRAGMA journal_mode = OFF")
        cur.execute("PRAGMA synchronous = OFF")
        for name in generation_order(spec):
            table = table_map(spec)[name]
            cols = []
            fks = []
            for c in table.columns:
                col = f'"{c.name}" {_SQL_TYPES[c.type]}'
                if c.primary_key:
                    col += " PRIMARY KEY"
                if not c.nullable and not c.primary_key:
                    col += " NOT NULL"
                cols.append(col)
                if c.references:
                    rt, rc = c.references.split(".", 1)
                    fks.append(f'FOREIGN KEY ("{c.name}") REFERENCES "{rt}" ("{rc}")')
            cur.execute(f'CREATE TABLE "{name}" ({", ".join(cols + fks)})')
            df = frames[name]
            placeholders = ", ".join("?" for _ in table.columns)
            rows = df.astype(object).where(df.notna(), None).itertuples(index=False, name=None)
            cur.executemany(f'INSERT INTO "{name}" VALUES ({placeholders})', rows)
            for c in table.columns:
                if c.references:
                    cur.execute(f'CREATE INDEX "ix_{name}_{c.name}" ON "{name}" ("{c.name}")')
        conn.commit()
        bad = cur.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise RuntimeError(f"foreign_key_check failed on {len(bad)} rows (first: {bad[0]})")
        cur.execute("ANALYZE")
        conn.commit()
    finally:
        conn.close()
    os.replace(tmp, path)
    return os.path.getsize(path)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

class DemoDatasetGenerator:
    def __init__(
        self,
        inference: InferenceFn,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        exec_timeout_s: float = DEFAULT_EXEC_TIMEOUT_S,
        today: Optional[date] = None,
    ):
        self.inference = inference
        self.max_attempts = max(1, max_attempts)
        self.exec_timeout_s = exec_timeout_s
        self.today = today

    async def generate_table(
        self,
        spec: DemoDatasetSpec,
        table: DemoTable,
        frames: Dict[str, pd.DataFrame],
        *,
        seed: int,
        progress: Optional[ProgressFn] = None,
    ) -> tuple[pd.DataFrame, TableResult]:
        start = pd.Timestamp(spec.date_range_start)
        end = pd.Timestamp(spec.date_range_end) + pd.Timedelta(hours=23, minutes=59, seconds=59)
        code: Optional[str] = None
        error: Optional[str] = None
        for attempt in range(1, self.max_attempts + 1):
            if progress:
                progress({"table": table.name, "state": "writing_code" if attempt == 1 else "retrying", "attempt": attempt})
            prompt = build_table_prompt(spec, table, frames, previous_code=code, error=error)
            try:
                text = await self.inference(SYSTEM_PROMPT, prompt)
            except Exception as e:  # provider error — retry within budget
                error = f"LLM call failed: {e}"
                logger.warning("demo generator: inference failed for %s: %s", table.name, e)
                continue
            code = extract_code(text)
            if progress:
                progress({"table": table.name, "state": "running", "attempt": attempt})
            try:
                raw = await run_generator(
                    code, n=table.row_count, seed=seed, frames=frames,
                    start=start, end=end, timeout_s=self.exec_timeout_s,
                )
                df, warnings = coerce_and_validate(raw, table, frames, spec, today=self.today)
                return df, TableResult(name=table.name, rows=len(df), attempts=attempt, warnings=warnings, code=code)
            except Exception as e:
                error = f"{type(e).__name__}: {e}{_failing_line(e, code)}"
                logger.info("demo generator: %s attempt %s failed: %s", table.name, attempt, error[:300])
        raise TableGenerationError(table.name, error or "generation failed", self.max_attempts, code=code)

    async def generate(self, spec: DemoDatasetSpec, progress: Optional[ProgressFn] = None) -> GenerationResult:
        t0 = time.monotonic()
        frames: Dict[str, pd.DataFrame] = {}
        results: Dict[str, TableResult] = {}
        tmap = table_map(spec)
        order = generation_order(spec)
        spec_index = {t.name: i for i, t in enumerate(spec.tables)}
        # Tables whose parents are all done run concurrently.
        done: set = set()
        while len(done) < len(order):
            level = [
                n for n in order if n not in done and all(
                    (c.references.split(".", 1)[0] in done or c.references.split(".", 1)[0] == n)
                    for c in tmap[n].columns if c.references
                )
            ]
            snapshot = dict(frames)
            outs = await asyncio.gather(*[
                self.generate_table(spec, tmap[n], snapshot, seed=spec.seed + spec_index[n] * 7919, progress=progress)
                for n in level
            ], return_exceptions=True)
            for o in outs:
                if isinstance(o, BaseException):
                    raise o
            for n, (df, res) in zip(level, outs):
                frames[n] = df
                results[n] = res
                done.add(n)
                if progress:
                    progress({"table": n, "state": "done", "rows": res.rows, "attempts": res.attempts})
        return GenerationResult(
            frames=frames,
            tables=[results[t.name] for t in spec.tables],
            seconds=round(time.monotonic() - t0, 1),
        )
