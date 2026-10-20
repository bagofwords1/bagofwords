"""JSON-RPC error translation from the edge agent (design B3)."""
from app.services.tunnel_errors import (
    RemoteError,
    RemoteUnknownConnection,
    translate_remote_error,
)


def test_unknown_connection_is_its_own_error_not_a_generic_remote_error():
    err = translate_remote_error(
        {"code": -32002, "message": "unknown connection 'ghost'",
         "data": {"kind": "unknown_connection", "connection": "ghost"}},
        "execute_query",
    )
    assert isinstance(err, RemoteUnknownConnection)
    assert err.connection_name == "ghost"


def test_method_not_found_stays_a_generic_remote_error():
    err = translate_remote_error(
        {"code": -32601, "message": "not implemented", "data": {"operation": "warm_all"}},
        "warm_all",
    )
    assert isinstance(err, RemoteError)
    assert not isinstance(err, RemoteUnknownConnection)
    assert err.code == -32601
