"""Demo dataset spec + generator contract.

The LLM is the only stubbed boundary: each stub returns the code a model
would write for a table. Everything else — sandbox validation, execution,
coercion, PK/FK/date checks, the retry loop and the SQLite writer — runs real.
"""
import asyncio
import os
import sqlite3
import uuid
from datetime import date

import pytest

from app.schemas.demo_dataset_schema import DemoDatasetSpec, generation_order, validate_spec
from app.services.demo_data.generator import DemoDatasetGenerator, TableGenerationError, write_sqlite
from app.services.demo_data.installer import DEMO_DATA_ROOT, emoji_token, remove_generated_file

TODAY = date(2026, 10, 8)


def _spec(**over):
    base = dict(
        name="Demo – Shop",
        domain="e-commerce",
        date_range_start="2025-01-01",
        date_range_end="2026-06-30",
        tables=[
            {"name": "customers", "description": "one row per customer", "row_count": 40,
             "columns": [
                 {"name": "customer_id", "type": "integer", "primary_key": True},
                 {"name": "segment", "type": "text"},
             ]},
            {"name": "orders", "description": "one row per order", "row_count": 120,
             "columns": [
                 {"name": "order_id", "type": "text", "primary_key": True},
                 {"name": "customer_id", "type": "integer", "references": "customers.customer_id"},
                 {"name": "ordered_at", "type": "datetime"},
                 {"name": "amount", "type": "real"},
                 {"name": "is_gift", "type": "boolean"},
                 {"name": "meta", "type": "json", "nullable": True},
             ]},
        ],
        agents=[{"name": "Revenue", "icon": "💰", "tables": ["orders", "customers"]}],
    )
    base.update(over)
    return DemoDatasetSpec(**base)


CUSTOMERS = """```python
def generate(n, rng, tables, start, end):
    return pd.DataFrame({
        'customer_id': np.arange(1, n + 1),
        'segment': rng.choice(['consumer', 'smb', 'enterprise'], size=n, p=[0.7, 0.25, 0.05]),
    })
```"""

ORDERS_OK = """```python
def generate(n, rng, tables, start, end):
    cust = tables['customers']['customer_id'].to_numpy()
    secs = rng.integers(0, int((end - start).total_seconds()), size=n)
    return pd.DataFrame({
        'order_id': [f'ORD-{i:05d}' for i in range(1, n + 1)],
        'customer_id': rng.choice(cust, size=n),
        'ordered_at': start + pd.to_timedelta(secs, unit='s'),
        'amount': np.round(rng.lognormal(3.5, 0.6, size=n), 2),
        'is_gift': rng.random(n) < 0.1,
        'meta': [{'channel': 'web'} if i % 3 else None for i in range(n)],
    })
```"""

ORDERS_BAD_FK = ORDERS_OK.replace("rng.choice(cust, size=n)", "np.full(n, 10_000)")
ORDERS_FUTURE = ORDERS_OK.replace("start + pd.to_timedelta(secs, unit='s')", "pd.Timestamp('2031-01-01') + pd.to_timedelta(secs, unit='s')")
ORDERS_WRITES_FILE = ORDERS_OK.replace("    return pd.DataFrame({", "    pd.DataFrame({'x': [1]}).to_csv('/tmp/leak.csv')\n    return pd.DataFrame({")


def _stub(orders_attempts):
    """Inference stub: customers always succeeds; orders returns the given
    sequence of answers (the last one repeats)."""
    calls = {"orders": 0}

    async def inference(system, prompt):
        if "TABLE TO GENERATE: customers" in prompt:
            return CUSTOMERS
        i = min(calls["orders"], len(orders_attempts) - 1)
        calls["orders"] += 1
        return orders_attempts[i]

    return inference, calls


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- spec


def test_valid_spec_has_no_errors():
    assert validate_spec(_spec(), today=TODAY) == []


