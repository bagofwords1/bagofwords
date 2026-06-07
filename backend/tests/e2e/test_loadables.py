"""Layer-1 deterministic tests for load_step / load_entity.

Proves the LLM-independent machinery:
- AST ref extraction (literals only)
- name-based generate_df binding
- in-sandbox closures
- grid -> DataFrame reconstruction
- LoadablesResolver scoping/resolution against a real DB
- end-to-end execution of generate_df that calls load_step / load_entity
"""

import asyncio
import uuid

import pandas as pd
import pytest

from app.dependencies import async_session_maker
from app.ai.code_execution.code_execution import StreamingCodeExecutor
from app.ai.code_execution.loadables import (
    extract_loadable_refs,
    grid_to_df,
    LoadablesResolver,
)
from app.models.organization import Organization
from app.models.user import User
from app.models.data_source import DataSource
from app.models.report import Report
from app.models.widget import Widget
from app.models.query import Query
from app.models.step import Step
from app.models.entity import Entity


def _run(coro):
    return asyncio.run(coro)


def _grid(rows, fields):
    return {
        "rows": rows,
        "columns": [{"field": f, "headerName": f} for f in fields],
        "info": {"total_rows": len(rows), "total_columns": len(fields)},
    }


async def _seed():
    """Create org/user/report/query + two default steps and one entity.

    Returns a dict of ids and the ORM report (for the resolver)."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Org {suffix}")
        db.add(org)
        await db.commit()
        await db.refresh(org)

        user = User(name=f"User {suffix}", email=f"user-{suffix}@bow.dev", hashed_password="x")
        db.add(user)
        await db.commit()
        await db.refresh(user)

        report = Report(
            title=f"Report {suffix}",
            slug=f"report-{suffix}",
            status="draft",
            user_id=str(user.id),
            organization_id=str(org.id),
        )
        widget = Widget(title="W", slug=f"w-{suffix}", status="draft", report=report)
        query = Query(
            title="Customer Sales",
            report=report,
            widget=widget,
            organization_id=str(org.id),
            user_id=str(user.id),
        )
        db.add_all([report, widget, query])
        await db.commit()
        await db.refresh(query)
        await db.refresh(widget)

        # Default step for the query, resolvable by id/slug/title.
        step = Step(
            title="Customer Sales",
            slug=f"step-{suffix}",
            status="success",
            code="",
            type="table",
            widget_id=str(widget.id),
            query_id=str(query.id),
            data=_grid(
                [{"customer_id": 1, "name": "Alice"}, {"customer_id": 2, "name": "Bob"}],
                ["customer_id", "name"],
            ),
        )
        db.add(step)
        await db.commit()
        await db.refresh(step)
        query.default_step_id = str(step.id)
        db.add(query)
        await db.commit()

        # A published entity (no data sources -> access auto-granted).
        entity = Entity(
            organization_id=str(org.id),
            owner_id=str(user.id),
            type="model",
            title="Monthly Revenue Model",
            slug=f"monthly-revenue-{suffix}",
            code="SELECT 1",
            status="published",
            data=_grid(
                [{"customer_id": 1, "revenue": 100}, {"customer_id": 2, "revenue": 200}],
                ["customer_id", "revenue"],
            ),
        )
        db.add(entity)
        await db.commit()
        await db.refresh(entity)

        return {
            "org_id": str(org.id),
            "user_id": str(user.id),
            "report_id": str(report.id),
            "query_id": str(query.id),
            "widget_id": str(widget.id),
            "step_id": str(step.id),
            "step_slug": step.slug,
            "entity_id": str(entity.id),
            "entity_slug": entity.slug,
        }


async def _load(db, model, pk):
    return await db.get(model, pk)


# --------------------------------------------------------------------------- #
# Pure (no DB)                                                                 #
# --------------------------------------------------------------------------- #

def test_reuse_directive_detection():
    from app.ai.agents.coder.coder import Coder
    ctx = '<available_steps count="1">\n<step id="s1" title="Customer Sales" rows="59">\n</step>\n</available_steps>'
    # explicit reference by title -> REUSE REQUIRED naming the step
    d = Coder._build_reuse_directive(ctx, "add a tier column to the Customer Sales step")
    assert "REUSE REQUIRED" in d and "Customer Sales" in d
    # generic reuse language -> softer PREFER REUSE
    d2 = Coder._build_reuse_directive(ctx, "reuse what you built earlier and add a flag")
    assert "PREFER REUSE" in d2
    # unrelated prompt -> no directive
    assert Coder._build_reuse_directive(ctx, "show revenue by country") == ""
    # no loadables -> no directive
    assert Coder._build_reuse_directive("", "reuse the Customer Sales step") == ""


def test_extract_loadable_refs_literals_only():
    code = (
        "def generate_df(ds_clients, excel_files, load_step, load_entity):\n"
        "    a = load_step('Customer Sales')\n"
        "    b = load_entity('Monthly Revenue Model')\n"
        "    c = load_step(some_var)\n"  # dynamic -> ignored
        "    d = load_step('Customer Sales')\n"  # dup -> deduped
        "    return a\n"
    )
    steps, entities = extract_loadable_refs(code)
    assert steps == ["Customer Sales"]
    assert entities == ["Monthly Revenue Model"]


def test_extract_handles_syntax_error():
    assert extract_loadable_refs("def f(:\n  pass") == ([], [])


def test_grid_to_df_reorders_columns():
    df = grid_to_df(_grid([{"b": 2, "a": 1}], ["a", "b"]))
    assert list(df.columns) == ["a", "b"]
    assert df.to_dict("records") == [{"a": 1, "b": 2}]


def test_grid_to_df_empty():
    assert grid_to_df({}).empty
    assert grid_to_df(None).empty


def test_build_loadable_closures_hit_and_miss():
    df = pd.DataFrame([{"k": 1}])
    load_step, load_entity = StreamingCodeExecutor._build_loadable_closures(
        {"steps": {"S": df}, "entities": {"E": df}}
    )
    assert load_step("S").to_dict("records") == [{"k": 1}]
    # returns a copy — mutation must not corrupt the registry
    load_step("S")["k"] = 99
    assert load_step("S").to_dict("records") == [{"k": 1}]
    assert load_entity("E").to_dict("records") == [{"k": 1}]
    with pytest.raises(KeyError):
        load_step("missing")
    with pytest.raises(KeyError):
        load_entity("missing")


def test_invoke_generate_df_name_based_binding():
    inv = StreamingCodeExecutor._invoke_generate_df
    sentinel_http, sentinel_ls, sentinel_le = "HTTP", "LS", "LE"

    def two(ds_clients, excel_files):
        return ("two", ds_clients, excel_files)

    def with_http(ds_clients, excel_files, http):
        return ("http", http)

    def with_step(ds_clients, excel_files, load_step):
        return ("step", load_step)

    def with_both(ds_clients, excel_files, load_step, load_entity):
        return ("both", load_step, load_entity)

    assert inv(two, {}, [], sentinel_http, sentinel_ls, sentinel_le)[0] == "two"
    assert inv(with_http, {}, [], sentinel_http, sentinel_ls, sentinel_le) == ("http", sentinel_http)
    assert inv(with_step, {}, [], sentinel_http, sentinel_ls, sentinel_le) == ("step", sentinel_ls)
    assert inv(with_both, {}, [], sentinel_http, sentinel_ls, sentinel_le) == ("both", sentinel_ls, sentinel_le)


# --------------------------------------------------------------------------- #
# DB-backed resolution                                                        #
# --------------------------------------------------------------------------- #

def test_resolve_step_by_id_slug_title():
    ids = _run(_seed())

    async def go():
        async with async_session_maker() as db:
            report = await _load(db, Report, ids["report_id"])
            org = await _load(db, Organization, ids["org_id"])
            r = LoadablesResolver(db, org, report, None)
            for ref in (ids["step_id"], ids["step_slug"], "Customer Sales"):
                res = await r.resolve([ref], [])
                assert not res["errors"], (ref, res["errors"])
                df = res["steps"][ref]
                assert list(df.columns) == ["customer_id", "name"]
                assert len(df) == 2
    _run(go())


def test_resolve_step_miss_reports_error():
    ids = _run(_seed())

    async def go():
        async with async_session_maker() as db:
            report = await _load(db, Report, ids["report_id"])
            org = await _load(db, Organization, ids["org_id"])
            r = LoadablesResolver(db, org, report, None)
            res = await r.resolve(["Nonexistent Step"], [])
            assert res["steps"] == {}
            assert res["errors"] and "Nonexistent Step" in res["errors"][0]
    _run(go())


def test_resolve_entity_happy_and_denied(monkeypatch):
    ids = _run(_seed())

    async def go():
        async with async_session_maker() as db:
            report = await _load(db, Report, ids["report_id"])
            org = await _load(db, Organization, ids["org_id"])
            user = await _load(db, User, ids["user_id"])
            r = LoadablesResolver(db, org, report, user)
            # happy path: entity has no data sources -> access granted
            res = await r.resolve([], ["Monthly Revenue Model"])
            assert not res["errors"], res["errors"]
            assert list(res["entities"]["Monthly Revenue Model"].columns) == ["customer_id", "revenue"]
            # by id
            res2 = await r.resolve([], [ids["entity_id"]])
            assert ids["entity_id"] in res2["entities"]
            # not found
            res3 = await r.resolve([], ["No Such Entity"])
            assert res3["errors"]
    _run(go())


def test_resolve_entity_denied_when_no_ds_access(monkeypatch):
    """Attach a data source and deny access -> entity is not returned."""
    ids = _run(_seed())

    async def go():
        async with async_session_maker() as db:
            org = await _load(db, Organization, ids["org_id"])
            user = await _load(db, User, ids["user_id"])
            report = await _load(db, Report, ids["report_id"])
            entity = await db.get(Entity, ids["entity_id"])
            ds = DataSource(name="Secret DS", organization_id=ids["org_id"], is_active=True, is_public=False)
            db.add(ds)
            await db.commit()
            await db.refresh(ds)
            entity.data_sources.append(ds)
            db.add(entity)
            await db.commit()

            import app.ai.code_execution.loadables as loadables_mod

            async def _deny(*args, **kwargs):
                return False

            monkeypatch.setattr(
                "app.core.permission_resolver.user_can_access_data_source", _deny
            )
            r = LoadablesResolver(db, org, report, user)
            res = await r.resolve([], ["Monthly Revenue Model"])
            assert res["entities"] == {}
            assert res["errors"] and "access" in res["errors"][0].lower()
    _run(go())


def test_build_codegen_context_includes_loadables():
    """The discovery section must reach the coder prompt via build_codegen_context."""
    ids = _run(_seed())
    from app.ai.prompt_formatters import build_codegen_context

    async def go():
        async with async_session_maker() as db:
            report = await _load(db, Report, ids["report_id"])
            org = await _load(db, Organization, ids["org_id"])
            user = await _load(db, User, ids["user_id"])
            runtime_ctx = {"db": db, "organization": org, "report": report, "user": user}
            ctx = await build_codegen_context(
                runtime_ctx=runtime_ctx,
                user_prompt="reuse the customer sales step",
                interpreted_prompt=None,
                schemas_excerpt="",
            )
            assert "available_steps" in ctx.loadables_context
            assert "Customer Sales" in ctx.loadables_context
    _run(go())


def test_list_for_discovery_lists_default_step():
    ids = _run(_seed())

    async def go():
        async with async_session_maker() as db:
            report = await _load(db, Report, ids["report_id"])
            org = await _load(db, Organization, ids["org_id"])
            r = LoadablesResolver(db, org, report, None)
            section = await r.list_for_discovery()
            assert section is not None
            rendered = section.render()
            assert "Customer Sales" in rendered
            assert "available_steps" in rendered
    _run(go())


# --------------------------------------------------------------------------- #
# End-to-end execution                                                        #
# --------------------------------------------------------------------------- #

def _stub_coder(code_to_return):
    """A code_generator_fn drop-in that ignores context and returns fixed code.

    Supports a list to vary output across retry attempts.
    """
    seq = code_to_return if isinstance(code_to_return, list) else [code_to_return]
    state = {"i": 0}

    async def _gen(**kwargs):
        i = min(state["i"], len(seq) - 1)
        state["i"] += 1
        return seq[i]

    return _gen


def test_stream_v2_agentic_path_resolves_and_injects_load_step():
    """The create_data streaming path AST-scans, resolves, injects and runs."""
    ids = _run(_seed())
    from app.ai.schemas.codegen import CodeGenContext, CodeGenRequest

    code = (
        "def generate_df(ds_clients, excel_files, load_step, load_entity):\n"
        "    a = load_step('Customer Sales')\n"
        "    b = load_entity('Monthly Revenue Model')\n"
        "    return a.merge(b, on='customer_id')\n"
    )

    async def go():
        async with async_session_maker() as db:
            report = await _load(db, Report, ids["report_id"])
            org = await _load(db, Organization, ids["org_id"])
            user = await _load(db, User, ids["user_id"])
            resolver = LoadablesResolver(db, org, report, user)
            executor = StreamingCodeExecutor()
            ctx = CodeGenContext(user_prompt="x", schemas_excerpt="")
            done = None
            async for ev in executor.generate_and_execute_stream_v2(
                request=CodeGenRequest(context=ctx, retries=2),
                ds_clients={},
                excel_files=[],
                code_generator_fn=_stub_coder(code),
                loadable_resolver_fn=resolver.resolve,
            ):
                if ev["type"] == "done":
                    done = ev["payload"]
            assert done is not None
            df = done["df"]
            assert df is not None and sorted(df.columns) == ["customer_id", "name", "revenue"]
            assert len(df) == 2
            assert not done["errors"], done["errors"]

    _run(go())


def test_stream_v2_miss_feeds_retry_loop():
    """A bad ref must surface as a code error (driving regeneration), not crash."""
    ids = _run(_seed())
    from app.ai.schemas.codegen import CodeGenContext, CodeGenRequest

    bad = (
        "def generate_df(ds_clients, excel_files, load_step):\n"
        "    return load_step('Does Not Exist')\n"
    )

    async def go():
        async with async_session_maker() as db:
            report = await _load(db, Report, ids["report_id"])
            org = await _load(db, Organization, ids["org_id"])
            resolver = LoadablesResolver(db, org, report, None)
            executor = StreamingCodeExecutor()
            ctx = CodeGenContext(user_prompt="x", schemas_excerpt="")
            stdout_msgs = []
            done = None
            async for ev in executor.generate_and_execute_stream_v2(
                request=CodeGenRequest(context=ctx, retries=2),
                ds_clients={},
                excel_files=[],
                code_generator_fn=_stub_coder([bad, bad]),
                loadable_resolver_fn=resolver.resolve,
            ):
                if ev["type"] == "stdout":
                    stdout_msgs.append(ev["payload"])
                if ev["type"] == "done":
                    done = ev["payload"]
            # resolution error was surfaced and folded into the error feedback
            assert any("Loadable resolution failed" in m for m in stdout_msgs), stdout_msgs
            assert done is not None
            assert any("Does Not Exist" in e[1] for e in (done["errors"] or []))

    _run(go())


def test_execute_code_end_to_end_load_step_and_entity():
    ids = _run(_seed())

    code = (
        "def generate_df(ds_clients, excel_files, load_step, load_entity):\n"
        "    prev = load_step('Customer Sales')\n"
        "    rev = load_entity('Monthly Revenue Model')\n"
        "    return prev.merge(rev, on='customer_id')\n"
    )

    async def go():
        async with async_session_maker() as db:
            report = await _load(db, Report, ids["report_id"])
            org = await _load(db, Organization, ids["org_id"])
            user = await _load(db, User, ids["user_id"])
            r = LoadablesResolver(db, org, report, user)
            step_refs, entity_refs = extract_loadable_refs(code)
            resolved = await r.resolve(step_refs, entity_refs)
            assert not resolved["errors"], resolved["errors"]
            loadables = {"steps": resolved["steps"], "entities": resolved["entities"]}

        executor = StreamingCodeExecutor()
        df, _log, _q = await executor.execute_code_async(
            code=code, ds_clients={}, excel_files=[], loadables=loadables
        )
        assert sorted(df.columns) == ["customer_id", "name", "revenue"]
        assert len(df) == 2
        recs = {row["customer_id"]: row["revenue"] for _, row in df.iterrows()}
        assert recs == {1: 100, 2: 200}

    _run(go())
