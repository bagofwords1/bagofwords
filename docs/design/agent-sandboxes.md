# Agent sandboxes — containers where a report's agent runs code

Status: phase 0 (spike) and phase 1 (core) are implemented on branch
`dima/bow-229-sandboxes-for-bow-agents`. Phases 2 and 3 are not built. Each section is
marked **Built**, **Built differently** or **Not built**; `Implementation notes` at the
end lists every deviation in one place.
Scope: four planner tools (`create_sandbox`, `run_in_sandbox`, `sandbox_status`,
`delete_sandbox`), a driver interface with one real driver (`docker-ssh`), admin-defined
environments, per-environment network policy, permissions, audit, three scheduled jobs,
and the Settings → Sandbox admin section (Backends, Environments, Settings).
Out of scope (deliberately, see [Deferred](#deferred)): MCP server tools, the fleet view
and per-sandbox page, the domain log UI, a report tool block and status chip, warm
pools, hosted apps, and a Kubernetes driver.

Source of the design: Linear BOW-229 (sub-issues BOW-234 to BOW-246). This document
describes the code. Where the issue and the code differ, the code is what is written
here and the difference is called out.

## Summary

An agent in a report can run Python, Node, Go or shell code in a persistent container.
The container runs on a Linux server the operator provides. BoW starts it over SSH.
Inside it runs `sandboxd`, the open-source runtime daemon from
`kubernetes-sigs/agent-sandbox`, which executes processes and serves files. Files the
code writes under `/workspace/outputs` are saved as report files.

The decisions that shape everything below:

1. **Off three times over.** A deployment flag, an organization switch, and a
   permission plus a per-environment grant. All default to off. With the deployment
   flag off the feature does not exist: nothing is registered, scheduled or visible.
2. **One seam.** Everything infrastructural is behind `SandboxDriver`. The service and
   the runtime client know nothing about Docker, SSH, Caddy or iptables. A unit test
   enforces this.
3. **Internet off by default.** Network access is a property of the environment:
   `none` (default), `registries`, `allowlist`, `full`. Report files enter a sandbox
   through `input_file_ids`, so open egress would be a path for data to leave.
4. **One sandbox per report, stopped when idle.** It keeps its files and installed
   packages. It is stopped at the idle timeout and resumed by the next run.
5. **BoW installs only its own containers and one directory on the host.** Docker and
   gVisor are prerequisites the admin installs. BoW detects them.

**Relation to `docs/design/code-execution-sandbox.md`.** That document describes the
process sandbox for generated Python in `create_data` / `inspect_data`: a disposable
child process on the BoW server, with data-source access brokered over a pipe. It is
unchanged. Agent sandboxes are a second, separate tier: a long-lived container on
another machine, with no data-source credentials, for work on files.

## What is built

| Plan item (BOW-229 delivery plan) | Status |
|---|---|
| Phase 0: spike on one Docker host | Built. The firewall question is answered for Ubuntu only |
| 1.1 Feature gate and flag-off tests | Built. Three existing registry tests had to change |
| 1.2 Data model and migration | Built |
| 1.3 Driver interface, registry, fake driver, contract suite | Built |
| 1.4 Runtime client for `sandboxd` | Built |
| 1.5 Sandbox service, 1.6 Permissions | Built |
| 1.7 `docker-ssh` driver | Built, with the spike's design changes |
| 1.8 Environments API, built-in environment, image builds | Built, except that CI has not built or published any image |
| 1.9 Planner tools, prompt section, artifacts, input files | Built |
| 1.10 Scheduled jobs | Built |
| 1.11 Admin UI | In progress when this was written; described from the files present |
| 1.12 Live test and this document | Built. The live run used a scripted stand-in model |
| Phase 2 (control and visibility), phase 3 (polish) | Not built. Some backend parts exist early (see Deferred) |

## Feature gate

**Built.** Code: `backend/app/services/sandbox/feature.py`.

| Level | Who turns it on | Where it lives |
|---|---|---|
| 1. Deployment flag `BOW_AGENT_SANDBOXES_ENABLED` | Whoever operates the deployment | `AgentSandboxesConfig` in `settings/bow_config.py`, read only through `sandbox_feature_enabled()` |
| 2. Organization switch | An admin with `manage_sandboxes` | `enabled` under the `agent_sandboxes` key of `OrganizationSettings.config` (`settings.py`) |
| 3. `use_agent_sandbox` and `use` on an environment | An admin, per role, group or user | `access.py`, `catalog.py` |

With the deployment flag off:

| Area | Behaviour | Code |
|---|---|---|
| API | Every `/api/sandbox/*` route answers 404 with code `agent_sandboxes.disabled` | Router dependency `require_sandbox_feature` |
| `/api/settings` | Reports `agent_sandboxes: {enabled: false}` | `routes/bow_settings.py` |
| Tools | Not listed by the tool registry in any mode, and not executable | `ToolRegistry._dark` in `ai/registry.py` |
| Prompt | Unchanged. The section is added only when `run_in_sandbox` is in the catalog | `services/sandbox/prompt.py` |
| Roles | The two permissions stay valid strings but are not in the role editor payload | `extend_role_editor_payload` |
| Jobs | Not scheduled. Startup logs a warning if sandboxes still exist | `jobs.py`, `main.py` |
| Admin menu | No Sandbox entry. The page redirects to `/settings` if opened by URL | `useAdminNav.ts`, `pages/settings/sandbox.vue` |
| Imports | `asyncssh` and the `docker-ssh` driver are loaded on first use only | `drivers/registry.py` |

The organization switch is a plain dict, not a `FeatureConfig`, so the generic settings
pages do not list it, and the generic settings writer refuses the key. Turning the
switch off stops new sandboxes and new runs (`SandboxService.ensure` raises
`sandbox.disabled`); the jobs keep running, so existing sandboxes expire normally.
Turning the deployment flag off stops the jobs, so delete the sandboxes first.

## Architecture

**Built.** Package: `backend/app/services/sandbox/`.

```
 planner tool (ai/tools/implementations/agent_sandbox.py)
        │
        ▼
 SandboxService ──────────── SandboxDriver (docker-ssh)          sandbox host
   │                    SSH (control)   ┌──────────────────────────────────────────┐
   │                  ─────────────────►│ docker run / stop / start / rm / stats    │
   ▼                                    │ admin APIs of router and proxy (sockets)  │
 SandboxRuntimeClient   HTTPS, one port │ router ──► sandbox A  sandboxd gRPC+REST  │
                      ─────────────────►│ (Caddy) └► sandbox B  ...                 │
                        X-Sandbox-Id    │ sandbox ──► egress proxy ──► internet     │
                        bearer token    │ firewall helper (host network)            │
                                        └──────────────────────────────────────────┘
```

| Layer | Owns | Knows about Docker or SSH |
|---|---|---|
| Service (`service.py`, `backends.py`, `environments.py`, `access.py`) | States, quotas, placement, permissions, records, audit | No |
| Runtime client (`runtime_client.py`) | Talking to `sandboxd`: run a process, move files | No. It needs an `Endpoint` only |
| Driver (`drivers/base.py`, `drivers/docker_ssh/`) | Create, stop, start, delete, network policy, isolation, bootstrap, stats, reconcile | Yes, and only here |

- The service hands the driver a `SandboxSpec` and stores the opaque `SandboxHandle` it
  gets back in `sandboxes.driver_ref`.
- A driver never holds a sandbox's token, only its SHA-256. `endpoint()` leaves `token`
  empty and the service fills it in.
- Drivers declare capabilities (`stop_resume`, `strong_isolation`, `domain_allowlist`,
  `live_stats`, `warm_pool`, `ports`). Placement and environment warnings read those.
- `drivers/registry.py` is the only file that maps a type name to code. Each type
  carries a Pydantic config schema and credentials schema; the admin form is rendered
  from them.
- `backend/tests/unit/test_agent_sandbox_layering.py` fails if code above the driver
  names the driver's parts or branches on the backend type.
- Two types are registered: `docker-ssh` and `fake`. The fake driver runs code as the
  BoW server's own user with no isolation. It is offered only under `TESTING` or when
  `BOW_AGENT_SANDBOXES_FAKE_DRIVER` is set.
- Any worker can serve any sandbox. There is no queue and no long-lived connection.

## Data model

**Built.** Migration `backend/alembic/versions/agentsbx01_agent_sandboxes.py`, models in
`backend/app/models/sandbox.py`. The migration adds eight tables and alters none.

| Table | Holds | Used in phase 1 |
|---|---|---|
| `sandbox_backends` | Type, config, encrypted credentials, driver state, capabilities, labels, capacity, bootstrap steps, health | Yes |
| `sandbox_environments` | Image, digest, resources, network policy, variables, encrypted secrets, labels, flags | Yes |
| `sandbox_agent_defaults` | An agent's default environment | Read only (see Open questions) |
| `sandboxes` | One per report: backend, environment, state, encrypted token, limits, timestamps | Yes |
| `sandbox_runs` | One per run: code (encrypted), hash, status, exit code, output (encrypted, capped), file ids | Yes |
| `sandbox_events` | The timeline: state changes, setup script, package installs, run start and finish | Written; no page reads it yet |
| `sandbox_egress_log`, `sandbox_exposures` | Domain log; reserved for hosted apps | No. Created empty |

- One live sandbox per report is a partial unique index on `sandboxes(report_id)`. When
  two workers create at once, the loser uses the winner's row.
- References to reports, users, completions, files and data sources are indexed ids
  without foreign keys. A sandbox row can never block deleting a report or a user.
- The Sandbox settings are not a table. They live in the organization settings.

## The docker-ssh driver

**Built, with the spike's design changes.** Code: `drivers/docker_ssh/`. Image sources:
`deploy/sandbox/`. A backend is one Linux server. Everything BoW puts on it is under
`/var/lib/bow-sandbox` (`layout.py`) and carries the label `bow.instance=<id>`.

### SSH (`ssh.py`)

- **Host key first.** Test connection reads the server's host keys without logging in
  and shows the fingerprint. No login happens until an admin confirms it
  (`POST /backends/{id}/confirm-identity`) or a fingerprint entered with the backend
  matches. The key is then pinned in `driver_state`. A changed key is refused.
  Bootstrap refuses an unconfirmed server.
- **root, or `sudo -n`.** The mode is detected per connection, and the `sudo -n` prefix
  is added in one place. A user who is only in the `docker` group is refused.
- **Commands are argument lists**, built as pure functions in `commands.py` and quoted
  by the SSH layer. Model-written text never reaches a command line. Secrets travel on
  standard input or in a mode-600 file removed after `docker run`.
- **Keys.** Creating a backend without a private key generates an ed25519 pair. The
  private key is encrypted and never returned. An optional one-time password is used
  for one login that installs the public key, after the host key is pinned, and is
  never stored.

### What a sandbox is on the host (`commands.sandbox_run`)

| Property | How |
|---|---|
| User and privileges | Non-root, all capabilities dropped, `no-new-privileges` |
| Limits | `--cpus`, `--memory` with no swap, `--pids-limit` 512 |
| Root filesystem | Read-only. `/tmp` is a 256 MB tmpfs |
| Workspace | A sparse ext4 image sized to the environment's disk limit, loop mounted on the host and bind mounted at `/workspace`. This is how the disk limit is enforced. `HOME` is inside it, so what a run installs persists and counts against the limit |
| Daemon | `sandboxd` bind mounted read-only from the host and used as the entrypoint, so any image works |
| Runtime | Always named: `runsc` when the host has gVisor registered, `runc` otherwise |
| DNS | A mounted `/etc/resolv.conf`. Policies other than `full` get one with no usable name server |
| Network | A fixed address on the private sandbox network. No published ports |

The driver mounts the workspace on create and resume and unmounts it on stop and
delete. Each sandbox also has a record file on the host (address, policy, domains,
ports). Reconcile and a repeated bootstrap rebuild routes and proxy rules from the
records without asking BoW's database.

### Isolation

- gVisor is used when the host has it. BoW does not install it and does not edit
  Docker's configuration. `Host.strong_runtime` tells three cases apart: registered,
  installed but not registered (with the fix), and not installed.
- The isolation level is reported: stored in `driver_state`, returned as
  `info.isolation` (`gvisor` or `runc`) on the backend, and exposed as the
  `strong_isolation` capability. Bootstrap's last step checks that the kernel a test
  sandbox sees ends in `-gvisor`.
- An environment with `require_strong_isolation` only fits backends with that capability.
- The router, the egress proxy and the firewall helper always run under `runc`.

### Router

One Caddy container per host terminates TLS on one published port (default 8443).

- One route per sandbox, matched on the `X-Sandbox-Id` header. Content type
  `application/grpc*` goes to `sandboxd`'s gRPC port over h2c; everything else goes to
  its REST port. Responses are never buffered. An unknown id gets a 404.
- A branch for `X-Sandbox-Port` exists for declared ports. `ports` is empty in v1.
- Routes are changed through Caddy's admin API on a unix socket in a host directory.
  BoW calls it with `curl --unix-socket` inside its SSH session.
- Caddy's internal CA issues the certificate. Bootstrap reads the CA certificate and
  pins it in `driver_state`. The runtime client trusts only that CA.

### Bootstrap (`bootstrap.py`)

Nine steps, each stored on the backend as it finishes. Safe to run again. It stops at
the first failed step and leaves the backend disabled.

In order: (1) check Docker, a supported setup and server ownership; (2) detect gVisor,
a warning and not a failure when absent; (3) create the sandbox network; (4) place the
`sandboxd` binary, copied out of the tools image; (5) pull the router image and the
firewall helper image; (6) start the router and pin its
CA; (7) start the egress proxy; (8) apply the firewall rules; (9) verify with a
throwaway sandbox: run a command in it over TLS, confirm it cannot reach the host, the
internet or the metadata address, remove it.

Reported as unsupported: Podman, rootless Docker, a firewall backend other than
iptables, and a host where bridge traffic does not pass the packet filter. With
`images_preloaded` set on the backend, bootstrap checks that each image is present
instead of pulling it (air-gapped hosts).

**One server, one BoW instance.** Step 1 writes an owner marker with this deployment's
instance id. A server owned by another instance is refused. `take_over` removes the
other instance's containers and claims the server. Reconcile and cleanup only touch
resources with their own instance label.

## Network policy and egress

**Built** for `none`, `registries` and `full`. `allowlist` is implemented in the API and
the driver, but the UI does not offer it for new environments and it has no contract or
live test. It was planned for phase 2.

| Policy | Reachable | Proxy route | Direct egress |
|---|---|---|---|
| `none` (default) | Nothing outside the sandbox | None; the proxy's default answers 403 | Blocked |
| `registries` | The registry domain list from the Sandbox settings | Those domains only | Blocked |
| `allowlist` | The environment's `allowed_domains` | Those domains only | Blocked |
| `full` | The public internet | Everything public | Allowed for that sandbox's address |

- **Egress proxy.** A second container from the same Caddy image, using the
  `forwardproxy` plugin. One route per sandbox, matched on the sandbox's remote IP
  (`commands.egress_route`). No route means no internet. Private, link-local and
  loopback destinations are refused under every policy, including a domain that
  resolves to one.
- **Sandbox-to-sandbox.** The Docker network is created with inter-container traffic
  disabled (`enable_icc=false`). Docker drops it itself, even when the firewall helper
  is not running.
- **Firewall helper.** A container on the host network with `NET_ADMIN` and `NET_RAW`
  (`deploy/sandbox/firewall/apply.sh`). It applies host rules in two chains, jumped
  from Docker's `DOCKER-USER` chain and from `INPUT`, and re-asserts them every few
  seconds. It allows router-to-sandbox and sandbox-to-proxy. It drops sandbox traffic
  to the host, to private and link-local ranges (which covers the cloud metadata
  address), and to port 25. When the backend's optional "BoW address" is set, only
  that address may reach the router port.
- **DNS.** Sandboxes not on `full` resolve nothing themselves; the proxy resolves names.
- **Domain log.** The proxy writes a JSON access log and `DockerSSHDriver.egress_log`
  aggregates it per sandbox. Nothing calls it yet (see Deferred).

## Environments

**Built** in the API and the service (`environments.py`, `schemas/sandbox_schema.py`).
The UI form covers a subset.

| Setting | API | UI form |
|---|---|---|
| Name, description, image, enabled, default | Yes | Yes |
| CPU, memory, disk, run timeout | Yes | Yes |
| Network policy | Yes | `none`, `registries`, `full` |
| Strong isolation required | Yes | Yes |
| Who may use it (roles, groups, users) | Yes (`PUT .../access`) | Yes |
| Allowed domains, variables, secrets, registry credentials, setup script, backend labels | Yes | No |
| Ports, warm pool size | Stored, unused | No |

- **Choosing one** (`choose_environment`): the one the agent asked for, then the
  agent's default, the organization default, an environment flagged default, and
  finally the only one there is. Only environments the acting user may use count.
- **Built-in "Default".** Seeded once per organization on the first environments
  listing: Python 3.12 with common data packages (`deploy/sandbox/image/`), policy
  `none`. If an admin deletes it, it does not come back.
- **Secrets** go in and never come out; responses carry names only. Secrets together
  with `full` return a warning.
- **Images.** A reference must start with a letter or digit (checked in the schema and
  in the driver). Saving a new image pulls it in the background on matching
  bootstrapped backends and records the digest.
- **Setup script.** Runs once at creation, 15-minute limit. A failing script fails the
  sandbox.

## Permissions

**Built.** Code: `access.py`, `core/permissions_registry.py`.

| Permission | Kind | Allows |
|---|---|---|
| `use_agent_sandbox` | Organization, Code category | The tools are offered to this user's agent. Not in the default member set |
| `manage_sandboxes` | Organization, Settings category | Every `/api/sandbox/*` route. `manage_settings` gives nothing here |
| `use` on `sandbox_environment` | Resource grant to a user, group or role | Create and run sandboxes from that environment |

- The tools are offered only when the user holds `use_agent_sandbox`, may use at least
  one enabled environment, and an enabled, bootstrapped backend fits one of them
  (`SandboxAccess.tools_offered`). Each tool checks again when called.
- A sandbox belongs to its report and is shared. Each run is checked and recorded
  against the user who triggered it. A run with no user (a scheduled run) acts as the
  report owner.
- `manage_sandboxes` does not let its holder run code. Full admins hold everything.
- The service does not check access to the report. The completion route does that
  before a tool is called.

## Lifecycle and jobs

**Built.** States and transitions are in `service.py` (`TRANSITIONS`).

`provisioning → ready ⇄ busy`, `ready → idle → stopped → deleted`, `stopped → ready` on
the next run, and any state `→ failed` (recreated on the next run).

- Every state change goes through `_set_state` and writes a `sandbox_events` row.
- One run at a time. The sandbox is claimed with one conditional `UPDATE`, so it holds
  across workers. A second run gets `sandbox.busy`.
- A sandbox found stopped (a reboot, an admin) is recorded as stopped and resumed. One
  that is gone or does not answer is marked failed and recreated; the tool result says
  the filesystem is fresh.
- Quotas: running sandboxes per user, an optional organization total, and per-backend
  capacity. A stopped sandbox does not count.
- Placement (`choose_backend`): enabled, bootstrapped, not unreachable, labels match,
  capabilities cover the environment, free capacity; then least loaded.

Three jobs run on the scheduler leader, registered only when the flag is on (`jobs.py`):

| Job | Every | One pass |
|---|---|---|
| Lifecycle | 60 s | idle → stop → delete: mark idle after 5 minutes, stop at the idle timeout, delete after the stopped retention, at the maximum lifetime, or when the report is archived, soft-deleted or gone |
| Backend health | 2 min | Test every bootstrapped backend. On reachable ones, check each ready or idle sandbox: gone is marked failed, found stopped is marked stopped |
| Reconcile | 10 min | Ask each reachable backend's driver to match the database: remove orphans and stale routes, repair missing routes, report known sandboxes that are gone |

The lifecycle job also recovers from a dead worker: a sandbox `busy` longer than its run
timeout plus 15 minutes is released, and one `provisioning` for 30 minutes is marked
failed. A failed sandbox with no next run is deleted after 24 hours.

Defaults (`settings.py`, `schemas/sandbox_schema.py`, `service.py`): 1 CPU, 2 GB memory,
5 GB disk; run timeout 10 minutes, at most 60; stop after 30 idle minutes; delete after
7 days stopped; maximum lifetime 30 days; 3 running sandboxes per user; capacity 20 per
backend; 50 artifacts per run, 100 MB each, 500 MB in total.

## Tools and runs

**Built.** Code: `ai/tools/implementations/agent_sandbox.py`,
`ai/tools/schemas/agent_sandbox.py`. Chat mode only.

| Tool | Input | Output |
|---|---|---|
| `create_sandbox` | optional `environment` | id, state, environment, network policy, languages, limits. Returns the existing sandbox if there is one |
| `run_in_sandbox` | `language`, `code` and/or `command`, `files[]`, `packages[]`, `input_file_ids[]`, `timeout_seconds` | status, exit code, stdout and stderr tails, `artifacts[]`, skipped artifacts, notes |
| `sandbox_status` | none | state, environment, limits, uptime, expiry, recent runs |
| `delete_sandbox` | none | confirmation |

Run sequence (`SandboxService.run`):

1. Create the sandbox, or resume it if stopped.
2. Refuse `packages` when the policy is `none`, in a sentence the agent can act on.
3. Claim the sandbox and insert the `sandbox_runs` row.
4. Upload extra files, the code file, and input files to `/workspace/inputs`. Only
   files of the same report are accepted, and each is audited.
5. Install packages with the language's own tool. Names are arguments, never a shell
   string.
6. Snapshot `outputs/`, run the command, stream stdout and stderr to the report thread
   and append them to the run row in batches.
7. Save every new or changed file under `outputs/` as a report file with
   `source_kind="artifact"`, registered for the same turn.

- A failing program is a normal result, not a tool error. Interpreters run through
  `sh`, so a missing `node` or `go` is "not found, exit 127". The built-in image has
  Python only.
- The run timeout is the smaller of the request's and the environment's. The tool
  declares 70 minutes to the tool runner and is never retried automatically. A quiet
  run sends a heartbeat every 30 s. Each tool call opens its own database session.
- The model gets the last 20 KB of each stream. 2 MiB per stream is stored.
- Viewers without `view_code` do not see a call's `code`, `command` or `files`
  (`core/code_visibility.py`).
- When the tools are offered, `agent_v2._apply_agent_sandbox_filter` appends a note to
  the tool descriptions: the report's sandbox and what it can reach, or the
  environments the user may use.

## Runtime client

**Built.** Code: `runtime_client.py`; gRPC stubs in `sandboxd_proto/`.

- BoW talks to `sandboxd` inside the container through the Caddy router with TLS:
  processes over gRPC (`process.v1.ProcessService`), files over REST (`/v1/files`).
  Every request carries the routing header and the sandbox's bearer token.
- The TLS context is built from the endpoint's CA only, with no system roots. Proxy
  settings from the server's environment are ignored.
- `sandboxd`'s streamed `Start` has no time limit. The client enforces timeout and
  cancel: `SIGTERM`, 5 seconds, `SIGKILL`.
- **Lost gRPC status.** When `sandboxd` refuses a call at once (wrong token, a command
  that cannot start) it answers with a trailers-only response. Through Caddy that
  status is sometimes lost and gRPC reports `UNKNOWN: Stream removed`. The client then
  falls back to REST `GET /v1/metadata`, which answers the same token check with a
  plain HTTP status, and reports the real reason (`_explain_lost_status`).
- **Liveness watchdog.** During a run the client asks the health endpoint every 10 s
  and gives up after two misses, because a sandbox killed outright does not always
  close its stream. Under gVisor, running out of memory kills the whole sandbox, not
  only the process. The service then asks the backend: a sandbox that only stopped is
  marked stopped ("stopped during a run") and resumes on the next run with its files.
  The agent is told it most likely ran out of memory.

## Audit

**Built.** Every entry goes through the existing audit service (`audit.py`) with the
report, backend or environment name in `details.title`. A failed audit write is logged
and never fails the operation.

- Sandboxes: `sandbox.created`, `.stopped`, `.resumed`, `.deleted`, `.run`, `.input_file`.
- Backends: `sandbox_backend.created`, `.updated`, `.enabled`, `.disabled`,
  `.identity_confirmed`, `.bootstrapped`, `.taken_over`, `.deleted`.
- Environments: `sandbox_environment.created`, `.updated`, `.image_changed`,
  `.secrets_changed`, `.access_changed`, `.image_pulled`, `.deleted`.
- Settings: `sandbox_settings.updated`, with old and new values.

Run entries carry the code's SHA-256, exit code, duration and file names, never the
code. The audit service redacts keys that look like secrets, so changed secret names
are sent as `protected_variable_names`.

## Admin API and UI

**API: built** for backends, environments and settings (`backend/app/routes/sandbox.py`).
**Not built:** the fleet routes (`/sandboxes`, stats, stop, kill-run, extend).

| Route under `/api/sandbox` | Purpose |
|---|---|
| `GET /status`, `GET`/`PUT /settings` | The gate levels; the Sandbox settings |
| `GET /backend-types` | Form schemas and capabilities per type |
| `/backends` (list, create), `/backends/{id}` (read, edit, delete) | Delete takes `cleanup` and `delete_sandboxes` |
| `POST /backends/{id}/test` | Test connection. Changes nothing on the server |
| `POST /backends/{id}/confirm-identity` | Pin the host key the admin confirmed |
| `POST /backends/{id}/bootstrap` | Returns at once; progress is read from the backend. Accepts `take_over` |
| `/environments` (list, create), `/environments/{id}` (read, edit, delete) | Environments |
| `PUT /environments/{id}/access`, `POST /environments/{id}/pull` | Grants; pull the image now |

A backend is created disabled and cannot be enabled until bootstrap has succeeded.
Disabling drains it. Delete is refused while it has sandboxes unless `delete_sandboxes`
is passed. Responses never include private keys, passwords, secret values or tokens.

**UI: in progress.** One page, `frontend/pages/settings/sandbox.vue`, with three tabs
selected by `?tab=`: Backends, Environments, Settings. Components are in
`frontend/components/sandbox/`; calls go through `useSandboxApi.ts`. Present in the
files: a backend form rendered from the type's schemas, a test-connection dialog with
fingerprint confirmation, a bootstrap dialog that polls step results and offers take
over, a delete dialog with clean-up, the environment list and form with access
pickers, and the settings form.

## Testing and verification

| Suite | Where | Runs against |
|---|---|---|
| Gate levels, flag off | `backend/tests/e2e/sandbox/test_feature_gate.py`, `tests/unit/test_agent_sandbox_imports.py` | The app |
| Driver contract (14 tests) | `backend/tests/contracts/sandbox_driver/` | The fake driver in CI; a real host when `BOW_SANDBOX_TEST_HOST` is set |
| Driver-only live tests | `tests/contracts/sandbox_driver/test_docker_ssh_host_live.py` | A real host |
| Command templates, SSH layer | `tests/unit/test_agent_sandbox_docker_ssh_*.py` | No server; an in-process SSH server |
| Runtime client | `tests/unit/test_agent_sandbox_runtime_client.py` | The stand-in `services/sandbox/testing/sandboxd_stub.py` |
| Service, permissions, tools, jobs, APIs, schema | `tests/e2e/sandbox/` | The fake driver |
| Layering | `tests/unit/test_agent_sandbox_layering.py` | The source tree |

**Spike.** `docs/feedback-loops/2026-11-26-agent-sandbox-spike/README.md`. Measured
there: create 0.6 s, stop 2.3 s, resume 0.4 s; a 100 MB file about 1 s each way; under
gVisor, starting processes about 12 times slower and CPU-bound work about 7% slower.

**Live end-to-end run.** `tools/agent/agent_sandbox_live.py` drives a running BoW
through its HTTP API against one real host. It passed 22 of 22 checks on 2026-11-26
against an Ubuntu host with gVisor. It covers: add, confirm, test, bootstrap; a run
that installs a package and writes a PNG that becomes a report file; a `none` sandbox
reaching nothing; idle stop and resume with packages intact; the health and reconcile
jobs. No working model key was available, so a scripted stand-in model
(`tools/agent/stub_llm_sandbox.py`) chose the tool calls. Everything after the tool
call is the real product. A run with a real model has not been done.

**Not tested.**

- Debian and RHEL-family hosts. Only Ubuntu was available.
- Logging in as root. Only a non-root user through `sudo -n` was run live. The root
  mode shares every command template and is covered against the in-process SSH server.
- Docker with iptables management turned off, and the `allowlist` policy, on a real
  server.
- The image-build workflow and arm64 builds.

## Deferred

Phase 2 and later in BOW-229. Not built unless noted.

- **MCP server tools.** No sandbox tool is in `ai/tools/mcp/`.
- **Fleet view and per-sandbox page**: live stats, timeline, run history, live output,
  and the stop, kill-run, extend and delete actions. `SandboxDriver.stats` and the
  response schemas exist; no route or page uses them.
- **Domain log.** Nothing ships `egress_log` to `sandbox_egress_log`; there is no UI.
- **Custom allowlists, secrets, registry credentials, setup scripts, backend labels.**
  The API, service and driver handle them. The environment form does not offer them.
- **Node and Go in the built-in image.**
- **Phase 3:** a report tool block and status chip (the generic tool block shows the
  calls today), a warm pool per environment (`warm_pool_size` is reserved), user docs.
- **Later:** hosted apps (`ports`, `sandbox_exposures` and `apps_domain` are reserved),
  a `kubernetes` driver, snapshots, detached long runs, an interactive terminal,
  promoting an output CSV to a report query.

## Open questions

1. **Agent default environment.** `sandbox_agent_defaults` is read when an environment
   is chosen, and `environments.set_agent_default` writes it, but no route or page
   calls that function. The issue says an agent can have a default and assigns it to
   no phase.
2. **Anti-spoofing rule.** The issue's notes ask for a firewall rule that stops a
   sandbox using another sandbox's address, on top of dropped capabilities. `apply.sh`
   has no such rule. Why is not recorded.
3. **`sandboxd` limit flags.** The issue's notes list `--execute-max-time-limit` and
   output limit flags. `commands.sandbox_run` passes only the ports, the root
   directory and the token hash; the client enforces the limits.
4. **Unreachable backends and tool visibility.** `choose_backend` skips a backend whose
   status is `unreachable`; `tools_offered` does not. With only an unreachable backend
   the tools are offered and the run answers `sandbox.no_backend`. This is kept on
   purpose for now: an outage is temporary, and an agent that has the tools can tell
   the user the server is down, where an agent without them can only say it cannot
   run code. Whether to hide the tools after a longer outage is undecided.
5. **Images by digest.** The issue says an image is pinned by digest when saved. The
   digest is recorded after the background pull. A locally built or preloaded image
   may have none, and the sandbox is then created from the tag.

## Implementation notes

What shipped differently from BOW-229, and why.

- **Egress proxy is Caddy with the `forwardproxy` plugin**, not smokescreen. One image
  serves as router and proxy, in two containers. Decided by the spike.
- **Sandbox-to-sandbox blocking is Docker's**, not a BoW firewall rule, so it holds
  when the helper is down.
- **The workspace is a loop-mounted ext4 image**, not a named volume. The plan had left
  the disk limit undecided; the image's size is the limit.
- **Read-only root, tmpfs `/tmp`, `HOME` in the workspace.** The plan had a writable
  container filesystem.
- **DNS is a mounted `resolv.conf`**, not `--dns` to a dead address. Docker's built-in
  DNS resolved public names under `runc` and did not work under gVisor.
- **The firewall helper uses `--restart always` and `docker exec`.** A helper signalled
  with `docker kill` did not come back after a reboot.
- **Host key confirmation is an explicit step** with its own route. The plan had it
  inside test connection.
- **No key-pair endpoint and no cleanup endpoint.** A pair is generated when a backend
  is created without a key. Clean-up is a flag on delete.
- **Per-sandbox record files on the host.** Not in the plan.
- **Lost gRPC status fallback, liveness watchdog, image reference validation.** All
  three were found on the real host.
- **`grpc` is not part of the lazy-import check.** `grpcio` was already imported at
  startup by an existing dependency. The check covers `asyncssh` and the driver.
- **Three existing registry tests changed.** They assert the exact set of valid
  permission strings, and the two new permissions must stay valid with the flag off.
- **The sandbox router is always mounted**; the gate is a router dependency. The plan
  also allowed including the router only when the flag is on.
- **Settings are a plain dict in organization settings**, not `FeatureConfig` entries.
- **No foreign keys to existing tables**, so the migration stays additive.
- **Setup scripts, secrets, registry credentials, labels and the allowlist policy work
  in the backend already**, ahead of their phase 2 UI.
- **Reconcile is told about sandboxes still being created**, so one is never taken for
  an orphan. Dead-worker recovery and the 24-hour failed retention are also additions.
- **A report counts as deleted when it is archived**, which is what "delete" does in
  the app.
- **One page with three tabs**, not a `pages/settings/sandbox/` directory with four.
  The fourth tab (Sandboxes) is phase 2.
- **Artifacts are read into memory one at a time** before saving, up to 100 MB each.

Not finished:

- **The image-build workflow is committed inert** at
  `deploy/sandbox/sandbox-images.workflow.yml`, because the push token lacked the
  `workflow` scope. Someone with permission has to move it to
  `.github/workflows/sandbox-images.yml` and run it once.
- **No image is published.** Until the workflow has run, the default image names do not
  exist in a registry. Set `BOW_SANDBOX_IMAGE`, `BOW_SANDBOX_CADDY_IMAGE`,
  `BOW_SANDBOX_FIREWALL_IMAGE` and `BOW_SANDBOX_TOOLS_IMAGE`, or preload the images
  (`deploy/sandbox/README.md`, which also lists the pinned versions).

### Concurrency rules added in review

These came out of the first code review and are now covered by tests in
`tests/e2e/sandbox/`.

- **Only a sandbox at rest is touched.** `_ensure_running` refuses with
  `sandbox.busy` when the sandbox is `provisioning` or `busy`. It never
  health-checks, and so never recreates, a sandbox another call is creating or
  running in.
- **Every state change made on a stale read is conditional.** The run claim, the
  idle marking (`mark_idle`), the stop (`stop` claims the sandbox as `busy` while
  the container is being stopped) and the abandoned-run recovery are
  `UPDATE ... WHERE state = ...` and are skipped when no row changes.
- **A cancelled run gives the sandbox back.** `run` catches `CancelledError`,
  marks the run `cancelled` and the sandbox `ready` in a fresh, shielded
  session, then re-raises.
- **A sandbox is marked deleted only when its container is gone** or its backend
  is. A backend that exists but cannot be reached refuses the delete, so it can
  be retried.
- **A backend pointed at another address starts over**: unpinned, not
  bootstrapped, disabled. The driver decides what counts as "another"
  (`is_another_backend`). It is refused while the backend has sandboxes.
- **Bootstrap is claimed atomically** (`claim_bootstrap`), so two requests
  cannot both start one.
- **Deleting an environment** removes its grants and is refused while sandboxes
  created from it exist, unless they are deleted with it.
- **Changed outputs.** A file also counts as changed when its whole-second
  timestamp is not before the moment the run began, by the sandbox's own clock.

### Bootstrap does not depend on environments

Changed after review of the first real setup. Bootstrap prepares the backend
and nothing else; it is done once per server, whatever environments exist.
The issue had it pre-pull every environment's image, which made one
environment with an image that could not be fetched fail the whole backend.

Environment images are now fetched in three places, none of which can fail a
bootstrap:

- right after a bootstrap succeeds, in the background
  (`backends.prefetch_environment_images`), with each environment's registry
  credentials. Failures are logged and audited as
  `sandbox_backend.images_fetched`;
- when an environment is saved with a new image
  (`environments.pull_image_on_backends`);
- when a sandbox is created and the server does not have the image yet.

Not built: a place in the UI that shows an environment whose image could not
be fetched. Today that is visible only in the audit log and when a first run
fails.
