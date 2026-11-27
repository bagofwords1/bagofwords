"""Retention deletes a report or dashboard with everything it owns. The purge
walks the schema, so every reference into a table it may delete needs a
decision: delete the referencing row with it, or keep that row and clear the
link. A reference without one would either be left dangling (SQLite does
not enforce foreign keys) or break the delete on Postgres.

These tests read the whole of ``Base.metadata``: adding a model that points
into a report's or dashboard's tree fails here until it gets a policy in
``app/services/retention/purge.py``.
"""
from app.services.retention import purge


def _references():
    return [ref for table in sorted(purge.deletable_tables()) for ref in purge.references_into(table)]


def test_every_reference_into_the_purge_tree_has_a_policy():
    missing = sorted(f"{r.child.name}.{r.column} -> {r.parent}" for r in _references() if r.policy is None)
    assert missing == [], (
        "These references point into what retention deletes but have no policy "
        f"(add DELETE or NULL to purge.POLICY): {missing}"
    )


def test_a_kept_row_can_let_go_of_its_link():
    # NULL keeps the referencing row and clears the column: that column must be nullable.
    bad = sorted(f"{r.child.name}.{r.column}" for r in _references() if r.policy == purge.NULL and not r.nullable)
    assert bad == []


def test_every_policy_names_a_real_reference():
    real = {(r.child.name, r.column) for r in _references()}
    stale = sorted(k for k in purge.POLICY if k not in real)
    assert stale == [], f"POLICY entries that match no reference (renamed or removed column?): {stale}"


def test_the_purge_never_deletes_what_outlives_a_report():
    """Knowledge, billing and other people's work stay: instructions, usage
    records, eval suites and cases, data sources, users, other conversations
    (forks) and files (files go only once nothing links to them)."""
    tree = purge.deletable_tables()
    for kept in ("instructions", "instruction_builds", "llm_usage_records", "test_cases", "test_suites",
                 "test_runs", "data_sources", "users", "organizations", "files", "entities",
                 "agent_lists", "agent_list_rows"):
        assert kept not in tree, kept
    # A dashboard purge reaches no conversation; a conversation purge reaches its dashboards.
    assert ("reports", "forked_from_artifact_id") in purge.POLICY
    assert purge.POLICY[("reports", "forked_from_artifact_id")] == purge.NULL
    assert purge.POLICY[("artifacts", "report_id")] == purge.DELETE
