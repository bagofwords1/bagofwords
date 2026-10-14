"""Unit tests for GitService.test_connection branch validation.

Only `git ls-remote` (the external git boundary) is mocked.
"""

from unittest.mock import patch
from urllib.parse import urlparse

import pytest
from fastapi import HTTPException

from app.schemas.git_repository_schema import GitRepositoryCreate
from app.services.git_service import GitService

SHA = "0123456789abcdef0123456789abcdef01234567"


def _ls_remote_output(*refs: str) -> str:
    return "\n".join(f"{SHA}\t{ref}" for ref in refs)


async def _test_connection(ls_remote_output: str, branch: str):
    repo = GitRepositoryCreate(
        provider="github",
        repo_url="https://github.com/example/repo.git",
        branch=branch,
    )
    with patch("git.cmd.Git.ls_remote", return_value=ls_remote_output):
        return await GitService().test_connection(db=None, git_repo=repo, organization=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["main", "develop"])
async def test_empty_repository_is_reported_as_empty(branch):
    with pytest.raises(HTTPException) as exc:
        await _test_connection("", branch)
    assert exc.value.status_code == 400
    assert "empty" in exc.value.detail.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "branch, existing",
    [
        ("main", ["main-old", "feature/main"]),
        ("dev", ["develop", "master"]),
    ],
)
async def test_branch_must_match_exactly(branch, existing):
    output = _ls_remote_output("HEAD", *(f"refs/heads/{b}" for b in existing), f"refs/tags/{branch}")
    with pytest.raises(HTTPException) as exc:
        await _test_connection(output, branch)
    assert exc.value.status_code == 400
    for b in existing:
        assert b in exc.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["main", "release/1.x"])
async def test_existing_branch_succeeds(branch):
    output = _ls_remote_output("HEAD", f"refs/heads/{branch}", "refs/heads/other")
    result = await _test_connection(output, branch)
    assert result["success"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider, repo_url, host",
    [
        ("github", "https://github.com/example/repo.git", "github.com"),
        ("github", "git@github.com:example/repo.git", "github.com"),
        ("gitlab", "git@gitlab.com:group/sub/repo.git", "gitlab.com"),
        ("bitbucket", "git@bitbucket.org:team/repo.git", "bitbucket.org"),
    ],
)
async def test_access_token_is_sent_as_password_for_any_url_form(provider, repo_url, host):
    """A token without an explicit username must still reach git as the
    password of a username:password pair — a bare token is read as the
    username and git fails prompting for a password it can't read."""
    token = "tok-0123456789abcdef"
    repo = GitRepositoryCreate(
        provider=provider, repo_url=repo_url, branch="main", access_token=token,
    )
    with patch("git.cmd.Git.ls_remote", return_value=_ls_remote_output("refs/heads/main")) as ls:
        await GitService().test_connection(db=None, git_repo=repo, organization=None)

    parsed = urlparse(ls.call_args.args[0])
    assert parsed.scheme == "https"
    assert parsed.hostname == host
    assert parsed.username and parsed.username != token
    assert parsed.password == token


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "repo_url",
    ["https://bitbucket.org/team/repo.git", "git@bitbucket.org:team/repo.git"],
)
async def test_explicit_access_token_username_is_used(repo_url):
    repo = GitRepositoryCreate(
        provider="bitbucket", repo_url=repo_url, branch="main",
        access_token="app-password-xyz", access_token_username="someone",
    )
    with patch("git.cmd.Git.ls_remote", return_value=_ls_remote_output("refs/heads/main")) as ls:
        await GitService().test_connection(db=None, git_repo=repo, organization=None)

    parsed = urlparse(ls.call_args.args[0])
    assert (parsed.username, parsed.password) == ("someone", "app-password-xyz")
