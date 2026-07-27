"""
RBAC coverage for instruction *count* visibility.

The /agents tree renders per-agent Instruction badges from
GET /api/instructions/counts (-> by_agent[agent_id]), then the node's lazy
list replaces the badge with the actual list length. The two MUST agree —
otherwise the badge flickers (the confirmed "3 -> 0" bug) or, worse, leaks
the existence of an instruction on an agent the caller can't even see.

The count service folds not-yet-in-main *pending* instructions into the same
per-agent surface as the list. That fold must apply the SAME visibility
scoping the list uses (member + public + global): a pending instruction on a
private agent the caller does not belong to must NOT be counted for them.

This test drives that invariant end-to-end for a non-member and a member.
"""
import datetime
import os
import uuid

import pytest
from sqlalchemy import create_engine, text


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _inject_pending_build(iid, org_id):
    """Attach a non-main pending_approval build whose version text DIFFERS from
    main, so the instruction has a LIVE review hunk (i.e. is genuinely pending).

    Mirrors the injection helper in test_instruction.py — named params + Python
    types so it runs on both the sqlite and postgres matrix legs.
    """
    url = os.environ["TEST_DATABASE_URL"]
    sync_url = url.replace("sqlite+aiosqlite:", "sqlite:").replace(
        "postgresql+asyncpg:", "postgresql:"
    )
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            main = conn.execute(
                text(
                    """SELECT bc.build_id AS build_id, iv.text AS text
                         FROM build_contents bc
                         JOIN instruction_builds ib ON ib.id = bc.build_id
                         JOIN instruction_versions iv ON iv.id = bc.instruction_version_id
                        WHERE bc.instruction_id = :iid AND ib.is_main = :is_main
                          AND ib.deleted_at IS NULL"""
                ),
                {"iid": iid, "is_main": True},
            ).mappings().fetchone()
            assert main is not None, "new instruction should be in the main build"
            now = datetime.datetime.utcnow()
            vid, bid, bcid = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
            vnum = conn.execute(
                text(
                    "SELECT COALESCE(MAX(version_number),0)+1 FROM instruction_versions"
                    " WHERE instruction_id=:iid"
                ),
                {"iid": iid},
            ).scalar()
            bnum = conn.execute(
                text(
                    "SELECT COALESCE(MAX(build_number),0)+1 FROM instruction_builds"
                    " WHERE organization_id=:org"
                ),
                {"org": org_id},
            ).scalar()
            # DIFFERENT text from main => a real, live review hunk => pending.
            conn.execute(
                text(
                    "INSERT INTO instruction_versions (id,created_at,updated_at,instruction_id,version_number,text,status,load_mode,content_hash)"
                    " VALUES (:id,:ca,:ua,:iid,:vnum,:txt,:status,:load_mode,:chash)"
                ),
                {"id": vid, "ca": now, "ua": now, "iid": iid, "vnum": vnum,
                 "txt": (main["text"] or "") + " -- proposed edit",
                 "status": "published", "load_mode": "always",
                 "chash": "h" + uuid.uuid4().hex[:12]},
            )
            conn.execute(
                text(
                    "INSERT INTO instruction_builds (id,created_at,updated_at,build_number,status,source,is_main,organization_id,base_build_id,title)"
                    " VALUES (:id,:ca,:ua,:bnum,:status,:source,:is_main,:org,:base,:title)"
                ),
                {"id": bid, "ca": now, "ua": now, "bnum": bnum, "status": "pending_approval",
                 "source": "ai", "is_main": False, "org": org_id,
                 "base": main["build_id"], "title": "pending edit"},
            )
            conn.execute(
                text(
                    "INSERT INTO build_contents (id,created_at,updated_at,build_id,instruction_id,instruction_version_id)"
                    " VALUES (:id,:ca,:ua,:bid,:iid,:vid)"
                ),
                {"id": bcid, "ca": now, "ua": now, "bid": bid, "iid": iid, "vid": vid},
            )
    finally:
        engine.dispose()


