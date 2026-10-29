"""A saved provider's secret must only ever be sent to the endpoint it was
saved for. ``/api/llm/test_connection`` reuses stored credentials when the
payload leaves them blank; redirecting the destination in that same payload
must not carry the stored secret along."""
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


class _Recorder(BaseHTTPRequestHandler):
    seen_auth: list = []

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        type(self).seen_auth.append(
            (self.headers.get("Authorization") or "") + (self.headers.get("api-key") or "")
        )
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error":{"message":"nope"}}')

    do_POST = do_GET = _record

    def log_message(self, *args):
        pass


@pytest.fixture
def capture_server():
    _Recorder.seen_auth = []
    server = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1", _Recorder.seen_auth
    server.shutdown()


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _create_provider(test_client, headers, provider_type, credentials, model_id):
    resp = test_client.post(
        "/api/llm/providers",
        json={
            "name": f"{provider_type} saved",
            "provider_type": provider_type,
            "credentials": credentials,
            "models": [{"model_id": model_id, "name": model_id, "is_custom": provider_type == "custom"}],
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


@pytest.mark.e2e
@pytest.mark.parametrize(
    "provider_type,saved_creds,redirect_creds,model_id",
    [
        ("openai", {"api_key": "sk-stored-openai-SECRET"}, lambda url: {"base_url": url}, "gpt-5.6-terra"),
        (
            "custom",
            {"api_key": "sk-stored-custom-SECRET", "base_url": "https://llm.internal.example/v1"},
            lambda url: {"base_url": url},
            "my-model",
        ),
    ],
)
def test_test_connection_never_sends_stored_secret_to_new_endpoint(
    test_client, create_user, login_user, whoami, capture_server,
    provider_type, saved_creds, redirect_creds, model_id,
):
    url, seen_auth = capture_server
    user = create_user()
    token = login_user(user["email"], user["password"])
    headers = _headers(token, whoami(token)["organizations"][0]["id"])
    provider_id = _create_provider(test_client, headers, provider_type, saved_creds, model_id)

    resp = test_client.post(
        "/api/llm/test_connection",
        json={
            "provider_id": provider_id,
            "name": "x",
            "provider_type": provider_type,
            "credentials": redirect_creds(url),
            "models": [{"model_id": model_id, "name": model_id, "is_custom": provider_type == "custom", "is_enabled": True}],
        },
        headers=headers,
    )
    assert resp.status_code in (200, 400), resp.text
    assert not any(saved_creds["api_key"] in h for h in seen_auth), seen_auth


@pytest.mark.e2e
def test_test_connection_uses_freshly_entered_key_for_new_endpoint(
    test_client, create_user, login_user, whoami, capture_server,
):
    url, seen_auth = capture_server
    user = create_user()
    token = login_user(user["email"], user["password"])
    headers = _headers(token, whoami(token)["organizations"][0]["id"])
    provider_id = _create_provider(
        test_client, headers, "openai", {"api_key": "sk-stored-SECRET"}, "gpt-5.6-terra"
    )

    test_client.post(
        "/api/llm/test_connection",
        json={
            "provider_id": provider_id, "name": "x", "provider_type": "openai",
            "credentials": {"api_key": "sk-typed-now", "base_url": url},
            "models": [{"model_id": "gpt-5.6-terra", "name": "t", "is_enabled": True}],
        },
        headers=headers,
    )
    assert seen_auth, "test call never reached the endpoint"
    assert any("sk-typed-now" in h for h in seen_auth)
    assert not any("sk-stored-SECRET" in h for h in seen_auth)


@pytest.mark.e2e
def test_test_connection_reuses_stored_secret_for_saved_endpoint(
    test_client, create_user, login_user, whoami, capture_server,
):
    """Retesting a saved provider unchanged still works with blank credentials."""
    url, seen_auth = capture_server
    user = create_user()
    token = login_user(user["email"], user["password"])
    headers = _headers(token, whoami(token)["organizations"][0]["id"])
    provider_id = _create_provider(
        test_client, headers, "custom", {"api_key": "sk-saved-ok", "base_url": url}, "m1"
    )

    test_client.post(
        "/api/llm/test_connection",
        json={
            "provider_id": provider_id, "name": "x", "provider_type": "custom",
            "credentials": {"base_url": url},
            "models": [{"model_id": "m1", "name": "m1", "is_custom": True, "is_enabled": True}],
        },
        headers=headers,
    )
    assert any("sk-saved-ok" in h for h in seen_auth)


@pytest.mark.e2e
def test_test_connection_rejects_provider_type_switch(test_client, create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    headers = _headers(token, whoami(token)["organizations"][0]["id"])
    provider_id = _create_provider(
        test_client, headers, "openai", {"api_key": "sk-stored"}, "gpt-5.6-terra"
    )
    resp = test_client.post(
        "/api/llm/test_connection",
        json={
            "provider_id": provider_id, "name": "x", "provider_type": "custom",
            "credentials": {"base_url": "http://127.0.0.1:9/v1"},
            "models": [{"model_id": "m", "name": "m", "is_custom": True, "is_enabled": True}],
        },
        headers=headers,
    )
    assert resp.status_code == 400
