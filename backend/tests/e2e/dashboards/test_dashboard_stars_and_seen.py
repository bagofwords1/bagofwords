"""Stars and the Shared tab's "seen" watermark (dashlist01, findability P3).

- `POST|DELETE /api/artifacts/{dashboard_id}/star`: a personal star on any
  dashboard the reader can open. Each card of `GET /api/artifacts` says
  `is_starred` for the reader. Repeating a star or an unstar is harmless.
  A dashboard the reader cannot open answers exactly as a missing one does,
  so the star route never tells whether a dashboard exists.
- `POST /api/artifacts/shared/seen`: when the reader last looked at the
  Shared tab of this organization. It returns the previous time (none on
  the first visit) and the new one.
"""
import uuid

import pytest

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share

CONV = "conv_three_dash"


def _star(test_client, corpus, person, dashboard_id, *, starred=True, headers=None):
    method = test_client.post if starred else test_client.delete
    return method(f"/api/artifacts/{dashboard_id}/star", headers=headers or as_(corpus, person))


def _starred(test_client, corpus, person, dashboard_id, tab):
    resp = test_client.get("/api/artifacts", params={"filter": tab}, headers=as_(corpus, person))
    assert resp.status_code == 200, resp.text
    return next(d for d in resp.json()["dashboards"] if d["id"] == dashboard_id)["is_starred"]


def _seen(test_client, headers):
    resp = test_client.post("/api/artifacts/shared/seen", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.e2e
def test_owner_and_reader_star_independently(corpus, corpus_rows, test_client):
    sales = own(corpus_rows, corpus, f"{CONV}/dash_sales")
    publish(sales)
    assert share(test_client, corpus, sales, "internal").status_code == 200
    assert _starred(test_client, corpus, "owner", sales, "my") is False
    assert _starred(test_client, corpus, "member", sales, "shared") is False

    resp = _star(test_client, corpus, "owner", sales)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": sales, "is_starred": True}
    assert _starred(test_client, corpus, "owner", sales, "my") is True
    assert _starred(test_client, corpus, "member", sales, "shared") is False  # the owner's, not theirs

    assert _star(test_client, corpus, "member", sales).status_code == 200
    resp = _star(test_client, corpus, "owner", sales, starred=False)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": sales, "is_starred": False}
    assert _starred(test_client, corpus, "owner", sales, "my") is False
    assert _starred(test_client, corpus, "member", sales, "shared") is True  # the member's stays


@pytest.mark.e2e
def test_star_is_idempotent(corpus, corpus_rows, test_client):
    """No existence check before the insert: the second POST meets the unique
    pair, exactly as a concurrent POST would, and still answers 200."""
    sales = own(corpus_rows, corpus, f"{CONV}/dash_sales")
    for _ in range(2):
        resp = _star(test_client, corpus, "owner", sales)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"id": sales, "is_starred": True}
    assert _starred(test_client, corpus, "owner", sales, "my") is True
    for _ in range(2):
        resp = _star(test_client, corpus, "owner", sales, starred=False)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"id": sales, "is_starred": False}
    assert _starred(test_client, corpus, "owner", sales, "my") is False
    # Unstarring removed the row: starring again works.
    assert _star(test_client, corpus, "owner", sales).status_code == 200
    assert _starred(test_client, corpus, "owner", sales, "my") is True


@pytest.mark.e2e
def test_star_on_a_dashboard_the_reader_cannot_open_is_404_like_a_missing_one(corpus, corpus_rows, test_client):
    sales = own(corpus_rows, corpus, f"{CONV}/dash_sales")
    stock = own(corpus_rows, corpus, f"{CONV}/dash_stock")
    for aid in (sales, stock):
        publish(aid)
        assert share(test_client, corpus, aid, "shared", users=["direct_share"]).status_code == 200
    # sales: published, shared with direct_share only -- the member has no grant.
    # stock: a view share on a dashboard that is not published. Sharing needs a
    # publish and the API has no unpublish, so this writes the state a deleted
    # release leaves (the release is set NULL) directly.
    corpus_rows.update("artifacts", stock, published_release_id=None)
    # The owner's own dashboard, deleted: deleting its conversation deletes a
    # never-published dashboard with it (dashboard_lifecycle, soft).
    gone = own(corpus_rows, corpus, "conv_one_dash/dash")
    resp = test_client.delete(f"/api/reports/{corpus.reports['conv_one_dash']}", headers=as_(corpus, "owner"))
    assert resp.status_code == 200, resp.text
    stranger = corpus.cast.people["other_org"].headers(corpus.cast.other_org_id)

    refused = [
        ("member", sales, None),        # shared, no grant
        ("direct_share", stock, None),  # view share on an unpublished dashboard
        ("member", str(uuid.uuid4()), None),  # no such dashboard
        ("other_org", sales, stranger),  # another organization's dashboard
        ("owner", gone, None),          # deleted, even for its owner
    ]
    bodies = []
    for person, dashboard_id, headers in refused:
        for starred in (True, False):
            resp = _star(test_client, corpus, person, dashboard_id, starred=starred, headers=headers)
            assert resp.status_code == 404, (person, dashboard_id, starred, resp.text)
            body = resp.json()
            assert body["error_code"] == "artifact.not_found"  # ErrorCode.ARTIFACT_NOT_FOUND
            bodies.append(body)
    assert all(b == bodies[0] for b in bodies), bodies  # nothing tells them apart
    # The one who may open it can star it.
    assert _star(test_client, corpus, "direct_share", sales).status_code == 200
    assert _starred(test_client, corpus, "direct_share", sales, "shared") is True


