"""Finding a dashboard on the dashboards page (dashboards list findability, P4).

`GET /api/artifacts` narrows and orders the list in SQL:

- `access` (Shared only): the reason the reader has the dashboard -- a
  share to them (`direct`), to one of their groups (`group`), its project
  (`project`), the organization (`org`), or that they may edit it (`edit`).
  It only narrows what Shared lists; it never lets in more.
- `owner_ids`, `created_*` / `edited_*` ranges (from inclusive, to
  exclusive), `starred`, and a `search` that also matches the owner's name
  or email.
- `status` (My only): published, published with unpublished changes, or a
  draft -- one of the three for every card. On Shared it is accepted and
  ignored, so a view-only reader cannot learn of unpublished changes.
- The order: starred first; on Shared with every reason, direct shares,
  then group and project, then the organization's; then the chosen `sort`;
  then the id, so pages never overlap.

Times far in the past, published times and blank titles are written
directly where no route makes them (each place says so).
"""
import asyncio
import uuid
from datetime import datetime

import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import as_, own, publish

CONV = "conv_three_dash"
SALES, STOCK, DECK = f"{CONV}/dash_sales", f"{CONV}/dash_stock", f"{CONV}/deck"
ONE = "conv_one_dash/dash"
PAGE_DASH = "doc_after_page/dash"
PAGE_DOC = "doc_after_page/doc"
MANY = "dash_many_versions/dash"
SORTS = ("edited", "created", "-created", "title", "published")
CHIPS = ("all", "direct", "group", "project", "org", "edit")


# ── helpers ───────────────────────────────────────────────────────────────

def _get(test_client, corpus, person, **params):
    return test_client.get("/api/artifacts", params=params, headers=as_(corpus, person))


