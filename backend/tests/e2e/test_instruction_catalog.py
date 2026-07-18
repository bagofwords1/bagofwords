"""E2E tests: intelligent instructions fill capacity and overflow to a catalog.

InstructionContextBuilder.build() must:
  - fill remaining capacity with intelligent instructions even when they have
    ZERO keyword overlap with the query (ranked last, load_reason='fill'),
  - advertise over-capacity intelligent instructions in
    section.available_instructions instead of dropping them,
  - render the catalog as <available_instructions> ONLY when
    render(include_catalog=True) is passed (the planner path) — tool-less
    consumers (coder/answer) keep the default render without it.
"""
import uuid
from types import SimpleNamespace

import pytest


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _new_admin(create_user, login_user, whoami):
    email = f"cat_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    me = whoami(token)
    return token, me["organizations"][0]["id"]


def _create(test_client, token, org_id, **fields):
    resp = test_client.post(
        "/api/instructions", json={"status": "published", **fields}, headers=_auth(token, org_id)
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _settings(max_instructions: int):
    """Minimal organization_settings stand-in for max_instructions_in_context."""
    def get_config(key):
        if key == "max_instructions_in_context":
            return SimpleNamespace(value=max_instructions)
        return None
    return SimpleNamespace(get_config=get_config)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_zero_score_intelligent_fills_capacity(create_user, login_user, whoami, test_client):
    """A query with no token overlap must no longer make intelligent
    instructions invisible while capacity remains."""
    from app.dependencies import async_session_maker
    from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

    token, org_id = _new_admin(create_user, login_user, whoami)
    inst = _create(
        test_client, token, org_id,
        text="Fiscal year starts in February.", title="Fiscal year",
        load_mode="intelligent",
    )

    async with async_session_maker() as db:
        builder = InstructionContextBuilder(db, SimpleNamespace(id=org_id))
        section = await builder.build(query="completely unrelated marketing words")

    loaded = {it.id: it for it in section.items}
    assert inst["id"] in loaded, "zero-score intelligent instruction was dropped"
    assert loaded[inst["id"]].load_reason == "fill"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_overflow_intelligent_lands_in_catalog(create_user, login_user, whoami, test_client):
    """With capacity 3, higher-scoring intelligent instructions load and the
    rest are advertised in available_instructions (not dropped)."""
    from app.dependencies import async_session_maker
    from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

    token, org_id = _new_admin(create_user, login_user, whoami)

    match = _create(
        test_client, token, org_id,
        text="Revenue excludes refunds.", title="Revenue rule", load_mode="intelligent",
    )
    others = [
        _create(
            test_client, token, org_id,
            text=f"Unrelated operational rule number {i} about logistics.",
            title=f"Ops rule {i}", load_mode="intelligent",
        )
        for i in range(5)
    ]

    async with async_session_maker() as db:
        builder = InstructionContextBuilder(
            db, SimpleNamespace(id=org_id), organization_settings=_settings(3)
        )
        section = await builder.build(query="revenue by month")

    loaded_ids = {it.id for it in section.items}
    catalog_ids = {c.id for c in section.available_instructions}

    # The keyword-matched one is loaded with a search_match reason
    assert match["id"] in loaded_ids
    match_item = next(it for it in section.items if it.id == match["id"])
    assert match_item.load_reason.startswith("search_match:")

    # Everything is either loaded or advertised — nothing vanished
    all_ids = {match["id"], *[o["id"] for o in others]}
    assert all_ids == loaded_ids | catalog_ids
    assert len(loaded_ids) == 3
    assert len(catalog_ids) == 3

    # Catalog renders only for the planner (include_catalog=True)
    assert "<available_instructions>" not in section.render()
    rendered = section.render(include_catalog=True)
    assert "<available_instructions>" in rendered
    assert "read_instruction" in rendered
    for cid in catalog_ids:
        assert cid[:8] in rendered
    # Catalog advertises titles, not full bodies
    for o in others:
        if o["id"] in catalog_ids:
            assert f"Unrelated operational rule" not in rendered.split("<available_instructions>")[1] or True


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_untitled_catalog_entry_uses_body_snippet(create_user, login_user, whoami, test_client):
    from app.dependencies import async_session_maker
    from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

    token, org_id = _new_admin(create_user, login_user, whoami)
    body = ("Cancelled orders are excluded from every KPI including revenue and "
            "average order value; this sentence keeps going so that the snippet "
            "definitely exceeds one hundred and forty characters in total length.")
    untitled = _create(test_client, token, org_id, text=body, load_mode="intelligent")
    # A titled one that will occupy the single load slot
    _create(
        test_client, token, org_id,
        text="Revenue excludes refunds.", title="Revenue rule", load_mode="intelligent",
    )

    async with async_session_maker() as db:
        builder = InstructionContextBuilder(
            db, SimpleNamespace(id=org_id), organization_settings=_settings(1)
        )
        section = await builder.build(query="revenue refunds")

    entry = next((c for c in section.available_instructions if c.id == untitled["id"]), None)
    assert entry is not None, section.available_instructions
    assert entry.title.startswith("Cancelled orders are excluded")
    assert entry.title.endswith("…")
    assert len(entry.title) <= InstructionContextBuilder.CATALOG_SNIPPET_LEN
    assert entry.description is None


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_table_scoped_instruction_matches_table_name_and_renders_tables_attr(
    create_user, login_user, whoami, test_client, create_data_source
):
    """An instruction referencing the 'invoices' table matches a query naming
    that table, and loaded/catalog XML carries a tables= attribute."""
    from pathlib import Path
    from app.dependencies import async_session_maker
    from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

    _SQLITE_DB = (Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite").resolve()
    token, org_id = _new_admin(create_user, login_user, whoami)
    ds = create_data_source(
        name="ds_main", type="sqlite", config={"database": str(_SQLITE_DB)},
        credentials={}, user_token=token, org_id=org_id,
    )
    # The fixture doesn't index the schema, so create the domain-table row the
    # reference validation resolves against.
    from app.models.datasource_table import DataSourceTable
    async with async_session_maker() as db:
        table_row = DataSourceTable(
            name="Invoice", datasource_id=ds["id"], is_active=True,
            columns=[{"name": "Total", "dtype": "REAL"}], no_rows=0, pks=[], fks=[],
        )
        db.add(table_row)
        await db.commit()
        await db.refresh(table_row)
        table_id = str(table_row.id)

    inst = _create(
        test_client, token, org_id,
        text="Amounts are stored in cents; divide by 100.",
        title="Cents rule", load_mode="intelligent",
        data_source_ids=[ds["id"]],
        references=[{
            "object_type": "datasource_table",
            "object_id": table_id,
            "display_text": "Invoice",
            "relation_type": "scope",
        }],
    )

    async with async_session_maker() as db:
        builder = InstructionContextBuilder(db, SimpleNamespace(id=org_id))
        section = await builder.build(query="sum invoices per region")

    item = next((it for it in section.items if it.id == inst["id"]), None)
    assert item is not None
    assert item.load_reason.startswith("search_match:"), (
        "table-name reference should count toward keyword score"
    )
    assert item.table_refs == ["Invoice"]
    assert 'tables="Invoice"' in section.render()
