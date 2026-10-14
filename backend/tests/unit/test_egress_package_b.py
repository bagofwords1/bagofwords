"""SSRF package B: non-HTTP and SDK egress paths never reach cloud metadata,
and admin-configured internal destinations keep working.

Covered paths: data-source connections (pre-save test used by create / PUT,
and the client factory), SMTP send + SMTP/IMAP "test", syslog and S3 audit
streams, git remotes, Azure / Azure AI Foundry LLM clients.

The network is played at the socket boundary (``tests.utils.egress_fakes.
NetTrap``): a resolver that can answer differently per lookup (DNS
rebinding) and a router that sends every connect to 169.254.169.254 to a
local metadata stand-in. ``trap.hits`` is therefore the ground truth for
"the metadata service was contacted" — for our code *and* for the drivers /
SDKs that resolve on their own. Internal services are real sockets on
127.0.0.1 reached through ``*.invalid`` names only the fake resolver knows.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import ssl
import subprocess
from email.message import EmailMessage

import pytest

from app.core import egress
from app.core.url_safety import UnsafeURLError
from tests.utils.egress_fakes import NetTrap, local_server, tcp_server

# Metadata in the forms an admin could type: dotted, hex, decimal, octal, and
# a name that resolves to it.
METADATA_FORMS = ["169.254.169.254", "0xa9fea9fe", "2852039166", "0251.0376.0251.0376", "imds.corp.invalid"]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(egress, "block_private_outbound", lambda: False)


@pytest.fixture
def trap(monkeypatch):
    traps = []

    def make(dns=None):
        t = NetTrap(monkeypatch, {"imds.corp.invalid": "169.254.169.254", **(dns or {})})
        traps.append(t)
        return t

    yield make
    for t in traps:
        t.close()


def _block_private(monkeypatch):
    monkeypatch.setattr(egress, "block_private_outbound", lambda: True)


# ── config parsing ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("value,hosts", [
    ("db.corp", ["db.corp"]),
    ("db.corp:5432", ["db.corp"]),
    ("[fd00::1]:5432", ["fd00::1"]),
    ("fd00::1", ["fd00::1"]),
    ("h1:27017,h2:27017", ["h1", "h2"]),
    ("mongodb+srv://u:p@h1,h2/db?x=1", ["h1", "h2"]),
    ("https://user@api.corp:8443/v1", ["api.corp"]),
    ("", []),
])
def test_hosts_in_every_host_form(value, hosts):
    assert egress.hosts_in(value) == hosts


def test_connection_hosts_reads_host_and_url_keys():
    params = {"host": "a.corp", "base_url": "http://b.corp/x", "site_url": "https://c.corp", "database": "d.corp"}
    assert set(egress.connection_hosts("whatever", params)) == {"a.corp", "b.corp", "c.corp"}
    # Salesforce `domain` names a host only in its full form.
    assert egress.connection_hosts("salesforce", {"domain": "login"}) == []
    assert egress.connection_hosts("salesforce", {"domain": "169.254.169.254"}) == ["169.254.169.254"]


# ── 1/2/7. data-source connections ──────────────────────────────────────────


def _mysql(host, port=3306):
    return {"host": host, "port": port, "database": "d", "user": "u"}, {"password": "p"}


@pytest.mark.asyncio
@pytest.mark.parametrize("host", METADATA_FORMS)
async def test_connection_test_params_never_contacts_metadata(trap, host):
    """The pre-save test (create, PUT revalidation, Test button) refuses the
    host before the driver connects anywhere."""
    from app.services.connection_service import ConnectionService

    t = trap()
    cfg, creds = _mysql(host, 80)
    res = await ConnectionService().test_connection_params("mysql", cfg, creds)
    assert res["success"] is False
    assert t.hits == []


@pytest.mark.asyncio
async def test_connection_to_internal_database_still_attempted_and_blocked_with_flag(trap, monkeypatch):
    from app.services.connection_service import ConnectionService

    with tcp_server() as (port, seen):
        trap({"mysql.corp.invalid": "127.0.0.1"})
        cfg, creds = _mysql("mysql.corp.invalid", port)
        await ConnectionService().test_connection_params("mysql", cfg, creds)
        assert len(seen) >= 1, "the internal database must still be reached (flag off)"

        _block_private(monkeypatch)
        seen.clear()
        res = await ConnectionService().test_connection_params("mysql", cfg, creds)
        assert res["success"] is False and seen == []


def test_connection_model_client_factory_refuses_metadata():
    """Connection.get_client() (indexing / legacy paths) checks too."""
    from app.models.connection import Connection

    conn = Connection(type="mysql", config={"host": "169.254.169.254", "port": 3306, "database": "d", "user": "u"})
    with pytest.raises(UnsafeURLError):
        conn.get_client()


# ── 3. SMTP / IMAP ──────────────────────────────────────────────────────────


def _msg():
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = "bow@corp.example", "ops@corp.example", "t"
    msg.set_content("hello")
    return msg


class _Inbox:
    def __init__(self):
        self.messages = []

    async def handle_DATA(self, server, session, envelope):
        self.messages.append(envelope.content)
        return "250 OK"


def _smtp_server(**kw):
    from aiosmtpd.controller import Controller

    import socket

    with socket.socket() as s:  # aiosmtpd needs a concrete port
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    inbox = _Inbox()
    ctl = Controller(inbox, hostname="127.0.0.1", port=port, **kw)
    ctl.start()
    return ctl, inbox


@pytest.mark.parametrize("host", METADATA_FORMS)
def test_smtp_send_never_contacts_metadata(trap, host):
    from app.services.email.sender import SmtpConfig, send_message_result

    t = trap()
    ok, err, _stage = asyncio.run(send_message_result(SmtpConfig(host=host, port=25, security="none"), _msg()))
    assert ok is False and err
    assert t.hits == []


def test_smtp_send_to_internal_relay_works_pinned_and_blocked_with_flag(trap, monkeypatch):
    from app.services.email.sender import SmtpConfig, send_message_result

    ctl, inbox = _smtp_server()
    try:
        # Rebinding resolver: the first answer is the relay, later ones metadata.
        t = trap({"relay.corp.invalid": ["127.0.0.1", "169.254.169.254"]})
        cfg = SmtpConfig(host="relay.corp.invalid", port=ctl.port, security="none")
        ok, err, _ = asyncio.run(send_message_result(cfg, _msg()))
        assert ok, err
        assert len(inbox.messages) == 1 and t.hits == []

        _block_private(monkeypatch)
        t.table["relay.corp.invalid"] = ["127.0.0.1"]
        ok, _, _ = asyncio.run(send_message_result(cfg, _msg()))
        assert not ok and len(inbox.messages) == 1
    finally:
        ctl.stop()


def test_smtp_tls_still_verifies_the_configured_hostname(trap, tmp_path):
    """The socket is pinned to the IP; TLS keeps the hostname (SNI + cert)."""
    from app.services.email.pinned import PinnedSMTP
    from tests.unit.test_egress import _self_signed

    cert, key = _self_signed(tmp_path, "mail.corp.invalid")
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert, key)
    ctl, _ = _smtp_server(ssl_context=server_ctx)
    try:
        trap({"mail.corp.invalid": "127.0.0.1", "impostor.corp.invalid": "127.0.0.1"})
        client_ctx = ssl.create_default_context(cafile=cert)

        async def connect(name):
            c = PinnedSMTP(hostname=name, port=ctl.port, use_tls=True, tls_context=client_ctx, timeout=10)
            await c.connect()
            await c.quit()

        asyncio.run(connect("mail.corp.invalid"))
        with pytest.raises(Exception) as exc:
            asyncio.run(connect("impostor.corp.invalid"))
        assert "certificate" in str(exc.value).lower() or "hostname" in str(exc.value).lower()
    finally:
        ctl.stop()


def _email_cfg(**kw):
    from app.schemas.external_platform_schema import EmailConfig

    base = dict(auth_type="password", from_address="bow@corp.example", smtp_username="u",
                smtp_password="p", smtp_security="none")
    return EmailConfig(**{**base, **kw})


@pytest.mark.parametrize("host", METADATA_FORMS)
def test_email_settings_test_never_contacts_metadata(trap, host):
    """The settings page's SMTP/IMAP "Test" button (user-entered hosts)."""
    from app.services.external_platform_service import ExternalPlatformService

    t = trap()
    res = asyncio.run(ExternalPlatformService().test_email_config(_email_cfg(smtp_host=host, smtp_port=80)))
    assert res["success"] is False
    assert t.hits == []