@pytest.mark.parametrize("mutate, needle", [
    (lambda s: s["tables"][1]["columns"][1].update(references="clients.customer_id"), "unknown table"),
    (lambda s: s["tables"][1]["columns"][1].update(references="customers.segment"), "primary key"),
    (lambda s: s.update(date_range_end="2027-01-01"), "future"),
    (lambda s: s["agents"][0].update(tables=["orders", "returns"]), "unknown tables"),
    (lambda s: s["tables"].append(dict(s["tables"][0])), "unique"),
    (lambda s: s["tables"][0]["columns"].append({"name": "parent_order", "type": "text", "references": "orders.order_id"}), "cycle"),
])
def test_spec_structural_problems_are_reported(mutate, needle):
    raw = _spec().model_dump(mode="json")
    mutate(raw)
    errors = validate_spec(DemoDatasetSpec(**raw), today=TODAY)
    assert errors, "expected the spec to be rejected"
    assert any(needle in e.lower() for e in errors), errors


def test_generation_order_puts_parents_first_and_allows_self_reference():
    raw = _spec().model_dump(mode="json")
    raw["tables"][0]["columns"].append(
        {"name": "referred_by", "type": "integer", "references": "customers.customer_id", "nullable": True}
    )
    order = generation_order(DemoDatasetSpec(**raw))
    assert order.index("customers") < order.index("orders")


# ---------------------------------------------------------------- generator


def test_generator_retries_until_foreign_keys_resolve_and_writes_constrained_sqlite(tmp_path):
    spec = _spec()
    inference, calls = _stub([ORDERS_BAD_FK, ORDERS_OK])
    result = _run(DemoDatasetGenerator(inference, today=TODAY).generate(spec))

    by_name = {t.name: t for t in result.tables}
    assert by_name["orders"].attempts == 2 and calls["orders"] == 2
    assert by_name["customers"].rows == 40 and by_name["orders"].rows == 120

    path = str(tmp_path / "shop.sqlite")
    write_sqlite(path, spec, result.frames)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("select count(*) from orders").fetchone()[0] == 120
        # Every order points at a real customer.
        orphans = conn.execute(
            "select count(*) from orders o left join customers c on c.customer_id = o.customer_id where c.customer_id is null"
        ).fetchone()[0]
        assert orphans == 0
        fks = conn.execute("pragma foreign_key_list(orders)").fetchall()
        assert any(r[2] == "customers" and r[3] == "customer_id" for r in fks)
        # Types landed as declared: booleans 0/1, datetimes as ISO text, json as text.
        gift_vals = {r[0] for r in conn.execute("select distinct is_gift from orders")}
        assert gift_vals <= {0, 1}
        ts = conn.execute("select ordered_at from orders limit 1").fetchone()[0]
        assert len(ts) == 19 and ts[4] == "-"
        assert conn.execute("select count(*) from orders where meta is null").fetchone()[0] > 0
    finally:
        conn.close()


def test_generator_rejects_accidental_future_dates():
    inference, _ = _stub([ORDERS_FUTURE])
    with pytest.raises(TableGenerationError) as exc:
        _run(DemoDatasetGenerator(inference, today=TODAY, max_attempts=2).generate(_spec()))
    assert exc.value.table == "orders"
    assert "future" in exc.value.message.lower()


def test_generator_never_runs_code_that_writes_files(tmp_path):
    leak = "/tmp/leak.csv"
    if os.path.exists(leak):
        os.remove(leak)
    inference, calls = _stub([ORDERS_WRITES_FILE])
    with pytest.raises(TableGenerationError):
        _run(DemoDatasetGenerator(inference, today=TODAY, max_attempts=2).generate(_spec()))
    assert calls["orders"] == 2  # retried, then gave up
    assert not os.path.exists(leak)


def test_generator_gives_up_after_its_attempt_budget():
    inference, calls = _stub(["no code here"])
    with pytest.raises(TableGenerationError):
        _run(DemoDatasetGenerator(inference, today=TODAY, max_attempts=3).generate(_spec()))
    assert calls["orders"] == 3


# ---------------------------------------------------------------- installer helpers


