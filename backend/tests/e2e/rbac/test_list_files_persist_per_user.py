"""list_files persist=true on a per-user (user_required) connection.

Contract: the saved file holds ONLY the calling user's own files — the rows of
that user's own catalog, built under their own credentials — and never a file
that only the service account (the shared catalog) can see. A user whose own
files are not indexed gets a refusal, not the shared catalog. The file belongs
to its creator (File.user_id) and follows the report's file access: someone
who cannot open the report can neither download nor load it.

The only simulated system is the SharePoint 2010 SOAP server (with per-identity
permissions). Everything else is real and seeded through the API: users,
agent, the shared index run, the switch to per-user, each user's own NTLM
credentials and their own index refresh.
"""
import uuid

import pytest

from tests.e2e.rbac.test_list_files_persist import (
    _h,
    _load_in_new_turn,
    list_files,
    report_file_count,
    run_async,
)
from tests.unit.test_sharepoint_2010_client import ROOT, SITE, legacy  # noqa: F401

pytestmark = pytest.mark.e2e

SERVICE, ALICE, BOB = "DOMAIN\\reader", "DOMAIN\\alice", "DOMAIN\\bob"


def _owner(path):
    """Who may see a library path: alice's cases, bob's cases, shared, or
    the service account only."""
    top = path[len(ROOT) + 1:].split("/", 1)[0]
    if top.startswith("Case A"):
        return {ALICE}
    if top.startswith("Case B"):
        return {BOB}
    if top.startswith("Shared"):
        return {ALICE, BOB}
    return set()  # "Restricted ..." folders: the service account alone


def visible(who):
    return lambda path: who == SERVICE or who in _owner(path)


@pytest.fixture
def acl_server(legacy):  # noqa: F811
    blobs = {}
    for top, n in (("Case A", 4), ("Case B", 3), ("Shared", 2), ("Restricted", 3)):
        for i in range(n):
            folder = f"{ROOT}/{top}-{i:02d}"
            blobs[f"{folder}/brief.pdf"] = b"brief"
            blobs[f"{folder}/notes-{i}.txt"] = b"notes"
            if i % 2 == 0:
                blobs[f"{folder}/Mail/מכתב.txt"] = b"letter"
    legacy.blobs = blobs
    original = legacy.handle

    def handle(method, url, data=None):
        # Serve each request with only what its NTLM identity may see.
        who = legacy.identities[-1] if legacy.identities else None
        full = legacy.blobs
        legacy.blobs = {p: b for p, b in full.items() if visible(who)(p)}
        try:
            return original(method, url, data)
        finally:
            legacy.blobs = full
    legacy.handle = handle
    legacy.page_size = 50
    return legacy


def _rel(paths):
    return {p[len(ROOT) + 1:] for p in paths}


@pytest.fixture
def world(acl_server, bootstrap_admin, invite_user_to_org, create_data_source, grant_resource, create_report,
          test_client):
    admin = bootstrap_admin("admin")
    org = admin["org_id"]
    ds = create_data_source(
        name=f"Legal library {uuid.uuid4().hex[:4]}", type="sharepoint_onprem",
        config={"site_url": SITE, "drive_name": "Documents", "api_mode": "2010", "recursive": True,
                "index_mode": "metadata"},
        credentials={"username": SERVICE, "password": "synthetic"}, user_token=admin["token"], org_id=org)
    ha = _h(admin["token"], org)
    # The shared (service-account) index: every file, including Restricted ones.
    assert test_client.get(f"/api/data_sources/{ds['id']}/refresh_schema", headers=ha).status_code == 200
    conn_id = test_client.get(f"/api/data_sources/{ds['id']}/connections", headers=ha).json()[0]["id"]
    r = test_client.put(f"/api/connections/{conn_id}", headers=ha,
                        json={"auth_policy": "user_required", "allowed_user_auth_modes": ["ntlm"]})
    assert r.status_code == 200, r.text

    member = invite_user_to_org(org_id=org, admin_token=admin["token"])
    outsider = invite_user_to_org(org_id=org, admin_token=admin["token"])
    for who in (member, outsider):
        g = grant_resource(resource_type="data_source", resource_id=ds["id"], principal_type="user",
                           principal_id=who["user_id"], permissions=["view"], user_token=admin["token"], org_id=org)
        assert g.status_code in (200, 201), g.text
    # Each user signs in with their own Windows identity and indexes their own files.
    for who, identity in ((admin, ALICE), (member, BOB)):
        h = _h(who["token"], org)
        r = test_client.post(f"/api/connections/{conn_id}/my-credentials", headers=h,
                             json={"auth_mode": "ntlm", "credentials": {"username": identity, "password": "pw"}})
        assert r.status_code == 200, r.text
        assert test_client.get(f"/api/data_sources/{ds['id']}/refresh_schema", headers=h).status_code == 200
    # The outsider signs in with their own identity but never indexes their files.
    r = test_client.post(f"/api/connections/{conn_id}/my-credentials", headers=_h(outsider["token"], org),
                         json={"auth_mode": "ntlm", "credentials": {"username": "DOMAIN\\carol", "password": "pw"}})
    assert r.status_code == 200, r.text

    reports = {key: create_report(title=f"Review {key}", user_token=who["token"], org_id=org, data_sources=[ds["id"]])
               for key, who in (("admin", admin), ("member", member), ("outsider", outsider))}
    return {"org_id": org, "admin": admin, "member": member, "outsider": outsider, "ds": ds,
            "conn_id": conn_id, "reports": reports, "server": acl_server}


