"""A connected git repository is indexed from its own regular files only.

The contract: nothing outside the clone directory is ever read while indexing
a repository, whatever the repository contains. A link is not followed, and a
clone is made with links checked out as plain files.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from app.core.dbt_parser import DBTResourceExtractor
from app.core.git_file_walker import is_repo_file, repo_glob, walk_repo_files
from app.core.lookml_parser import LookMLResourceExtractor
from app.core.markdown_parser import MarkdownResourceExtractor
from app.core.sqlx_parser import SQLXResourceExtractor
from app.core.tableau_parser import TableauTDSResourceExtractor
from app.services.git_service import GitService

MARKER = "OUTSIDE-THE-REPOSITORY-7c1f"


@pytest.fixture
def repo(tmp_path):
    """A clone directory with regular files, plus links that leave it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "notes.md").write_text(f"# {MARKER}\n")
    (outside / "model.sql").write_text(f"select '{MARKER}' as x\n")
    (outside / "schema.yml").write_text(f"models:\n  - name: {MARKER}\n")
    (outside / "view.lkml").write_text(f"view: {MARKER.replace('-', '_')} {{}}\n")
    (outside / "def.sqlx").write_text(f"config {{ type: 'table' }}\nselect '{MARKER}'\n")
    (outside / "ds.tds").write_text(f"<datasource formatted-name='{MARKER}' />\n")

    root = tmp_path / "clone"
    (root / "models").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "dbt_project.yml").write_text("name: demo\n")
    (root / "README.md").write_text("# Inside\n\nregular file\n")
    (root / "models" / "orders.sql").write_text("select 1 as order_id\n")
    (root / "models" / "schema.yml").write_text("models:\n  - name: orders\n")
    (root / "docs" / "guide.md").write_text("# Guide\n")

    os.symlink(outside / "notes.md", root / "leak.md")
    os.symlink(outside / "model.sql", root / "models" / "leak.sql")
    os.symlink(outside / "schema.yml", root / "models" / "leak.yml")
    os.symlink(outside / "view.lkml", root / "leak.lkml")
    os.symlink(outside / "def.sqlx", root / "leak.sqlx")
    os.symlink(outside / "ds.tds", root / "leak.tds")
    # A linked directory: its files are regular files, reached through a link.
    os.symlink(outside, root / "linked_dir")
    # A link that stays inside the repository is still a link.
    os.symlink(root / "README.md", root / "docs" / "alias.md")
    return root


def test_walker_indexes_regular_files_and_no_linked_ones(repo):
    files = walk_repo_files(str(repo), "demo")
    paths = {f.relative_path for f in files}
    assert {"demo/README.md", "demo/models/orders.sql", "demo/models/schema.yml", "demo/docs/guide.md"} <= paths
    assert not any("leak" in p or "linked_dir" in p or "alias" in p for p in paths)
    assert all(MARKER not in f.content for f in files)


@pytest.mark.parametrize("extractor", [
    DBTResourceExtractor, LookMLResourceExtractor, MarkdownResourceExtractor,
    SQLXResourceExtractor, TableauTDSResourceExtractor,
])
def test_parsers_never_read_through_a_link(repo, extractor):
    resources, columns, docs = extractor(str(repo)).extract_all_resources()
    assert MARKER not in repr((resources, dict(columns), dict(docs)))
    assert MARKER.replace("-", "_") not in repr((resources, dict(columns), dict(docs)))


def test_parsers_still_read_the_repository_itself(repo):
    md, _, _ = MarkdownResourceExtractor(str(repo)).extract_all_resources()
    assert any("Inside" in repr(doc) for doc in md["markdown_documents"])
    dbt, _, _ = DBTResourceExtractor(str(repo)).extract_all_resources()
    assert "orders" in repr(dbt)