def _list(test_client, corpus, person, **params) -> dict:
    resp = _get(test_client, corpus, person, **params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ids(test_client, corpus, person, **params) -> list[str]:
    params.setdefault("limit", 100)
    body = _list(test_client, corpus, person, **params)
    assert body["meta"]["total"] <= 100
    return [d["id"] for d in body["dashboards"]]


def _sharing(test_client, corpus, aid, visibility, *, users=(), groups=(), editors=(), editor_groups=(),
             by="owner"):
    resp = test_client.put(f"/api/artifacts/{aid}/sharing", headers=as_(corpus, by), json={
        "visibility": visibility,
        "user_ids": [corpus.cast.id(u) for u in users],
        "group_ids": [corpus.cast.groups[g] for g in groups],
        "editor_user_ids": [corpus.cast.id(u) for u in editors],
        "editor_group_ids": [corpus.cast.groups[g] for g in editor_groups],
    })
    assert resp.status_code == 200, resp.text


def _date(corpus_rows, aid, *, edited=None, created=None):
    # Written directly: no route dates a dashboard; distinct, known times make
    # the expected order a literal.
    values = {}
    if edited is not None:
        values["content_updated_at"] = edited
    if created is not None:
        values["created_at"] = created
    corpus_rows.update("artifacts", aid, **values)


def _rows(corpus_rows, sql, **params):
    with corpus_rows.engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def _star(test_client, corpus, person, aid):
    resp = test_client.post(f"/api/artifacts/{aid}/star", headers=as_(corpus, person))
    assert resp.status_code == 200, resp.text


def _reasons(test_client, corpus, corpus_rows) -> dict:
    """One dashboard per reason:
    - sales: shared with direct_share by name;
    - stock: shared with the analysts group (group_share, proj_group);
    - deck: in the private project (proj_viewer; the analysts by a group grant);
    - one: the organization's (internal);
    - many: member may edit it (never published)."""
    sales, stock = own(corpus_rows, corpus, SALES), own(corpus_rows, corpus, STOCK)
    deck = own(corpus_rows, corpus, DECK, project="private")
    one, many = own(corpus_rows, corpus, ONE), own(corpus_rows, corpus, MANY)
    for aid in (sales, stock, one):
        publish(aid)
    _sharing(test_client, corpus, sales, "shared", users=["direct_share"])
    _sharing(test_client, corpus, stock, "shared", groups=["analysts"])
    _sharing(test_client, corpus, one, "internal")
    _sharing(test_client, corpus, many, "none", editors=["member"])
    return {"sales": sales, "stock": stock, "deck": deck, "one": one, "many": many}


# ── access ────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_each_access_chip_keeps_only_its_reason(corpus, corpus_rows, test_client):
    d = _reasons(test_client, corpus, corpus_rows)
    expected = {
        "direct_share": {"all": {"sales", "one"}, "direct": {"sales"}, "group": set(), "project": set(),
                         "org": {"one"}, "edit": set()},
        # The analysts group may also view the private project.
        "group_share": {"all": {"stock", "deck", "one"}, "direct": set(), "group": {"stock"},
                        "project": {"deck"}, "org": {"one"}, "edit": {"deck"}},
        "proj_viewer": {"all": {"deck", "one"}, "direct": set(), "group": set(), "project": {"deck"},
                        "org": {"one"}, "edit": {"deck"}},
        # An edit grant by name is a direct share too.
        "member": {"all": {"one", "many"}, "direct": {"many"}, "group": set(), "project": set(),
                   "org": {"one"}, "edit": {"many"}},
        "proj_group": {"all": {"stock", "deck", "one"}, "direct": set(), "group": {"stock"},
                       "project": {"deck"}, "org": {"one"}, "edit": {"deck"}},
    }
    for person, chips in expected.items():
        for chip, keys in chips.items():
            body = _list(test_client, corpus, person, filter="shared", access=chip, limit=100)
            assert {c["id"] for c in body["dashboards"]} == {d[k] for k in keys}, (person, chip)
            # Nothing here is refused by the per-card check, so the SQL itself
            # must narrow to exactly these: a wider admission shows in the total.
            assert body["meta"]["total"] == len(body["dashboards"]), (person, chip)


@pytest.mark.e2e
def test_access_is_ignored_on_my(corpus, corpus_rows, test_client):
    d = _reasons(test_client, corpus, corpus_rows)
    mine = set(_ids(test_client, corpus, "owner", filter="my"))
    assert mine == set(d.values())
    for chip in CHIPS:
        assert set(_ids(test_client, corpus, "owner", filter="my", access=chip)) == mine, chip


# ── order ─────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_shared_all_lists_direct_then_group_and_project_then_org_each_in_the_chosen_sort(
        corpus, corpus_rows, test_client):
    sales, stock = own(corpus_rows, corpus, SALES), own(corpus_rows, corpus, STOCK)
    deck = own(corpus_rows, corpus, DECK, project="private")
    one, page = own(corpus_rows, corpus, ONE), own(corpus_rows, corpus, PAGE_DASH)
    for aid in (sales, stock, one, page):
        publish(aid)  # released 2026-04-01, after every content time below
    _sharing(test_client, corpus, sales, "shared", users=["proj_group"])  # direct
    _sharing(test_client, corpus, stock, "shared", groups=["analysts"])  # group
    _sharing(test_client, corpus, deck, "none")  # its project's alone
    _sharing(test_client, corpus, one, "internal")  # org
    _sharing(test_client, corpus, page, "internal")  # org
    # The newer the edit, the lower the reason: only the rank puts them in order.
    # proj_group may only view the others: their Edited is the earlier of the
    # content and publish times -- here the content time (review #1).
    _date(corpus_rows, sales, edited=datetime(2026, 1, 1), created=datetime(2026, 2, 5))
    _date(corpus_rows, deck, edited=datetime(2026, 1, 2), created=datetime(2026, 2, 2))
    _date(corpus_rows, stock, edited=datetime(2026, 1, 3), created=datetime(2026, 2, 1))
    _date(corpus_rows, page, edited=datetime(2026, 1, 4), created=datetime(2026, 2, 3))
    _date(corpus_rows, one, edited=datetime(2026, 1, 5), created=datetime(2026, 2, 4))
    # proj_group may edit `deck` (its project): Edited is its content time,
    # even with a release from before that edit (a direct write: no route
    # back-dates a publish).
    corpus_rows.update("artifact_releases", publish(deck), published_at=datetime(2025, 12, 15))

    def order(**params):
        return _ids(test_client, corpus, "proj_group", filter="shared", **params)

    (card,) = [c for c in _cards(test_client, corpus, "proj_group", filter="shared") if c["id"] == deck]
    assert (card["role"], datetime.fromisoformat(card["edited_at"])) == ("edit", datetime(2026, 1, 2))

    assert order() == [sales, stock, deck, one, page]  # default: Edited, newest first
    assert order(access="all", sort="edited") == [sales, stock, deck, one, page]
    assert order(sort="-created") == [sales, stock, deck, page, one]  # oldest first
    assert order(sort="created") == [sales, deck, stock, one, page]  # newest first
    # A single reason has no rank: the sort alone.
    assert order(access="org", sort="-created") == [page, one]