def _shared_catalog(world):
    from sqlalchemy import select

    from app.models.connection_table import ConnectionTable

    async def _fn(db, maker):
        return set((await db.execute(select(ConnectionTable.name).where(
            ConnectionTable.connection_id == world["conn_id"]))).scalars().all())
    return run_async(_fn)


def _file_owner(file_id):
    from app.models.file import File

    async def _fn(db, maker):
        return str((await db.get(File, file_id)).user_id)
    return run_async(_fn)


@pytest.mark.parametrize("actor,identity", [("admin", ALICE), ("member", BOB)])
def test_per_user_persist_saves_only_the_callers_own_files(world, actor, identity):
    everything = _rel(world["server"].blobs)
    own = {p for p in everything if visible(identity)(f"{ROOT}/{p}")}
    service_only = {p for p in everything if not _owner(f"{ROOT}/{p}")}
    assert service_only and own and service_only <= _shared_catalog(world)  # the shared catalog holds them

    out, df = list_files(world, actor=actor, persist=True, recursive=True)
    o = out["output"]
    assert o["success"] and o["persisted"], o
    assert set(df["path"]) == own and len(df) == len(own)
    assert not set(df["path"]) & service_only
    uid = _file_owner(o["file_id"])
    assert o["listed_as"] == f"user:{uid}"

    out, folders = list_files(world, actor=actor, persist=True, group_by="folder")
    assert set(folders["path"]) == {p.split("/", 1)[0] for p in own}


def test_two_users_get_disjoint_private_rows(world):
    _, alice = list_files(world, actor="admin", persist=True, recursive=True)
    _, bob = list_files(world, actor="member", persist=True, recursive=True)
    assert not {p for p in alice["path"] if p.startswith("Case B")}
    assert not {p for p in bob["path"] if p.startswith("Case A")}
    assert {p.split("/")[0] for p in alice["path"]} & {p.split("/")[0] for p in bob["path"]} == {
        f"Shared-{i:02d}" for i in range(2)}


def test_user_without_an_own_index_is_refused_never_given_the_shared_catalog(world):
    before = report_file_count(world, "outsider")
    for inputs in ({"recursive": True}, {"group_by": "folder"}, {"source": "catalog"}):
        out, df = list_files(world, actor="outsider", persist=True, **inputs)
        assert out["output"]["success"] is False and df is None, (inputs, out)
    assert report_file_count(world, "outsider") == before


def test_live_and_cursor_are_refused_on_per_user_too(world):
    for inputs in ({"source": "live", "recursive": True}, {"cursor": "abc", "recursive": True}):
        out, df = list_files(world, actor="member", persist=True, **inputs)
        assert out["output"]["success"] is False and df is None


def test_per_user_listing_is_not_readable_by_someone_outside_the_report(world, test_client):
    out, df = list_files(world, actor="member", persist=True, recursive=True)
    file_id, org = out["output"]["file_id"], world["org_id"]
    assert test_client.get(f"/api/files/{file_id}/content", headers=_h(world["member"]["token"], org)).status_code == 200
    denied = test_client.get(f"/api/files/{file_id}/content", headers=_h(world["outsider"]["token"], org))
    assert denied.status_code in (403, 404)
    loaded, missing = _load_in_new_turn(world, file_id, actor="outsider")
    assert loaded is None and missing == [file_id]
    # Even the other per-user caller in the org (admin of the org but not of
    # this conversation's identity) gets their OWN listing, not this one.
    _, admin_df = list_files(world, actor="admin", persist=True, recursive=True)
    assert set(admin_df["path"]) != set(df["path"])
