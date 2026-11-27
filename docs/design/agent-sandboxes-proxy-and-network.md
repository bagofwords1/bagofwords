# Agent sandboxes — corporate proxies, internal hosts and custom CAs

Status: **not built, parked.** Raised in the design review of PR #1941 (point 10 of the
"Design feedback" comment) and set aside on 27 Nov 2026 so the rest of the review could
move. This file is the note to come back to. Nothing here is decided.
Scope: what a sandbox can reach, and how, on networks that are not "a server with
direct internet access".
Related: `docs/design/agent-sandboxes.md` (sections "Network policy and egress" and
"Deferred").

## Why this matters

Phase 1 works on a server that reaches the internet directly. Many enterprise
networks do not look like that:

- outbound traffic must go through the company's own proxy;
- packages come from an internal mirror (Artifactory, Nexus) on a private address;
- the proxy inspects TLS, so every client must trust the company's CA.

On such a network the `registries`, `allowlist` and `full` policies are all broken
today, and "packages only from our mirror" cannot be configured at all. The reviewer
listed items 1 to 3 below as ship blockers. Our position at the time of parking: they
block customers behind a proxy, not the feature itself, which is off by default. That
disagreement is open and is the product owner's to settle before release.

## How it works today

| What | Where | Behaviour |
|---|---|---|
| Proxy variables in the sandbox | `drivers/docker_ssh/commands.py`, `sandbox_environment` | `HTTP(S)_PROXY` point at the host's egress proxy. They are set **after** the environment's own variables, so an admin cannot override them |
| `NO_PROXY` | same function | Hard-coded to `localhost,127.0.0.1` |
| Private addresses | `commands.py`, `PROXY_DENIED_RANGES` and `egress_route` | Private, link-local, loopback and carrier-grade NAT ranges are refused under **every** policy, first in the access list |
| Egress proxy | Caddy with the `forwardproxy` plugin, one route per sandbox | Connects straight to the destination. No upstream proxy |
| DNS | none inside the sandbox on restricted policies | The proxy resolves names |
| Protocols | proxy only | Only proxy-aware HTTP and HTTPS work, under every policy including `full` |
| Trust store | the image's own | Nothing is mounted or injected |
| Image pulls | `drivers/docker_ssh/host.py`, `pull` | Uses the server's Docker daemon as it is configured. Bootstrap neither sets nor checks a daemon proxy |
| Reaching the server | `drivers/docker_ssh/ssh.py` | Direct SSH. No jump host |

## The six items

### 1. Upstream corporate proxy

**Need:** the egress proxy must forward through the customer's proxy.
**Sketch:** the `forwardproxy` plugin has an `upstream` setting. Add a setting on the
backend (the proxy URL), with its username and password stored as a credential, never
in config. Apply it when the egress proxy's config is written at bootstrap.
**Open questions:**
- Per backend only, or also overridable per environment?
- Does `upstream` in the pinned plugin version support authenticated HTTPS upstreams?
  Check before designing around it.
- With an upstream proxy, our "refuse private ranges" rule sees only the proxy's
  address. The domain list still applies; the private-range rule no longer protects
  anything on the far side of the upstream proxy. Say so in the UI.

### 2. Internal hosts (the risky one)

**Need:** allow a sandbox to reach named internal services that resolve to private
addresses: a package mirror, a Git server, an internal API.
**Why it is risky:** the design promises that a sandbox can never reach the internal
network. This item breaks that promise on purpose. Report data enters a sandbox, so
anything a sandbox can reach is a place data can go, and a place model-written code can
attack from.
**Decisions needed before any code:**
- **Granularity.** Per environment (like the allowlist), per backend, or both? An
  environment-level list keeps "who may reach what" next to "who may use it".
- **By name, by address range, or both?** A name is what admins think in. But a name
  that is allowed today can resolve to a different private address tomorrow (DNS
  rebinding, or simply a changed record). Options: resolve once and pin; allow only
  when the name resolves inside a declared range; or require both a name and a range.
- **Who may set it.** `manage_sandboxes` is enough for everything else. Reaching into
  the internal network may deserve its own permission, or at least its own audit event
  and a visible warning on the environment.