@pytest.mark.parametrize("raw, expected", [
    ("💰", "emoji:💰"),
    ("emoji:🏭", "emoji:🏭"),
    ("👩‍💻 team", "emoji:👩‍💻"),
    ("chart", None),
    ("", None),
    (None, None),
])
def test_emoji_token_keeps_one_emoji_or_nothing(raw, expected):
    assert emoji_token(raw) == expected


def test_remove_generated_file_only_touches_the_demo_data_root(tmp_path):
    inside = os.path.join(DEMO_DATA_ROOT, "test-org", f"{uuid.uuid4()}.sqlite")
    os.makedirs(os.path.dirname(inside), exist_ok=True)
    open(inside, "w").close()
    outside = tmp_path / "keep.sqlite"
    outside.write_text("x")

    remove_generated_file({"database": str(outside), "demo_generated": True})
    remove_generated_file({"database": inside})  # not marked generated
    assert outside.exists() and os.path.exists(inside)

    remove_generated_file({"database": inside, "demo_generated": True})
    assert not os.path.exists(inside)


def test_self_reference_to_a_later_row_writes(tmp_path):
    """employees.manager_id may point at a row that comes later in the same
    frame (or appear in any order). Validation accepts it; the writer must too —
    in production it generated every table, then failed the write with
    'FOREIGN KEY constraint failed'."""
    import pandas as pd
    spec = DemoDatasetSpec(
        name="Org", domain="hr", date_range_start="2025-01-01", date_range_end="2025-12-31",
        tables=[{"name": "employees", "description": "one row per employee", "row_count": 4, "columns": [
            {"name": "employee_id", "type": "integer", "primary_key": True},
            {"name": "manager_id", "type": "integer", "references": "employees.employee_id", "nullable": True},
        ]}],
    )
    frames = {"employees": pd.DataFrame({
        "employee_id": pd.array([1, 2, 3, 4], dtype="Int64"),
        "manager_id": pd.array([4, 4, 1, None], dtype="Int64"),  # 1 and 2 report to 4, inserted last
    })}
    path = str(tmp_path / "org.sqlite")
    write_sqlite(path, spec, frames)
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("select count(*) from employees").fetchone()[0] == 4
        assert conn.execute("pragma foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_writer_still_rejects_real_orphans(tmp_path):
    import pandas as pd
    spec = DemoDatasetSpec(
        name="Org", domain="hr", date_range_start="2025-01-01", date_range_end="2025-12-31",
        tables=[{"name": "employees", "description": "one row per employee", "row_count": 2, "columns": [
            {"name": "employee_id", "type": "integer", "primary_key": True},
            {"name": "manager_id", "type": "integer", "references": "employees.employee_id", "nullable": True},
        ]}],
    )
    frames = {"employees": pd.DataFrame({
        "employee_id": pd.array([1, 2], dtype="Int64"),
        "manager_id": pd.array([None, 99], dtype="Int64"),
    })}
    with pytest.raises(RuntimeError, match="foreign_key_check"):
        write_sqlite(str(tmp_path / "bad.sqlite"), spec, frames)


def test_retry_prompt_names_the_failing_line():
    """The production failure ('Index does not support mutable operations')
    repeated on every retry: the model only got the exception text. The retry
    must show which statement failed."""
    bad = """```python
def generate(n, rng, tables, start, end):
    ids = pd.RangeIndex(1, n + 1)
    ids[0] = 99
    return pd.DataFrame({'customer_id': ids, 'segment': ['a'] * n})
```"""
    prompts = []

    async def inference(system, prompt):
        prompts.append(prompt)
        return bad if len(prompts) == 1 else CUSTOMERS

    spec = _spec(tables=[_spec().model_dump(mode="json")["tables"][0]], agents=[])
    result = _run(DemoDatasetGenerator(inference, today=TODAY).generate(spec))
    assert result.tables[0].attempts == 2
    assert "ids[0] = 99" in prompts[1] and "line 3" in prompts[1]
    assert result.tables[0].code and "def generate" in result.tables[0].code
