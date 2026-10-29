"""Before and after the data migration, every reader sees the same thing.

"Before" is the golden files: what each kind of reader got from a
conversation's /r link on main. "After" is the same corpus migrated
(dashfc08): the same reader follows the same old link to its dashboard
(GET /api/r/{report}/dashboard, what the /r page asks) and opens it on /d --
the published dashboard, or the draft for those who may read it. Every
conversation a reader could open, they open; every chart holds the rows it
held; nobody opens anything they could not.

The differences are the ones decided, listed in ALLOWED with the decision.
"""
import pytest
from sqlalchemy import text

from tests.e2e.dashboards.dashboard_setup import as_
from tests.e2e.dashboards.test_visibility_baseline import GROUPS
from tests.fixtures.dashboard_corpus.snapshot import golden_path, load_bodies

# Conversations whose link stays a conversation page: there is no dashboard
# to open (decision 7: a conversation without one is served as before).
NO_DASHBOARD = {"conv_no_dash", "shared_no_dash", "legacy_widgets", "widgets_cron", "widget_elsewhere_home"}

# (reader, conversation) -> why the reader's answer is allowed to differ;
# "*" for every reader.
ALLOWED: dict[tuple[str, str], str] = {
    # Decision 6: a deleted (archived) conversation nobody else could open
    # takes its dashboards with it, as deleting one does since phase 7.
    ("*", "archived_private"): "decision 6",
    ("*", "fork_retired"): "decision 6",
    ("*", "in_project_deleted"): "decision 6",  # archived with its deleted project, never shared
}


def _allowed(reader, key):
    return (reader, key) in ALLOWED or ("*", key) in ALLOWED


def _golden(reader):
    import json

    return json.loads(golden_path(reader).read_text(encoding="utf-8"))["reports"]


def _body(bodies, ref):
    return bodies[ref.lstrip("#")]


def _title(corpus_rows, query_id):
    with corpus_rows.engine.connect() as conn:
        return conn.execute(text("SELECT title FROM queries WHERE id = :q"), {"q": query_id}).scalar_one()


def _open(test_client, corpus, did, reader):
    """Where the reader lands: '/d' or '/d/draft', or None (refused)."""
    headers = as_(corpus, reader)
    resp = test_client.get(f"/api/d/{did}", headers=headers)
    if resp.status_code == 200:
        return f"/api/d/{did}"
    if resp.status_code == 404 and resp.json().get("error_code") == "dashboard.not_published":
        if test_client.get(f"/api/d/{did}/draft", headers=headers).status_code == 200:
            return f"/api/d/{did}/draft"
    return None


@pytest.mark.e2e
@pytest.mark.parametrize("group", sorted(GROUPS))
def test_every_reader_opens_the_same_dashboards_with_the_same_rows(migrated_corpus, corpus_rows, test_client,
                                                                    group):
    c = migrated_corpus
    bodies = load_bodies()
    problems = []
    for reader in GROUPS[group]:
        for key, before in _golden(reader).items():
            if key not in c.reports or _allowed(reader, key):
                continue
            opened_before = before.get("r", {}).get("status") == 200 and before.get("r_shows")
            link = test_client.get(f"/api/r/{c.reports[key]}/dashboard")
            if key in NO_DASHBOARD:
                if link.status_code != 404:
                    problems.append(f"{reader}/{key}: a dashboard appeared for a conversation without one")
                continue
            if link.status_code != 200:
                if opened_before:
                    problems.append(f"{reader}/{key}: the old link leads nowhere ({link.status_code})")
                continue
            did = link.json()["dashboard_id"]
            base = _open(test_client, c, did, reader)
            if bool(base) != bool(opened_before):
                problems.append(f"{reader}/{key}: opened before={bool(opened_before)}, after={bool(base)}")
                continue
            if not base:
                continue
            after = {q["title"]: q["id"] for q in test_client.get(f"{base}/queries", headers=as_(c, reader)).json()}
            # The charts the page showed: the queries of the shown dashboard
            # (r_queries_shown), not every query of the conversation.
            shown = {q.get("title") for q in _body(bodies, before["r_queries_shown"]["body"])} \
                if (before.get("r_queries_shown") or {}).get("status") == 200 else set()
            # Nothing a reader did not have: no query beyond those main's /r
            # answered them (its list held the page's and their hidden
            # dependencies -- the ones a release copies with it).
            if (before.get("r_queries") or {}).get("status") == 200:
                had = {q.get("title") for q in _body(bodies, before["r_queries"]["body"])}
                extra = set(after) - had
                if extra:
                    problems.append(f"{reader}/{key}: new queries {sorted(extra)}")
            for qname, answer in (before.get("r_steps") or {}).items():
                if answer.get("status") != 200 or _title(corpus_rows, c.ids[qname]) not in shown:
                    continue
                was = _body(bodies, answer["body"])
                title = _title(corpus_rows, c.ids[qname])
                if title not in after:
                    problems.append(f"{reader}/{key}: chart {title!r} is gone")
                    continue
                now = test_client.get(f"{base}/queries/{after[title]}/step", headers=as_(c, reader)).json()
                if ((now.get("data") or {}).get("rows") or []) != ((was.get("data") or {}).get("rows") or []):
                    problems.append(f"{reader}/{key}: {title!r} holds different rows")
                if bool(now.get("snapshot_withheld")) != bool(was.get("snapshot_withheld")):
                    problems.append(f"{reader}/{key}: {title!r} withheld before={was.get('snapshot_withheld')}, "
                                    f"after={now.get('snapshot_withheld')}")
                if bool(now.get("viewer_result")) != bool(was.get("viewer_result")):
                    problems.append(f"{reader}/{key}: {title!r} own result before={bool(was.get('viewer_result'))}, "
                                    f"after={bool(now.get('viewer_result'))}")
    assert not problems, "\n".join(problems)