@pytest.mark.e2e
def test_a_view_only_readers_edited_sort_and_filter_use_the_publish_time(corpus, corpus_rows, test_client):
    """Edited is per reader (review #1): for its editors the content edit, for
    a reader who may only view it the live release's publish time -- on the
    card, in the Recently edited sort, in the Edited filter and in the
    counts alike, so the order never hints at unpublished edits."""
    drafted, republished = own(corpus_rows, corpus, SALES), own(corpus_rows, corpus, STOCK)
    releases = {aid: publish(aid) for aid in (drafted, republished)}
    for aid in (drafted, republished):
        _sharing(test_client, corpus, aid, "shared", users=["direct_share"], editors=["member"])
    # Direct writes: no route back-dates an edit or a publish. `drafted` was
    # published long ago and edited since; `republished` published since, its
    # content time its publish time (as the publish switch leaves them).
    _date(corpus_rows, drafted, edited=datetime(2026, 6, 1))
    corpus_rows.update("artifact_releases", releases[drafted], published_at=datetime(2026, 2, 1))
    _date(corpus_rows, republished, edited=datetime(2026, 3, 1))
    corpus_rows.update("artifact_releases", releases[republished], published_at=datetime(2026, 3, 1))

    def shared(person, **params):
        return _ids(test_client, corpus, person, filter="shared", **params)

    def edited_at(person):
        return {c["id"]: (c["role"], datetime.fromisoformat(c["edited_at"]))
                for c in _cards(test_client, corpus, person, filter="shared")}

    assert edited_at("member") == {drafted: ("edit", datetime(2026, 6, 1)), republished: ("edit", datetime(2026, 3, 1))}
    assert edited_at("direct_share") == {drafted: ("view", datetime(2026, 2, 1)),
                                         republished: ("view", datetime(2026, 3, 1))}
    assert shared("member", sort="edited") == [drafted, republished]
    assert shared("direct_share", sort="edited") == [republished, drafted]
    assert shared("direct_share") == [republished, drafted]  # the default sort

    since_april = {"edited_from": "2026-04-01T00:00:00Z"}
    assert shared("member", **since_april) == [drafted]
    assert shared("direct_share", **since_april) == []
    assert shared("direct_share", edited_from="2026-02-15T00:00:00Z") == [republished]
    assert shared("direct_share", edited_to="2026-02-15T00:00:00Z") == [drafted]
    for person, expected in (("member", 1), ("direct_share", 0)):
        resp = test_client.get("/api/artifacts/facets", params={"filter": "shared", **since_april},
                               headers=as_(corpus, person))
        assert resp.status_code == 200, resp.text
        assert resp.json()["access"]["all"] == expected, (person, resp.json()["access"])


@pytest.mark.e2e
def test_on_shared_the_readers_role_decides_which_time_edited_is(corpus, corpus_rows, test_client):
    """On Shared, whoever may edit a dashboard -- by name or through a group
    -- sees its content time; a reader who may only view it sees the earlier
    of its content and publish times (review #1). `drafted` was edited after
    its publish, `republished` published at its last edit, and `migrated`
    carries a publish time later than its content, as the data migration
    (dashfc08) left every existing dashboard."""
    drafted, republished = own(corpus_rows, corpus, SALES), own(corpus_rows, corpus, STOCK)
    migrated = own(corpus_rows, corpus, ONE)
    releases = {aid: publish(aid) for aid in (drafted, republished, migrated)}
    for aid in releases:
        _sharing(test_client, corpus, aid, "shared", users=["direct_share"], editors=["member"],
                 editor_groups=["analysts"])
    # Direct writes: no route back-dates an edit or a publish.
    for aid, edited, published in ((drafted, datetime(2026, 6, 1), datetime(2026, 2, 1)),
                                   (republished, datetime(2026, 3, 1), datetime(2026, 3, 1)),
                                   (migrated, datetime(2025, 8, 1), datetime(2026, 4, 1))):
        _date(corpus_rows, aid, edited=edited)
        corpus_rows.update("artifact_releases", releases[aid], published_at=published)
    content = {drafted: datetime(2026, 6, 1), republished: datetime(2026, 3, 1), migrated: datetime(2025, 8, 1)}
    earlier = {drafted: datetime(2026, 2, 1), republished: datetime(2026, 3, 1), migrated: datetime(2025, 8, 1)}

    roles = {}
    # member: an edit grant by name; group_share: one through the analysts group.
    for person in ("member", "group_share", "direct_share"):
        cards = [c for c in _cards(test_client, corpus, person, filter="shared") if c["id"] in content]
        assert {c["id"] for c in cards} == set(content), person
        for c in cards:
            roles[person, c["id"]] = c["role"]
            expected = content if c["role"] in ("owner", "edit") else earlier
            assert datetime.fromisoformat(c["edited_at"]) == expected[c["id"]], (person, c["role"], c["id"])
    assert {roles["member", a] for a in content} == {roles["group_share", a] for a in content} == {"edit"}
    assert {roles["direct_share", a] for a in content} == {"view"}
    in_order = [a for a in _ids(test_client, corpus, "group_share", filter="shared", sort="edited") if a in content]
    assert in_order == [drafted, republished, migrated]
    in_order = [a for a in _ids(test_client, corpus, "direct_share", filter="shared", sort="edited") if a in content]
    assert in_order == [republished, drafted, migrated]


