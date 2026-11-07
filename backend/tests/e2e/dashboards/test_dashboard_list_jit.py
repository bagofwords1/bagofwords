"""On Postgres the dashboards list and its facets run without JIT
compilation (P12): on a big organization, compiling their wide plans cost
more than running them.

The setting is the transaction's own (`SET LOCAL`), so the service
functions behind GET /api/artifacts and /api/artifacts/facets are called
here directly, in one transaction, as their routes call them: `jit` is on
before the call and still off after it -- set by the call, and no commit in
it has ended the transaction. SQLite has no JIT.
"""
import asyncio

import pytest
from sqlalchemy import text

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.services.dashboard_list import ListQuery, list_dashboards, list_facets
from tests.e2e.dashboards.dashboard_setup import own, publish, share

CALLS = {"list": list_dashboards, "facets": list_facets}


async def _jit_around(call, user_id: str, org_id: str, query: ListQuery) -> tuple[str, str, dict]:
    async with async_session_maker() as db:
        # As a server with JIT on: the test container's own default must not
        # decide the outcome. LOCAL: it ends with this transaction.
        await db.execute(text("SET LOCAL jit = on"))
        before = (await db.execute(text("SHOW jit"))).scalar()
        user, organization = await db.get(User, user_id), await db.get(Organization, org_id)
        result = await call(db, user, organization, query)
        after = (await db.execute(text("SHOW jit"))).scalar()
        await db.rollback()
    return before, after, result


@pytest.mark.e2e
@pytest.mark.parametrize("name", sorted(CALLS))
@pytest.mark.parametrize("tab", ["my", "shared"])
def test_the_list_and_its_facets_turn_jit_off_for_their_transaction(name, tab, corpus, corpus_rows, test_client):
    if corpus_rows.engine.dialect.name != "postgresql":
        pytest.skip("JIT is a Postgres setting; SQLite has none")
    aid = own(corpus_rows, corpus, "conv_three_dash/dash_sales")
    publish(aid)
    assert share(test_client, corpus, aid, "shared", users=["direct_share"]).status_code == 200
    person = "owner" if tab == "my" else "direct_share"

    before, after, result = asyncio.run(_jit_around(
        CALLS[name], corpus.cast.id(person), corpus.cast.org_id, ListQuery(filter=tab)))

    assert before == "on"
    assert after == "off", f"{name} on {tab}: jit is {after} after the call"
    # The call ran its real path: the shared dashboard is there.
    if name == "list":
        assert aid in {card["id"] for card in result["dashboards"]}
    else:
        assert result["tabs"][tab] >= 1