def _imap_reply(chunk: bytes) -> bytes:
    tag = chunk.split(b" ", 1)[0]
    return b"* CAPABILITY IMAP4rev1 AUTH=PLAIN\r\n" + tag + b" OK done\r\n"


def test_imap_reader_connects_pinned_to_internal_host_and_refuses_metadata(trap):
    from app.services.email.mailbox_reader import ImapConfig, ImapMailboxReader

    with tcp_server(greeting=b"* OK IMAP4rev1 ready\r\n", reply_to=_imap_reply) as (port, seen):
        t = trap({"imap.corp.invalid": ["127.0.0.1", "169.254.169.254"]})
        reader = ImapMailboxReader(ImapConfig(host="imap.corp.invalid", port=port, use_ssl=False,
                                              username="u", password="p"))
        try:
            asyncio.run(reader.fetch_unseen())  # the fake does not implement LOGIN
        except Exception:
            pass
        assert seen and b"CAPABILITY" in seen[0]

        bad = ImapMailboxReader(ImapConfig(host="imds.corp.invalid", port=port, use_ssl=False,
                                           username="u", password="p"))
        try:
            asyncio.run(bad.fetch_unseen())
        except Exception:
            pass
        assert len(seen) == 1 and t.hits == []


# ── 8. audit streams: syslog + S3 ───────────────────────────────────────────