@pytest.mark.e2e
def test_starred_first_on_both_tabs(corpus, corpus_rows, test_client):
    d = _reasons(test_client, corpus, corpus_rows)
    for n, key in enumerate(("sales", "stock", "deck", "one", "many")):
        _date(corpus_rows, d[key], edited=datetime(2026, 1, 1 + n))
    mine = [d[k] for k in ("many", "one", "deck", "stock", "sales")]
    assert _ids(test_client, corpus, "owner", filter="my") == mine
    _star(test_client, corpus, "owner", d["sales"])  # the oldest edit
    assert _ids(test_client, corpus, "owner", filter="my") == [d["sales"]] + mine[:-1]

    assert _ids(test_client, corpus, "direct_share", filter="shared") == [d["sales"], d["one"]]
    _star(test_client, corpus, "direct_share", d["one"])  # the organization's: ranked last
    assert _ids(test_client, corpus, "direct_share", filter="shared") == [d["one"], d["sales"]]


@pytest.mark.e2e
def test_starred_only(corpus, corpus_rows, test_client):
    d = _reasons(test_client, corpus, corpus_rows)
    assert _ids(test_client, corpus, "owner", filter="my", starred="true") == []
    _star(test_client, corpus, "owner", d["stock"])
    _star(test_client, corpus, "direct_share", d["one"])
    assert _ids(test_client, corpus, "owner", filter="my", starred="true") == [d["stock"]]
    assert _ids(test_client, corpus, "direct_share", filter="shared", starred="true") == [d["one"]]
    assert _ids(test_client, corpus, "member", filter="shared", starred="true") == []  # stars are per reader
    assert set(_ids(test_client, corpus, "owner", filter="my", starred="false")) == set(d.values())


# ── owner, search, dates ──────────────────────────────────────────────────

@pytest.mark.e2e
def test_owner_filter_and_search_by_owner_name_or_email(corpus, corpus_rows, test_client):
    mine = own(corpus_rows, corpus, SALES)  # owner: "corpus owner", owner@corpus.com
    theirs = own(corpus_rows, corpus, STOCK, owner="proj_owner")  # "corpus proj_owner", proj.owner@corpus.com
    for aid, by in ((mine, "owner"), (theirs, "proj_owner")):
        publish(aid)
        _sharing(test_client, corpus, aid, "internal", by=by)
    owner_id, proj_owner_id = corpus.cast.id("owner"), corpus.cast.id("proj_owner")

    def shared(**params):
        return set(_ids(test_client, corpus, "member", filter="shared", **params))

    assert shared() == {mine, theirs}
    assert shared(owner_ids=proj_owner_id) == {theirs}
    assert shared(owner_ids=f" {owner_id}, ,{owner_id},") == {mine}  # blanks and repeats are ignored
    assert shared(owner_ids=f"{owner_id},{proj_owner_id}") == {mine, theirs}
    assert shared(owner_ids=str(uuid.uuid4())) == set()

    assert shared(search="proj_owner") == {theirs}  # the name, anywhere in it
    assert shared(search="corpus owner") == {mine}
    assert shared(search="PROJ.OWNER@CORPUS") == {theirs}  # the email from its start, any case
    assert shared(search="owner@corpus") == {mine}  # not proj.owner@corpus.com: a prefix only
    assert shared(search="corpus.com") == set()  # the domain alone matches nobody's email
    assert shared(search="מלאי") == {theirs}  # the title, as before
    assert shared(search="proj_owner", owner_ids=owner_id) == set()  # filters combine
    # On My the owner is always the reader: the search is the title's alone.
    assert set(_ids(test_client, corpus, "owner", filter="my", search="corpus owner")) == set()
    assert set(_ids(test_client, corpus, "owner", filter="my", search="owner@")) == set()
    assert set(_ids(test_client, corpus, "owner", filter="my", search="מכירות")) == {mine}


