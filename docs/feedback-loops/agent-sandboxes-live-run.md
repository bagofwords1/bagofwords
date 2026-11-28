# Feedback Loop — Agent sandboxes, live end-to-end run (BOW-246)

The phase 1 definition of done of
[BOW-229 "Sandboxes for BoW agents"](https://linear.app/bow1/issue/BOW-229/sandboxes-for-bow-agents),
run for real: a running BoW with `BOW_AGENT_SANDBOXES_ENABLED` on, driven through
its HTTP API, against one real Docker host over SSH.

Run on 26 Nov 2026 from a DevBox against the dedicated test VM described in
BOW-234 (Ubuntu 26.04, Docker CE 29, gVisor, login through `sudo -n`). The
address, user, key and fingerprint are passed in environment variables and are
not recorded here.

## Verdict

**22 of 22 checks passed.** Every "Done when" line of BOW-246 is shown working,
with one limit on the second line: the model was a scripted stand-in (see
"What is not real").

| Done when | Result |
|---|---|
| An admin adds a server, bootstraps it, creates an environment and grants permission | **Pass.** Through the admin API. Bootstrap took 18 s with images already on the host |
| A user's agent installs a package, runs Python and writes a PNG that becomes a report file | **Pass**, through the agent loop. The tool call was chosen by a stand-in model |
| A sandbox with no internet cannot reach the internet | **Pass.** TCP to the internet, the metadata address and the host all time out; no name resolves; a package install is refused before it starts |
| The sandbox stops when idle and resumes on the next run with its packages | **Pass.** The container is stopped, not removed; the next run resumes the same sandbox and imports the package without installing |
| The flag-off suite and the driver contract suite pass | **Pass.** See "Suites" |

## How to run it

```bash
cd backend
source ../tools/agent/devbox_env.sh        # the admin's login; never printed
BOW_SANDBOX_TEST_HOST=<address> BOW_SANDBOX_TEST_USER=<user> \
BOW_SANDBOX_TEST_KEY=<path to the private key> \
BOW_SANDBOX_TEST_FINGERPRINT=<the server's host key fingerprint> \
uv run --no-sync python ../tools/agent/agent_sandbox_live.py
```

The backend it talks to must run with `BOW_AGENT_SANDBOXES_ENABLED=1`. No sandbox
image is published yet, so this run used images built on the host
(`BOW_SANDBOX_IMAGE`, `BOW_SANDBOX_CADDY_IMAGE`, `BOW_SANDBOX_FIREWALL_IMAGE`,
`BOW_SANDBOX_TOOLS_IMAGE` set for both the compute and the script, and
`BOW_SANDBOX_TEST_PRELOADED=1`). Sandboxes get a 1 GB memory limit. At the end
the script deletes its sandboxes, environments and compute, and removes what
BoW put on the server. `--keep` skips that.

## What is not real

Steps 2 and 4 go through the real agent loop: a user message in a report, the
planner, the tool registry with its three gates, `run_in_sandbox`, the real
server. **The model was not real.** The only model provider configured in the
DevBox answered "no credits remaining", and no other key was available. So the
organization's default model was pointed at `tools/agent/stub_llm_sandbox.py`
for the run (`tools/agent/setup_stub_llm.py --stub-url http://127.0.0.1:9312/v1`)
and restored afterwards.

The stand-in only decides which tool call to make and with which arguments.
It is offered the same tool list a real model gets (the log shows
`create_sandbox`, `run_in_sandbox`, `sandbox_status` and `delete_sandbox`
among the planner tools). What this run therefore does **not** show is that a
real model, reading the tool descriptions and the prompt section, chooses these
tools and fills the arguments well. That needs one run with a working model
key; the script needs no change for it.

Steps 3 and 5, and the idle stop in step 4, call the service and the jobs
directly, with a clock moved past the idle timeout. That is deliberate: waiting
out a real idle timeout adds nothing.

## Output

```
== 1. An admin adds a server, bootstraps it, creates environments and turns the feature on
  [PASS] the compute is created disabled, and no private key comes back
  [PASS] Test connection shows the server's fingerprint before anything is trusted — SHA256:<fingerprint>
  [PASS] the fingerprint is the one expected for this server
  [PASS] after confirming it, Test connection logs in and reports Docker and gVisor — {"host_key": "ok", "ssh": "ok", "docker": "ok", "isolation": "ok", "bootstrap": "warning"}
  [PASS] bootstrap finishes with every step reported — 18s; docker=ok, isolation=ok, network=ok, sandboxd=ok, images=ok, router=ok, egress=ok, firewall=ok, verify=ok
     verify: A test sandbox ran a command over TLS and could not reach the host, the internet or the metadata address. Strong isolation: verified (kernel 4.19.0-gvisor).
  [PASS] isolation is reported — gvisor
  [PASS] two environments exist and are granted to the user
  [PASS] the feature is switched on with a ready compute and environments
== 2. In a report, the agent installs a package, runs Python and writes a PNG
  [PASS] the agent created the report's sandbox — state=ready, events=['created', 'busy', 'run_started', 'packages_installed', 'run_finished', 'ready']
  [PASS] a run succeeded and produced a file — succeeded/0
  [PASS] the package installed from the registry was used
  [PASS] the PNG is a report file — chart.png (image/png)
  [PASS] the run is attributed to the user who asked
  [PASS] the sandbox uses the environment that allows the registries
== 3. A sandbox with no internet cannot reach the internet
     probe: {'internet': 'TimeoutError', 'metadata': 'TimeoutError', 'host': 'TimeoutError', 'dns': 'gaierror', 'https': 'URLError'}
  [PASS] nothing outside is reachable and no name resolves — {"internet": "TimeoutError", "metadata": "TimeoutError", "host": "TimeoutError", "dns": "gaierror", "https": "URLError"}
  [PASS] installing a package there is refused in plain words — This sandbox has no internet access, so it cannot install packages. Use what is already installed, or ask an admin for an environment that allows the package registries.
== 4. The sandbox stops when idle and resumes on the next run with its packages
  [PASS] the lifecycle job stopped the idle sandbox — counts={'idle': 0, 'stopped': 1, 'deleted': 0, 'recovered': 0, 'errors': 0}, state=stopped
  [PASS] its container is stopped on the server, not removed
  [PASS] the same sandbox was resumed — state=ready, events tail=['stopped', 'resumed', 'busy', 'run_started', 'run_finished', 'ready']
  [PASS] the package installed before the stop is still there — succeeded: '                  (oo)\\_______\n                            (__)\\       )\\/\\\n                                ||----w |\n                                ||     ||\n' ''
== 5. Compute health and reconcile against the real server
  [PASS] the compute health job finds the server healthy — {"healthy": 1, "unreachable": 0, "sandboxes_failed": 0, "sandboxes_stopped": 0, "errors": 0}
  [PASS] reconcile has nothing to remove and loses nothing — {"computes": 1, "removed": 0, "repaired": 0, "lost": 0, "errors": 0}
== Cleaning up
22/22 checks passed
```

## Found while getting it to pass

| Finding | Fix |
|---|---|
| `asyncssh` logs every connection, channel and full command line at INFO: dozens of lines per run, in the application log | Its logger is set to WARNING in `drivers/docker_ssh/ssh.py` |
| The sandbox child tables were missing from the organization scope inventory, failing `test_org_scope_inventory` | Listed as parent-scoped in `app/core/org_scope_inventory.py` |
| A layering test flagged a loop variable named `compute_type` in the routes | Renamed; the rule (no branching on the compute type outside the driver registry) stands |

## Suites

Run with the deployment flag unset, on SQLite:

```bash
cd backend
TESTING=true uv run --no-sync pytest tests/e2e/sandbox tests/unit/test_agent_sandbox_*.py \
  tests/unit/test_sandbox_driver_contract_fake.py tests/unit/test_permissions_registry.py \
  tests/e2e/rbac/test_rbac_registry.py tests/unit/test_org_scope_inventory.py --db=sqlite
```

384 tests, all passing after the layering fix above. This includes the flag-off
suite (`tests/e2e/sandbox/test_feature_gate.py`) and the driver contract suite
against the fake driver.

The same contract suite against the real `docker-ssh` driver, on the same host:

```bash
BOW_SANDBOX_TEST_HOST=<address> ... TESTING=true uv run --no-sync pytest tests/contracts/sandbox_driver --db=sqlite
```

22 tests, all passing, in 3 min 51 s. They are skipped without
`BOW_SANDBOX_TEST_HOST`. The suite leaves the host bootstrapped; it was cleaned
afterwards with the driver's own `cleanup()`.

## Not covered

- A real model (above).
- Hosts other than Ubuntu, and a root login. Unchanged from the spike.
- The published image path: bootstrap pulling images from a registry. Images
  were preloaded.
- The admin UI. This run used the API; the UI has its own evidence under
  `media/pr/`.
