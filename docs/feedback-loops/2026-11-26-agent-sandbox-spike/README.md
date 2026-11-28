# Feedback Loop — Agent sandboxes, phase 0 spike (BOW-234)

Throwaway work on one Docker host, run before phase 1 of
[BOW-229 "Sandboxes for BoW agents"](https://linear.app/bow1/issue/BOW-229/sandboxes-for-bow-agents)
depends on it. It answers the eight questions in BOW-234 by running each
piece for real. **Nothing in `scripts/` ships**; the driver in phase 1.7 is
written fresh from these findings.

Run on 26 Nov 2026 from a DevBox against the dedicated test VM described in
BOW-234 (addresses are replaced by `SANDBOX_HOST` and `BOW_HOST` here).

## Verdict

| # | Question | Answer |
|---|---|---|
| 1 | `sandboxd` from a pinned commit, mounted read-only, in arbitrary images | **Yes.** Debian (`python:3.12-slim`) and Alpine (`node:22-alpine`, `golang:1.27-alpine`) |
| 2 | Process over gRPC through Caddy: live output, >10 minutes, cancel | **Yes.** 11-minute streamed run and 11-minute silent run both completed |
| 3 | Files through Caddy: 100 MB each way, recursive listing | **Yes.** About 1 s each way, hashes match |
| 4 | Caddy admin API on a local socket, routes survive restart, CA pinning, connect by IP | **Yes** |
| 5 | Egress proxy choice | **Caddy with the `forwardproxy` plugin.** Per-sandbox policy, live reload, domain log, `pip`/`npm`/`go get` with no DNS all work |
| 6 | Firewall helper, reloads, reboot, sandbox-to-sandbox | **Yes on Ubuntu through `sudo -n`**, including ufw and firewalld reloads and a reboot. **Not run:** a Debian host, a RHEL-family host, and a root login (one test VM). See "Not covered" |
| 7 | gVisor | **Yes.** Detected, workloads run, memory limit kills cleanly, overhead measured |
| 8 | Stop and start keep workspace and packages | **Yes.** Stop 2.3 s, resume 0.4 s, create 0.6 s |

No question came back "no". Six points changed the design; they are listed
under "Design changes" and written back to BOW-229.

## Pinned versions

| Component | Version |
|---|---|
| `sandboxd` | `kubernetes-sigs/agent-sandbox` commit `b0040f5e51a4b98341af899e3ce9da767e63216f` (25 Nov 2026), built with Go 1.27.4, `CGO_ENABLED=0`; binary SHA-256 `7ac687d6b73927255dabf1e8d77911b4b838d4449c1fae685d0dbcf0d424bcb9` |
| Caddy | v2.11.9 (`caddy:2.11`), used for both the TLS router and the egress proxy |
| Egress proxy | `github.com/caddyserver/forwardproxy` `v0.0.0-20260321230143-0aab84dad4fc`, compiled into Caddy with `xcaddy` (`scripts/caddy.Dockerfile`) |
| Firewall helper | `alpine:3.22` with `iptables` 1.8.11 (nf_tables and legacy) |
| Host | Ubuntu 26.04, kernel 7.0.0-1018-aws, Docker CE 29.10.1 (firewall backend `iptables`, storage driver `overlayfs`), `runsc release-20261123.1` |
| Client | Python 3.13, `grpcio` 1.85.4, `httpx` 0.28.1 |

## What was run

Everything on the host went through `sudo -n` as the non-root `ubuntu` user.

```
DevBox ── SSH ──► host:  docker run/stop/start/rm, curl --unix-socket (Caddy admin)
DevBox ── HTTPS :8443 ──► bow-sbx-caddy ──► bow-sbx-<id> (sandboxd :9090 gRPC, :8080 REST)
                          bow-sbx-<id> ──► bow-sbx-egress :3128 ──► internet
                          bow-sbx-firewall (host network) asserts the iptables chains
```

| Script | Role |
|---|---|
| `scripts/host.sh` | network, sandbox create / stop / start / destroy |
| `scripts/caddy-host.sh`, `caddy-base.json` | TLS router: start, add and remove a route, read the CA |
| `scripts/egress-host.sh`, `egress-base.json` | egress proxy: start, set a sandbox's policy, read the domain log |
| `scripts/fw/`, `scripts/fw-host.sh` | firewall helper image and its configuration |
| `scripts/client.py` | Python client: gRPC process calls and REST file calls through Caddy, trusting only the pinned CA |
| `scripts/probe*.py`, `probe.js` | reachability matrix run inside a sandbox |

## Findings

### 1. sandboxd in arbitrary images

The binary is static (40 MB). With `--entrypoint /usr/local/bin/sandboxd`
and the host file mounted read-only it started in every image tried, as
uid 1000 with all capabilities dropped, under both `runc` and `runsc`.
`GET /v1/health` needs no token; everything else answers 401 (REST) or
`UNAUTHENTICATED` (gRPC) without the sandbox's token. A token hash that is
not valid hex makes `sandboxd` exit at once, so create must wait for health
with a deadline rather than assume the container stays up.

### 2 and 3. gRPC and files through Caddy

Route per sandbox, matched on `X-Sandbox-Id`: gRPC (`Content-Type:
application/grpc*`) to `h2c://<ip>:9090`, everything else to `<ip>:8080`,
both with `flush_interval -1`. Upstreams are dialled by IP, not by name.

* Output arrives live (one event per second for a one-line-per-second script).
* `SendSignal(SIGTERM)` ends the run; exit code is the script's own.
* An 11-minute run printing every 30 s and an 11-minute run printing nothing
  both completed: no idle timeout in Caddy or gRPC cuts a quiet stream.
* Adding and removing other routes during a run does not interrupt it.
* 100 MB upload 0.7 to 1.7 s, download 0.8 to 0.9 s, SHA-256 equal on both sides.
* `GET /v1/files/outputs?recursive=true` returns a flat list with paths
  relative to the listed directory, sizes, types and modification times.

### 4. Caddy admin API

Admin API on `unix//run/caddy-admin/admin.sock`, the directory mounted from
the host; the socket is root-only. `PUT .../routes/0` adds a route ahead of
the 404 fallback, `DELETE /id/sbx-<id>` removes it. With `caddy run --resume`
and `/config` on the host, routes survive `docker restart` and a host reboot.
`GET /pki/ca/local` returns the root certificate, unchanged across restarts
and reboots (`/data` is on the host). With the host's address as the site
name and `default_sni`, Python connects by IP and verifies against that CA
alone; without the CA the handshake fails.

The Caddy binary carries a file capability, so with `--cap-drop ALL` the
container must add `NET_BIND_SERVICE` back or it cannot exec.

### 5. Egress proxy: Caddy forward proxy

Chosen over smokescreen: stock smokescreen identifies clients by TLS
client certificate, so identifying a sandbox by source IP needs a custom Go
build either way, and Caddy is already on every host with an admin API.

* One route per sandbox, matched on `remote_ip`, holding a `forward_proxy`
  handler with that sandbox's ACL. No route means 403: that is policy "none".
* ACL order: deny private, link-local and loopback ranges; allow the
  policy's domains (or everything, for "full"); deny the rest.
* Changing a route through the admin API takes effect at once and does not
  break tunnels in flight (five reloads during a 100 MB download).
* A hostname that resolves to a private address is refused (403), so a
  domain cannot be used to reach the host or the cloud metadata address.
* The access log has client IP, method, host and status for every request,
  allowed or refused. It is read with `docker logs --since`.
* With no DNS in the container, `pip install`, `npm install` and `go get`
  work through `HTTP(S)_PROXY`; a domain off the list fails with 403.
* BusyBox `wget` cannot fetch `https://` through a plain HTTP proxy (it
  sends a `GET https://…` the proxy answers with 502, for allowed domains
  too). `curl`, Python, Node and Go use `CONNECT` and work.

### 6. Firewall

* **Sandbox-to-sandbox.** `br_netfilter` was not loaded. Creating the
  sandbox network with `com.docker.network.bridge.enable_icc=false` makes
  Docker load it, set `bridge-nf-call-iptables=1` and add its own
  `-i bow-sbx0 -o bow-sbx0 -j DROP`. BoW's chain then only has to *accept*
  Caddy-to-sandbox and sandbox-to-proxy. If the helper is not running,
  sandboxes still cannot reach each other.
* **Chains.** `BOW-SBX-FWD` jumped from `DOCKER-USER`, `BOW-SBX-IN` jumped
  from `INPUT` for the bridge interface, both written with
  `iptables-restore --noflush` and re-pinned to position 1.
* **Matrix from inside a sandbox** (policies none and registries): other
  sandbox, Caddy, host by bridge address, host by its own address, another
  VPC host, the metadata address and the public internet all time out; only
  the proxy port connects. With policy "full" the public internet also
  connects; the private targets still time out.
* **Reloads.** After `ufw enable` + `ufw reload`, and after installing
  firewalld (nftables backend) + `firewall-cmd --reload`, the jumps were
  still first and the matrix unchanged. After flushing BoW's chains by hand
  the helper restored them on its next pass.
* **Reboot.** Caddy, the proxy and the helper came back and the rules were
  applied within seconds of Docker starting. Sandboxes have no restart
  policy, so they are stopped after a reboot and resume normally.
* **Restart policy trap.** Signalling the helper with `docker kill -s HUP`
  makes Docker treat it as manually stopped, and it did not come back after
  the reboot. Reload with `docker exec` and run it with `--restart always`.
* **BoW address.** With it set, the Caddy port answers only that source.
* The helper needs `NET_ADMIN` and `NET_RAW` on the host network and picks
  the iptables variant in which `DOCKER-USER` exists (`iptables-nft` here).

### 7. gVisor

* Detection: `docker info` lists `runsc`; a sandbox reports kernel
  `4.19.0-gvisor`.
* `pip install --user`, `npm install`, `go get` + `go run` all work.
* An unbounded allocation is killed with exit code 137 at a 64 MB and a
  256 MB limit, under both runtimes. The `runc` failure noted in BOW-234
  (exit 125 at start) did not reproduce.
* Docker's embedded DNS (`127.0.0.11`) does not answer under gVisor.
* Overhead on this VM, `runsc` against `runc`: Python CPU loop 1.10 s vs
  1.03 s; SHA-256 of 200 MB equal; 4,000 small-file operations 1.04 s vs
  0.40 s; 200 process spawns 2.42 s vs 0.20 s; Node CPU loop equal. Start
  time 0.64 s vs 0.56 s. Idle memory about 80 MB per sandbox.

### 8. Stop and resume

`docker stop` 2.3 s, `docker start` 0.4 s and healthy at once, same IP and
token. Installed packages and files were still there, also after a host
reboot (resume 0.7 s including remounting the workspace).

## Design changes

1. **Egress proxy is Caddy with `forwardproxy`.** One BoW Caddy image
   serves as router and as proxy, in two containers.
2. **The sandbox network has inter-container traffic disabled in Docker.**
   This is what loads `br_netfilter`, and it fails closed.
3. **Workspace disk limit: a size-limited ext4 image per sandbox**, loop
   mounted on the host and bind mounted at `/workspace`. Docker's
   `overlayfs` driver and plain volumes cannot cap size, and the `local`
   volume driver cannot loop-mount. The driver mounts on create and resume
   and unmounts on stop and delete. A 1 GiB workspace refuses writes at
   957 MiB.
4. **Root filesystem read-only, `/tmp` a 256 MB tmpfs with `exec`,
   `HOME=/workspace/.home`.** Everything a run installs lands in the
   workspace: it persists across stop and resume and counts against the
   disk limit. Without `exec` on `/tmp`, `go run` fails.
5. **DNS through a mounted `/etc/resolv.conf`, not `--dns`.** Under `runc`
   Docker's embedded DNS resolves public names through the host for any
   container, which is the leak the design wants closed; under gVisor it
   does not work at all. Policies none, registries and allowlist mount a
   file with no usable nameserver; "full" mounts one with real resolvers.
6. **Firewall helper: `--restart always`, reloaded with `docker exec`.**

## Not covered

* **Debian and RHEL-family hosts, and logging in as root.** There is one
  test VM (Ubuntu, non-root with sudo). firewalld was installed on it to
  exercise the reload path. The helper uses only `iptables` from its own
  image and Docker's `DOCKER-USER` chain, so nothing in it is specific to
  Ubuntu, but this is reasoning, not a test. SELinux on RHEL-family hosts
  may need `:z` on the bind mounts.
* **Source address spoofing between sandboxes.** Capabilities are dropped,
  so a sandbox cannot craft packets; no explicit anti-spoof rule was tried.
* **A custom domain allowlist with wildcards** (phase 2).
* The test VM's disk is still 19 GB (BOW-234 asks for about 40 GB).

## Reproduce

```bash
# on the host, as a sudo-capable user
git clone https://github.com/kubernetes-sigs/agent-sandbox && cd agent-sandbox
git checkout b0040f5e51a4b98341af899e3ce9da767e63216f
sudo docker build --target builder -f packages/sandboxd/Dockerfile -t sandboxd-builder .
# copy /sandboxd out of that image to /var/lib/bow-sandbox/bin/sandboxd
sudo docker build -t bow-spike-caddy-fp -f caddy.Dockerfile .
sudo docker build -t bow-spike-fw fw/
./host.sh net && ./caddy-host.sh up && ./egress-host.sh up
./fw-host.sh conf - && ./fw-host.sh up
SHA=$(printf %s tok-a | sha256sum | cut -d' ' -f1)
./host.sh create a 10.213.77.10 python:3.12-slim runsc $SHA
./caddy-host.sh route a 10.213.77.10 && ./caddy-host.sh ca > ca.pem

# on the BoW side
python -m grpc_tools.protoc -Iproto --python_out=gen --grpc_python_out=gen proto/process.proto
SANDBOX_HOST=<address> python client.py a tok-a live
```