- **Ports.** Any port on an allowed host, or a declared list?
- **Interaction with `none`.** Does "no internet, internal mirror only" exist as a
  policy? It is probably the most asked-for combination.
**Never allow, whatever the list says:** the cloud metadata address, loopback, the
sandbox network itself, and the host's own addresses.

### 3. Custom CA bundle

**Need:** TLS-inspecting proxies re-sign every certificate. Without the company's CA,
`pip`, `npm` and `requests` all fail.
**Sketch:** the admin uploads a CA bundle (per backend, since it describes that
network). The driver writes it on the host and mounts it read-only into every sandbox,
and sets `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `PIP_CERT`, `NODE_EXTRA_CA_CERTS`
(and `GIT_SSL_CAINFO`, `CURL_CA_BUNDLE`).
**Open questions:**
- Replace the image's bundle, or append to it? Appending needs the image's bundle,
  whose path differs between Debian and Alpine. Mounting a combined bundle that BoW
  builds from a known public set plus the customer's CA avoids reading the image.
- Go reads the system pool, not an environment variable, on most setups.
  `SSL_CERT_FILE` is honoured by Go on Linux; verify.
- A CA is not a secret, but it is security-relevant: audit changes to it.

### 4. `NO_PROXY` configurable

**Need:** traffic to allowed internal hosts must not be sent to a proxy that cannot
reach them, depending on the topology.
**Sketch:** compute it from the internal-hosts list (item 2) and let the admin add
entries. Small, but pointless before item 2: with every private address refused there
is nothing useful to put in it. That is why it was not done on its own.

### 5. Say that only proxy-aware HTTP and HTTPS work

**Need:** under `full`, people will expect `git clone git@...`, a database driver or a
raw socket to work. They do not. That can stay a deliberate choice; it must be stated.
**Where:** the network policy hints in the environment form (`sandbox.network.*` in the
locale catalogs) and the agent-facing descriptions in
`backend/app/services/sandbox/prompt.py` (`NETWORK_EXPLANATIONS`), so the agent stops
trying things that cannot work.
**Size:** a few strings. This one is safe to do at any time, with or without the rest.

### 6. Host level

- **Docker daemon proxy.** Behind a proxy, image pulls at bootstrap fail with a bare
  Docker error. Bootstrap should at least detect it (a pull that fails with a network
  error while the server cannot reach the registry directly) and say what to configure.
  Setting the daemon's proxy for the operator is a bigger step and changes host
  configuration outside BoW's own directory.
- **SSH jump host.** Some servers are only reachable through a bastion. `asyncssh`
  supports a tunnel connection; it needs its own host key pinning and its own
  credential.

## Suggested order when this is picked up

1. Item 5. No design needed.
2. A short written decision on item 2 (the questions above), agreed before code.
3. Items 2 and 4 together.
4. Items 1 and 3 together: a customer behind a proxy usually needs both.
5. Item 6.

## What is needed to test it

None of this could be verified on the phase 1 test server, which has direct internet
access. Before building, set up:

- a forward proxy the server must use (Squid is enough), with and without
  authentication;
- the same proxy doing TLS inspection with its own CA;
- an internal package mirror on a private address (a `devpi` or Verdaccio container on
  a second private network is enough);
- a server with no direct route to the internet, so that a missed code path fails
  instead of silently working.

The live script `tools/agent/agent_sandbox_live.py` is the place to add a run against
that setup.

## Source

The review text, for reference (PR #1941, "Design feedback", point 10):

> `sandbox_environment()` hard-codes `HTTP(S)_PROXY` to the egress Caddy and
> `NO_PROXY=localhost,127.0.0.1`, after the environment's own variables, so an admin
> can't override them. The egress route refuses private, link-local and loopback ranges
> under every policy.
>
> - No upstream corporate proxy.
> - Internal hosts are always blocked.
> - No custom CA.
> - `NO_PROXY` isn't configurable.
> - Only proxy-aware HTTP/S works, even under `full`.
> - Host level: bootstrap doesn't set or check the Docker daemon's proxy; there's no
>   SSH jump host or bastion option.
