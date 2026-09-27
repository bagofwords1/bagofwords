"""Artifact app data: who may read and write which collection, per visibility.

Wiring proof for the two access layers of the app-data endpoints
(`/api/artifacts/{artifact_id}/data/{collection}[/{record_id}]`):

  * Layer 1 is the report's artifact visibility (`_check_visibility`) and
    always runs first; Layer 2 (the collection's scope/create/modify rules)
    can only narrow it (PP1).
  * Anonymous callers never write (PP2): every write is 401 and no row
    changes, whatever the visibility.
  * Signed-in outsiders and anonymous visitors read only owner-written shared
    collections (PP3, RD1); an org admin who does not own the report has no
    special rights (RD2).
  * per_user collections are private to their author: lists never contain
    another user's rows and another user's record is 404 (PP4).
  * The stored author is always the session user (PP5).

One test per visibility. Each seeds ONE artifact carrying all four
collection kinds, then loops every principal over list/create/update/delete.
The exhaustive visibility x rule x principal space is proven by the pure
tests in tests/unit/test_app_data_rules.py; this file proves the routes and
the service apply those rules to real sessions, shares, and rows.

The expected outcomes below are written out from the spec's decision tables
(design spec section 7, plan E1), not computed with the code under test.
"""
import asyncio
import uuid

import pytest
from sqlalchemy import func, select

from app.dependencies import async_session_maker
from app.models.app_record import AppRecord

pytestmark = pytest.mark.e2e


STORAGE = {
    "collections": {
        # shared, members create, authors modify their own
        "notes": {"scope": "shared", "create": "members", "modify": "author",
                  "fields": {"text": {"type": "string"}}},
        # shared, members create, only the owner modifies
        "votes": {"scope": "shared", "create": "members", "modify": "owner",
                  "fields": {"text": {"type": "string"}}},
        # shared, only the owner writes (published to every viewer)
        "news": {"scope": "shared", "create": "owner", "modify": "owner",
                 "fields": {"text": {"type": "string"}}},
        # private to each user
        "prefs": {"scope": "per_user", "fields": {"text": {"type": "string"}}},
    }
}
COLLECTIONS = list(STORAGE["collections"])

# Layer 1: what _check_visibility answers per visibility and principal
# (None = passes). "admin" is an org admin who does not own the report;
# "recipient" is an org member with an explicit artifact share; "member" is an
# org member without one; "outsider" is signed in to a different org.
LAYER1 = {
    "none": {"owner": None, "admin": 404, "member": 404, "recipient": 404, "outsider": 404, "anonymous": 404},
    "shared": {"owner": None, "admin": 403, "member": 403, "recipient": None, "outsider": 403, "anonymous": 401},
    "internal": {"owner": None, "admin": None, "member": None, "recipient": None, "outsider": 403, "anonymous": 401},
    "public": {"owner": None, "admin": None, "member": None, "recipient": None, "outsider": None, "anonymous": None},
}
LAYER1_ERROR = {404: "artifact.not_found", 403: "app_data.forbidden", 401: "app_data.unauthenticated"}

# Principal class for Layer 2 (RD1: outsider like anonymous; RD2: admin = member).
CLASS = {"owner": "owner", "admin": "member", "member": "member", "recipient": "member",
         "outsider": "outsider", "anonymous": "anonymous"}

# Layer 2 per collection and class: (read, create, modify own, modify others').
T, F = True, False
RULES = {
    "notes": {"owner": (T, T, T, T), "member": (T, T, T, F), "outsider": (F, F, F, F), "anonymous": (F, F, F, F)},
    "votes": {"owner": (T, T, T, T), "member": (T, T, F, F), "outsider": (F, F, F, F), "anonymous": (F, F, F, F)},
    "news": {"owner": (T, T, T, T), "member": (T, F, F, F), "outsider": (T, F, F, F), "anonymous": (T, F, F, F)},
    # per_user: others' records are never visible (404 on write, see expected()).
    "prefs": {"owner": (T, T, T, F), "member": (T, T, T, F), "outsider": (F, F, F, F), "anonymous": (F, F, F, F)},
}

# Owner goes last: it is the only principal allowed to delete another user's
# record, so the seed rows stay in place for everyone before it.
PRINCIPAL_ORDER = ["anonymous", "outsider", "admin", "member", "recipient", "owner"]


