"""End-to-end: GitHub / GitLab / Bitbucket code connections through the real stack.

    POST /api/data_sources (type github_repo|gitlab_repo|bitbucket_repo)
      → test_connection (git ls-remote, no clone)
    agent tool run_stream (list_files / grep_files / read_file)
      → resolve_file_client (report allow-list) → construct_client
        → GitRepoClient → repo_cache → a real HTTPS git server (git http-backend)

Contract: each tile connects with its provider's token convention, the agent
can list, grep and read the code, secrets stay hidden, and a bad token is
rejected at connect time without echoing it back.
"""
from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from tests.mocks.git_https_server import GitHttpsServer, make_bare_repo

pytestmark = pytest.mark.e2e

FILES = {
    "README.md": "# Billing service\n",
    "src/billing/invoice.py": "def compute_invoice_total(lines):\n    return sum(l.amount for l in lines)\n",
    ".env": "STRIPE_KEY=sk_live_should_never_show\n",
}

# Provider → (registry type, credentials, username the server expects).
PROVIDERS = {
    "github": ("github_repo", {"token": "gh-tok"}, "x-access-token"),
    "gitlab": ("gitlab_repo", {"token": "gl-tok"}, "oauth2"),
    "bitbucket": ("bitbucket_repo", {"token": "bb-tok"}, "x-bitbucket-api-token-auth"),
}


@pytest.fixture
def git_env(tmp_path, monkeypatch):
    from app.services import git_hardening, repo_cache

    root = tmp_path / "srv"
    root.mkdir()
    make_bare_repo(root, "acme/billing", FILES)
    monkeypatch.setattr(repo_cache, "CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(repo_cache, "_last_evict", time.time())
    monkeypatch.setattr(git_hardening, "_STATE_DIR", tmp_path / "state")
    for k in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(k, "127.0.0.1,localhost")
    srv = GitHttpsServer(root, cert_dir=tmp_path).start()
    monkeypatch.setenv("GIT_SSL_CAINFO", str(srv.cert_path))
    yield srv
    srv.stop()


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _connect(test_client, create_user, login_user, whoami, srv, provider, *, credentials=None):
    ds_type, creds, username = PROVIDERS[provider]
    srv.username, srv.password = username, creds["token"]
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    r = test_client.post("/api/data_sources", headers=_h(token, org_id), json={
        "name": f"{provider} billing {uuid.uuid4().hex[:6]}",
        "type": ds_type,
        "config": {"repo": "acme/billing", "host": srv.host},
        "credentials": credentials if credentials is not None else creds,
        "auth_policy": "system_only",
        "generate_summary": False, "generate_conversation_starters": False, "generate_ai_rules": False,
    })
    return r, token, org_id


async def _tool_calls(ds_id, conn_id):
    """Run list_files → grep_files → read_file as the agent would."""
    from app.ai.tools.implementations.grep_files import GrepFilesTool
    from app.ai.tools.implementations.list_files import ListFilesTool
    from app.ai.tools.implementations.read_file import ReadFileTool
    from app.dependencies import async_session_maker
    from app.models.data_source import DataSource
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.report_data_source_association import report_data_source_association
    from app.models.user import User

    async def end(agen):
        last = None
        async for ev in agen:
            last = ev
        return last.payload["output"]

    async with async_session_maker() as db:
        ds = (await db.execute(select(DataSource).where(DataSource.id == ds_id))).scalar_one()
        org = (await db.execute(select(Organization).where(Organization.id == ds.organization_id))).scalar_one()
        user = (await db.execute(select(User).where(User.id == ds.owner_user_id))).scalar_one()
        report = Report(title="code", slug=f"code-{uuid.uuid4().hex[:8]}", organization_id=org.id, user_id=user.id)
        db.add(report)
        await db.flush()
        await db.execute(report_data_source_association.insert().values(report_id=report.id, data_source_id=ds.id))
        await db.commit()
        report = (await db.execute(
            select(Report).where(Report.id == report.id)
            .options(selectinload(Report.data_sources).selectinload(DataSource.connections))
        )).scalar_one()
        ctx = {"db": db, "organization": org, "user": user, "report": report}
        listed = await end(ListFilesTool().run_stream({"connection_id": conn_id}, ctx))
        grep = await end(GrepFilesTool().run_stream(
            {"connection_id": conn_id, "pattern": "compute_invoice_total"}, ctx))
        read = await end(ReadFileTool().run_stream(
            {"connection_id": conn_id, "file_id": "src/billing/invoice.py"}, ctx))
        secret = await end(ReadFileTool().run_stream({"connection_id": conn_id, "file_id": ".env"}, ctx))
        return listed, grep, read, secret


@pytest.mark.parametrize("provider", list(PROVIDERS))
def test_agent_reads_code_through_each_provider_tile(
    provider, git_env, test_client, create_user, login_user, whoami,
):
    r, _token, _org = _connect(test_client, create_user, login_user, whoami, git_env, provider)
    assert r.status_code == 200, r.text
    ds = r.json()
    conn_id = ds["connections"][0]["id"]

    listed, grep, read, secret = asyncio.run(_tool_calls(ds["id"], conn_id))

    assert "invoice.py" in str(listed) and ".env" not in str(listed)
    assert "compute_invoice_total" in str(grep) and "invoice.py" in str(grep)
    assert "sum(l.amount" in str(read)
    assert "sk_live_should_never_show" not in str(secret)
    # The provider's token convention reached the server as a header.
    assert git_env.auth_headers and all(h for h in git_env.auth_headers)


def test_bad_token_is_rejected_at_connect_without_echoing_it(
    git_env, test_client, create_user, login_user, whoami,
):
    r, _t, _o = _connect(test_client, create_user, login_user, whoami, git_env, "github",
                         credentials={"token": "wrong-token-999"})
    assert r.status_code >= 400
    assert "wrong-token-999" not in r.text
