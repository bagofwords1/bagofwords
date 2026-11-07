"""A dashboard reaches each recipient once (findability P3b, ADR-4).

The Shared tab tells a reader what is new by when a dashboard reached them:
the `created_at` of their share row. So a sharing save keeps the rows of the
people and groups it keeps, a role change edits the row in place, and an
ownership transfer dates the previous owner's edit row at the dashboard's
creation (they had it all along). Who ends up with which grant is exactly as
before.

No API returns a share's `created_at` or row id, so these tests observe the
`artifact_shares` rows directly; every change still goes through the API.
"""
from datetime import datetime

import pytest
from sqlalchemy import select

from tests.e2e.dashboards.dashboard_setup import as_, own, publish

DASH = "conv_three_dash/dash_sales"
# Share rows are moved back to this time after they are made, so "kept" and
# "made again" differ by far more than the clock's resolution.
EARLIER = datetime(2026, 1, 1)


def _share_rows(corpus_rows, aid):
    """{("user"|"group", principal id): (row id, role, created_at)}.

    An observation: no API returns a share's row id or created_at."""
    shares = corpus_rows.table("artifact_shares")
    with corpus_rows.engine.connect() as conn:
        rows = conn.execute(select(shares.c.id, shares.c.user_id, shares.c.group_id, shares.c.role,
                                   shares.c.created_at).where(shares.c.artifact_id == aid)).all()
    return {(("user", r.user_id) if r.user_id else ("group", r.group_id)): (r.id, r.role, r.created_at)
            for r in rows}


def _date_shares_earlier(corpus_rows, aid):
    """A direct write: the API cannot make a share in the past, and the tests
    need shares older than the save under test."""
    for row_id, _, _ in _share_rows(corpus_rows, aid).values():
        corpus_rows.update("artifact_shares", row_id, created_at=EARLIER)
    return _share_rows(corpus_rows, aid)


def _user(corpus, person):
    return ("user", corpus.cast.id(person))


def _group(corpus, name):
    return ("group", corpus.cast.groups[name])


