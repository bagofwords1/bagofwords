"""What a dashboard's card says (dashboards list findability, P5).

Each card of `GET /api/artifacts` carries, besides its title, owner and
dates:

- `access` (Shared only): why the reader has it -- `reasons` in the order
  direct, group, project, org; `primary`, the first; `via_name`, the
  group's name (the alphabetically first of several) or the project's.
- `visibility`, as readers meet it: `public` reads as `internal` while the
  organization has public links off.
- `published_at`, the live release's publish time.
- `is_new` (Shared only): the dashboard reached the reader after their last
  visit to Shared (the watermark: `new_since`, else the visit stored by
  `POST /api/artifacts/shared/seen`). It reached them when their earliest
  share was made, or -- through the organization -- when its live release
  was published. A project dashboard is never new.
- `cron_schedule`, `last_run_status` and `unpublished_changes` for its
  editors only (owner or edit); `last_run_at` for everyone.

The fields are loaded for the page in a fixed number of queries, so a
bigger page costs no more statements per card than before.

Times that no route sets (a publish time, a share made in the past, a failed
run) are written directly, each place says so.
"""
import asyncio
from datetime import datetime

import pytest
from sqlalchemy import event, text

from tests.e2e.dashboards.dashboard_setup import as_, own, publish

CONV = "conv_three_dash"
SALES, STOCK, DECK = f"{CONV}/dash_sales", f"{CONV}/dash_stock", f"{CONV}/deck"
ONE = "conv_one_dash/dash"
THUMB = "thumbnail/dash"
ANALYSTS = "אנליסטים"  # the corpus's analysts group (group_share, proj_group)
PRIVATE = "פרויקט פרטי"  # the corpus's private project (proj_viewer; analysts by a group grant)
EARLIER = datetime(2026, 1, 1)
EDITOR_ONLY = ("cron_schedule", "last_run_status", "unpublished_changes")


# ── helpers ───────────────────────────────────────────────────────────────

def _list(test_client, corpus, person, **params) -> dict:
    whole = "limit" not in params
    params.setdefault("limit", 100)
    resp = test_client.get("/api/artifacts", params=params, headers=as_(corpus, person))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    if whole:  # every card on one page
        assert body["meta"]["total"] <= params["limit"]
    return body


def _cards(test_client, corpus, person, **params) -> dict:
    return {d["id"]: d for d in _list(test_client, corpus, person, **params)["dashboards"]}


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


def _visit(test_client, corpus, person) -> dict:
    resp = test_client.post("/api/artifacts/shared/seen", headers=as_(corpus, person))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _rows(corpus_rows, sql, **params):
    with corpus_rows.engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def _date_shares(corpus_rows, aid, when=EARLIER):
    """A direct write: no route makes a share in the past, and these tests
    need shares made well before the visit under test."""
    for row in _rows(corpus_rows, "SELECT id FROM artifact_shares WHERE artifact_id = :a", a=aid):
        corpus_rows.update("artifact_shares", row["id"], created_at=when)


def _published_at(corpus_rows, release_id, when):
    """A direct write: the publish helper publishes at one fixed time and no
    route dates a publish."""
    corpus_rows.update("artifact_releases", release_id, published_at=when)


def _set_public_sharing(test_client, corpus, enabled):
    resp = test_client.put("/api/organization/settings", headers=as_(corpus, "admin"),
                           json={"config": {"allow_public_sharing": {"value": enabled}}})
    assert resp.status_code == 200, resp.text


def _new_version(aid: str, user_id: str) -> None:
    """A newer completed version, as the agent's edit tools write it (no
    route writes a version without an LLM run)."""
    from sqlalchemy import select

    from app.dependencies import async_session_maker
    from app.models.artifact import ArtifactVersion
    from app.services.artifact_service import new_version

    async def write():
        async with async_session_maker() as db:
            source = (await db.execute(
                select(ArtifactVersion).where(ArtifactVersion.artifact_id == aid,
                                              ArtifactVersion.status == "completed",
                                              ArtifactVersion.deleted_at.is_(None))
                .order_by(ArtifactVersion.version.desc()).limit(1))).scalar_one()
            await new_version(db, source, content=dict(source.content or {}), user_id=user_id)
            await db.commit()

    asyncio.run(write())


def _has_unpublished_changes(test_client, corpus, aid) -> bool:
    resp = test_client.get(f"/api/artifacts/{aid}/publish", headers=as_(corpus, "owner"))
    assert resp.status_code == 200, resp.text
    return resp.json()["has_unpublished_changes"]


