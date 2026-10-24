"""The dashboards page lists dashboards, one card each (phase 8).

`GET /api/dashboards` replaces the conversation list the page read
(`/reports?has_artifacts=yes`): one row per dashboard, two tabs as before --
"my" (the dashboards I own, published or not) and "shared" (the others I
can open) -- with the page's search, type filter and paging. A dashboard is
listed only if its /d page would open for the reader (the same check), so a
card never leads to "not found". The conversation id is given only to
whoever may edit the dashboard (the card's pencil); readers get the /d link.
"""
import pytest

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share

CONV = "conv_three_dash"


def _list(test_client, corpus, person, **params):
    resp = test_client.get("/api/dashboards", params=params, headers=as_(corpus, person))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ids(body):
    return [d["id"] for d in body["dashboards"]]


@pytest.mark.e2e
def test_my_dashboards_are_one_card_each_published_or_not(corpus, corpus_rows, test_client):
    sales = own(corpus_rows, corpus, f"{CONV}/dash_sales")
    stock = own(corpus_rows, corpus, f"{CONV}/dash_stock")
    deck = own(corpus_rows, corpus, f"{CONV}/deck")
    publish(sales)
    body = _list(test_client, corpus, "owner", filter="my")
    assert set(_ids(body)) == {sales, stock, deck}  # one conversation, three cards
    card = next(d for d in body["dashboards"] if d["id"] == sales)
    assert card["title"] == "לוח מכירות" and card["mode"] == "page" and card["published"] is True
    assert card["is_owner"] is True and card["report_id"] == corpus.reports[CONV]
    assert card["owner"]["id"] == corpus.cast.id("owner")
    assert next(d for d in body["dashboards"] if d["id"] == stock)["published"] is False
    assert body["meta"]["total"] == 3
    # A dashboard the member can open is still not theirs.
    assert share(test_client, corpus, sales, "internal").status_code == 200
    assert _ids(_list(test_client, corpus, "member", filter="my")) == []
    assert _ids(_list(test_client, corpus, "owner", filter="shared")) == []


@pytest.mark.e2e
def test_shared_with_me_lists_what_i_can_open_and_nothing_else(corpus, corpus_rows, test_client):
    sales = own(corpus_rows, corpus, f"{CONV}/dash_sales")
    stock = own(corpus_rows, corpus, f"{CONV}/dash_stock")
    deck = own(corpus_rows, corpus, f"{CONV}/deck")
    for aid in (sales, stock):
        publish(aid)
    assert share(test_client, corpus, sales, "internal").status_code == 200
    assert share(test_client, corpus, stock, "shared", users=["direct_share"]).status_code == 200
    corpus_rows.update("artifacts", deck, visibility="internal")  # never published: opens to nobody
    member = _list(test_client, corpus, "member", filter="shared")
    assert _ids(member) == [sales]
    card = member["dashboards"][0]
    assert card["is_owner"] is False and card["report_id"] is None  # readers get the /d link only
    assert set(_ids(_list(test_client, corpus, "direct_share", filter="shared"))) == {sales, stock}
    assert _ids(_list(test_client, corpus, "owner", filter="shared")) == []  # mine are under "my"
    for aid in _ids(member):  # every card opens
        assert test_client.get(f"/api/d/{aid}", headers=as_(corpus, "member")).status_code == 200


@pytest.mark.e2e
def test_a_projects_dashboards_are_shared_with_its_members(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, f"{CONV}/dash_sales", project="private")
    body = _list(test_client, corpus, "proj_viewer", filter="shared")
    assert _ids(body) == [aid]  # its editors open it, published or not (the draft)
    # The pencil opens the reader's own editing conversation (collab01): the
    # project member has none yet, so the page asks /edit for one.
    card = body["dashboards"][0]
    assert card["report_id"] is None and card["role"] == "edit"
    assert _ids(_list(test_client, corpus, "member", filter="shared")) == []


@pytest.mark.e2e
def test_search_type_and_paging(corpus, corpus_rows, test_client):
    sales = own(corpus_rows, corpus, f"{CONV}/dash_sales")
    stock = own(corpus_rows, corpus, f"{CONV}/dash_stock")
    deck = own(corpus_rows, corpus, f"{CONV}/deck")
    assert _ids(_list(test_client, corpus, "owner", filter="my", search="מלאי")) == [stock]
    assert set(_ids(_list(test_client, corpus, "owner", filter="my", mode="page"))) == {sales, stock}
    assert _ids(_list(test_client, corpus, "owner", filter="my", mode="slides")) == [deck]
    first = _list(test_client, corpus, "owner", filter="my", limit=2, page=1)
    second = _list(test_client, corpus, "owner", filter="my", limit=2, page=2)
    assert first["meta"] == {"total": 3, "page": 1, "limit": 2, "total_pages": 2, "has_next": True, "has_prev": False}
    assert len(first["dashboards"]) == 2 and len(second["dashboards"]) == 1
    assert set(_ids(first) + _ids(second)) == {sales, stock, deck}