def _save(test_client, corpus, aid, visibility, *, users=None, groups=None, editors=None,
          editor_groups=None, by="owner"):
    cast = corpus.cast
    body = {"visibility": visibility}
    for key, people in (("user_ids", users), ("editor_user_ids", editors)):
        if people is not None:
            body[key] = [cast.id(p) for p in people]
    for key, names in (("group_ids", groups), ("editor_group_ids", editor_groups)):
        if names is not None:
            body[key] = [cast.groups[g] for g in names]
    resp = test_client.put(f"/api/artifacts/{aid}/sharing", json=body, headers=as_(corpus, by))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _grants(test_client, corpus, aid):
    resp = test_client.get(f"/api/artifacts/{aid}/sharing", headers=as_(corpus, "owner"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    return {k: body[k] for k in ("visibility", "user_ids", "group_ids", "editor_user_ids", "editor_group_ids")}


# The grants every case starts from: shared with direct_share and the
# analysts group to view, and with member to edit.
BASE = {"users": ["direct_share"], "groups": ["analysts"], "editors": ["member"], "editor_groups": []}

# (case, the save, the grants after it). Expected values were written by hand
# from the sharing rules and checked against the code before P3b changed it.
SAVES = [
    ("editors only",
     {"visibility": "shared", "editors": ["group_share"]},
     {"visibility": "shared", "users": ["direct_share"], "groups": ["analysts"],
      "editors": ["group_share"], "editor_groups": []}),
    ("viewers only",
     {"visibility": "shared", "users": ["forker"]},
     {"visibility": "shared", "users": ["forker"], "groups": ["analysts"],
      "editors": ["member"], "editor_groups": []}),
    ("both",
     {"visibility": "shared", "users": ["forker"], "groups": [], "editors": ["chat_viewer"],
      "editor_groups": ["analysts"]},
     {"visibility": "shared", "users": ["forker"], "groups": [],
      "editors": ["chat_viewer"], "editor_groups": ["analysts"]}),
    ("a person in both lists is an editor",
     {"visibility": "shared", "users": ["direct_share", "forker"], "editors": ["direct_share"]},
     {"visibility": "shared", "users": ["forker"], "groups": ["analysts"],
      "editors": ["direct_share"], "editor_groups": []}),
    ("a viewer who is already an editor stays one",
     {"visibility": "shared", "users": ["member", "forker"]},
     {"visibility": "shared", "users": ["forker"], "groups": ["analysts"],
      "editors": ["member"], "editor_groups": []}),
    ("a group in both lists is an editor",
     {"visibility": "shared", "groups": ["analysts", "former"], "editor_groups": ["analysts"]},
     {"visibility": "shared", "users": ["direct_share"], "groups": ["former"],
      "editors": ["member"], "editor_groups": ["analysts"]}),
    ("a group moves from view to edit",
     {"visibility": "shared", "editor_groups": ["analysts", "former"]},
     {"visibility": "shared", "users": ["direct_share"], "groups": [],
      "editors": ["member"], "editor_groups": ["analysts", "former"]}),
    ("the owner is never a grant",
     {"visibility": "shared", "users": ["owner", "forker"], "editors": ["owner", "member"]},
     {"visibility": "shared", "users": ["forker"], "groups": ["analysts"],
      "editors": ["member"], "editor_groups": []}),
    ("lists left out keep their grants",
     {"visibility": "shared"},
     {"visibility": "shared", "users": ["direct_share"], "groups": ["analysts"],
      "editors": ["member"], "editor_groups": []}),
    ("internal drops view grants and keeps edit grants",
     {"visibility": "internal", "users": ["forker"]},
     {"visibility": "internal", "users": [], "groups": [], "editors": ["member"], "editor_groups": []}),
    ("internal with viewers and editors given",
     {"visibility": "internal", "users": ["forker"], "groups": ["former"], "editors": ["chat_viewer"],
      "editor_groups": ["analysts"]},
     {"visibility": "internal", "users": [], "groups": [], "editors": ["chat_viewer"],
      "editor_groups": ["analysts"]}),
    ("public with viewers and editors given",
     {"visibility": "public", "users": ["forker"], "editors": ["chat_viewer"]},
     {"visibility": "public", "users": [], "groups": [], "editors": ["chat_viewer"], "editor_groups": []}),
    ("none, with new editors",
     {"visibility": "none", "editors": ["forker"], "editor_groups": ["former"]},
     {"visibility": "none", "users": [], "groups": [], "editors": ["forker"], "editor_groups": ["former"]}),
]


@pytest.mark.e2e
@pytest.mark.parametrize("save,expected", [(s, e) for _, s, e in SAVES], ids=[c for c, _, _ in SAVES])
def test_the_grants_after_a_save_are_as_before(corpus, corpus_rows, test_client, save, expected):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    _save(test_client, corpus, aid, "shared", **BASE)
    save = dict(save)
    _save(test_client, corpus, aid, save.pop("visibility"), **save)
    cast = corpus.cast
    assert _grants(test_client, corpus, aid) == {
        "visibility": expected["visibility"],
        "user_ids": sorted(cast.id(p) for p in expected["users"]),
        "group_ids": sorted(cast.groups[g] for g in expected["groups"]),
        "editor_user_ids": sorted(cast.id(p) for p in expected["editors"]),
        "editor_group_ids": sorted(cast.groups[g] for g in expected["editor_groups"]),
    }


@pytest.mark.e2e
def test_resaving_sharing_keeps_existing_share_rows(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    _save(test_client, corpus, aid, "shared", users=["direct_share"], groups=["analysts"], editors=["member"])
    before = _date_shares_earlier(corpus_rows, aid)
    kept = [_user(corpus, "direct_share"), _user(corpus, "member"), _group(corpus, "analysts")]
    assert set(before) == set(kept)

    _save(test_client, corpus, aid, "shared", users=["direct_share", "forker"], groups=["analysts"],
          editors=["member"])
    after = _share_rows(corpus_rows, aid)
    for key in kept:
        assert after[key] == before[key], key
    new_id, role, created_at = after[_user(corpus, "forker")]
    assert role == "view" and created_at > EARLIER
    assert new_id not in {row_id for row_id, _, _ in before.values()}


@pytest.mark.e2e
def test_changing_a_role_updates_the_row_in_place(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    _save(test_client, corpus, aid, "shared", users=["direct_share"])
    key = _user(corpus, "direct_share")
    row_id, role, created_at = _date_shares_earlier(corpus_rows, aid)[key]
    assert role == "view"

    _save(test_client, corpus, aid, "shared", users=["direct_share"], editors=["direct_share"])
    assert _share_rows(corpus_rows, aid)[key] == (row_id, "edit", created_at)

    _save(test_client, corpus, aid, "shared", users=["direct_share"], editors=[])
    assert _share_rows(corpus_rows, aid)[key] == (row_id, "view", created_at)

    # The same for a group.
    _save(test_client, corpus, aid, "shared", groups=["analysts"])
    key = _group(corpus, "analysts")
    row_id, role, created_at = _date_shares_earlier(corpus_rows, aid)[key]
    assert role == "view"

    _save(test_client, corpus, aid, "shared", groups=["analysts"], editor_groups=["analysts"])
    assert _share_rows(corpus_rows, aid)[key] == (row_id, "edit", created_at)

    _save(test_client, corpus, aid, "shared", groups=["analysts"], editor_groups=[])
    assert _share_rows(corpus_rows, aid)[key] == (row_id, "view", created_at)


@pytest.mark.e2e
def test_a_share_removed_while_its_role_changes_is_skipped(corpus, corpus_rows, test_client):
    """Another save (or a group's deletion) removes a share after this save
    read the rows and before it writes the new role. This save still
    succeeds and writes the rest; the removed share stays removed. The other
    writer is played by a separate connection at the moment this request
    flushes (no route can be paused there)."""
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    from app.models.artifact_release import ArtifactShare

    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    _save(test_client, corpus, aid, "shared", users=["direct_share"], groups=["analysts"])
    row_id = _share_rows(corpus_rows, aid)[_user(corpus, "direct_share")][0]
    raced = []

    def the_other_save_removes_it_first(session, flush_context, instances):
        # The save's first flush of share changes (member's new edit row).
        if not raced and any(isinstance(o, ArtifactShare) for o in (*session.new, *session.dirty)):
            raced.append(True)
            shares = corpus_rows.table("artifact_shares")
            with corpus_rows.engine.begin() as conn:
                conn.execute(shares.delete().where(shares.c.id == row_id))

    event.listen(Session, "before_flush", the_other_save_removes_it_first)
    try:
        body = _save(test_client, corpus, aid, "shared", users=["direct_share"], groups=["analysts"],
                     editors=["direct_share", "member"])
    finally:
        event.remove(Session, "before_flush", the_other_save_removes_it_first)
    assert raced == [True]  # the race did happen
    cast = corpus.cast
    assert (body["user_ids"], body["group_ids"], body["editor_user_ids"]) == (
        [], [cast.groups["analysts"]], [cast.id("member")])
    assert _grants(test_client, corpus, aid)["editor_user_ids"] == [cast.id("member")]


@pytest.mark.e2e
def test_removing_then_adding_again_makes_a_new_row(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    _save(test_client, corpus, aid, "shared", users=["direct_share"])
    key = _user(corpus, "direct_share")
    row_id, _, created_at = _date_shares_earlier(corpus_rows, aid)[key]

    _save(test_client, corpus, aid, "shared", users=[])
    assert key not in _share_rows(corpus_rows, aid)
    _save(test_client, corpus, aid, "shared", users=["direct_share"])
    again_id, role, again_at = _share_rows(corpus_rows, aid)[key]
    assert role == "view" and again_id != row_id and again_at > created_at


@pytest.mark.e2e
def test_leaving_shared_drops_view_rows_and_keeps_edit_rows(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    publish(aid)
    _save(test_client, corpus, aid, "shared", users=["direct_share"], groups=["analysts"],
          editors=["member"], editor_groups=["former"])
    before = _date_shares_earlier(corpus_rows, aid)

    _save(test_client, corpus, aid, "internal")
    after = _share_rows(corpus_rows, aid)
    editors = [_user(corpus, "member"), _group(corpus, "former")]
    assert set(after) == set(editors)
    for key in editors:
        assert after[key] == before[key] and after[key][1] == "edit", key


@pytest.mark.e2e
def test_transfer_dates_the_previous_owners_share_at_creation(corpus, corpus_rows, test_client):
    aid = own(corpus_rows, corpus, DASH)
    _save(test_client, corpus, aid, "none", editors=["member"])
    moved = test_client.post(f"/api/artifacts/{aid}/transfer", json={"user_id": corpus.cast.id("member")},
                             headers=as_(corpus, "owner"))
    assert moved.status_code == 200, moved.text
    assert moved.json()["editor_user_ids"] == [corpus.cast.id("owner")]

    artifacts = corpus_rows.table("artifacts")
    with corpus_rows.engine.connect() as conn:
        created = conn.execute(select(artifacts.c.created_at).where(artifacts.c.id == aid)).scalar_one()
    rows = _share_rows(corpus_rows, aid)
    assert set(rows) == {_user(corpus, "owner")}
    _, role, created_at = rows[_user(corpus, "owner")]
    assert role == "edit" and created_at == created