def _access(card):
    access = card["access"]
    return access["reasons"], access["primary"], access["via_name"]


# ── why the reader has it ─────────────────────────────────────────────────

@pytest.mark.e2e
def test_reasons_primary_and_via_name(corpus, corpus_rows, test_client):
    sales, stock, one = own(corpus_rows, corpus, SALES), own(corpus_rows, corpus, STOCK), own(corpus_rows, corpus, ONE)
    deck = own(corpus_rows, corpus, DECK, project="private")
    for aid in (sales, stock, one):
        publish(aid)
    _sharing(test_client, corpus, sales, "shared", users=["direct_share"])
    _sharing(test_client, corpus, stock, "shared", groups=["analysts"])
    # The organization's, and direct_share may edit it: a share and the org.
    _sharing(test_client, corpus, one, "internal", editors=["direct_share"])
    # In the private project, and the analysts may edit it: a group and the project.
    _sharing(test_client, corpus, deck, "none", editor_groups=["analysts"])

    direct = _cards(test_client, corpus, "direct_share", filter="shared")
    assert _access(direct[sales]) == (["direct"], "direct", None)
    assert _access(direct[one]) == (["direct", "org"], "direct", None)
    group = _cards(test_client, corpus, "group_share", filter="shared")
    assert _access(group[stock]) == (["group"], "group", ANALYSTS)
    assert _access(group[deck]) == (["group", "project"], "group", ANALYSTS)
    assert _access(group[one]) == (["org"], "org", None)
    project = _cards(test_client, corpus, "proj_viewer", filter="shared")
    assert _access(project[deck]) == (["project"], "project", PRIVATE)
    member = _cards(test_client, corpus, "member", filter="shared")
    assert set(member) == {one}
    assert _access(member[one]) == (["org"], "org", None)
    # My has no reasons: every card there is the reader's own.
    assert all(c["access"] is None for c in _cards(test_client, corpus, "owner", filter="my").values())


def _new_group(test_client, corpus, name, members) -> str:
    """A group of the corpus organization, made and filled through the API."""
    org = corpus.cast.org_id
    resp = test_client.post(f"/api/organizations/{org}/groups", json={"name": name}, headers=as_(corpus, "admin"))
    assert resp.status_code in (200, 201), resp.text
    gid = resp.json()["id"]
    for person in members:
        added = test_client.post(f"/api/organizations/{org}/groups/{gid}/members",
                                 json={"user_id": corpus.cast.id(person)}, headers=as_(corpus, "admin"))
        assert added.status_code in (200, 201), added.text
    corpus.cast.groups[name] = gid
    return gid


@pytest.mark.e2e
def test_via_name_is_the_alphabetically_first_of_my_groups(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, SALES)
    publish(aid)
    # "Alpha readers" sorts before the analysts' Hebrew name; it is made second,
    # so neither creation order nor share order decides.
    _new_group(test_client, corpus, "Alpha readers", ["group_share"])
    _sharing(test_client, corpus, aid, "shared", groups=["analysts", "Alpha readers"])
    card = _cards(test_client, corpus, "group_share", filter="shared")[aid]
    assert _access(card) == (["group"], "group", "Alpha readers")
    # A reader in only one of the two groups gets that one's name.
    assert _access(_cards(test_client, corpus, "proj_group", filter="shared")[aid]) == (["group"], "group", ANALYSTS)


@pytest.mark.e2e
def test_every_shared_card_has_a_reason(migrated_corpus, test_client):
    c = migrated_corpus
    seen = 0
    for person in ("member", "direct_share", "group_share", "proj_viewer", "proj_group", "admin"):
        for card in _cards(test_client, c, person, filter="shared").values():
            reasons, primary, _ = _access(card)
            assert reasons and set(reasons) <= {"direct", "group", "project", "org"}, (person, card["id"])
            order = ["direct", "group", "project", "org"]
            assert reasons == sorted(reasons, key=order.index), (person, card["id"])
            assert primary == reasons[0], (person, card["id"])
            seen += 1
    assert seen > 2