@pytest.mark.e2e
def test_star_needs_sign_in(corpus, corpus_rows, test_client):
    sales = own(corpus_rows, corpus, f"{CONV}/dash_sales")
    assert _star(test_client, corpus, "anonymous", sales).status_code == 401
    assert _star(test_client, corpus, "anonymous", sales, starred=False).status_code == 401
    assert test_client.post("/api/artifacts/shared/seen", headers=as_(corpus, "anonymous")).status_code == 401


@pytest.mark.e2e
def test_first_seen_has_no_previous_then_the_next_returns_it(corpus, test_client):
    first = _seen(test_client, as_(corpus, "member"))
    assert first["previous_seen_at"] is None and first["seen_at"]
    second = _seen(test_client, as_(corpus, "member"))
    assert second["previous_seen_at"] == first["seen_at"]
    assert second["seen_at"] >= first["seen_at"]
    # Per user: another reader's first visit has no previous one.
    assert _seen(test_client, as_(corpus, "direct_share"))["previous_seen_at"] is None


@pytest.mark.e2e
def test_seen_is_per_organization(corpus, test_client):
    stranger = corpus.cast.people["other_org"]
    # The admin of the other organization joins this one too.
    resp = test_client.post(f"/api/organizations/{corpus.cast.org_id}/members", headers=as_(corpus, "admin"),
                            json={"organization_id": corpus.cast.org_id, "email": stranger.email, "role": "member"})
    assert resp.status_code == 200, resp.text
    home, here = stranger.headers(corpus.cast.other_org_id), stranger.headers(corpus.cast.org_id)

    at_home = _seen(test_client, home)
    assert at_home["previous_seen_at"] is None
    assert _seen(test_client, here)["previous_seen_at"] is None  # a visit at home is not one here
    again = _seen(test_client, here)
    assert _seen(test_client, home)["previous_seen_at"] == at_home["seen_at"]  # untouched by the visits here
    assert _seen(test_client, here)["previous_seen_at"] == again["seen_at"]


def _seen_rows(corpus_rows, user_id, org_id):
    """Read directly: the API has no route that lists watermark rows."""
    from sqlalchemy import text

    with corpus_rows.engine.connect() as conn:
        return conn.execute(text(
            "SELECT shared_seen_at FROM artifact_list_seen WHERE user_id = :u AND organization_id = :o"),
            {"u": user_id, "o": org_id}).scalars().all()


@pytest.mark.e2e
def test_two_first_visits_at_once_leave_one_watermark(corpus, corpus_rows, test_client):
    """The other first visit writes its row after this one looked and found
    none, just before this one inserts: the unique pair fires, and this visit
    reads that row back and moves it on. The competing visit is played by a
    separate connection at the moment this request flushes its insert (no
    route can be paused there)."""
    from datetime import datetime

    from sqlalchemy import event
    from sqlalchemy.orm import Session

    from app.models.artifact_list_seen import ArtifactListSeen

    uid, oid = corpus.cast.id("member"), corpus.cast.org_id
    other_visit = datetime(2026, 1, 1)
    raced = []

    def the_other_visit_commits_first(session, flush_context, instances):
        if not raced and any(isinstance(o, ArtifactListSeen) and o.user_id == uid for o in session.new):
            raced.append(True)
            corpus_rows.insert("artifact_list_seen", user_id=uid, organization_id=oid, shared_seen_at=other_visit)

    event.listen(Session, "before_flush", the_other_visit_commits_first)
    try:
        body = _seen(test_client, as_(corpus, "member"))
    finally:
        event.remove(Session, "before_flush", the_other_visit_commits_first)
    assert raced == [True]  # the race did happen
    assert body["previous_seen_at"] == other_visit.isoformat()  # the other visit's, read back
    assert body["seen_at"] > body["previous_seen_at"]
    assert len(_seen_rows(corpus_rows, uid, oid)) == 1


@pytest.mark.e2e
def test_the_watermark_never_moves_back(corpus, corpus_rows, test_client):
    """A visit that stamps an earlier time than the stored one (a concurrent
    visit that committed a later time first) leaves the stored time as it is."""
    from datetime import datetime

    from sqlalchemy import text

    uid, oid = corpus.cast.id("member"), corpus.cast.org_id
    _seen(test_client, as_(corpus, "member"))
    later = datetime(2100, 1, 1)
    # The later visit's time, written directly: the API only ever stamps now.
    with corpus_rows.engine.begin() as conn:
        conn.execute(text("UPDATE artifact_list_seen SET shared_seen_at = :t "
                          "WHERE user_id = :u AND organization_id = :o"), {"t": later, "u": uid, "o": oid})
    body = _seen(test_client, as_(corpus, "member"))
    assert body["previous_seen_at"] == later.isoformat()
    assert body["seen_at"] == later.isoformat()
    rows = _seen_rows(corpus_rows, uid, oid)
    assert len(rows) == 1 and str(rows[0]).startswith("2100-01-01")


