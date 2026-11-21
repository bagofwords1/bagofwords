"""GET /files?mine=true&source_kind=upload&q=&limit= — the prompt-box picker's
"Recent uploads" slice.

The picker promises three things the plain listing does not: only the current
user's OWN uploads (never another user's, never a file the agent fetched or a
tool produced), newest first, and a typeable filename filter. These tests pin
those invariants, and that the no-param listing is unchanged.
"""
import asyncio

import pytest

from tests.e2e.rbac.conftest import invite_user_to_org as invite_user_to_org  # noqa: F401


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _set_source_kind(file_id, kind):
    """Flip a file's origin in the DB — the upload API only ever makes
    'upload' files, so a 'connector'/'artifact' file (which the picker must
    hide) can't be produced through the API. Last-resort direct write."""
    from sqlalchemy import update
    from app.dependencies import async_session_maker
    from app.models.file import File

    async def _go():
        async with async_session_maker() as db:
            await db.execute(update(File).where(File.id == file_id).values(source_kind=kind))
            await db.commit()
    asyncio.run(_go())


@pytest.fixture
def owner(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    return token, org_id


def _recent(test_client, token, org_id, **params):
    r = test_client.get("/api/files", params={"mine": "true", "source_kind": "upload", **params},
                        headers=_h(token, org_id))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.e2e
def test_recent_uploads_are_only_mine_and_newest_first(
    owner, test_client, upload_csv_file, invite_user_to_org,
):
    token, org_id = owner
    a = upload_csv_file(user_token=token, org_id=org_id, filename="alpha.csv")
    b = upload_csv_file(user_token=token, org_id=org_id, filename="bravo.csv")
    c = upload_csv_file(user_token=token, org_id=org_id, filename="charlie.csv")

    # A second member of the same org uploads their own file.
    member = invite_user_to_org(org_id=org_id, admin_token=token)
    other = upload_csv_file(user_token=member["token"], org_id=org_id, filename="theirs.csv")

    mine = _recent(test_client, token, org_id)
    ids = [f["id"] for f in mine]
    assert other["id"] not in ids, "another user's upload must never appear in my recents"
    assert {a["id"], b["id"], c["id"]} <= set(ids)
    # Newest first: the last file uploaded leads, the first trails.
    assert ids[0] == c["id"]
    assert ids.index(a["id"]) > ids.index(b["id"]) > ids.index(c["id"])


@pytest.mark.e2e
def test_recent_uploads_exclude_agent_and_tool_files(owner, test_client, upload_csv_file):
    token, org_id = owner
    mine_file = upload_csv_file(user_token=token, org_id=org_id, filename="mine.csv")
    fetched = upload_csv_file(user_token=token, org_id=org_id, filename="agent_fetched.csv")
    _set_source_kind(fetched["id"], "connector")

    ids = [f["id"] for f in _recent(test_client, token, org_id)]
    assert mine_file["id"] in ids
    assert fetched["id"] not in ids, "a connector-origin file is the agent's, not a recent upload"


@pytest.mark.e2e
def test_filename_filter_is_a_case_insensitive_substring(owner, test_client, upload_csv_file):
    token, org_id = owner
    budget = upload_csv_file(user_token=token, org_id=org_id, filename="Q1_Budget.csv")
    upload_csv_file(user_token=token, org_id=org_id, filename="sales.csv")

    ids = [f["id"] for f in _recent(test_client, token, org_id, q="budget")]
    assert ids == [budget["id"]]


@pytest.mark.e2e
def test_limit_caps_to_the_newest_n(owner, test_client, upload_csv_file):
    token, org_id = owner
    uploaded = [upload_csv_file(user_token=token, org_id=org_id, filename=f"f{i}.csv")["id"]
                for i in range(5)]

    page = _recent(test_client, token, org_id, limit=2)
    assert len(page) == 2
    assert [f["id"] for f in page] == [uploaded[4], uploaded[3]]


@pytest.mark.e2e
def test_unfiltered_listing_is_unchanged(owner, test_client, upload_csv_file):
    """No params → every file the user may see, as before — the picker's
    filters must not change the default contract other callers rely on."""
    token, org_id = owner
    f = upload_csv_file(user_token=token, org_id=org_id, filename="visible.csv")
    r = test_client.get("/api/files", headers=_h(token, org_id))
    assert r.status_code == 200, r.text
    assert any(x["id"] == f["id"] for x in r.json())