@pytest.mark.e2e
def test_public_reads_as_organization_when_public_links_are_off(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, SALES)
    publish(aid)
    _sharing(test_client, corpus, aid, "public")
    assert _cards(test_client, corpus, "member", filter="shared")[aid]["visibility"] == "public"
    assert _cards(test_client, corpus, "owner", filter="my")[aid]["visibility"] == "public"

    _set_public_sharing(test_client, corpus, False)
    member = _cards(test_client, corpus, "member", filter="shared")[aid]
    assert member["visibility"] == "internal" and _access(member)[0] == ["org"]
    assert _cards(test_client, corpus, "owner", filter="my")[aid]["visibility"] == "internal"

    _set_public_sharing(test_client, corpus, True)  # the stored visibility never changed
    assert _cards(test_client, corpus, "member", filter="shared")[aid]["visibility"] == "public"


# ── new ───────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_first_visit_shows_nothing_new(corpus, corpus_rows, test_client):
    sales, one = own(corpus_rows, corpus, SALES), own(corpus_rows, corpus, ONE)
    for aid in (sales, one):
        publish(aid)
    _sharing(test_client, corpus, sales, "shared", users=["direct_share"])
    _sharing(test_client, corpus, one, "internal")
    # Never visited: no watermark, nothing is new.
    before = _cards(test_client, corpus, "direct_share", filter="shared")
    assert set(before) == {sales, one} and not any(c["is_new"] for c in before.values())
    assert _visit(test_client, corpus, "direct_share")["previous_seen_at"] is None
    after = _cards(test_client, corpus, "direct_share", filter="shared")
    assert set(after) == {sales, one} and not any(c["is_new"] for c in after.values())


@pytest.mark.e2e
def test_a_share_after_the_visit_is_new_and_new_since_keeps_it(corpus, corpus_rows, test_client):
    old, fresh = own(corpus_rows, corpus, SALES), own(corpus_rows, corpus, STOCK)
    for aid in (old, fresh):
        publish(aid)
    _sharing(test_client, corpus, old, "shared", users=["direct_share"])
    _date_shares(corpus_rows, old)
    _visit(test_client, corpus, "direct_share")
    _sharing(test_client, corpus, fresh, "shared", groups=["analysts"], users=["direct_share"])

    cards = _cards(test_client, corpus, "direct_share", filter="shared")
    assert cards[fresh]["is_new"] is True and cards[old]["is_new"] is False
    # A group member who never visited sees nothing new.
    assert _cards(test_client, corpus, "group_share", filter="shared")[fresh]["is_new"] is False

    # The next visit moves the stored watermark; the page keeps showing what
    # was new against the previous one by passing it as new_since.
    previous = _visit(test_client, corpus, "direct_share")["previous_seen_at"]
    assert previous is not None
    assert _cards(test_client, corpus, "direct_share", filter="shared")[fresh]["is_new"] is False
    kept = _cards(test_client, corpus, "direct_share", filter="shared", new_since=previous)
    assert kept[fresh]["is_new"] is True and kept[old]["is_new"] is False
    # A watermark in the future: nothing is new.
    assert not any(c["is_new"] for c in _cards(test_client, corpus, "direct_share", filter="shared",
                                               new_since="2999-01-01T00:00:00Z").values())
    resp = test_client.get("/api/artifacts", params={"filter": "shared", "new_since": "yesterday"},
                           headers=as_(corpus, "direct_share"))
    assert resp.status_code == 422


@pytest.mark.e2e
def test_a_group_share_after_the_visit_is_new(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, SALES)
    publish(aid)
    _visit(test_client, corpus, "group_share")  # a member of the analysts group only
    _sharing(test_client, corpus, aid, "shared", groups=["analysts"])
    card = _cards(test_client, corpus, "group_share", filter="shared")[aid]
    assert _access(card)[0] == ["group"] and card["is_new"] is True


@pytest.mark.e2e
def test_the_earliest_share_is_when_it_reached_me(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, SALES)
    publish(aid)
    _sharing(test_client, corpus, aid, "shared", users=["group_share"])
    _date_shares(corpus_rows, aid)  # shared with them by name long ago
    _visit(test_client, corpus, "group_share")
    # Then shared with their group too: it does not reach them again.
    _sharing(test_client, corpus, aid, "shared", users=["group_share"], groups=["analysts"])
    card = _cards(test_client, corpus, "group_share", filter="shared")[aid]
    assert _access(card)[0] == ["direct", "group"] and card["is_new"] is False