_EVENTS = [{"id": "e1", "action": "user.login", "occurred_at": "2026-01-01T00:00:00Z"}]


def _syslog(host, port):
    from app.ee.audit.streams.destinations.syslog import SyslogDestination

    return SyslogDestination({"host": host, "port": port, "tls": False, "format": "json", "app_name": "bow"}, {})


@pytest.mark.parametrize("host", METADATA_FORMS)
def test_syslog_stream_never_contacts_metadata(trap, host):
    from app.ee.audit.streams.destinations.base import INVALID

    t = trap()
    res = asyncio.run(_syslog(host, 80).send(_EVENTS))
    assert res.kind == INVALID
    assert t.hits == []


def test_syslog_stream_internal_collector_works_rebinding_refused(trap, monkeypatch):
    with tcp_server() as (port, seen):
        t = trap({"siem.corp.invalid": ["127.0.0.1", "169.254.169.254"]})
        first = asyncio.run(_syslog("siem.corp.invalid", port).send(_EVENTS))
        second = asyncio.run(_syslog("siem.corp.invalid", port).send(_EVENTS))
        assert first.ok and not second.ok
        assert len(seen) == 1 and b"user.login" in seen[0]
        assert t.hits == []

        _block_private(monkeypatch)
        t.table["siem.corp.invalid"] = ["127.0.0.1"]
        assert not asyncio.run(_syslog("siem.corp.invalid", port).send(_EVENTS)).ok
        assert len(seen) == 1


def test_syslog_stream_save_refuses_metadata_host():
    from app.ee.audit.streams.service import normalize
    from app.errors import AppError

    with pytest.raises(AppError):
        normalize("syslog", {"host": "169.254.169.254", "port": 514}, {})
    cfg, _ = normalize("syslog", {"host": "10.0.0.5", "port": 514}, {})
    assert cfg["host"] == "10.0.0.5"


def _s3(endpoint):
    from app.ee.audit.streams.destinations.s3 import S3Destination

    return S3Destination({"bucket": "audit", "region": "us-east-1", "endpoint_url": endpoint},
                         {"access_key_id": "AKIA", "secret_access_key": "s"})


@pytest.mark.parametrize("host", METADATA_FORMS)
def test_s3_stream_custom_endpoint_never_contacts_metadata(trap, host):
    from app.ee.audit.streams.destinations.base import INVALID

    t = trap()
    res = asyncio.run(_s3(f"http://{host}").send(_EVENTS))
    assert res.kind == INVALID
    assert t.hits == []


class _S3Ok(dict):
    """Any request -> 200 with an ETag (enough for PutObject)."""

    def get(self, key, default=None):
        return (200, {"ETag": '"etag"'}, b"")


def test_s3_stream_internal_endpoint_works_and_blocked_with_flag(trap, monkeypatch):
    with local_server(_S3Ok()) as (port, seen):
        trap({"minio.corp.invalid": "127.0.0.1"})
        assert asyncio.run(_s3(f"http://minio.corp.invalid:{port}").send(_EVENTS)).ok
        assert [m for m, _, _ in seen] == ["PUT"]

        _block_private(monkeypatch)
        assert not asyncio.run(_s3(f"http://minio.corp.invalid:{port}").send(_EVENTS)).ok
        assert len(seen) == 1


# ── 6. git ──────────────────────────────────────────────────────────────────


@pytest.fixture
def dumb_http_repo(tmp_path):
    """A bare repo with a `main` branch, served over git's dumb HTTP protocol."""
    if not shutil.which("git"):
        pytest.skip("git not installed")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    work, bare = tmp_path / "work", tmp_path / "srv" / "repo.git"
    run = lambda *a, cwd=None: subprocess.run(a, cwd=cwd, env=env, check=True, capture_output=True)
    run("git", "init", "-b", "main", str(work))
    (work / "README.md").write_text("hi")
    run("git", "add", ".", cwd=work)
    run("git", "commit", "-m", "init", cwd=work)
    run("git", "clone", "--bare", str(work), str(bare))
    run("git", "update-server-info", cwd=bare)

    import functools
    import http.server
    import threading

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path / "srv"))
    handler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _git_test(url):
    from app.schemas.git_repository_schema import GitRepositoryCreate
    from app.services.git_service import GitService

    return asyncio.run(GitService().test_connection(None, GitRepositoryCreate(provider="github", repo_url=url, branch="main"), None))