@pytest.mark.e2e
def test_a_later_visit_committed_just_before_this_ones_write_is_kept(corpus, corpus_rows, test_client):
    """Two visits at once: this one read the stored time, then another visit
    commits a later one just before this one writes. The later time stays.
    The other visit is played by a separate connection at the moment this
    request sends its UPDATE (no route can be paused there)."""
    from datetime import datetime

    from sqlalchemy import event, text
    from sqlalchemy.engine import Engine

    uid, oid = corpus.cast.id("member"), corpus.cast.org_id
    first = _seen(test_client, as_(corpus, "member"))
    later = datetime(2100, 1, 1)
    raced = []

    def the_later_visit_commits_first(conn, cursor, statement, parameters, context, executemany):
        if not raced and statement.lstrip().upper().startswith("UPDATE ARTIFACT_LIST_SEEN"):
            raced.append(True)
            with corpus_rows.engine.begin() as other:
                other.execute(text("UPDATE artifact_list_seen SET shared_seen_at = :t "
                                   "WHERE user_id = :u AND organization_id = :o"), {"t": later, "u": uid, "o": oid})

    event.listen(Engine, "before_cursor_execute", the_later_visit_commits_first)
    try:
        body = _seen(test_client, as_(corpus, "member"))
    finally:
        event.remove(Engine, "before_cursor_execute", the_later_visit_commits_first)
    assert raced == [True]  # the race did happen
    assert body["previous_seen_at"] == first["seen_at"]  # what this visit read before the other one committed
    rows = _seen_rows(corpus_rows, uid, oid)
    assert len(rows) == 1 and str(rows[0]).startswith("2100-01-01")


@pytest.mark.e2e
def test_a_visit_moves_the_rows_updated_at(corpus, corpus_rows, test_client):
    from datetime import datetime

    from sqlalchemy import text

    uid, oid = corpus.cast.id("member"), corpus.cast.org_id
    mine = {"u": uid, "o": oid}
    where = "WHERE user_id = :u AND organization_id = :o"
    _seen(test_client, as_(corpus, "member"))
    # Long ago, written directly: the API only ever stamps now.
    with corpus_rows.engine.begin() as conn:
        conn.execute(text(f"UPDATE artifact_list_seen SET updated_at = :t {where}"), {"t": datetime(2001, 1, 1), **mine})
    _seen(test_client, as_(corpus, "member"))
    with corpus_rows.engine.connect() as conn:
        moved = conn.execute(text(f"SELECT updated_at FROM artifact_list_seen {where}"), mine).scalar_one()
    assert not str(moved).startswith("2001-")


@pytest.fixture
def break_at_flush():
    """Force a real NOT NULL failure: `break_at_flush(Model, column)` clears
    that column of the new row as its request flushes it (no route sends such
    a row). The list it returns holds one entry per row it broke."""
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    listeners = []

    def arm(model, column):
        broken = []

        def clear_the_column(session, flush_context, instances):
            for row in session.new:
                if isinstance(row, model):
                    setattr(row, column, None)
                    broken.append(row)

        event.listen(Session, "before_flush", clear_the_column)
        listeners.append(clear_the_column)
        return broken

    yield arm
    for listener in listeners:
        event.remove(Session, "before_flush", listener)


@pytest.mark.e2e
def test_a_star_that_fails_for_another_reason_is_not_taken_for_a_repeat(corpus, corpus_rows, test_client,
                                                                        break_at_flush):
    """Only the star's own unique pair means "already starred": any other
    integrity failure reaches the reader as a failure, and nothing is stored."""
    from app.models.artifact_star import ArtifactStar

    sales = own(corpus_rows, corpus, f"{CONV}/dash_sales")
    broken = break_at_flush(ArtifactStar, "user_id")
    resp = _star(test_client, corpus, "owner", sales)
    assert broken  # the failure did happen
    assert resp.status_code == 500, resp.text
    assert resp.json()["error_code"] == "generic"  # ErrorCode.INTERNAL
    assert _starred(test_client, corpus, "owner", sales, "my") is False


@pytest.mark.e2e
def test_a_first_visit_that_fails_for_another_reason_is_not_taken_for_a_race(corpus, corpus_rows, test_client,
                                                                             break_at_flush):
    """Only the watermark's own unique pair means "the other visit wrote it":
    any other integrity failure reaches the reader as a failure, and nothing
    is stored."""
    from app.models.artifact_list_seen import ArtifactListSeen

    broken = break_at_flush(ArtifactListSeen, "organization_id")
    resp = test_client.post("/api/artifacts/shared/seen", headers=as_(corpus, "member"))
    assert broken  # the failure did happen
    assert resp.status_code == 500, resp.text
    assert resp.json()["error_code"] == "generic"  # ErrorCode.INTERNAL
    assert _seen_rows(corpus_rows, corpus.cast.id("member"), corpus.cast.org_id) == []
