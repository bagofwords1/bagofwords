"""Client authentication selection survives connection construction."""
from types import SimpleNamespace

import pytest

from app.models.connection import Connection
from app.services.data_source_service import DataSourceService


@pytest.mark.parametrize("selected", ["directory", "federated", "native"])
@pytest.mark.parametrize("signature", ["explicit", "forwarding", "unsupported"])
@pytest.mark.parametrize("path", ["new", "saved", "runtime"])
@pytest.mark.asyncio
async def test_connection_auth_selection(monkeypatch, selected, signature, path):
    class Driver:
        def __init__(self, auth_type="default"):
            self.auth_type = auth_type

        async def atest_connection(self):
            return {"success": self.auth_type == selected}

        async def aget_schemas(self):
            return []

    class ForwardingDriver(Driver):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)

    class UnauthenticatedDriver(Driver):
        def __init__(self):
            self.auth_type = selected

    driver = {"explicit": Driver, "forwarding": ForwardingDriver,
              "unsupported": UnauthenticatedDriver}[signature]
    monkeypatch.setattr("app.services.data_source_service.resolve_client_class", lambda _: driver)
    monkeypatch.setattr("app.schemas.data_source_registry.resolve_client_class", lambda _: driver)
    payload = {"type": "test_driver", "config": {"auth_type": "userpass"},
               "credentials": {"auth_type": selected}}
    if path == "new":
        result = await DataSourceService().test_new_data_source_connection(
            None, SimpleNamespace(dict=lambda: payload), None, None
        )
        assert result["success"], result
    else:
        connection = Connection(
            type="test_driver", config={"auth_type": "userpass"},
            auth_policy="system_only",
        )
        connection.encrypt_credentials({"auth_type": selected})
        if path == "saved":
            client = connection.get_client()
        else:
            client = await DataSourceService().construct_client(
                None, SimpleNamespace(connections=[connection]), None
            )
        assert client.auth_type == selected
