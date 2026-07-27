"""GET /instructions?view=light — the projection list and tree surfaces use.

The full row carries the instruction body three ways over (text,
formatted_content, structured_data), so it was capped at 200 per page and every
caller rendered that page as if it were the whole set. The light row drops the
body and the nested user records, which is what makes loading an org's entire
instruction set affordable instead of silently truncating it.
"""
import uuid

import pytest

from app.routes.instruction import FULL_MAX_LIMIT, LIGHT_MAX_LIMIT

# Fields the light projection must NOT carry — the reason it exists.
HEAVY_FIELDS = ("text", "formatted_content", "structured_data", "user", "reviewed_by")
# Fields list/tree surfaces render off each row.
REQUIRED_FIELDS = ("id", "title", "preview", "status", "category", "kind",
                   "load_mode", "source_type", "data_sources", "labels",
                   "created_at", "updated_at")


@pytest.fixture
def agent_with_instructions(create_user, login_user, whoami, create_data_source,
                            create_instruction):
    def _make(n=3, body="short rule"):
        user = create_user()
        token = login_user(user["email"], user["password"])
        org_id = str(whoami(token)["organizations"][0]["id"])
        ds = create_data_source(
            name=f"agent-{uuid.uuid4().hex[:6]}", type="sqlite",
            config={"database": ":memory:"}, credentials={},
            user_token=token, org_id=org_id,
        )
        for i in range(n):
            create_instruction(text=f"{body} {i}", user_token=token, org_id=org_id,
                               status="published", data_source_ids=[ds["id"]])
        return {"org_id": org_id, "ds_id": ds["id"], "count": n,
                "headers": {"Authorization": f"Bearer {token}",
                            "X-Organization-Id": org_id}}
    return _make


def _get(client, ctx, **params):
    q = {"data_source_ids": ctx["ds_id"], "include_global": "true",
         "include_own": "true", "include_drafts": "true"}
    q.update(params)
    return client.get("/api/instructions", params=q, headers=ctx["headers"])


@pytest.mark.e2e
def test_light_view_drops_the_body_but_keeps_what_a_list_renders(test_client,
                                                                 agent_with_instructions):
    ctx = agent_with_instructions(3)
    r = _get(test_client, ctx, view="light", limit=200)
    assert r.status_code == 200, r.text[:400]
    body = r.json()
    assert body["total"] == ctx["count"]
    assert len(body["items"]) == ctx["count"]
    for item in body["items"]:
        for f in HEAVY_FIELDS:
            assert f not in item, f"light row still carries {f}"
        for f in REQUIRED_FIELDS:
            assert f in item, f"light row is missing {f}"


@pytest.mark.e2e
def test_preview_carries_enough_for_the_row_label(test_client, agent_with_instructions):
    """The tree label falls back to the body's first line when there's no title,
    so the light row has to carry a usable prefix."""
    long_body = "Reconcile ledgers against the closed period. " * 40
    ctx = agent_with_instructions(1, body=long_body)
    item = _get(test_client, ctx, view="light", limit=200).json()["items"][0]
    assert item["preview"], "no preview to label the row with"
    assert item["preview"].startswith("Reconcile ledgers")
    # Bounded — the point is not to ship the body.
    assert len(item["preview"]) < len(long_body)


@pytest.mark.e2e
def test_full_view_is_unchanged(test_client, agent_with_instructions):
    """Existing callers keep the complete row."""
    ctx = agent_with_instructions(2)
    body = _get(test_client, ctx, limit=200).json()
    assert body["total"] == ctx["count"]
    for item in body["items"]:
        assert "text" in item and item["text"]
        assert "organization_id" in item


@pytest.mark.e2e
def test_both_views_return_the_same_instructions(test_client, agent_with_instructions):
    """The projection changes the shape of a row, never which rows come back."""
    ctx = agent_with_instructions(5)
    full = _get(test_client, ctx, limit=200).json()
    light = _get(test_client, ctx, view="light", limit=200).json()
    assert light["total"] == full["total"]
    assert [i["id"] for i in light["items"]] == [i["id"] for i in full["items"]]


@pytest.mark.e2e
def test_light_view_may_page_past_the_full_view_ceiling(test_client,
                                                        agent_with_instructions):
    """The cap is a property of the heavy row, so only the light view lifts it.
    A full-view caller asking for more is told, not silently given 200."""
    ctx = agent_with_instructions(1)
    over = FULL_MAX_LIMIT + 1

    assert _get(test_client, ctx, view="light", limit=over).status_code == 200
    assert _get(test_client, ctx, view="light", limit=LIGHT_MAX_LIMIT).status_code == 200

    rejected = _get(test_client, ctx, limit=over)
    assert rejected.status_code >= 400
    assert _get(test_client, ctx, view="light", limit=LIGHT_MAX_LIMIT + 1).status_code == 422


@pytest.mark.e2e
def test_unknown_view_is_rejected(test_client, agent_with_instructions):
    ctx = agent_with_instructions(1)
    assert _get(test_client, ctx, view="tiny").status_code >= 400