@pytest.mark.e2e
def test_created_and_edited_ranges_from_inclusive_to_exclusive(corpus, corpus_rows, test_client):
    sales, stock, deck = (own(corpus_rows, corpus, k) for k in (SALES, STOCK, DECK))
    for n, aid in enumerate((sales, stock, deck)):
        _date(corpus_rows, aid, created=datetime(2026, 3, 1 + n), edited=datetime(2026, 5, 1 + n))

    def mine(**params):
        return set(_ids(test_client, corpus, "owner", filter="my", **params))

    assert mine(created_from="2026-03-01T00:00:00Z", created_to="2026-03-03T00:00:00Z") == {sales, stock}
    # 02:00 at +02:00 is midnight UTC: the start is in, the end is out.
    assert mine(created_from="2026-03-02T02:00:00+02:00") == {stock, deck}
    assert mine(created_to="2026-03-02T02:00:00+02:00") == {sales}
    assert mine(created_from="2026-03-01T22:00:00-02:00") == {stock, deck}
    assert mine(edited_from="2026-05-02T00:00:00", edited_to="2026-05-03T00:00:01Z") == {stock, deck}  # naive
    assert mine(edited_to="2026-05-01T00:00:00Z") == set()
    assert mine(edited_from="2026-05-03T00:00:00Z", created_to="2026-03-03T00:00:00Z") == set()


@pytest.mark.e2e
def test_invalid_params_are_422(corpus, corpus_rows, test_client):
    own(corpus_rows, corpus, SALES)
    for params in ({"sort": "newest"}, {"access": "friends"}, {"status": "live"},
                   {"created_from": "yesterday"}, {"edited_to": "2026-13-45"},
                   {"owner_ids": ",".join(str(uuid.uuid4()) for _ in range(101))},
                   {"owner_ids": f"{uuid.uuid4()},not-an-id"}, {"owner_ids": "x" * 5000},
                   # Valid dates whose UTC time is out of range.
                   {"created_from": "0001-01-01T00:00:00+02:00"}, {"edited_to": "9999-12-31T23:00:00-02:00"},
                   # An empty or reversed range.
                   {"created_from": "2026-03-02T00:00:00Z", "created_to": "2026-03-02T00:00:00Z"},
                   {"edited_from": "2026-03-03T00:00:00Z", "edited_to": "2026-03-02T00:00:00Z"},
                   {"starred": "maybe"}):
        resp = _get(test_client, corpus, "owner", filter="my", **params)
        assert resp.status_code == 422, (params, resp.status_code, resp.text)
    # Exactly 100, empty items, and an id in capitals are fine.
    for params in ({"owner_ids": ",".join(str(uuid.uuid4()) for _ in range(100))}, {"owner_ids": ",, ,"}):
        assert _get(test_client, corpus, "owner", filter="my", **params).status_code == 200
    me = corpus.cast.id("owner")
    shouted = _list(test_client, corpus, "owner", filter="my", owner_ids=me.upper())
    assert [d["id"] for d in shouted["dashboards"]] == [corpus.ids[SALES]]


# ── status ────────────────────────────────────────────────────────────────

@pytest.fixture
def spawned(monkeypatch):
    """The background runs a route starts (a join's thumbnail), run when the test says."""
    from app.services.thumbnail_service import ThumbnailService

    monkeypatch.setattr(ThumbnailService, "_build_thumbnail_html", lambda self, **kw: "")

    async def draw(self, artifact_id, html_content, mode="page"):
        return None

    monkeypatch.setattr(ThumbnailService, "generate_thumbnail", draw)
    jobs = []
    monkeypatch.setattr("app.core.fire_and_forget.spawn", jobs.append)
    try:
        yield jobs
    finally:
        while jobs:
            job = jobs.pop(0)
            if asyncio.iscoroutine(job):
                job.close()


