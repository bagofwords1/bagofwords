import contextlib

import pytest
from fastapi.testclient import TestClient
from main import app


class _ReleasingTestClient(TestClient):
    """Each request runs on its own event loop (one blocking portal per
    request). Close the pooled DB connections that loop opened while it is
    still running — asyncpg can only close a connection on its own loop —
    so they neither leak nor get offered to the next request's loop."""

    @contextlib.contextmanager
    def _portal_factory(self):
        with super()._portal_factory() as portal:
            try:
                yield portal
            finally:
                if self.portal is None:
                    from app.settings.database import release_test_connections

                    portal.call(release_test_connections)


@pytest.fixture(scope="session")
def test_client():
    return _ReleasingTestClient(app)