@pytest.mark.e2e
def test_a_deleted_dashboard_is_not_listed(corpus, corpus_rows, test_client):
    from datetime import datetime

    sales = own(corpus_rows, corpus, f"{CONV}/dash_sales")
    corpus_rows.update("artifacts", sales, deleted_at=datetime(2026, 3, 1))
    assert sales not in _ids(_list(test_client, corpus, "owner", filter="my"))


@pytest.mark.e2e
def test_the_list_needs_a_member_of_the_organization(corpus, test_client):
    assert test_client.get("/api/dashboards", headers=as_(corpus, "anonymous")).status_code == 401


@pytest.mark.e2e
def test_search_matches_the_words_typed_not_patterns(corpus, corpus_rows, test_client):
    own(corpus_rows, corpus, f"{CONV}/dash_sales")
    own(corpus_rows, corpus, f"{CONV}/dash_stock")
    assert _ids(_list(test_client, corpus, "owner", filter="my", search="%")) == []
    assert _ids(_list(test_client, corpus, "owner", filter="my", search="_")) == []


@pytest.mark.e2e
def test_versions_from_before_their_dashboard_tie_list_as_one_card(migrated_corpus, test_client):
    """artver01 made every old version a dashboard of its own (same id): its
    owner sees one card per kind of each conversation, its newest -- as the
    editor and the link showed one dashboard (decision 4, by the list, not by
    changing the rows)."""
    c = migrated_corpus
    ids = {d["id"] for d in _list(test_client, c, "owner", search="לוח מכירות", limit=100)["dashboards"]}
    assert c.ids["artver01_chain/legacy_3"] in ids
    assert not {c.ids["artver01_chain/legacy_1"], c.ids["artver01_chain/legacy_2"]} & ids
    # A dashboard with a chain of its own is listed as ever.
    assert c.ids["legacy_grown/old"] in ids and c.ids["dash_many_versions/dash"] in ids


@pytest.mark.e2e
def test_the_check_runs_only_on_the_page_shown(migrated_corpus, test_client, monkeypatch):
    """The SQL narrows and pages; the per-dashboard check guards only the
    cards returned -- not every candidate (PR #1455 review). The total is
    the SQL's, beyond any candidate cap."""
    from app.services import dashboard_access

    c = migrated_corpus
    everything = _list(test_client, c, "owner", filter="my", limit=100)["meta"]["total"]
    assert everything > 3
    checked = []
    admit = dashboard_access.admit_reader

    async def counting(db, artifact, user):
        checked.append(str(artifact.id))
        return await admit(db, artifact, user)

    monkeypatch.setattr(dashboard_access, "admit_reader", counting)
    page = _list(test_client, c, "owner", filter="my", limit=2, page=2)
    assert len(page["dashboards"]) == 2 and page["meta"]["total"] == everything
    assert len(checked) <= 2


@pytest.mark.e2e
def test_a_dashboard_whose_every_version_failed_is_not_listed(corpus, corpus_rows, test_client):
    """create_artifact keeps a dashboard whose render validation failed (its
    one version `failed`) for debugging; it is not one anyone built, so no
    card -- until a version works, or something went live from it."""
    sales = own(corpus_rows, corpus, f"{CONV}/dash_sales")
    stock = own(corpus_rows, corpus, f"{CONV}/dash_stock")
    versions = corpus_rows.table("artifact_versions")
    with corpus_rows.engine.begin() as conn:
        conn.execute(versions.update().where(versions.c.artifact_id == sales).values(status="failed"))
        ids = [r[0] for r in conn.execute(versions.select().with_only_columns(versions.c.id)
                                          .where(versions.c.artifact_id == sales))]
    assert ids  # the dashboard has versions, all failed
    assert sales not in _ids(_list(test_client, corpus, "owner", filter="my"))
    assert stock in _ids(_list(test_client, corpus, "owner", filter="my"))
    corpus_rows.update("artifact_versions", ids[0], status="completed")
    assert sales in _ids(_list(test_client, corpus, "owner", filter="my"))
