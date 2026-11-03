"""Client authentication selection survives connection construction."""
from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from app.schemas import data_source_registry as registry
from app.services.user_data_source_credentials_service import UserDataSourceCredentialsService
from app.schemas.user_data_source_credentials_schema import UserDataSourceCredentialsCreate

from app.models.connection import Connection
from app.services.data_source_service import DataSourceService


@pytest.mark.parametrize("selected", ["directory", "federated", "native", None, ""])
@pytest.mark.parametrize("signature", ["explicit", "forwarding", "unsupported", "opaque"])
@pytest.mark.parametrize("path", ["new", "saved", "runtime", "personal"])
@pytest.mark.asyncio
async def test_connection_auth_selection(monkeypatch, selected, signature, path):
    expected = (selected or "default") if signature != "unsupported" else "default"

    class Driver:
        def __init__(self, auth_type="default"):
            self.auth_type = auth_type

        async def atest_connection(self):
            return {"success": self.auth_type == expected}

        async def aget_schemas(self):
            return []

    class ForwardingDriver(Driver):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)

    class UnauthenticatedDriver(Driver):
        def __init__(self):
            self.auth_type = "default"

    if signature == "opaque":
        Driver.__init__.__signature__ = "unavailable"
    driver = {"explicit": Driver, "forwarding": ForwardingDriver,
              "unsupported": UnauthenticatedDriver, "opaque": Driver}[signature]
    monkeypatch.setattr("app.services.data_source_service.resolve_client_class", lambda _: driver)
    monkeypatch.setattr("app.schemas.data_source_registry.resolve_client_class", lambda _: driver)
    monkeypatch.setattr("app.services.user_data_source_credentials_service.resolve_client_class", lambda _: driver)
    class Credentials(BaseModel):
        auth_type: str | None = None
    monkeypatch.setitem(registry.REGISTRY, "test_driver", registry.DataSourceRegistryEntry(
        type="test_driver", title="Test driver", description="Test boundary",
        config_schema=BaseModel,
        credentials_auth=registry.AuthOptions(default="userpass", by_auth={
            "userpass": registry.AuthVariant(title="Credentials", schema=Credentials),
        }),
    ))
    credentials = {} if selected is None else {"auth_type": selected}
    payload = {"type": "test_driver", "config": {"auth_type": "userpass"},
               "credentials": credentials}
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
        connection.encrypt_credentials(credentials)
        if path == "personal":
            result = await UserDataSourceCredentialsService().test_my_credentials(
                None, SimpleNamespace(connections=[connection]), None,
                UserDataSourceCredentialsCreate(auth_mode="userpass", credentials=credentials),
            )
            assert result["success"], result
            return
        if path == "saved":
            client = connection.get_client()
        else:
            client = await DataSourceService().construct_client(
                None, SimpleNamespace(connections=[connection]), None
            )
        assert client.auth_type == expected
