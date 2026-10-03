from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from app.core.spa import mount_spa
from app.core.cors import init_cors


def test_only_public_font_assets_allow_opaque_origin(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<html>Application</html>")
    fonts = tmp_path / "libs" / "fonts"
    fonts.mkdir(parents=True)
    (fonts / "sample.woff2").write_bytes(b"font fixture")
    monkeypatch.setenv("SERVE_FRONTEND", "true")
    monkeypatch.setenv("FRONTEND_DIST_DIR", str(tmp_path))
    monkeypatch.delenv("BOW_CORS_ALLOWED_ORIGINS", raising=False)
    app = FastAPI()
    init_cors(app)

    @app.get("/api/private")
    def private():
        return {"private": "not cross-origin readable"}

    mount_spa(app)
    with TestClient(app) as client:
        for origin in ["null", "https://elsewhere.example"]:
            font = client.get("/libs/fonts/sample.woff2", headers={"Origin": origin})
            assert font.status_code == 200
            assert font.headers["access-control-allow-origin"] == "*"
            for url in ["/api/private", "/", "/libs/fonts/missing.woff2"]:
                assert "access-control-allow-origin" not in client.get(url, headers={"Origin": origin}).headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "peer,forwarded,expected",
    [
        ("10.0.0.7", "198.51.100.3", "198.51.100.3"),
        ("10.0.0.7", "198.51.100.4", "198.51.100.4"),
        ("10.0.0.8", "198.51.100.3", "10.0.0.8"),
        ("10.0.0.7", "192.0.2.99, 198.51.100.4", "198.51.100.4"),
    ],
)
async def test_proxy_identity_uses_only_explicitly_trusted_peers(peer, forwarded, expected):
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["client"][0])

    middleware = ProxyHeadersMiddleware(app, trusted_hosts=["10.0.0.7"])
    await middleware(
        {
            "type": "http",
            "scheme": "http",
            "client": (peer, 1234),
            "headers": [(b"x-forwarded-for", forwarded.encode())],
        },
        None,
        None,
    )
    assert seen == [expected]
