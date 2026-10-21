"""E2E tests: smart instructions behave like skills — catalog only.

InstructionContextBuilder.build() must:
  - NEVER inline an intelligent (smart) instruction, whatever the query and
    however well it matches; 'always' instructions still load in full,
  - advertise every smart instruction in section.available_instructions with
    its description, references and size, so the LLM decides what to read,
  - list strong matches (a query keyword hits the title / labels / tables)
    first, flagged matched="title|labels|tables"; body-only matches are not
    flagged,
  - not pull a smart instruction in through a reference from a loaded one,
  - render the catalog only for render(include_catalog=True) (the planner),
    and give tool-less consumers (render()) the smart instructions the agent
    READ (section.read_items) instead — the planner never gets those twice.
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


async def _build(org_id, query):
    from app.dependencies import async_session_maker
    from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

    async with async_session_maker() as db:
        return await InstructionContextBuilder(db, SimpleNamespace(id=org_id)).build(query=query)


def _catalog(section):
    return {c.id: c for c in section.available_instructions}


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("query", [None, "revenue by region last quarter", "completely unrelated words"])
async def test_smart_instructions_are_never_inlined(create_user, login_user, whoami, test_client, query):
    """Whatever the query (none, matching, unrelated), smart instructions are
    advertised, never loaded; 'always' instructions load in full."""
    token, org_id = _new_admin(create_user, login_user, whoami)
    smart = [
        _create(test_client, token, org_id, title="Revenue rule", load_mode="intelligent",
                text="Revenue excludes refunds."),
        _create(test_client, token, org_id, title="Region mapping", load_mode="intelligent",
                text="EMEA includes Israel."),
        _create(test_client, token, org_id, title="Fiscal year", load_mode="intelligent",
                text="Fiscal year starts in February."),
    ]
    always = _create(test_client, token, org_id, title="Currency", load_mode="always",
                     text="Always answer in USD.")

    section = await _build(org_id, query)

    loaded_ids = {it.id for it in section.items}
    assert always["id"] in loaded_ids
    smart_ids = {s["id"] for s in smart}
    assert not (smart_ids & loaded_ids), "smart instructions must never be inlined"
    assert smart_ids <= set(_catalog(section)), "every smart instruction must be advertised"
    assert "Revenue excludes refunds" not in section.render()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_strong_matches_listed_first_and_flagged(create_user, login_user, whoami, test_client):
    """Title / label matches are listed first with matched=...; a body-only
    match is advertised without the flag, after them."""
    token, org_id = _new_admin(create_user, login_user, whoami)
    label_name = f"churn{uuid.uuid4().hex[:4]}"
    resp = test_client.post("/api/instructions/labels", json={"name": label_name},
                            headers=_auth(token, org_id))
    assert resp.status_code == 200, resp.json()

    body_only = _create(test_client, token, org_id, title="Net figures", load_mode="intelligent",
                        text="Net figures rule.\nAlways exclude refunds and chargebacks.")
    by_label = _create(test_client, token, org_id, title="Inactivity window", load_mode="intelligent",
                       text="Inactive after 60 days.", label_ids=[resp.json()["id"]])
    by_title = _create(test_client, token, org_id, title="Refunds policy", load_mode="intelligent",
                       text="Refunds are booked on the return date.")
    for i in range(3):
        _create(test_client, token, org_id, title=f"Ops rule {i}", load_mode="intelligent",
                text=f"Unrelated logistics rule {i}.")

    section = await _build(org_id, f"refunds and {label_name} by month")

    order = [c.id for c in section.available_instructions]
    cat = _catalog(section)
    assert set(order[:2]) == {by_title["id"], by_label["id"]}, "strong matches must be listed first"
    assert cat[by_title["id"]].matched == "title"
    assert cat[by_label["id"]].matched == "labels"
    assert cat[body_only["id"]].matched is None, "a body-only match is not a strong match"
    assert all(cat[i].tokens for i in order), "entries carry their size"
    rendered = section.render(include_catalog=True)
    assert 'matched="title"' in rendered
    assert "chargebacks" not in rendered, "catalog must not carry the full body"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_catalog_entry_carries_description_and_references(
    create_user, login_user, whoami, test_client
):
    """Catalog entries show the description and non-table references
    (e.g. linked instructions) so the agent can decide what to open."""
    token, org_id = _new_admin(create_user, login_user, whoami)
    target = _create(test_client, token, org_id, text="Revenue excludes refunds.",
                     title="Revenue definition", load_mode="always")
    linked = _create(
        test_client, token, org_id,
        text="Gross margin is revenue minus cost of goods sold.",
        title="Gross margin", description="How gross margin is computed",
        load_mode="intelligent",
        references=[{"object_type": "instruction", "object_id": target["id"],
                     "display_text": "Revenue definition", "relation_type": "scope"}],
    )

    section = await _build(org_id, "weekly active users")

    entry = _catalog(section).get(linked["id"])
    assert entry is not None
    assert entry.description == "How gross margin is computed"
    assert "instruction:Revenue definition" in entry.other_refs
    assert 'refs="instruction:Revenue definition"' in section.render(include_catalog=True)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_untitled_catalog_entry_uses_body_snippet(create_user, login_user, whoami, test_client):
    from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

    token, org_id = _new_admin(create_user, login_user, whoami)
    body = ("Cancelled orders are excluded from every KPI including revenue and "
            "average order value; this sentence keeps going so that the snippet "
            "definitely exceeds one hundred and forty characters in total length.")
    untitled = _create(test_client, token, org_id, text=body, load_mode="intelligent")

    section = await _build(org_id, "revenue refunds")

    entry = _catalog(section).get(untitled["id"])
    assert entry is not None, section.available_instructions
    assert entry.title.startswith("Cancelled orders are excluded")
    assert entry.title.endswith("…")
    assert len(entry.title) <= InstructionContextBuilder.CATALOG_SNIPPET_LEN
    assert entry.description is None


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_table_scoped_instruction_matches_table_name(
    create_user, login_user, whoami, test_client, create_data_source
):
    """An instruction referencing the 'Invoice' table is flagged
    matched="tables" for a query naming that table, with a tables= attribute —
    and still not inlined."""
    from pathlib import Path
    from app.dependencies import async_session_maker
    from app.models.datasource_table import DataSourceTable

    sqlite_db = (Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite").resolve()
    token, org_id = _new_admin(create_user, login_user, whoami)
    ds = create_data_source(name="ds_main", type="sqlite", config={"database": str(sqlite_db)},
                            credentials={}, user_token=token, org_id=org_id)
    # The fixture doesn't index the schema, so create the domain-table row the
    # reference validation resolves against.
    async with async_session_maker() as db:
        table_row = DataSourceTable(name="Invoice", datasource_id=ds["id"], is_active=True,
                                    columns=[{"name": "Total", "dtype": "REAL"}], no_rows=0, pks=[], fks=[])
        db.add(table_row)
        await db.commit()
        await db.refresh(table_row)
        table_id = str(table_row.id)

    inst = _create(
        test_client, token, org_id, text="Amounts are stored in cents; divide by 100.",
        title="Cents rule", load_mode="intelligent", data_source_ids=[ds["id"]],
        references=[{"object_type": "datasource_table", "object_id": table_id,
                     "display_text": "Invoice", "relation_type": "scope"}],
    )

    section = await _build(org_id, "sum invoices per region")

    assert inst["id"] not in {it.id for it in section.items}
    entry = _catalog(section)[inst["id"]]
    assert entry.matched == "tables"
    assert entry.table_refs == ["Invoice"]
    assert 'tables="Invoice"' in section.render(include_catalog=True)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_smart_instruction_not_pulled_in_by_reference(create_user, login_user, whoami, test_client):
    """A loaded 'always' instruction that references smart instructions does
    not drag their text in; they stay in the catalog."""
    token, org_id = _new_admin(create_user, login_user, whoami)
    deps = [
        _create(test_client, token, org_id, title="Close process", load_mode="intelligent",
                text=f"Close process detail {uuid.uuid4().hex}"),
        _create(test_client, token, org_id, title="Fiscal calendar", load_mode="intelligent",
                text="Fiscal year starts in February."),
    ]
    _create(
        test_client, token, org_id, title="Finance basics", load_mode="always",
        text="Follow the finance rules.",
        references=[{"object_type": "instruction", "object_id": d["id"], "display_text": d["title"],
                     "relation_type": "scope"} for d in deps],
    )

    section = await _build(org_id, "weekly active users")

    loaded_ids = {it.id for it in section.items}
    for d in deps:
        assert d["id"] not in loaded_ids
        assert d["id"] in _catalog(section)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_read_items_go_to_tool_less_consumers_only(create_user, login_user, whoami, test_client):
    """Smart instructions the agent read are rendered in full for tool-less
    consumers (render()) and omitted from the planner render, which already
    has them from its own read."""
    from app.ai.context.sections.instructions_section import InstructionItem

    token, org_id = _new_admin(create_user, login_user, whoami)
    marker = f"READ{uuid.uuid4().hex[:8]}"
    inst = _create(test_client, token, org_id, title="Cents rule", load_mode="intelligent",
                   text=f"Cents rule.\nAmounts are in cents. {marker}")

    section = await _build(org_id, "anything")
    assert marker not in section.render()

    section.read_items = [InstructionItem(id=inst["id"], category="general",
                                          text=f"Amounts are in cents. {marker}",
                                          load_mode="intelligent", title="Cents rule")]
    assert marker in section.render(), "tool-less consumers get what the agent read"
    assert marker not in section.render(include_catalog=True), "planner must not get it twice"
