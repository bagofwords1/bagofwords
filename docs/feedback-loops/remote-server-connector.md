# Remote server connector — build and verification

The requested feature is Linux/Windows inspection and log access from a Linux
Docker Compose deployment, with optional explicitly approved maintenance commands.
Two catalog entries now implement it: Remote Server (SSH) and Windows Logs (SMB).

## Implementation boundary

Previously the registry and tool-provider execution paths accepted MCP/custom API
connections, but had no remote SSH/SMB provider. Initial remote registry tests failed
for the missing types. The new providers are registered in
`backend/app/schemas/data_source_registry.py`; their bounded subprocess transports
live in `backend/app/data_sources/clients/remote_server_client.py` and
`remote_smb_worker.py`. They use the existing encrypted credential storage,
connection/agent authorization and durable tool-confirmation flow.

Inspection exposes typed operations with bounded arguments. It never asks an LLM
to decide whether Bash is read-only. Shell execution follows connection-wide
Allow/Deny, strict approval, or policy-controlled operation with persistent user
choices and command rules. The direct test-tool endpoint cannot execute shell;
the gateway runs only policy-authorized commands and cannot bypass Ask. Access, policy, connection settings and resolved credentials are
checked again after the approval wait. OS permissions and a forced reader provide
the remote enforcement boundary; see `tools/remote_server/README.md`.

## Deterministic regression loop

From a fresh dependency-installed checkout:

```bash
cd backend
uv sync --frozen --extra dev
uv run pytest tests/unit/test_remote_server_client.py \
  tests/unit/test_tool_policy_resolution.py tests/unit/test_tool_confirmation_service.py \
  tests/e2e/test_data_source.py tests/e2e/test_connection.py \
  tests/e2e/rbac/test_remote_server.py tests/integrations/test_remote_server.py \
  --db=sqlite -q --disable-warnings
```

Initial implementation on 2026-11-04: **94 passed, 2 skipped**. The skipped integration cases
require environment-provided customer SSH/SMB configurations. Existing warnings
were emitted; this was not a warnings-clean run.

The existing MCP execution and RBAC regression suites also passed: **31 passed**
(`tests/e2e/test_mcp_tools.py`, `tests/e2e/rbac/test_mcp_tools_run_scope.py`,
`tests/e2e/rbac/test_mcp_tools_cap.py`). Total selected coverage: **125 passed,
2 environment-dependent skips**. Ruff checks for the new Python modules and
`git diff --check` passed. A full Docker image build was not performed.

Tests cover registry resolution, policy combinations, disabled operations, path
traversal, symlinks, FIFO rejection, literal filenames, output/time limits,
organization isolation, member permissions, mandatory approval and direct-call
rejection. The approval tests use the real database, services and streaming tool
executor, replacing only the external SSH executable. They prove that approval
executes once, denial/unattended execution does not execute, and changing the
connection target while approval is pending prevents execution.

## Live SSH verification

```bash
BOW_VERIFY_SSH_ALIAS=bow backend/.venv/bin/python tools/remote_server/verify_ssh.py
```

This opt-in script reads the existing SSH alias and trusted known-hosts entry. It
creates a unique temporary fixture, transfers the reader, runs the real provider,
and removes the fixture in `finally`. It never modifies sshd, production services,
production logs, or the source private key. Encrypted-key verification uses a
private temporary copy of the key.

Observed after connection reuse: **14/14 checks true** — authentication, encrypted private key,
wrong-host-key rejection, listing, reading, literal search, system status,
out-of-root rejection, unapproved-shell rejection, approved harmless `printf`,
single-use authorization, transport reuse across client instances, fresh shell state, and selectable POSIX sh execution. This validates transport and reader behavior, not that
the existing `bow` account is confined by sshd/OS policy.

## Live SMB verification

A disposable Samba container on `bow` exposed only synthetic fixture logs through
a read-only bind/share, with encryption required, resource limits and a loopback
listener. The real SMB provider connected through a temporary SSH tunnel. Observed:
connection, listing, reading and literal search passed; incorrect password was
rejected. Server-side session IDs remained unchanged across repeated operations, verifying reuse. No production shares were read or changed. The container, tunnel and
fixture are removed after verification.

For customer Windows verification, set `REMOTE_SMB_TEST_CONFIG` or
`REMOTE_SSH_TEST_CONFIG` to connection JSON in the test process environment and run
`tests/integrations/test_remote_server.py`. Never commit credentials.

## Browser verification

