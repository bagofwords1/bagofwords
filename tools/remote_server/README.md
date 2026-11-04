# Remote server connectors

`remote_ssh` exposes bounded inspection tools and an optional maintenance shell.
`remote_smb` reads Windows shares without installing BOW on Windows. Both use the
existing agent/connection RBAC, per-tool policies, user preferences and audit flow.
The catalog forms are generated from `backend/app/schemas/data_sources/remote_server.py`.

## Default: agentless SSH

No reader installation is required. The connection runs bounded inspection code
in memory using existing Python 3 on Linux or PowerShell on Windows. It writes no
reader or policy files on the host. If the required runtime is unavailable, the
connection test fails with setup guidance; it does not install software.

Host, port, OS, verified host key and authentication are the main settings.
Advanced settings contains inspection mode, allowed paths, shell policy and limits.
The shell is enabled by default with policy-controlled permissions: unmatched
commands require approval. Saved permissions and administrator restrictions apply.

Allowed paths accepts literal roots and wildcard patterns (JSON list), for example
`["/var/log/*.log"]`, `["C:\\Logs\\*.log"]`, or `["*"]`. A bare `*` permits any
valid path readable by the remote account (within the configured share for SMB).
Wildcards match full paths, including separators; inspection requests still name
one concrete file/directory and remain bounded. Empty paths deny file inspection.
Traversal, special files, symlinks and Windows alternate streams remain prohibited.
For a file pattern, use a concrete matching file; listing an ancestor directory
requires its own allowed root. These app settings do not constrain the SSH account
outside the app. OS permissions remain the enforcement boundary.

## Optional: installed restricted reader

Select **Restricted reader** under Advanced settings to use the installation
instructions below. The server-owned policy is an additional restriction; a client
wildcard cannot widen it. Use a separate maintenance account if ForceCommand is set.

## Linux SSH inspection setup

An administrator installs the reader and its policy. Use a dedicated unprivileged
account with read access only to approved logs, no sudo, and no Docker group/socket.
Do not grant write access to its home, startup files, reader, policy or parent directories.
The reader uses directory file descriptors with `O_NOFOLLOW` and refuses special files.

Install `reader.py` as `/usr/local/lib/bow/reader.py`, owned by root and mode 0644,
with root-owned parent directories. Install `/etc/bow-reader.json`, root-owned and
readable by the reader account:

```json
{
  "allowed_paths": ["/var/log/myapp"],
  "allowed_operations": ["list_directory", "read_file", "search_file", "system_status"],
  "max_lines": 200,
  "max_output_bytes": 65536
}
```

Configure a dedicated account in `sshd_config` (adapt paths to the host):

```text
Match User bow-reader
    AuthenticationMethods publickey
    ForceCommand /usr/bin/python3 -I /usr/local/lib/bow/reader.py
    DisableForwarding yes
    PermitTTY no
    PermitUserRC no
    PermitUserEnvironment no
```

Use an administrator-owned authorized-keys file. Validate with `sshd -t` and
inspect effective settings with `sshd -T -C user=bow-reader,host=localhost,addr=127.0.0.1`
before reloading SSH. Do not accept client-supplied Python/shell environment variables.
Apply host resource limits (CPU, memory, processes) to this account. The reader
also has a 120-second alarm; BOW has shorter configurable request/output limits.
For a stronger boundary, expose read-only log snapshots in an isolated environment.
The reader's capability response proves reachability, not that the account is confined.

BOW configuration: actual hostname/port, trusted **server public key** (not merely
its fingerprint), allowed roots matching the server policy, and credentials. Host
key changes fail closed; update the pin only after verifying the new key independently.
Default reader paths are selected by OS. An explicit reader-path override is available.
No ambient `~/.ssh/config`, identity agent, private key, or known-hosts file is inherited
by production clients.

## Windows SSH inspection setup

Enable Windows OpenSSH Server. Install `reader.ps1` as
`C:\ProgramData\BOW\reader.ps1` and an administrator-owned `reader.json` beside it:

```json
{
  "allowed_paths": ["D:\\SERVERLOGS\\AppsLogs", "D:\\SERVERLOGS\\IISLogs"],
  "allowed_operations": ["list_directory", "read_file", "search_file", "system_status"],
  "max_lines": 200,
  "max_output_bytes": 65536
}
```

Use a non-administrator account with NTFS read/list access, and configure OpenSSH
`ForceCommand` to run `powershell.exe -NoLogo -NoProfile -NonInteractive -File
C:\ProgramData\BOW\reader.ps1`. Disable forwarding and PTYs. Validate the directives
against the installed Windows OpenSSH version. Protect the script, policy, account
startup files and all log-directory ancestors from modification by that account.
The reader rejects alternate data streams, traversal, junctions and symlinks.
Its path checks require an administrator-controlled directory namespace: unlike
Linux's descriptor-based traversal they are not atomic against another writer
concurrently replacing directories. Use a dedicated share/snapshot when that
assumption cannot be guaranteed. Configure Windows process/resource isolation
separately; Linux sandbox settings do not apply to Windows.

## Windows SMB / IIS logs

No remote helper needed. Create a dedicated share such as `BOWLogs` pointing to
`D:\SERVERLOGS`. Grant the service account read access in **both** share permissions
and NTFS ACLs, inherited by new rotated logs. Keep directories administrator-controlled;
reparse points and DFS referrals are unsupported. Avoid the `d$` administrative share.
Allow TCP 445 from the BOW deployment; the backend container connects directly, with
no privileged container or CIFS host mount. SMB signing is required; encryption is
required by default. Disabling encryption requires an explicit connector setting.

Use share-relative allowed roots (`AppsLogs`, `IISLogs`), or `.` for the entire
**dedicated** share. Each user/connection has an isolated reusable worker with a separate
connection/credential cache; credentials are not shared across organizations.

## Authentication

- SSH key: private key, optional passphrase, optional OpenSSH user certificate.
- SSH password: one account password, no key-agent fallback.
- SSH keyboard-interactive: one fixed password response. Dynamic OTP, push MFA,
  multi-step challenges and interactive login sessions are not supported.
- SSH Kerberos: service principal, base64 keytab and remote username.
- SMB NTLMv2: `DOMAIN\user` or `SERVER\localuser` and password.
- SMB Kerberos: principal and base64 keytab; no fallback to NTLM.

Kerberos uses a temporary per-session ticket cache, removed when the session closes.
The Docker image already includes OpenSSH, kinit and the Python Kerberos extra.
Reader scripts and this guide are packaged at `/app/tools/remote_server`; copy the
appropriate reader to the target server during administrator setup.
Mount the organization's `/etc/krb5.conf` read-only, configure DNS/KDC reachability
and clock synchronization. Kerberos requires correct FQDN/SPNs. Interactive MFA
and arbitrary deployment ticket-cache paths are deliberately not exposed in forms.
Credentials are stored using BOW's existing encrypted connection credential storage.
The identity and principal may be shown in setup/approval; keys and passwords are not.

## Maintenance and approvals

Use a separate maintenance connection/account when inspection must remain confined.
An SSH account forced to the reader cannot also execute arbitrary shell commands.
Enable maintenance shell only for a credential with the intended remote privileges.
For a maintenance-only connection, clear all inspection operations; its connection
test checks SSH authentication without requiring the inspection reader.
Restart, deploy, Docker update, package installation and other maintenance are
expressed as Bash, POSIX sh, or PowerShell commands (Linux shell is selectable); there are no automatic command-prefix
allowlists or automatic sudo escalation. Noninteractive commands only; elevation
must be explicitly configured on the host and visible in the proposed command.

Connection shell permissions support four modes:

- **Policy controlled** (default): unmatched commands ask. Saved personal
  Allow/Ask/Deny choices and command rules determine subsequent execution.
- **Require approval every time**: human approval for every execution, even with
  a saved personal Allow. Personal Deny still blocks.
- **Always allow for everyone**: no prompts for authorized users, regardless of
  their personal preferences. Disabled tools and administrator tool/agent Deny
  still block. This does not grant access to an otherwise inaccessible connection.