@pytest.mark.e2e
def test_a_project_dashboard_is_never_new(corpus, corpus_rows, test_client):
    deck = own(corpus_rows, corpus, DECK, project="private")
    one = own(corpus_rows, corpus, ONE)
    publish(one)
    _sharing(test_client, corpus, one, "internal")
    long_ago = "2000-01-01T00:00:00Z"
    cards = _cards(test_client, corpus, "proj_viewer", filter="shared", new_since=long_ago)
    assert cards[one]["is_new"] is True  # reached through the organization after the watermark
    assert _access(cards[deck])[0] == ["project"] and cards[deck]["is_new"] is False
    # Also a project dashboard shared with a group of the reader.
    _sharing(test_client, corpus, deck, "none", editor_groups=["analysts"])
    group = _cards(test_client, corpus, "group_share", filter="shared", new_since=long_ago)
    assert _access(group[deck])[0] == ["group", "project"] and group[deck]["is_new"] is False


@pytest.mark.e2e
def test_is_new_only_on_shared(corpus, corpus_rows, test_client):
    one = own(corpus_rows, corpus, ONE)
    publish(one)
    _sharing(test_client, corpus, one, "internal")
    long_ago = "2000-01-01T00:00:00Z"
    assert _cards(test_client, corpus, "member", filter="shared", new_since=long_ago)[one]["is_new"] is True
    mine = _cards(test_client, corpus, "owner", filter="my", new_since=long_ago)
    assert one in mine and not any(c["is_new"] for c in mine.values())


@pytest.mark.e2e
def test_resaving_sharing_does_not_make_existing_recipients_cards_new(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, SALES)
    publish(aid)
    _sharing(test_client, corpus, aid, "shared", users=["direct_share"], groups=["analysts"])
    _date_shares(corpus_rows, aid)
    for person in ("direct_share", "group_share", "forker"):
        _visit(test_client, corpus, person)
    # The same lists plus forker; direct_share now edits.
    _sharing(test_client, corpus, aid, "shared", users=["direct_share", "forker"], groups=["analysts"],
             editors=["direct_share"])

    a = _cards(test_client, corpus, "direct_share", filter="shared")[aid]
    assert a["role"] == "edit" and a["is_new"] is False
    assert _cards(test_client, corpus, "group_share", filter="shared")[aid]["is_new"] is False
    assert _cards(test_client, corpus, "forker", filter="shared")[aid]["is_new"] is True


@pytest.mark.e2e
def test_transfer_does_not_make_it_new_for_the_previous_owner(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, SALES)
    _sharing(test_client, corpus, aid, "none", editors=["member"])
    _visit(test_client, corpus, "owner")
    moved = test_client.post(f"/api/artifacts/{aid}/transfer", json={"user_id": corpus.cast.id("member")},
                             headers=as_(corpus, "owner"))
    assert moved.status_code == 200, moved.text

    card = _cards(test_client, corpus, "owner", filter="shared")[aid]
    assert card["role"] == "edit" and _access(card)[0] == ["direct"]
    assert card["is_new"] is False


@pytest.mark.e2e
def test_organization_reach_is_the_current_releases_publish_time(corpus, corpus_rows, test_client):
    """Known limitations 7-8 (Nir: "Use publish time")."""
    visit, next_visit = "2026-06-01T00:00:00Z", "2026-06-15T00:00:00Z"
    fresh, older = own(corpus_rows, corpus, SALES), own(corpus_rows, corpus, STOCK)
    # (1) Published after member's visit, then shared with the organization: new.
    _published_at(corpus_rows, publish(fresh), datetime(2026, 6, 2))
    _sharing(test_client, corpus, fresh, "internal")
    assert _cards(test_client, corpus, "member", filter="shared", new_since=visit)[fresh]["is_new"] is True

    # (2) Published before the visit, shared with the organization after it:
    # listed for the organization, not new (the visibility change is not a reach).
    _published_at(corpus_rows, publish(older), datetime(2026, 5, 1))
    _sharing(test_client, corpus, older, "none", editors=["direct_share"])
    _date_shares(corpus_rows, older)  # direct_share has had it all along
    _sharing(test_client, corpus, older, "internal", editors=["direct_share"])
    card = _cards(test_client, corpus, "member", filter="shared", new_since=visit)[older]
    assert _access(card)[0] == ["org"] and card["is_new"] is False

    # (3) A republish after member's next visit: new again.
    assert _cards(test_client, corpus, "member", filter="shared", new_since=next_visit)[older]["is_new"] is False
    _published_at(corpus_rows, publish(older), datetime(2026, 7, 1))
    assert _cards(test_client, corpus, "member", filter="shared", new_since=next_visit)[older]["is_new"] is True

    # (4) Not for a reader who also has a share from before the visit.
    shared = _cards(test_client, corpus, "direct_share", filter="shared", new_since=next_visit)[older]
    assert _access(shared)[0] == ["direct", "org"] and shared["is_new"] is False


