# Feedback Loop — generated code can outlive the sandbox or exhaust the API worker

PR #1095 moved generated code to a child process. This loop checks three
security properties of that boundary: production refuses execution without
kernel confinement, child output has a parent-side memory budget, and generated
code cannot fork a process that survives cleanup.

## Root cause (validated)

- `sandbox/config.py:61` defaulted `require_landlock` to false in every
  environment. `sandbox/child.py:336` therefore sent `ready` and executed code
  when filesystem and TCP confinement were unavailable.
- `sandbox/protocol.py:29` allowed a 2 GiB child frame. `sandbox/runner.py:452`
  assembled the entire payload in the trusted API worker before Arrow decoded
  it. The child's `RLIMIT_AS` does not limit this allocation.
- `sandbox/runner.py:255` killed the child's process group. Generated code could
  fork and call `setsid()`, moving a descendant out of that group. A local
  direct-run probe observed `detached_child_survived=True` after `run_job`
  returned; the probe then killed the test process.

## Loop A — deterministic reproduction

From `backend`, with Python 3.12 and the project's dev dependencies:

```bash
BOW_DATABASE_URL=sqlite:///db/test.db uv run pytest -q --disable-warnings \
  tests/unit/test_sandbox_review_repro.py
```

Before the fix: the production-default assertion failed (`require_landlock` was
false), and the 1 MB result-budget test failed because a 2 MB DataFrame was
accepted. Observed: `2 failed` (the Linux-only process tests skip on macOS).

The separate process-escape probe submitted code that forked, called `setsid`
in the fork, and returned the fork PID. After the job returned, `os.kill(pid, 0)`
succeeded. The probe sent `SIGKILL` to that PID before exiting.

## The fix

- `sandbox/config.py` requires confinement by default when
  `ENVIRONMENT=production`; an explicit `BOW_SANDBOX_REQUIRE_LANDLOCK=0` opts
  into weaker operation. `sandbox/child.py` requires both filesystem and TCP
  Landlock when strict mode is selected.
- `sandbox/child.py` rejects Arrow and PPTX results above
  `BOW_SANDBOX_MAX_RESULT_MB` (64 MB by default). `sandbox/runner.py` rejects a
  larger child frame from its header before reading the payload.
- `sandbox/seccomp.py` installs a Linux filter that denies process creation
  while allowing threads. Strict production mode refuses to execute if this
  filter or the mandatory resource limits cannot be installed.
- On timeout or cancellation, `sandbox/runner.py` tells the parent-side query
  wrapper to request source cancellation for any in-flight query.

After the fix, the reproduction tests reported `4 passed, 2 skipped`. The full
sandbox, query-timeout, and query-cancellation suites reported `71 passed,
4 skipped`. The memory-limit test is
skipped on macOS because that kernel does not enforce `RLIMIT_AS`, and the
scratch-path assertion now resolves macOS's `/var` → `/private/var` symlink.
A disposable Linux container verified that a full sandbox job returns a DataFrame with
`process_creation_blocked=True`, generated `os.fork()` raises `PermissionError`,
an ordinary Python thread still starts, and the memory limit raises a sandbox
error. A 64-job, 8-worker macOS stress run reported `completed=64 correct=64`;
a 16-job, 4-worker Linux fork-server run reported `completed 16 correct 16`.

## What this proves / regression notes

The reproductions cover the production default, a configurable result budget,
and Linux process creation at the generated-code boundary. The local Docker
kernel reports Landlock ABI 0, so the active Landlock syscall path still needs
verification on a host with ABI 4 or newer. A database query already in flight
can outlive a cancelled job if its driver cannot cancel the query; the broker
thread remains bounded by the existing per-query timeout.