- **Always deny**: shell execution is blocked.

In policy-controlled mode, administrator command Deny rules cannot be overridden
by personal choices. Other administrator rules set defaults; the personal tool choice applies next,
then matching user rules apply (last matching rule wins). A rule has `pattern`, `action`
(`allow`, `ask`, `deny`), `match` (`exact`, `glob`) and an optional
`working_directory`. Wildcards match the complete command, case-sensitively.
Prefix Allow rules refuse commands containing shell operators or expansions;
use an exact command rule for a reviewed script, or explicitly configure `*` for
unrestricted shell access. Patterns do not prove program behavior: permitted
programs can run other code. Enforce real boundaries through remote OS permissions.

Approval cards offer Allow once, Allow for this session, and Always allow matching.
The editable pattern defaults to the exact command; saved approvals are scoped to
its working directory. Session rules apply to interactive runs for the current user and report, expire
after eight hours, and survive reloads/workers during that period. Permanent rules
are stored per user and connection tool in the database. Review/edit them under
**My shell rules**; resetting the personal tool preference clears all its rules.
Strict mode does not offer remembered approvals.

The approval shows host, port, resolved username, command and working directory.
Connection/credential changes or access revocation while waiting invalidate it.
Headless runs execute only when policy resolves to Allow; Ask fails closed. Gateway
calls use the same policy and cannot use a caller-supplied confirmation flag to
bypass Ask. The connection test-tool endpoint does not execute shell commands.

## Connection reuse

SSH reuses an authenticated OpenSSH multiplexed transport; each command opens a
fresh channel/shell, without persisting working directory or environment changes.
SMB uses a persistent isolated process and credential/session cache. Pools are
process-local, capped at 32 sessions per application worker, and keyed by
organization, connection and requesting user. A fingerprint of settings, resolved
credentials and the validated destination address prevents reuse across changes.
Defaults expire idle sessions after 120 seconds (configurable 10–900); a reaper
closes them within five seconds of expiry. Configuration changes replace an old
transport on its next use; unused transports expire. Application shutdown cleans
up sessions and temporary credentials. Multiple application workers have separate
pools. Access and policy checks run on every operation, outside the pool.

Failed operations are not replayed automatically. A dead transport is replaced
for a later request; a command may already have executed when its connection fails.

Approval is authorization, not a proof of safety. Root shell remains root shell.
BOW times out/closes transport on excessive runtime/output, but an arbitrary remote
command may continue after disconnect. Check actual state before retrying. A command
that invokes downloaded/mutable code authorizes that execution, not a frozen copy of
its transitive dependencies. No automatic shell retries are performed by the client.

## Inspection behavior

`list_directory` lists one directory (including nested/rotated log names).
`read_file` reads the beginning or end of a bounded byte window and limits lines.
`search_file` performs literal search within the beginning byte window of one file;
it is not an exhaustive recursive filesystem search. Results report the window size
and truncation. Use directory listing and explicit file reads to investigate rotations.
`system_status` reports OS, memory, uptime and Linux load/disk usage. Other diagnostics,
including Docker/service inspection, can be requested through the approved shell.
SMB has only file operations. No promises of full-file search are made when truncated.

## Verification

```bash
cd backend
uv run pytest tests/unit/test_remote_server_client.py tests/unit/test_tool_policy_resolution.py \
  tests/e2e/rbac/test_remote_server.py tests/e2e/test_connection.py tests/e2e/test_data_source.py --db=sqlite -q
cd ..
BOW_VERIFY_SSH_ALIAS=bow backend/.venv/bin/python tools/remote_server/verify_ssh.py
```

The opt-in live loop reads the existing alias and trusted known-hosts entry, creates
and removes a unique `/tmp` fixture, tests the real client and a harmless `printf`
shell command. It does **not** install/configure sshd or claim the supplied account
is restricted. Never commit SSH credentials or screenshots of populated secret fields.
See `docs/feedback-loops/remote-server-connector.md` for observed results and limits.