def _finish(spawned):
    while spawned:
        job = spawned.pop(0)
        if asyncio.iscoroutine(job):
            asyncio.run(job)


async def _new_version(aid: str, report_id: str, user_id: str) -> str:
    """A new completed version in one conversation, as the agent's edit
    tools write it (no route writes a version without an LLM run)."""
    from sqlalchemy import select

    from app.dependencies import async_session_maker
    from app.models.artifact import ArtifactVersion
    from app.services.artifact_service import new_version

    async with async_session_maker() as db:
        source = (await db.execute(
            select(ArtifactVersion).where(ArtifactVersion.artifact_id == aid, ArtifactVersion.report_id == report_id,
                                          ArtifactVersion.status == "completed")
            .order_by(ArtifactVersion.version.desc()).limit(1))).scalar_one()
        version = await new_version(db, source, content=dict(source.content or {}), user_id=user_id)
        await db.commit()
        return str(version.id)


def _own_report(corpus_rows, aid) -> str:
    return _rows(corpus_rows, "SELECT report_id FROM artifacts WHERE id = :a", a=aid)[0]["report_id"]


def _has_unpublished_changes(test_client, corpus, aid) -> bool:
    resp = test_client.get(f"/api/artifacts/{aid}/publish", headers=as_(corpus, "owner"))
    assert resp.status_code == 200, resp.text
    return resp.json()["has_unpublished_changes"]


def _cards(test_client, corpus, person, **params) -> list[dict]:
    body = _list(test_client, corpus, person, limit=100, **params)
    assert body["meta"]["total"] <= 100
    return body["dashboards"]


@pytest.mark.e2e
def test_status_published_changes_draft(migrated_corpus, corpus_rows, test_client):
    c = migrated_corpus
    # One published with a newer draft version, one published as it is; the
    # migrated corpus has dashboards never published.
    changed, kept = c.ids[SALES], c.ids[STOCK]
    publish(changed)
    publish(kept)
    asyncio.run(_new_version(changed, _own_report(corpus_rows, changed), c.cast.id("owner")))

    cards = _cards(test_client, c, "owner", filter="my")
    everything = {d["id"] for d in cards}
    by_status = {s: set(_ids(test_client, c, "owner", filter="my", status=s))
                 for s in ("published", "changes", "draft")}
    assert by_status["published"] & by_status["changes"] == set()
    assert by_status["published"] & by_status["draft"] == set()
    assert by_status["changes"] & by_status["draft"] == set()
    assert by_status["published"] | by_status["changes"] | by_status["draft"] == everything
    assert all(by_status.values()), {s: len(ids) for s, ids in by_status.items()}
    assert changed in by_status["changes"] and kept in by_status["published"]
    published = {d["id"]: d["published"] for d in cards}
    assert not any(published[i] for i in by_status["draft"])


@pytest.mark.e2e
def test_status_is_ignored_on_shared(corpus, corpus_rows, test_client):
    sales, one = own(corpus_rows, corpus, SALES), own(corpus_rows, corpus, ONE)
    for aid in (sales, one):
        publish(aid)
        _sharing(test_client, corpus, aid, "internal")
    asyncio.run(_new_version(sales, corpus.reports[CONV], corpus.cast.id("owner")))
    assert _has_unpublished_changes(test_client, corpus, sales)
    assert sales in _ids(test_client, corpus, "owner", filter="my", status="changes")  # it does filter on My

    plain = _list(test_client, corpus, "member", filter="shared")
    assert {d["id"] for d in plain["dashboards"]} == {sales, one}
    for status in ("changes", "published", "draft"):
        body = _list(test_client, corpus, "member", filter="shared", status=status)
        assert [d["id"] for d in body["dashboards"]] == [d["id"] for d in plain["dashboards"]], status
        assert body["meta"]["total"] == plain["meta"]["total"], status


