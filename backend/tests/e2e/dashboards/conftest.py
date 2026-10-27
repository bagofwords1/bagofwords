"""Fixtures for the dashboard corpus suite.

The RBAC helpers (users, groups, roles, data sources, the enterprise license
stub and the relaxed sign-up flags) are reused from e2e/rbac rather than
copied.
"""
import pytest

from tests.e2e.rbac.conftest import (  # noqa: F401  (pytest fixtures)
    _rbac_relaxed_signup_flags,
    enterprise_license,
    sqlite_data_source,
)
from tests.fixtures.dashboard_corpus.build import build_corpus, remove_corpus_files
from tests.fixtures.dashboard_corpus.rows import Rows


@pytest.fixture
def corpus_rows():
    rows = Rows()
    try:
        yield rows
    finally:
        rows.close()


@pytest.fixture
def corpus(test_client, create_user, login_user, whoami, sqlite_data_source,  # noqa: F811
           create_project, update_project, upsert_project_member, remove_project_member,
           delete_project, enterprise_license, corpus_rows):  # noqa: F811
    """The full corpus on a fresh database (see tests/fixtures/dashboard_corpus)."""
    built = build_corpus({
        "test_client": test_client, "create_user": create_user, "login_user": login_user,
        "whoami": whoami, "sqlite_data_source": sqlite_data_source,
        "create_project": create_project, "update_project": update_project,
        "upsert_project_member": upsert_project_member,
        "remove_project_member": remove_project_member, "delete_project": delete_project,
    }, corpus_rows)
    try:
        yield built
    finally:
        _remove_migrated_release_files(corpus_rows)
        remove_corpus_files(built)


@pytest.fixture
def migrated_corpus(test_client, create_user, login_user, whoami, sqlite_data_source,  # noqa: F811
                    create_project, update_project, upsert_project_member, remove_project_member,
                    delete_project, enterprise_license, corpus_rows):  # noqa: F811
    """The corpus after the data migration (phase 9): the same rows, upgraded
    through every revision, as a customer's database after the upgrade."""
    built = build_corpus({
        "test_client": test_client, "create_user": create_user, "login_user": login_user,
        "whoami": whoami, "sqlite_data_source": sqlite_data_source,
        "create_project": create_project, "update_project": update_project,
        "upsert_project_member": upsert_project_member,
        "remove_project_member": remove_project_member, "delete_project": delete_project,
    }, corpus_rows, migrate=True)
    try:
        yield built
    finally:
        _remove_migrated_release_files(corpus_rows)
        remove_corpus_files(built)


def _remove_migrated_release_files(corpus_rows) -> None:
    """The files of every release in the corpus database -- the data
    migration's copies among them (a test upgrading a `corpus` itself makes
    some too): uploads/ is shared by every test run."""
    from sqlalchemy import inspect, text

    from tests.e2e.dashboards.dashboard_setup import remove_release_files

    try:
        with corpus_rows.engine.connect() as conn:
            if "artifact_releases" not in inspect(conn).get_table_names():
                return
            ids = [str(r[0]) for r in conn.execute(text("SELECT id FROM artifact_releases"))]
    except Exception:  # the database is already gone: nothing to read
        return
    remove_release_files(ids)


@pytest.fixture(autouse=True)
def _remove_published_files():
    """Files the copy engine wrote for releases this test published."""
    from tests.e2e.dashboards.dashboard_setup import PUBLISHED, remove_release_files

    PUBLISHED.clear()
    yield
    remove_release_files(PUBLISHED)
    PUBLISHED.clear()


@pytest.fixture
def multi_org():
    """Several organizations allowed, so a removed member's account stays
    active and it is the dashboard that refuses. Restored by hand, as the
    other tests of this flag do: monkeypatch's undo did not restore it here,
    and the flag then leaked into later tests (test_membership failing with
    403 instead of 401 in a full run)."""
    from app.settings.config import settings

    flags = settings.bow_config.features
    saved = flags.allow_multiple_organizations
    flags.allow_multiple_organizations = True
    try:
        yield
    finally:
        flags.allow_multiple_organizations = saved