An isolated SQLite application ran on backend 8057/frontend 3057 with a throwaway
organization. A real SSH connection was created through the UI and discovered
tools. Browser checks exercised the SSH/SMB forms, inspection checkbox toggles,
Windows OS selection and mandatory approval rendering. The approval harness used
a synthetic command/target; no browser click restarted a service.

Evidence is under `media/pr/remote-server/`:

- `approval-before.png` / `approval-after.png`: same component fixture and viewport;
  the base component offered Always allow, the changed component hides it and
  expands exact arguments for mandatory approval.
- `ssh-form-after.png`, `windows-ssh-form.png`, `smb-form.png`: empty-secret setup forms.
- `video/`: recorded connector-selection and configuration flows.

The temporary approval harness route is removed after capture. No LLM provider
was configured in the isolated organization: verification exercises the actual
streaming tool executor, not a full natural-language model conversation.

## Limits and deployment requirements

- Windows OpenSSH/PowerShell and AD/Kerberos require a real Windows/domain target
  for acceptance testing. Samba is evidence of SMB interoperability, not proof of
  every Windows/AD configuration. SSH password, keyboard-interactive and user
  certificates are implemented but were not live-tested against `bow`.
- The Windows reader and SMB path checks require administrator-controlled
  directories; they do not provide Linux-style atomic descriptor traversal.
- Inspection reads bounded windows; it does not promise exhaustive full-log search.
- A shell account can perform anything its OS privileges allow when authorized by policy or approval.
  Disconnect/timeout does not guarantee remote process termination.
- No production reader installation, Docker image deployment or sshd change was
  performed. The image packages reader/setup assets at `/app/tools/remote_server`.
- Existing locale key drift predates this change; this feature adds the same four
  inspection-operation labels to all shipped locale catalogs.

## Persistent permissions and transport reuse update

The new policy tests initially failed because `remote_shell_policy` did not exist.
The previous behavior forced Ask regardless of saved Allow and opened a new
transport for each request. The final behavior is documented in
`tools/remote_server/README.md`, including rule precedence, wildcard limits,
session lifetime and the distinction between a transport and a persistent shell.

The migration `remotepolicy01` adds JSON command rules to the existing per-user,
per-tool preference table. Member-accessible `my_shell_rules` GET/PUT endpoints
read and replace only the caller's rules. Approval cards persist exact/wildcard
rules for the user or the current report (eight-hour expiry). Tool preference
reset removes the rules. Connection Always allow overrides personal preferences
for authorized users; disabled tools and administrator tool/agent Deny still win.

Add these to the regression command above:

```bash
uv run pytest tests/unit/test_remote_shell_policy.py tests/unit/test_remote_pool.py \
  tests/e2e/rbac/test_remote_server.py --db=sqlite -q --disable-warnings
```

The expanded core run passed 108 tests, followed by 11 remote API/approval tests
including the added session/user persistence cases. The existing MCP/RBAC suites
plus pool/policy unit cases passed 45 tests. These overlap and must not be summed.
Final combined run: **145 passed, 2 skipped** in 104.90 seconds. The two skipped tests require customer environment credentials; warnings were emitted.

Pool tests verify reuse, tenant/user scope separation, replacement after settings
changes, idle eviction, capacity eviction and no replay on failure. Pools do not
cache access decisions. SSH settings/credentials remain in a private temporary
workspace for the transport lifetime; SMB sessions live in isolated reusable
processes. A worker only reuses the same validated endpoint and credential/config
fingerprint. Idle sessions close automatically; changed settings replace an old
session when next used. No universal remote process cancellation is promised.

Browser evidence for the updated approval card and connection settings is under
`media/pr/remote-policy/`. Browser approval tests intercept a synthetic confirmation
request to inspect its scope and pattern; the backend approval persistence and
execution checks run separately against real services and the test database.

Final follow-up checks: the remote API/gateway suite passed 11 cases after testing
member execution under connection-wide Allow and disabled-tool rejection. Approval
and confirmation regression checks passed 17 cases after making remembered shell
decisions atomic; policy unit checks passed 13 cases after precedence refinement.
The browser pass verified the connection-wide setting, user rule editor save,
request scope/pattern payload, and Hebrew rendering. Screenshots and a flow video
are retained; temporary harness routes and test credentials are removed.

PR isolation verification: the selected combined suite on a clean worktree based
on `82b20abe6`, containing only this connector change, passed **147 tests**, with
**2 credential-gated skips**, in 47.09 seconds. Ruff on new remote modules and
`git diff --check` passed. The initial invocation needed an explicit disposable
`BOW_DATABASE_URL`; no tests ran until that environment setting was supplied.