@pytest.mark.e2e
def test_unpublished_changes_matches_publish_status_for_every_owner_dashboard(
        migrated_corpus, corpus_rows, test_client, spawned):
    c = migrated_corpus
    joined, ended, reordered = c.ids[SALES], c.ids[STOCK], c.ids[MANY]
    for aid in (joined, ended, reordered):
        publish(aid)  # the release's source: the highest completed version
    for aid in (joined, ended):
        visibility = test_client.get(f"/api/artifacts/{aid}/sharing", headers=as_(c, "owner")).json()["visibility"]
        _sharing(test_client, c, aid, visibility, editors=["member"])
        resp = test_client.post(f"/api/artifacts/{aid}/edit", headers=as_(c, "member"))
        assert resp.status_code == 200, resp.text
        _finish(spawned)
        if aid == ended:  # the member's conversation goes: it no longer edits the dashboard
            gone = test_client.delete(f"/api/reports/{resp.json()['report_id']}", headers=as_(c, "member"))
            assert gone.status_code == 200, gone.text
    # A published dashboard no conversation edits any more, while another
    # dashboard (`joined`) is still edited: its published version came from
    # an editor's conversation that ended, a newer one from its own, which
    # was then deleted. Every version counts again, so it has changes --
    # however many conversations edit other dashboards.
    orphaned = c.ids[ONE]
    own_report = _own_report(corpus_rows, orphaned)
    visibility = test_client.get(f"/api/artifacts/{orphaned}/sharing", headers=as_(c, "owner")).json()["visibility"]
    _sharing(test_client, c, orphaned, visibility, editors=["member"])
    resp = test_client.post(f"/api/artifacts/{orphaned}/edit", headers=as_(c, "member"))
    assert resp.status_code == 200, resp.text
    _finish(spawned)
    publish(orphaned)  # the editor's copy: the highest version
    asyncio.run(_new_version(orphaned, own_report, c.cast.id("owner")))
    for report_id, person in ((resp.json()["report_id"], "member"), (own_report, "owner")):
        gone = test_client.delete(f"/api/reports/{report_id}", headers=as_(c, person))
        assert gone.status_code == 200, gone.text
    assert _own_report(corpus_rows, orphaned) is None  # published: it outlives its conversations
    # Direct write: the highest version made long before the others (no route
    # back-dates a version), so "newest" must mean the highest number, not
    # the latest creation.
    (top,) = _rows(corpus_rows, "SELECT id FROM artifact_versions WHERE artifact_id = :a AND status = 'completed' "
                                "AND deleted_at IS NULL ORDER BY version DESC LIMIT 1", a=reordered)
    corpus_rows.update("artifact_versions", top["id"], created_at=datetime(2000, 1, 1))

    cards = _cards(test_client, c, "owner", filter="my")
    changes = set(_ids(test_client, c, "owner", filter="my", status="changes"))
    kinds = {"changes": 0, "published": 0, "never published with a version": 0}
    for card in cards:
        status = _has_unpublished_changes(test_client, c, card["id"])
        assert (card["id"] in changes) == (card["published"] and status), card["id"]
        if card["published"]:
            kinds["changes" if status else "published"] += 1
        elif status:
            kinds["never published with a version"] += 1
    assert all(kinds.values()), kinds
    assert joined in changes and ended not in changes and reordered not in changes
    assert orphaned in changes


# ── paging, opening, sorts ────────────────────────────────────────────────

@pytest.mark.e2e
def test_pages_are_a_stable_partition(migrated_corpus, test_client):
    c = migrated_corpus
    for person, tab in (("owner", "my"), ("member", "shared")):
        for sort in SORTS:
            full = _ids(test_client, c, person, filter=tab, sort=sort)
            assert len(full) > 2, (person, sort)
            pages, page = [], 1
            while True:
                body = _list(test_client, c, person, filter=tab, sort=sort, limit=2, page=page)
                pages += [d["id"] for d in body["dashboards"]]
                if not body["meta"]["has_next"]:
                    break
                page += 1
            assert pages == full, (person, sort)  # no duplicates, nothing missing, the same order
            past = _list(test_client, c, person, filter=tab, sort=sort, limit=2, page=page + 1)
            assert past["dashboards"] == [] and past["meta"]["total"] == body["meta"]["total"]


@pytest.mark.e2e
def test_every_card_still_opens(corpus, corpus_rows, test_client):
    _reasons(test_client, corpus, corpus_rows)
    for person in ("direct_share", "group_share", "proj_viewer", "member", "proj_group"):
        for chip in CHIPS:
            for card in _cards(test_client, corpus, person, filter="shared", access=chip):
                url = f"/api/d/{card['id']}" if card["role"] == "view" else f"/api/d/{card['id']}/draft"
                resp = test_client.get(url, headers=as_(corpus, person))
                assert resp.status_code == 200, (person, chip, url, resp.text)