def test_a_file_is_a_repo_file_only_when_it_really_lives_in_the_clone(repo):
    assert is_repo_file(repo / "README.md", repo)
    assert not is_repo_file(repo / "leak.md", repo)
    assert not is_repo_file(repo / "docs" / "alias.md", repo)
    assert not is_repo_file(repo / "linked_dir" / "notes.md", repo)
    assert not is_repo_file(repo / "missing.md", repo)
    assert {p.name for p in repo_glob(repo, "**/*.md")} == {"README.md", "guide.md"}


def _git(args, cwd, env=None):
    base = {"PATH": os.environ["PATH"], "HOME": str(cwd), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"}
    return subprocess.run(["git", *args], cwd=cwd, env=env or base, check=True, capture_output=True, text=True)


def _append(env, key, value):
    n = int(env.get("GIT_CONFIG_COUNT") or 0)
    env[f"GIT_CONFIG_KEY_{n}"], env[f"GIT_CONFIG_VALUE_{n}"] = key, value
    env["GIT_CONFIG_COUNT"] = str(n + 1)


def _source_repo(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _git(["init", "-q", "-b", "main"], src)
    (src / "README.md").write_text("# hello\n")
    os.symlink("/etc/hostname", src / "leak.md")
    _git(["add", "-A"], src)
    _git(["commit", "-q", "-m", "init"], src)
    return src


def test_clone_keeps_the_server_s_git_config_and_checks_links_out_as_plain_files(tmp_path):
    """Real git, with the environment git_service clones with. The operator's
    global gitconfig is honoured (here it is what makes the remote reachable),
    and the link in the repository arrives as a plain file."""
    src = _source_repo(tmp_path)
    global_config = tmp_path / "gitconfig"
    global_config.write_text(
        f'[url "{src}"]\n\tinsteadOf = https://git.example.test/org/repo.git\n'
        "[core]\n\tsymlinks = true\n"
    )

    # What _pat_env hands over: the app's environment plus an auth header.
    base = dict(os.environ, GIT_CONFIG_GLOBAL=str(global_config), HTTPS_PROXY="http://proxy.example.test:3128",
                GIT_SSL_CAINFO="/etc/ssl/corp.pem")
    _append(base, "http.extraHeader", "Authorization: Basic dGVzdDp0ZXN0")
    env = GitService._clone_env(base, "https://git.example.test/org/repo.git")

    # Nothing the deployment configured is taken away.
    assert {k: v for k, v in base.items() if not k.startswith("GIT_CONFIG_COUNT")}.items() <= env.items()
    pairs = [(env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(int(env["GIT_CONFIG_COUNT"]))]
    assert ("http.extraHeader", "Authorization: Basic dGVzdDp0ZXN0") in pairs

    dest = tmp_path / "dest"
    _git(["clone", "-q", "--depth", "1", "https://git.example.test/org/repo.git", str(dest)], tmp_path, env=env)

    assert (dest / "README.md").read_text() == "# hello\n"
    assert not (dest / "leak.md").is_symlink()
    assert (dest / "leak.md").read_text() == "/etc/hostname"
    # And indexing the clone reads only the repository's own content.
    files = walk_repo_files(str(dest), "repo")
    assert {f.relative_path for f in files} >= {"repo/README.md"}
    assert all(f.content in ("# hello\n", "/etc/hostname") for f in files)


def test_clone_protections_cover_hooks_submodules_and_remote_helpers():
    env = GitService._clone_env({"PATH": "/usr/bin"}, "git@git.example.test:org/repo.git")
    pairs = dict((env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(int(env["GIT_CONFIG_COUNT"])))
    assert pairs["core.symlinks"] == "false" and pairs["core.hooksPath"] == os.devnull
    assert pairs["submodule.recurse"] == "false" and pairs["protocol.ext.allow"] == "never"
    # ssh and https remotes are not restricted.
    assert not any(k.startswith("protocol.") and v == "never" for k, v in pairs.items() if k != "protocol.ext.allow")
