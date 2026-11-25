"""The workspace gate's allowlist (app/core/workspace_access.py): which
requests an app user's session may make. Deny by default — a path is closed
unless it is one an artifact page, the launcher or the account itself needs."""
import pytest

from app.core.workspace_access import is_open

OPEN = [
    ("GET", "/api/users/whoami"), ("PATCH", "/api/users/me"), ("POST", "/api/auth/jwt/login"),
    ("POST", "/api/auth/jwt/logout"), ("GET", "/api/organizations"), ("GET", "/api/organization/locale"),
    ("GET", "/api/organization/settings"), ("GET", "/api/apps"), ("POST", "/api/apps/access-request"),
    ("GET", "/api/d/sales"), ("POST", "/api/d/sales/chat/completions"), ("GET", "/api/d/sales/previews/0"),
    ("POST", "/api/artifacts/a1/runtime/conversations"), ("GET", "/api/artifacts/a1/runtime/resources/x"),
    ("POST", "/api/artifacts/a1/access_requests"), ("GET", "/api/files/f1/embed"),
    ("POST", "/api/embed/host_ab12cd34/sso/code"), ("GET", "/api/thumbnails/release-1.png"),
    ("GET", "/api/users/me/viewer_context"), ("POST", "/api/invites/accept"),
    # A dashboard may be called "draft"; only the draft *routes* are closed.
    ("GET", "/api/d/draft"), ("GET", "/api/d/drafts-2026/content"), ("GET", "/api/d/sales/forked"),
]
CLOSED = [
    ("GET", "/api/reports"), ("POST", "/api/reports"), ("GET", "/api/artifacts"), ("GET", "/api/artifacts/a1"),
    ("PUT", "/api/artifacts/a1/sharing"), ("POST", "/api/artifacts/a1/publish"), ("GET", "/api/data_sources"),
    ("POST", "/api/organizations"), ("PUT", "/api/organization/locale"), ("PUT", "/api/organization/settings"),
    ("GET", "/api/organization/members"), ("GET", "/api/organizations/o1/members"), ("GET", "/api/files/f1/content"),
    ("GET", "/api/notifications"), ("GET", "/api/users/me/default_model"), ("GET", "/api/users/u1/viewer_context"),
    ("POST", "/api/api_keys"), ("GET", "/api/mcp/tools"), ("GET", "/api/host_applications"),
    ("GET", "/api/d/sales/draft"), ("GET", "/api/d/sales/draft/content"), ("POST", "/api/d/sales/fork"),
    ("GET", "/api/apps-admin"), ("GET", "/api/dashboards"), ("GET", "/api/r/x/chat"), ("GET", "/api/something/new"),
]


@pytest.mark.parametrize("method,path", OPEN)
def test_what_an_app_user_needs_is_open(method, path):
    assert is_open(path, method)


@pytest.mark.parametrize("method,path", CLOSED)
def test_everything_else_is_closed(method, path):
    assert not is_open(path, method)


def test_the_api_prefix_is_optional_and_the_method_matters():
    assert is_open("/d/sales", "GET") and is_open("/api/d/sales", "get")
    assert is_open("/api/organizations", "GET") and not is_open("/api/organizations", "DELETE")
