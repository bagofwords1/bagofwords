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
        remove_corpus_files(built)


@pytest.fixture(autouse=True)
def _remove_published_files():
    """Files the copy engine wrote for releases this test published."""
    from tests.e2e.dashboards.dashboard_setup import PUBLISHED, remove_release_files

    PUBLISHED.clear()
    yield
    remove_release_files(PUBLISHED)
    PUBLISHED.clear()