# ── editors' fields ───────────────────────────────────────────────────────

@pytest.mark.e2e
def test_unpublished_changes_on_the_card_matches_publish_status(migrated_corpus, test_client):
    c = migrated_corpus
    changed, kept = c.ids[SALES], c.ids[STOCK]
    publish(changed)
    publish(kept)
    _new_version(changed, c.cast.id("owner"))
    cards = _cards(test_client, c, "owner", filter="my")
    seen = set()
    for card in cards.values():
        status = _has_unpublished_changes(test_client, c, card["id"])
        assert card["unpublished_changes"] == (card["published"] and status), card["id"]
        seen.add((card["published"], card["unpublished_changes"]))
    assert {(True, True), (True, False), (False, False)} <= seen
    assert cards[changed]["unpublished_changes"] is True and cards[kept]["unpublished_changes"] is False


@pytest.mark.e2e
def test_refresh_fields_only_for_editors(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, SALES)
    publish(aid)
    resp = test_client.put(f"/api/artifacts/{aid}/settings", json={"cron_schedule": "0 8 * * *"},
                           headers=as_(corpus, "owner"))
    assert resp.status_code == 200, resp.text
    # Direct write: a failed scheduled run needs a data source that fails;
    # only its recorded outcome matters here.
    ran = datetime(2026, 5, 1, 8, 0)
    corpus_rows.update("artifacts", aid, last_run_status="failed", last_run_at=ran)
    _new_version(aid, corpus.cast.id("owner"))  # a draft newer than the live release

    def editors_see(card):
        assert card["cron_schedule"] == "0 8 * * *" and card["last_run_status"] == "failed"
        assert card["unpublished_changes"] is True and card["last_run_at"].startswith("2026-05-01T08:00")

    def readers_see(card):
        assert all(card[f] is None for f in EDITOR_ONLY), {f: card[f] for f in EDITOR_ONLY}
        assert card["last_run_at"].startswith("2026-05-01T08:00")

    # View shares count while the visibility is 'shared', the organization's
    # reach while 'internal': the two readers are seen one after the other.
    _sharing(test_client, corpus, aid, "shared", users=["direct_share"], editors=["member"])
    editors_see(_cards(test_client, corpus, "owner", filter="my")[aid])
    edit = _cards(test_client, corpus, "member", filter="shared")[aid]
    assert edit["role"] == "edit"
    editors_see(edit)
    view = _cards(test_client, corpus, "direct_share", filter="shared")[aid]
    assert view["role"] == "view"
    readers_see(view)

    _sharing(test_client, corpus, aid, "internal", editors=["member"])
    org = _cards(test_client, corpus, "forker", filter="shared")[aid]
    assert org["role"] == "view" and _access(org)[0] == ["org"]
    readers_see(org)
    editors_see(_cards(test_client, corpus, "member", filter="shared")[aid])


# ── what stays as it was ──────────────────────────────────────────────────

@pytest.mark.e2e
def test_role_report_id_and_thumbnail_unchanged(corpus, corpus_rows, test_client):
    """As test_dashboard_list.py has them: the pencil's conversation for the
    owner, none for others; the release's thumbnail for everyone once
    published, the draft's for its editors before."""
    thumb = own(corpus_rows, corpus, THUMB, project="private")
    sales = own(corpus_rows, corpus, SALES)
    draft_thumbnail = f"/thumbnails/{corpus.ids[THUMB + '/v1']}.png"

    mine = _cards(test_client, corpus, "owner", filter="my")
    assert mine[thumb]["role"] == "owner" and mine[thumb]["thumbnail_url"] == draft_thumbnail
    assert mine[sales]["report_id"] == corpus.reports[CONV] and mine[sales]["is_owner"] is True
    project = _cards(test_client, corpus, "proj_viewer", filter="shared")[thumb]
    assert project["role"] == "edit" and project["report_id"] is None
    assert project["thumbnail_url"] == draft_thumbnail and project["published"] is False

    release = publish(thumb)
    _sharing(test_client, corpus, thumb, "internal")
    live = f"/thumbnails/release-{release}.png"
    assert _cards(test_client, corpus, "owner", filter="my")[thumb]["thumbnail_url"] == live
    reader = _cards(test_client, corpus, "member", filter="shared")[thumb]
    assert reader["role"] == "view" and reader["report_id"] is None and reader["is_owner"] is False
    assert reader["thumbnail_url"] == live and reader["published"] is True
    assert reader["published_at"] is not None