def expected(visibility, principal, collection, op, target=None):
    """(status, error_code or None) the spec promises for one call."""
    if op != "list" and principal == "anonymous":
        return 401, "app_data.unauthenticated"
    layer1 = LAYER1[visibility][principal]
    if layer1 is not None:
        return layer1, LAYER1_ERROR[layer1]
    read, create, modify_own, modify_others = RULES[collection][CLASS[principal]]
    if op == "list":
        allowed, ok = read, 200
    elif op == "create":
        allowed, ok = create, 201
    else:
        if target == "other" and collection == "prefs":
            return 404, "app_data.record_not_found"
        allowed, ok = (modify_own if target == "own" else modify_others), 200
    if allowed:
        return ok, None
    if principal == "anonymous":
        return 401, "app_data.unauthenticated"
    return 403, "app_data.forbidden"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _auth(token=None):
    return {"Authorization": f"Bearer {token}"} if token else {}


def _org_headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


# Direct DB reads only ASSERT row state (live counts, versions): per_user
# collections hide other users' rows from every API list, so no API caller can
# observe the total.
def _live_count(artifact_id, collection):
    async def _q():
        async with async_session_maker() as db:
            return (await db.execute(
                select(func.count(AppRecord.id)).where(
                    AppRecord.artifact_id == artifact_id,
                    AppRecord.collection == collection,
                    AppRecord.deleted_at.is_(None),
                )
            )).scalar_one()
    return asyncio.run(_q())


def _stored_version(record_id):
    async def _q():
        async with async_session_maker() as db:
            row = await db.get(AppRecord, record_id)
            return None if row is None else (row.version, row.deleted_at, row.user_id)
    return asyncio.run(_q())


class _Api:
    def __init__(self, test_client, artifact_id):
        self.c = test_client
        self.base = f"/api/artifacts/{artifact_id}/data"

    def list(self, coll, token=None):
        return self.c.get(f"{self.base}/{coll}", headers=_auth(token))

    def create(self, coll, data, token=None):
        return self.c.post(f"{self.base}/{coll}", json={"data": data}, headers=_auth(token))

    def update(self, coll, rid, data, version, token=None):
        return self.c.patch(f"{self.base}/{coll}/{rid}", json={"data": data, "version": version}, headers=_auth(token))

    def delete(self, coll, rid, version, token=None):
        return self.c.request("DELETE", f"{self.base}/{coll}/{rid}", json={"version": version}, headers=_auth(token))


def _assert_outcome(resp, want, context):
    status, code = want
    assert resp.status_code == status, (context, resp.status_code, resp.text)
    if code is not None:
        assert resp.json().get("error_code") == code, (context, resp.json())


def _set_visibility(test_client, report_id, owner, visibility, **extra):
    resp = test_client.put(
        f"/api/reports/{report_id}/visibility/artifact",
        json={"visibility": visibility, **extra},
        headers=_org_headers(owner["token"], owner["org_id"]),
    )
    assert resp.status_code == 200, resp.text


def _seed(test_client, bootstrap_admin, invite_user_to_org, create_report, visibility):
    admin = bootstrap_admin("appadmin")
    org_id = admin["org_id"]
    owner = {**invite_user_to_org(org_id=org_id, admin_token=admin["token"]), "org_id": org_id}
    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    recipient = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    outsider = bootstrap_admin("outsider")
    assert outsider["org_id"] != org_id

    report = create_report(title=f"App {uuid.uuid4().hex[:6]}", user_token=owner["token"],
                           org_id=org_id, data_sources=[])
    resp = test_client.post(
        "/api/artifacts",
        json={
            "report_id": report["id"], "title": "Team board", "mode": "page",
            "content": {"code": 'function App() { useCollection("notes"); return null; }',
                        "visualization_ids": [], "runtime_version": 11, "storage": STORAGE},
        },
        headers=_org_headers(owner["token"], org_id),
    )
    assert resp.status_code == 200, resp.text
    artifact_id = resp.json()["artifact_id"]
    api = _Api(test_client, artifact_id)

    # Seed while the recipient holds an artifact share, so it can write the
    # member-authored rows; then move to the visibility under test.
    _set_visibility(test_client, report["id"], owner, "shared", shared_user_ids=[recipient["user_id"]])
    seeds = {"owner": {}, "recipient": {}}
    for coll in COLLECTIONS:
        r = api.create(coll, {"text": f"owner {coll}"}, owner["token"])
        assert r.status_code == 201, r.text
        seeds["owner"][coll] = r.json()["id"]
        if coll != "news":  # members cannot write owner-only collections
            r = api.create(coll, {"text": f"member {coll}"}, recipient["token"])
            assert r.status_code == 201, r.text
            seeds["recipient"][coll] = r.json()["id"]
    if visibility != "shared":
        _set_visibility(test_client, report["id"], owner, visibility)

    people = {
        "owner": owner, "admin": admin, "member": member, "recipient": recipient,
        "outsider": outsider, "anonymous": {"token": None, "user_id": None},
    }
    return api, artifact_id, people, seeds


