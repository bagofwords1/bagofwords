# Code connections: GitHub / GitLab / Bitbucket (read-only repo files)

**Goal.** Let the agent read, search and grep the code of a repository, the way
coding agents do (Hermes, OpenCode, OpenClaw): a local copy plus plain file
reads, not one provider API call per file.

**Shape.** Three tiles in the data-source catalog — `github_repo`,
`gitlab_repo`, `bitbucket_repo` — one repository per connection. Issues, PRs
and pipelines stay with the separate MCP presets.

```
agent file tools (list_files / grep_files / read_file / search_files)
  → resolve_file_client (FILE_SOURCE_TYPES)
    → GitRepoClient  (subclass of NetworkDirClient: root confinement, grep, docs)
      → repo_cache.ensure(spec)  → cached shallow clone, refreshed when stale
        → git (hardened) over HTTPS with a per-command Authorization header
```

## Design decisions

| Decision | Why |
|---|---|
| Clone lazily, on the first file-tool call (or the first indexing run) | Connect stays fast; `test_connection` only runs `git ls-remote` |
| `--depth 1 --single-branch --no-tags`, **not** `--filter=blob:none` | A working tree needs every blob at HEAD, so a partial clone saves nothing here; a size cap (`BOW_REPO_CACHE_MAX_REPO_MB`, 1 GB) and timeouts bound it instead |
| One entry per (url, branch, **credential hash**) | A connection with a weaker token can never read a clone made with a stronger one |
| Refresh when older than `BOW_REPO_CACHE_TTL_SECONDS` (600) | Same model as OpenCode's repository cache, tighter TTL for a shared server |
| Failed refresh → serve the last checkout, retry after 120 s | A flaky remote doesn't break reads or hammer the server |
| `fcntl` lock per entry | Concurrent chats / workers clone once |
| Idle eviction (7 days) + disk budget (`BOW_REPO_CACHE_MAX_MB`, 5 GB, LRU) | Clones are a cache; always safe to delete |

## Security model — nothing in the repo ever runs

`app/services/git_hardening.py` (`hardened_args` / `hardened_env`):

| Risk | Control |
|---|---|
| Server's global/system git config (LFS filters, hooks, fsmonitor) | `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_SYSTEM=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`, `HOME=/dev/null` |
| Hooks / templates | `core.hooksPath=/dev/null`, `init.templateDir=` |
| `.gitattributes` filter/smudge drivers | `core.attributesFile=/dev/null`, `filter.lfs.*` emptied, `GIT_LFS_SKIP_SMUDGE=1` |
| Submodules to internal URLs | `--no-recurse-submodules`, `submodule.recurse=false` |
| Odd transports (`ext::`, `file://`), redirects | `protocol.allow=never` + `protocol.https.allow=always`, `http.followRedirects=false`; host re-validated and pinned (`GitService._git_env`) |
| Symlinks to `/etc/passwd` | `core.symlinks=false` (plain files) + `_resolve` root confinement |
| Executable files | `core.fileMode=false` + files chmod 0600, dirs 0700 |
| Token on disk | per-command `http.extraHeader` via `GIT_CONFIG_*`; remote URL stays clean |
| Prompts / hangs | `GIT_TERMINAL_PROMPT=0`, empty askpass, timeouts |

Read layer (`GitRepoClient`): `.git/` and vendor dirs never listed or read;
secret-looking files (`.env`, `*.pem`, keys, …) hidden unless
`allow_secret_files`; invisible Unicode TAG characters stripped; reads capped at
`max_file_mb` (default 5).

**Existing Git connection** (dbt/LookML indexing, push, PRs) got the two
credential fixes: PATs moved from `https://user:token@…` URLs to the
per-command header, and SSH no longer uses `StrictHostKeyChecking=no` —
GitHub/GitLab/Bitbucket keys are pinned (official docs), other hosts are
learned on first use and then enforced (`accept-new`).

## Reproduce → verify

```bash
cd backend
# cache, refresh, locking, credentials, hostile repo — real HTTPS git server
uv run --extra dev pytest tests/unit/test_repo_cache.py -q
# the existing Git connection keeps tokens off disk, verifies SSH host keys
uv run --extra dev pytest tests/unit/test_git_credentials_hardening.py tests/unit/test_git_test_connection.py -q
# API create → agent list/grep/read through each tile
TESTING=true uv run --extra dev pytest tests/e2e/test_git_repo_connector_e2e.py -m e2e --db=sqlite -q
```

`tests/mocks/git_https_server.py` is a real smart-HTTPS git server
(`git http-backend` behind uvicorn, self-signed cert, optional Basic auth) — it
can back a manual UI check too.

Hostile-repo fixture (`test_repo_cache.py::hostile`): a server-wide git config
that would run a smudge filter and a post-checkout hook, a symlink to
`/etc/passwd`, a gitlink submodule to `169.254.169.254`, an executable script, a
`.env`, a private key, hidden Unicode, a 2 MB binary. Mutation check: with the
hardening disabled, the filter/hook and symlink tests fail (verified).

Live: `GitHubRepoClient(repo="octocat/Hello-World")` → `test_connection` OK,
clone 0.5 s, `read_file("README")` → `Hello World!`, second client → `cached`.

## Not covered / follow-ups

- Per-user tokens (each user's clone); OAuth sign-in instead of tokens.
- SSH-key auth for the code tiles (the existing Git connection keeps SSH).
- One tile offering both code and the MCP issues/PRs with a single sign-in.
- Folding "Connect Git" (dbt/LookML indexing) into these tiles.
