"""A dashboard's name in its link (phase 8).

The owner names a dashboard in its settings; `/d/{name}` then opens it as
`/d/{id}` does, which keeps working with or without a name. A name is
lowercase English letters, digits and hyphens, 3 to 80 long, unique in the
installation, never shaped like an id, and not one of a few reserved words.
Renaming keeps the old name leading to the same dashboard, and no other
dashboard can take it (artifact_slug_history); removing the name does the
same -- a link already sent never stops working (who may open it is the
dashboard's sharing). A fork has no name.
"""

import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share

SALES = "conv_three_dash/dash_sales"
STOCK = "conv_three_dash/dash_stock"


def _name(test_client, corpus, aid, slug, person="owner"):
    return test_client.put(f"/api/artifacts/{aid}/settings", json={"slug": slug}, headers=as_(corpus, person))


def _open(test_client, corpus, ref, person="member"):
    return test_client.get(f"/api/d/{ref}", headers=as_(corpus, person))


def _published(corpus, corpus_rows, test_client, key=SALES):
    aid = own(corpus_rows, corpus, key)
    publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    return aid


@pytest.mark.e2e
def test_a_named_dashboard_opens_by_its_name_and_by_its_id(corpus, corpus_rows, test_client):
    aid = _published(corpus, corpus_rows, test_client)
    resp = _name(test_client, corpus, aid, "sales-2026")
    assert resp.status_code == 200, resp.text
    assert resp.json()["slug"] == "sales-2026"
    by_name, by_id = _open(test_client, corpus, "sales-2026"), _open(test_client, corpus, aid)
    assert by_name.status_code == 200 and by_name.json()["id"] == aid and by_name.json()["slug"] == "sales-2026"
    assert by_id.status_code == 200
    # The name opens the dashboard for its readers only, as the id does.
    assert _open(test_client, corpus, "sales-2026", person="other_org").status_code == 403


@pytest.mark.e2e
@pytest.mark.parametrize("slug", ["ab", "Sales", "has space", "-sales", "sales-", "a" * 81, "מכירות",
                                  "5f1c417e-5abd-4ed4-99e0-e302108b0953", "admin", "api", "sales/2026"])
def test_a_name_that_breaks_the_rules_is_refused(corpus, corpus_rows, test_client, slug):
    aid = _published(corpus, corpus_rows, test_client)
    resp = _name(test_client, corpus, aid, slug)
    assert resp.status_code == 400 and resp.json()["error_code"] == "dashboard.slug_invalid", (slug, resp.text)


@pytest.mark.e2e
def test_a_name_is_unique(corpus, corpus_rows, test_client):
    sales = _published(corpus, corpus_rows, test_client)
    stock = _published(corpus, corpus_rows, test_client, STOCK)
    assert _name(test_client, corpus, sales, "weekly").status_code == 200
    taken = _name(test_client, corpus, stock, "weekly")
    assert taken.status_code == 409 and taken.json()["error_code"] == "dashboard.slug_taken"
    assert _name(test_client, corpus, sales, "weekly").status_code == 200  # its own name again: nothing changes


@pytest.mark.e2e
def test_a_renamed_dashboards_old_name_keeps_leading_to_it(corpus, corpus_rows, test_client):
    sales = _published(corpus, corpus_rows, test_client)
    stock = _published(corpus, corpus_rows, test_client, STOCK)
    assert _name(test_client, corpus, sales, "sales-2026").status_code == 200
    assert _name(test_client, corpus, sales, "sales").status_code == 200
    old = _open(test_client, corpus, "sales-2026")
    assert old.status_code == 200 and old.json()["id"] == sales and old.json()["slug"] == "sales"
    assert _open(test_client, corpus, "sales").json()["id"] == sales
    # Nobody else takes the old name; the dashboard itself may take it back.
    assert _name(test_client, corpus, stock, "sales-2026").json()["error_code"] == "dashboard.slug_taken"
    assert _name(test_client, corpus, sales, "sales-2026").status_code == 200
    assert _open(test_client, corpus, "sales").json()["id"] == sales  # and "sales" became an old name


@pytest.mark.e2e
def test_removing_the_name_keeps_every_name_link_working(corpus, corpus_rows, test_client):
    sales = _published(corpus, corpus_rows, test_client)
    stock = _published(corpus, corpus_rows, test_client, STOCK)
    for slug in ("sales-2026", "sales"):
        assert _name(test_client, corpus, sales, slug).status_code == 200
    resp = _name(test_client, corpus, sales, "")
    assert resp.status_code == 200 and resp.json()["slug"] is None
    for ref in ("sales-2026", "sales"):
        opened = _open(test_client, corpus, ref)
        assert opened.status_code == 200 and opened.json()["id"] == sales, ref
        assert opened.json()["slug"] is None
        assert _name(test_client, corpus, stock, ref).json()["error_code"] == "dashboard.slug_taken"
    assert _open(test_client, corpus, sales).status_code == 200
    # The owner may take a name back afterwards.
    assert _name(test_client, corpus, sales, "sales").status_code == 200
    assert _open(test_client, corpus, "sales").json()["slug"] == "sales"


@pytest.mark.e2e
def test_only_its_editors_name_the_dashboard(corpus, corpus_rows, test_client):
    """The owner and its editors (collab01: whoever can open its project
    edits it -- the project's members, and the admin, who sees every
    project) name it; a member who only reads it does not."""
    aid = own(corpus_rows, corpus, SALES, project="private")
    publish(aid)
    assert _name(test_client, corpus, aid, "sales", person="member").status_code in (403, 404)
    assert _open(test_client, corpus, "sales", person="owner").status_code == 404
    assert _name(test_client, corpus, aid, "sales", person="proj_viewer").status_code == 200
    assert _open(test_client, corpus, "sales", person="owner").status_code == 200


@pytest.mark.e2e
def test_a_fork_has_no_name(corpus, corpus_rows, test_client, monkeypatch):
    monkeypatch.setattr("app.core.fire_and_forget.spawn", lambda coro: coro.close())
    aid = _published(corpus, corpus_rows, test_client)
    assert _name(test_client, corpus, aid, "sales").status_code == 200
    fork = test_client.post(f"/api/d/{aid}/fork", json={}, headers=as_(corpus, "member"))
    assert fork.status_code == 200, fork.text
    with corpus_rows.engine.connect() as conn:
        slug = conn.execute(text("SELECT slug FROM artifacts WHERE id = :a"),
                            {"a": fork.json()["artifact_id"]}).scalar()
    assert slug is None


@pytest.mark.e2e
def test_a_deleted_dashboards_name_opens_nothing_and_stays_taken(corpus, corpus_rows, test_client):
    from datetime import datetime

    sales = _published(corpus, corpus_rows, test_client)
    stock = _published(corpus, corpus_rows, test_client, STOCK)
    assert _name(test_client, corpus, sales, "sales-2026").status_code == 200
    assert _name(test_client, corpus, sales, "sales").status_code == 200
    corpus_rows.update("artifacts", sales, deleted_at=datetime(2026, 3, 1))
    for ref in ("sales", "sales-2026"):
        assert _open(test_client, corpus, ref).status_code == 404, ref
        assert _name(test_client, corpus, stock, ref).json()["error_code"] == "dashboard.slug_taken"