def _run_matrix(visibility, test_client, bootstrap_admin, invite_user_to_org, create_report):
    api, artifact_id, people, seeds = _seed(test_client, bootstrap_admin, invite_user_to_org,
                                            create_report, visibility)
    versions = {rid: 1 for by in seeds.values() for rid in by.values()}

    for principal in PRINCIPAL_ORDER:
        person = people[principal]
        token, user_id = person["token"], person["user_id"]
        other_author = "recipient" if principal == "owner" else "owner"

        for coll in COLLECTIONS:
            ctx = (visibility, principal, coll)

            # -- list ---------------------------------------------------------
            resp = api.list(coll, token)
            _assert_outcome(resp, expected(visibility, principal, coll, "list"), ctx + ("list",))
            if resp.status_code == 200:
                items = resp.json()["items"]
                ids = {i["id"] for i in items}
                if coll == "prefs":
                    # PP4: only the caller's own rows, never another author's.
                    assert all(i["mine"] and i["user"]["id"] == user_id for i in items), (ctx, items)
                    assert seeds[other_author][coll] not in ids, ctx
                else:
                    # PP3 for outsiders/anonymous: every row of an owner-written collection.
                    assert seeds["owner"][coll] in ids, ctx
                    if coll in seeds["recipient"]:
                        assert seeds["recipient"][coll] in ids, ctx
                    for item in items:
                        assert item["mine"] == (user_id is not None and item["user"]["id"] == user_id), (ctx, item)

            # -- create -------------------------------------------------------
            before = _live_count(artifact_id, coll)
            resp = api.create(coll, {"text": f"by {principal}"}, token)
            want = expected(visibility, principal, coll, "create")
            _assert_outcome(resp, want, ctx + ("create",))
            own_id = None
            if want[0] == 201:
                body = resp.json()
                # PP5: author is the session user, computed server-side.
                assert body["user"]["id"] == user_id and body["mine"] is True and body["version"] == 1, body
                assert _stored_version(body["id"])[2] == user_id
                assert _live_count(artifact_id, coll) == before + 1, ctx
                own_id = body["id"]
                versions[own_id] = 1
            else:
                assert _live_count(artifact_id, coll) == before, ctx  # PP2: no row

            # -- update / delete: the caller's own record, then another author's
            targets = [("own", own_id)] if own_id else []
            other_id = seeds[other_author].get(coll)
            if other_id:
                targets.append(("other", other_id))
            for target, rid in targets:
                want = expected(visibility, principal, coll, "update", target)
                resp = api.update(coll, rid, {"text": f"edit by {principal}"}, versions[rid], token)
                _assert_outcome(resp, want, ctx + ("update", target))
                if want[0] == 200:
                    assert resp.json()["version"] == versions[rid] + 1
                    versions[rid] += 1
                assert _stored_version(rid)[0] == versions[rid], (ctx, "update", target)

                before = _live_count(artifact_id, coll)
                want = expected(visibility, principal, coll, "delete", target)
                resp = api.delete(coll, rid, versions[rid], token)
                _assert_outcome(resp, want, ctx + ("delete", target))
                if want[0] == 200:
                    assert resp.json() == {"id": rid, "deleted": True}
                    assert _live_count(artifact_id, coll) == before - 1, ctx
                    assert _stored_version(rid)[1] is not None  # soft delete keeps the row
                else:
                    assert _live_count(artifact_id, coll) == before, ctx


def test_app_data_access_private_report(test_client, bootstrap_admin, invite_user_to_org, create_report):
    _run_matrix("none", test_client, bootstrap_admin, invite_user_to_org, create_report)


def test_app_data_access_shared_with_users(test_client, bootstrap_admin, invite_user_to_org, create_report):
    _run_matrix("shared", test_client, bootstrap_admin, invite_user_to_org, create_report)


def test_app_data_access_internal_to_org(test_client, bootstrap_admin, invite_user_to_org, create_report):
    _run_matrix("internal", test_client, bootstrap_admin, invite_user_to_org, create_report)


def test_app_data_access_public_report(test_client, bootstrap_admin, invite_user_to_org, create_report):
    _run_matrix("public", test_client, bootstrap_admin, invite_user_to_org, create_report)
