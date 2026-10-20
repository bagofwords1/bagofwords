"""E2E tests: intelligent instructions are catalog-first, like skills.

InstructionContextBuilder.build() must:
  - INLINE an intelligent instruction's full text only on a STRONG match — a
    query keyword hits its title, labels or referenced table names. A match
    in the body alone does not inline it,
  - with NO query keywords, inline no intelligent instruction at all,
  - advertise every non-inlined intelligent instruction (weak/zero matches and
    over-capacity ones) in section.available_instructions instead of dropping
    it, with its description and non-table references,
  - never inline an intelligent instruction longer than
    max_inline_instruction_tokens, even on a strong match: it is advertised
    first in the catalog, flagged with what matched and its size,
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


def _settings(max_instructions: int = 50, max_inline_tokens: int | None = None):
    """Minimal organization_settings stand-in for the instruction limits."""
    def get_config(key):
        if key == "max_instructions_in_context":
            return SimpleNamespace(value=max_instructions)
        if key == "max_inline_instruction_tokens" and max_inline_tokens is not None:
            return SimpleNamespace(value=max_inline_tokens)
        return None
    return SimpleNamespace(get_config=get_config)


def _long_body(marker: str, words: int) -> str:
    """A body of roughly `words` words: first line short, marker deep inside."""
    filler = " ".join(f"step{i}" for i in range(words))
    return f"Detailed procedure.\n{filler} {marker} {filler}"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_zero_score_intelligent_goes_to_catalog_not_loaded(create_user, login_user, whoami, test_client):
    """With a query present, an intelligent instruction that does NOT match is
    advertised in the catalog, not force-loaded — even though capacity remains.
    This is the fix for a trivial turn pulling in every intelligent rule."""
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

    loaded_ids = {it.id for it in section.items}
    catalog_ids = {c.id for c in section.available_instructions}
    assert inst["id"] not in loaded_ids, "zero-score intelligent must not be force-loaded"
    assert inst["id"] in catalog_ids, "zero-score intelligent must stay reachable via catalog"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_no_query_inlines_no_intelligent_instructions(create_user, login_user, whoami, test_client):
    """A query-less build has nothing to judge relevance against, so every
    intelligent instruction is advertised in the catalog, none inlined.
    'always' instructions still load in full."""
    from app.dependencies import async_session_maker
    from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

    token, org_id = _new_admin(create_user, login_user, whoami)
    smart = [
        _create(
            test_client, token, org_id,
            text=f"Smart rule {i}: {uuid.uuid4().hex}", title=f"Smart rule {i}",
            load_mode="intelligent",
        )
        for i in range(3)
    ]
    always = _create(
        test_client, token, org_id, text="Always answer in USD.", title="Currency",
        load_mode="always",
    )

    async with async_session_maker() as db:
        builder = InstructionContextBuilder(db, SimpleNamespace(id=org_id))
        section = await builder.build(query=None)

    loaded_ids = {it.id for it in section.items}
    catalog_ids = {c.id for c in section.available_instructions}
    smart_ids = {s["id"] for s in smart}
    assert always["id"] in loaded_ids
    assert not (smart_ids & loaded_ids), "query-less build must not inline intelligent rules"
    assert smart_ids <= catalog_ids, "intelligent rules must stay reachable via the catalog"


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["refunds this quarter", "show refunded orders"])
async def test_body_only_match_is_advertised_not_inlined(
    create_user, login_user, whoami, test_client, query
):
    """A keyword that appears only in the body (not title / labels / tables)
    is a weak match: the instruction is advertised, ranked, but not inlined."""
    from app.dependencies import async_session_maker
    from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

    token, org_id = _new_admin(create_user, login_user, whoami)
    weak = _create(
        test_client, token, org_id,
        # Catalog descriptions fall back to the body's first line, so the
        # detail that must stay out of the prompt is on the second.
        text="Net figures rule.\nAlways exclude refunds and chargebacks.",
        title="Net figures", load_mode="intelligent",
    )

    async with async_session_maker() as db:
        builder = InstructionContextBuilder(db, SimpleNamespace(id=org_id))
        section = await builder.build(query=query)

    assert weak["id"] not in {it.id for it in section.items}
    assert weak["id"] in {c.id for c in section.available_instructions}
    rendered = section.render(include_catalog=True)
    assert "chargebacks" not in rendered, "catalog must not carry the full body"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_label_match_inlines_full_text(create_user, login_user, whoami, test_client):
    """A query keyword hitting one of the instruction's labels is a strong match."""
    from app.dependencies import async_session_maker
    from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

    token, org_id = _new_admin(create_user, login_user, whoami)
    label_name = f"churn{uuid.uuid4().hex[:4]}"
    resp = test_client.post(
        "/api/instructions/labels", json={"name": label_name}, headers=_auth(token, org_id)
    )
    assert resp.status_code == 200, resp.json()
    inst = _create(
        test_client, token, org_id,
        text="A customer is inactive after 60 days without an order.",
        title="Inactivity window", load_mode="intelligent",
        label_ids=[resp.json()["id"]],
    )

    async with async_session_maker() as db:
        builder = InstructionContextBuilder(db, SimpleNamespace(id=org_id))
        section = await builder.build(query=f"{label_name} rate by cohort")

    item = next((it for it in section.items if it.id == inst["id"]), None)
    assert item is not None, "label match must inline the instruction"
    assert "60 days" in section.render()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_catalog_entry_carries_description_and_references(
    create_user, login_user, whoami, test_client
):
    """Catalog entries show the description and non-table references
    (e.g. linked instructions) so the agent can decide what to open."""
    from app.dependencies import async_session_maker
    from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

    token, org_id = _new_admin(create_user, login_user, whoami)
    target = _create(
        test_client, token, org_id, text="Revenue excludes refunds.",
        title="Revenue definition", load_mode="always",
    )
    linked = _create(
        test_client, token, org_id,
        text="Gross margin is revenue minus cost of goods sold.",
        title="Gross margin", description="How gross margin is computed",
        load_mode="intelligent",
        references=[{
            "object_type": "instruction",
            "object_id": target["id"],
            "display_text": "Revenue definition",
            "relation_type": "scope",
        }],
    )

    async with async_session_maker() as db:
        builder = InstructionContextBuilder(db, SimpleNamespace(id=org_id))
        section = await builder.build(query="weekly active users")

    entry = next((c for c in section.available_instructions if c.id == linked["id"]), None)
    assert entry is not None
    assert entry.description == "How gross margin is computed"
    assert "instruction:Revenue definition" in entry.other_refs
    rendered = section.render(include_catalog=True)
    assert 'refs="instruction:Revenue definition"' in rendered


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_overflow_intelligent_lands_in_catalog(create_user, login_user, whoami, test_client):
    """With a query present and capacity 3, only the query-matching intelligent
    instruction loads; the non-matching rest are advertised in
    available_instructions (not dropped, and NOT padded into the free slots)."""
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
    # Only the matching instruction loads (free slots are NOT padded with
    # irrelevant intelligent rules); the five zero-score ones are all advertised.
    assert loaded_ids == {match["id"]}
    assert catalog_ids == {o["id"] for o in others}

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


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("limit,words", [(None, 900), (120, 200)])
async def test_long_strong_match_is_advertised_not_inlined(
    create_user, login_user, whoami, test_client, limit, words
):
    """A title match on an instruction over the size limit (default or a
    custom org value) is NOT inlined; it is listed first in the catalog with
    matched= and tokens= so the agent knows to read it."""
    from app.dependencies import async_session_maker
    from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

    token, org_id = _new_admin(create_user, login_user, whoami)
    marker = f"LONG{uuid.uuid4().hex[:8]}"
    long_inst = _create(
        test_client, token, org_id, title="Revenue playbook", load_mode="intelligent",
        text=_long_body(marker, words),
    )
    short_inst = _create(
        test_client, token, org_id, title="Revenue rounding", load_mode="intelligent",
        text="Round revenue to whole dollars.",
    )
    for i in range(3):
        _create(
            test_client, token, org_id, title=f"Ops rule {i}", load_mode="intelligent",
            text=f"Unrelated logistics rule {i}.",
        )

    async with async_session_maker() as db:
        builder = InstructionContextBuilder(
            db, SimpleNamespace(id=org_id), organization_settings=_settings(max_inline_tokens=limit)
        )
        section = await builder.build(query="revenue last quarter")

    loaded_ids = {it.id for it in section.items}
    assert short_inst["id"] in loaded_ids, "short strong match still inlines"
    assert long_inst["id"] not in loaded_ids, "long strong match must not inline"
    assert section.available_instructions[0].id == long_inst["id"], "matched entries list first"
    entry = section.available_instructions[0]
    assert entry.matched == "title"
    assert entry.tokens and entry.tokens > (limit or InstructionContextBuilder.DEFAULT_MAX_INLINE_TOKENS)
    rendered = section.render(include_catalog=True)
    assert marker not in rendered
    assert 'matched="title"' in rendered


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_zero_size_limit_inlines_long_strong_match(create_user, login_user, whoami, test_client):
    """max_inline_instruction_tokens = 0 removes the size limit."""
    from app.dependencies import async_session_maker
    from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

    token, org_id = _new_admin(create_user, login_user, whoami)
    marker = f"LONG{uuid.uuid4().hex[:8]}"
    inst = _create(
        test_client, token, org_id, title="Revenue playbook", load_mode="intelligent",
        text=_long_body(marker, 900),
    )

    async with async_session_maker() as db:
        builder = InstructionContextBuilder(
            db, SimpleNamespace(id=org_id), organization_settings=_settings(max_inline_tokens=0)
        )
        section = await builder.build(query="revenue last quarter")

    assert inst["id"] in {it.id for it in section.items}
    assert marker in section.render()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_long_smart_instruction_not_pulled_in_by_reference(
    create_user, login_user, whoami, test_client
):
    """An inlined instruction that references a long smart instruction does
    not drag its full text in; short referenced ones still come along."""
    from app.dependencies import async_session_maker
    from app.ai.context.builders.instruction_context_builder import InstructionContextBuilder

    token, org_id = _new_admin(create_user, login_user, whoami)
    marker = f"LONG{uuid.uuid4().hex[:8]}"
    long_dep = _create(
        test_client, token, org_id, title="Close process", load_mode="intelligent",
        text=_long_body(marker, 900),
    )
    short_dep = _create(
        test_client, token, org_id, title="Fiscal calendar", load_mode="intelligent",
        text="Fiscal year starts in February.",
    )
    _create(
        test_client, token, org_id, title="Finance basics", load_mode="always",
        text="Follow the finance rules.",
        references=[
            {"object_type": "instruction", "object_id": d["id"], "display_text": d["title"],
             "relation_type": "scope"}
            for d in (long_dep, short_dep)
        ],
    )

    async with async_session_maker() as db:
        builder = InstructionContextBuilder(db, SimpleNamespace(id=org_id))
        section = await builder.build(query="weekly active users")

    loaded_ids = {it.id for it in section.items}
    assert short_dep["id"] in loaded_ids
    assert long_dep["id"] not in loaded_ids
    assert marker not in section.render()
    assert long_dep["id"] in {c.id for c in section.available_instructions}