@pytest.mark.e2e
def test_pending_count_scoped_to_agent_membership(
    test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source
):
    """A pending instruction on a PRIVATE agent must be counted only for members.

    by_agent[X] must equal the per-agent list length the caller would see:
      - non-member admin: 0 == 0 (must not leak the pending instruction)
      - creator/member:   1 == 1
    """
    creator = bootstrap_admin("creator")
    org_id = creator["org_id"]

    # Private agent — access requires explicit membership; creator is a member.
    ds = sqlite_data_source(name="private_agent", user_token=creator["token"], org_id=org_id)
    ds_id = ds["id"]

    # A second full admin who is NOT a member of the private agent.
    second_admin = invite_user_to_org(
        org_id=org_id, admin_token=creator["token"], role="admin"
    )

    # Create an instruction attached to the agent (lands in main, published),
    # then make it pending via a divergent non-main build.
    resp = test_client.post(
        "/api/instructions",
        json={
            "text": "Agent rule alpha",
            "status": "published",
            "category": "general",
            "data_source_ids": [ds_id],
        },
        headers=_hdr(creator["token"], org_id),
    )
    assert resp.status_code == 200, resp.json()
    iid = resp.json()["id"]
    _inject_pending_build(iid, org_id)

    def by_agent_count(token):
        r = test_client.get("/api/instructions/counts", headers=_hdr(token, org_id))
        assert r.status_code == 200, r.json()
        return r.json()["by_agent"].get(ds_id, 0)

    def list_count(token):
        r = test_client.get(
            "/api/instructions",
            params={"data_source_ids": ds_id, "include_global": "false", "limit": 200,
                    "include_own": "true", "include_drafts": "true", "include_archived": "true"},
            headers=_hdr(token, org_id),
        )
        assert r.status_code == 200, r.json()
        return len(r.json()["items"])

    # Non-member admin: the private agent's pending instruction is invisible.
    # The count must match the list (both 0) — no leak, no 3->0 flicker.
    assert list_count(second_admin["token"]) == 0
    assert by_agent_count(second_admin["token"]) == 0, (
        "pending instruction on a private agent must not be counted for a non-member"
    )
    assert by_agent_count(second_admin["token"]) == list_count(second_admin["token"])

    # Creator (a member) sees it in both the count and the list.
    assert list_count(creator["token"]) == 1
    assert by_agent_count(creator["token"]) == 1
    assert by_agent_count(creator["token"]) == list_count(creator["token"])


@pytest.mark.e2e
def test_pending_count_visible_on_public_agent(
    test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source
):
    """On a PUBLIC agent the pending instruction is visible to every member, so
    the badge count matches the list for a non-owner member too."""
    creator = bootstrap_admin("creator")
    org_id = creator["org_id"]

    ds = sqlite_data_source(
        name="public_agent", user_token=creator["token"], org_id=org_id, is_public=True
    )
    ds_id = ds["id"]

    member = invite_user_to_org(org_id=org_id, admin_token=creator["token"])

    resp = test_client.post(
        "/api/instructions",
        json={
            "text": "Public agent rule",
            "status": "published",
            "category": "general",
            "data_source_ids": [ds_id],
        },
        headers=_hdr(creator["token"], org_id),
    )
    assert resp.status_code == 200, resp.json()
    _inject_pending_build(resp.json()["id"], org_id)

    def by_agent_count(token):
        r = test_client.get("/api/instructions/counts", headers=_hdr(token, org_id))
        assert r.status_code == 200, r.json()
        return r.json()["by_agent"].get(ds_id, 0)

    def list_count(token):
        r = test_client.get(
            "/api/instructions",
            params={"data_source_ids": ds_id, "include_global": "false", "limit": 200,
                    "include_own": "true", "include_drafts": "true", "include_archived": "true"},
            headers=_hdr(token, org_id),
        )
        assert r.status_code == 200, r.json()
        return len(r.json()["items"])

    # Public agent → visible to the plain member; badge matches the list.
    assert list_count(member["token"]) == 1
    assert by_agent_count(member["token"]) == 1
    assert by_agent_count(member["token"]) == list_count(member["token"])