# ── cost ──────────────────────────────────────────────────────────────────

def _statements(test_client, corpus, person, **params) -> tuple[dict, int]:
    """The SQL statements the app runs for one list request: an observation
    on the app's own engine, not a mock."""
    from app.dependencies import async_session_maker

    engine = async_session_maker.kw["bind"].sync_engine
    statements = []

    def count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count)
    try:
        body = _list(test_client, corpus, person, **params)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    return body, len(statements)


# Measured on the code before the card fields were batched (P4, 33761006d),
# owner, migrated_corpus, filter=my, page 1: N=4 cards ran 42 statements and
# 2N=8 ran 70 on SQLite (43 and 71 on Postgres), so S = (70 - 42) / 4 = 7
# statements per card on both. Batching must not make a card cost more.
SLOPE_BEFORE = 7
# Shared, for proj_group with group and project cards (the setup below), on
# the P4 code: 82 -> 126 statements on SQLite (83 -> 127 on Postgres), 11 per
# card.
SHARED_SLOPE_BEFORE = 11
# Measured after batching (P5), the same on SQLite and Postgres: My 42 -> 67
# (43 -> 68), 6.25 per card; Shared 76 -> 109 (77 -> 110), 8.25 per card.
# The rest of a card's cost is the per-card access check, can_edit and the
# pencil's conversation; a lookup moved back to per card shows here.
MY_SLOPE_AFTER = 6.25
SHARED_SLOPE_AFTER = 8.25
N = 4


def _slope(test_client, corpus, person, **params) -> tuple[float, int, int, dict]:
    """Statements per card between a page of N and of 2N cards."""
    small, at_n = _statements(test_client, corpus, person, limit=N, **params)
    large, at_2n = _statements(test_client, corpus, person, limit=2 * N, **params)
    assert len(small["dashboards"]) == N and len(large["dashboards"]) == 2 * N
    assert at_n > 0  # the counter watches the engine the app uses
    return (at_2n - at_n) / N, at_n, at_2n, large


@pytest.mark.e2e
def test_per_card_statements_do_not_grow(migrated_corpus, corpus_rows, test_client):
    c = migrated_corpus
    slope, at_n, at_2n, _ = _slope(test_client, c, "owner", filter="my")
    assert slope <= MY_SLOPE_AFTER, (at_n, at_2n, slope)
    assert slope <= SLOPE_BEFORE, (at_n, at_2n, slope)  # the plan's bound (implied by the one above)

    # Shared, with the reasons' lookups (my shares, group and project names)
    # under the counter: half of the owner's first cards go to the private
    # project, the other half are shared with the analysts (proj_group is in
    # both).
    ids = [d["id"] for d in _list(test_client, c, "owner", filter="my", limit=2 * N)["dashboards"]]
    for aid in ids[:N]:
        # Direct write: no route moves a dashboard into a project.
        corpus_rows.update("artifacts", aid, project_id=c.cast.projects["private"])
    for aid in ids[N:]:
        visibility = test_client.get(f"/api/artifacts/{aid}/sharing", headers=as_(c, "owner")).json()["visibility"]
        _sharing(test_client, c, aid, visibility, editor_groups=["analysts"])
    # Only the cards proj_group may edit (access=edit): a page that mixed in
    # view-only cards, which skip the pencil's lookups, would measure the mix,
    # not a card. Since review #1 a view-only card's Edited is its publish
    # time, so such a card can sort among these.
    slope, at_n, at_2n, large = _slope(test_client, c, "proj_group", filter="shared", access="edit")
    assert {card["role"] for card in large["dashboards"]} == {"edit"}
    assert slope <= SHARED_SLOPE_AFTER, (at_n, at_2n, slope)
    assert slope <= SHARED_SLOPE_BEFORE, (at_n, at_2n, slope)
    reasons = {r for card in large["dashboards"] for r in card["access"]["reasons"]}
    assert {"group", "project"} <= reasons, reasons
