"""What every kind of reader can open and see -- compared with the golden files.

The golden files record main's behaviour when this branch was cut, reviewed
by hand (tests/AGENTS.md rule 9) through SUMMARY.md. Every later change to
them must be a decision of the dashboards project, named in its PR.

Regenerate on purpose only, and without xdist (the groups share bodies.json):
    BOW_UPDATE_GOLDEN=1 TESTING=true uv run pytest -m e2e tests/e2e/dashboards/test_visibility_baseline.py

People are snapshotted in four groups: the corpus is built once per group
(each test gets a fresh database), and four groups keep every test well
inside the CI per-test timeout while xdist runs them side by side.
"""
import json
import os

import pytest

from tests.fixtures.dashboard_corpus.cast import ROLES
from tests.fixtures.dashboard_corpus.snapshot import (
    GOLDEN_DIR,
    Snapshotter,
    dump,
    explain_diff,
    golden_path,
    load_bodies,
    referenced_hashes,
    split_bodies,
    summary_rows,
    write_bodies,
)

GROUPS = {
    "owners_and_admins": ["owner", "admin", "proj_owner", "service_account"],
    "sharing": ["member", "direct_share", "group_share", "group_removed", "anonymous"],
    "projects_and_orgs": ["proj_viewer", "proj_group", "proj_revoked", "other_org", "forker"],
    "data_policy": ["no_creds", "has_results", "bow_scoped", "bow_unscoped", "chat_viewer"],
}
UPDATE = os.environ.get("BOW_UPDATE_GOLDEN") == "1"


@pytest.mark.e2e
def test_groups_cover_every_role():
    assert sorted(p for g in GROUPS.values() for p in g) == sorted(ROLES)


def _summary(snaps: dict, bodies: dict, *, partial: bool = False) -> str:
    return (
        "# Who sees what on /r — main baseline\n\n"
        "Each cell: HTTP status of `GET /api/r/{id}`, then the dashboard version the "
        "page shows (first non-doc row of `/api/r/{id}/artifacts`).\n"
        "Generated from the golden JSON files; review it by hand, never just regenerate.\n\n"
        + summary_rows(snaps, partial=partial)
    )


def _finish_update() -> None:
    """Once every reader has a file: drop unreferenced bodies, write SUMMARY.md."""
    if not all(golden_path(r).exists() for r in ROLES):
        return
    snaps = {r: json.loads(golden_path(r).read_text(encoding="utf-8")) for r in ROLES}
    used: set = set()
    for snap in snaps.values():
        referenced_hashes(snap, used)
    bodies = {h: b for h, b in load_bodies().items() if h in used}
    write_bodies(bodies)
    (GOLDEN_DIR / "SUMMARY.md").write_text(_summary(snaps, bodies, partial=True), encoding="utf-8")


@pytest.mark.e2e
@pytest.mark.parametrize("group", sorted(GROUPS))
def test_every_reader_sees_what_main_shows(group, corpus):
    if UPDATE and os.environ.get("PYTEST_XDIST_WORKER"):
        pytest.fail("regenerate golden files without xdist: the groups share bodies.json")
    # Server errors are part of what a reader gets; record them as statuses.
    from fastapi.testclient import TestClient

    from main import app

    snapper = Snapshotter(TestClient(app, raise_server_exceptions=False), corpus)
    golden_bodies = load_bodies()
    new_bodies: dict = {}
    failures = []
    for role in GROUPS[group]:
        path = golden_path(role)
        snap = split_bodies(snapper.take(corpus.cast.people[role]), new_bodies)
        if path.exists():
            # Since phase 9 the conversations list reads the dashboards, which
            # the data migration fills: it is compared after the migration
            # (test_migration_before_after), and its golden record stays main's.
            snap["lists"] = json.loads(path.read_text(encoding="utf-8"))["lists"]
        actual = dump(snap)
        if UPDATE:
            GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(actual, encoding="utf-8")
            continue
        assert path.exists(), f"missing golden file {path.name}; run with BOW_UPDATE_GOLDEN=1"
        expected = path.read_text(encoding="utf-8")
        if actual != expected:
            failures.append(explain_diff(role, expected, actual, golden_bodies, new_bodies))
    if UPDATE:
        write_bodies({**golden_bodies, **new_bodies})
        _finish_update()
    assert not failures, "\n\n".join(failures)


@pytest.mark.e2e
def test_golden_files_are_consistent():
    """Every hash a reader file names exists, and SUMMARY.md is current."""
    snaps = {r: json.loads(golden_path(r).read_text(encoding="utf-8")) for r in ROLES}
    bodies = load_bodies()
    used: set = set()
    for snap in snaps.values():
        referenced_hashes(snap, used)
    assert used <= set(bodies), f"bodies missing: {sorted(used - set(bodies))[:5]}"
    assert set(bodies) <= used, "bodies.json holds unreferenced bodies; regenerate"
    summary = (GOLDEN_DIR / "SUMMARY.md").read_text(encoding="utf-8")
    assert summary == _summary(snaps, bodies)