@pytest.mark.e2e
def test_a_refused_candidate_is_logged_by_id_only(corpus, corpus_rows, test_client, monkeypatch):
    """A candidate the per-card check refuses (a release not ready yet) is
    left out of the page and counted in the total, as documented; the
    debug log names it by id, never by title."""
    import logging

    ready, pending = own(corpus_rows, corpus, SALES), own(corpus_rows, corpus, STOCK)
    releases = {aid: publish(aid) for aid in (ready, pending)}
    for aid in (ready, pending):
        _sharing(test_client, corpus, aid, "internal")
    # Direct write: a live release that is not ready (sharing needs a ready
    # one first, and no route leaves a release pending in a test).
    corpus_rows.update("artifact_releases", releases[pending], status="pending")
    # Every debug call of this module, whatever the handlers and levels the
    # app's logging setup applies (as test_bow_config_encryption_key does).
    logged = []
    debug = logging.Logger.debug

    def record(self, msg, *args, **kwargs):
        if self.name == "app.services.dashboard_list":
            logged.append(str(msg) % args)
        return debug(self, msg, *args, **kwargs)

    monkeypatch.setattr(logging.Logger, "debug", record)
    body = _list(test_client, corpus, "member", filter="shared", limit=100)
    assert [d["id"] for d in body["dashboards"]] == [ready] and body["meta"]["total"] == 2
    assert any(pending in m for m in logged), logged
    assert not any("מלאי" in m or ready in m for m in logged), logged


def _new_conversation(test_client, corpus, title) -> str:
    resp = test_client.post("/api/reports", json={"title": title}, headers=as_(corpus, "owner"))
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


@pytest.mark.e2e
def test_sort_title_ignores_case_and_puts_untitled_last(corpus, corpus_rows, test_client):
    alpha, bravo, charlie = (own(corpus_rows, corpus, k) for k in (SALES, STOCK, DECK))
    delta, untitled, blank = (own(corpus_rows, corpus, k) for k in (ONE, PAGE_DASH, PAGE_DOC))
    # Direct writes: no route makes a blank or missing title, or a legacy
    # conversation (the data migration does).
    corpus_rows.update("artifacts", alpha, title="alpha board")
    corpus_rows.update("artifacts", bravo, title="Bravo board")
    corpus_rows.update("artifacts", charlie, title="charlie board")
    # Blank is any run of spaces, tabs and line breaks, as the card's strip() sees it.
    corpus_rows.update("artifacts", delta, title=" \t\r\n ",
                       legacy_report_id=_new_conversation(test_client, corpus, "delta board"))
    corpus_rows.update("artifacts", untitled, title=None)
    blank_convo = _new_conversation(test_client, corpus, "to be blanked")
    corpus_rows.update("reports", blank_convo, title="\t \n")
    corpus_rows.update("artifacts", blank, title="", legacy_report_id=blank_convo)

    order = _ids(test_client, corpus, "owner", filter="my", sort="title")
    at = {aid: order.index(aid) for aid in (alpha, bravo, charlie, delta, untitled, blank)}
    assert at[alpha] < at[bravo] < at[charlie] < at[delta] < min(at[untitled], at[blank])
    assert {order[-1], order[-2]} == {untitled, blank}  # no title at all: last


@pytest.mark.e2e
def test_sort_recently_published(corpus, corpus_rows, test_client):
    sales, stock, deck, one = (own(corpus_rows, corpus, k, project="private") for k in (SALES, STOCK, DECK, ONE))
    releases = {aid: publish(aid) for aid in (sales, stock, one)}  # deck: never published
    # Direct writes: the helper publishes at one fixed time and no route
    # back-dates a publish.
    for aid, day in ((sales, 3), (stock, 1), (one, 2)):
        corpus_rows.update("artifact_releases", releases[aid], published_at=datetime(2026, 4, day))
    for n, aid in enumerate((sales, one, stock, deck)):  # Edited in the opposite order
        _date(corpus_rows, aid, edited=datetime(2026, 1, 1 + n))
    assert _ids(test_client, corpus, "proj_viewer", filter="shared", sort="published") == [sales, one, stock, deck]
    assert _ids(test_client, corpus, "owner", filter="my", sort="published") == [sales, one, stock, deck]