def test_git_internal_server_reached_through_the_validated_address(trap, dumb_http_repo, monkeypatch):
    """git resolves names itself; the egress pin (http.curloptResolve) makes
    it use the address that was validated. `git.corp.invalid` exists only in
    the fake resolver, so success proves git used the pinned address."""
    trap({"git.corp.invalid": "127.0.0.1"})
    assert _git_test(f"http://git.corp.invalid:{dumb_http_repo}/repo.git")["success"] is True

    from fastapi import HTTPException

    _block_private(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        _git_test(f"http://git.corp.invalid:{dumb_http_repo}/repo.git")
    assert exc.value.status_code == 400


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/x.git", "https://0xa9fea9fe/x.git", "http://imds.corp.invalid/x.git",
    "git@imds.corp.invalid:org/repo.git", "ssh://git@[::ffff:169.254.169.254]/x.git",
])
def test_git_remote_on_metadata_refused(trap, url):
    from fastapi import HTTPException

    trap()
    with pytest.raises(HTTPException) as exc:
        _git_test(url)
    assert exc.value.status_code == 400


def test_git_env_pins_every_http_command_and_rechecks_the_host(trap):
    from fastapi import HTTPException
    from app.services.git_service import GitService

    t = trap({"git.corp.invalid": ["10.1.2.3", "169.254.169.254"]})
    env = GitService._git_env("https://tok@git.corp.invalid/org/repo.git", base_env={})
    n = int(env["GIT_CONFIG_COUNT"]) - 1
    assert env[f"GIT_CONFIG_KEY_{n}"] == "http.curloptResolve"
    assert env[f"GIT_CONFIG_VALUE_{n}"] == "git.corp.invalid:443:10.1.2.3"
    # The next network command re-resolves — and is refused once the name flips.
    with pytest.raises(HTTPException):
        GitService._git_env("https://tok@git.corp.invalid/org/repo.git", base_env={})
    assert t.hits == []


# ── 8. LLM: Azure OpenAI / Azure AI Foundry ─────────────────────────────────


def _azure_llm(model_id, endpoint, **extra):
    from app.ai.llm.llm import LLM
    from tests.unit.test_llm_provider_headers import make_model

    model = make_model("azure", {"endpoint_url": endpoint, **extra})
    model.model_id = model_id
    llm = LLM(model)
    llm.client.client.max_retries = 0
    return llm


def _ping(llm):
    sdk = llm.client.client
    if hasattr(sdk, "models"):
        return sdk.models.list()
    return sdk.messages.create(model="m", max_tokens=1, messages=[{"role": "user", "content": "hi"}])


@pytest.mark.parametrize("model_id,endpoint,extra", [
    ("gpt-4o", "http://aoai.corp.invalid", {}),                          # Azure SDK client
    ("gpt-4o", "http://res.services.ai.azure.com", {}),                 # Foundry, OpenAI-compatible
    ("gpt-4o", "http://aoai.corp.invalid", {"use_responses_api": True}),  # Responses API
    ("claude-sonnet-4-5", "http://res.services.ai.azure.com", {}),      # Anthropic on Foundry
])
def test_azure_llm_clients_rebinding_never_reaches_metadata(trap, model_id, endpoint, extra):
    from urllib.parse import urlsplit

    host = urlsplit(endpoint).hostname
    # Answer 1 passes the construction-time check; every later answer is metadata.
    t = trap({host: ["127.0.0.1", "169.254.169.254"]})
    llm = _azure_llm(model_id, endpoint, **extra)
    with pytest.raises(Exception):
        _ping(llm)
    assert t.hits == []


@pytest.mark.parametrize("model_id", ["gpt-4o", "claude-sonnet-4-5"])
def test_azure_llm_custom_internal_endpoint_still_reached(trap, model_id):
    with local_server() as (port, seen):
        trap({"aoai.corp.invalid": "127.0.0.1"})
        llm = _azure_llm(model_id, f"http://aoai.corp.invalid:{port}")
        try:
            _ping(llm)
        except Exception:
            pass  # the fake is not an LLM API; reaching it is the point
        assert seen, "custom (internal) Azure endpoint must still be reachable"