# Schedule values main stored for "no schedule"; the list now answers null.
NO_SCHEDULE = (None, "", "None")
# Decision 7: the widget-era schedule is removed with its job.
SCHEDULE_REMOVED = {"widgets_cron"}


@pytest.mark.e2e
@pytest.mark.parametrize("group", sorted(GROUPS))
def test_every_reader_lists_the_same_conversations(migrated_corpus, corpus_rows, group):
    """The conversations list (every filter, as the golden files record it)
    reads the same after the migration: the same cards, in the same order,
    with the same badges and schedule icons -- which now come from the
    dashboards (decision 15), filled by the migration from the conversations."""
    import json

    from fastapi.testclient import TestClient

    from main import app
    from tests.fixtures.dashboard_corpus.snapshot import Snapshotter

    c = migrated_corpus
    removed = {f"<{k}>" for k in SCHEDULE_REMOVED}  # ids as the files name them
    key_of = {rid: key for key, rid in c.reports.items()}
    with corpus_rows.engine.connect() as conn:
        internal = {f"<{key_of[str(r[0])]}>" for r in conn.execute(text(
            "SELECT legacy_report_id FROM artifacts WHERE legacy_report_id IS NOT NULL "
            "AND visibility = 'internal'")) if str(r[0]) in key_of}
    snapper = Snapshotter(TestClient(app, raise_server_exceptions=False), c)
    bodies = load_bodies()
    problems = []
    for reader in GROUPS[group]:
        before = _cards(json.loads(golden_path(reader).read_text(encoding="utf-8"))["lists"], bodies)
        after = _cards(snapper.lists(c.cast.people[reader]), None)
        # Card order within each list (the files sort the list labels).
        if sorted(before, key=lambda k: k[0]) != sorted(after, key=lambda k: k[0]):
            problems.append(f"{reader}: the lists hold other cards or another order")
            continue
        for key, was in before.items():
            now = after[key]
            for field in set(was) | set(now):
                old, new = was.get(field), now.get(field)
                if field == "cron_schedule" and (old in NO_SCHEDULE or key[2] in removed) and new is None:
                    continue
                # A reader's picture is the release's: the migration's copy of
                # the picture the shown version had (2026-10-21 review).
                if field == "thumbnail_url" and old and str(new or "").startswith("/thumbnails/release-"):
                    continue
                # A service account has no membership: it opens no 'internal'
                # dashboard (phase 4), so it gets none of their pictures.
                if field == "thumbnail_url" and reader == "service_account" and new is None and key[2] in internal:
                    continue
                if old != new:
                    problems.append(f"{reader}: {key[0]} {key[1]}: {field} {old!r} -> {new!r}")
    assert not problems, "\n".join(problems[:40])


def _cards(lists, bodies):
    """(list label, card title, card id) -> card, in list order."""
    out = {}
    for label, pages in lists.items():
        if not isinstance(pages, list):
            continue
        for page in pages:
            if not isinstance(page, dict):
                continue
            for card in page.get("cards") or []:
                body = bodies[card["body"].lstrip("#")] if bodies is not None else card["body"]
                out[(label, body.get("title"), body.get("id"))] = body
    return out
