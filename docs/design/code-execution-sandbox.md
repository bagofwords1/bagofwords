# Code execution sandbox

Model-generated Python (the `create_data` / `inspect_data` code path and the
python-pptx slides path) runs in a **separate, disposable process** rather
than inside the API worker. This document describes the boundary, what it
does and does not guarantee, and the knobs an operator has.

## Why

Before this change, generated code ran through `exec()` inside the uvicorn
worker. The only barrier was the AST denylist (`validate_python_code`), which
is good at shaping model output but is not a security boundary: Python
denylists are a well-studied bypass genre. One escape had the whole product's
blast radius — the worker holds `BOW_ENCRYPTION_KEY`, the app database URL,
live connector objects for every configured data source, and (where
configured) a Kerberos keytab. There was also no memory cap and no way to
kill a runaway loop.

## What runs where

```
API worker (trusted)                          sandbox child (untrusted)
─────────────────────────────                 ─────────────────────────────
validate_python_code (AST)                    fork of …sandbox.zygote → child.run
wrap_clients_for_capture      ── job ───────▶ scrubbed env, rlimits,
  QueryCapturingClientWrapper                 no_new_privs, Landlock
  (capture, timeout, quotas,   ◀── rpc ─────  exec(code); generate_df()
   rate limit, SQL validation) ── result ──▶    ds_clients[k].execute_query()
SafeHttpClient (SSRF policy)                    http.get / batch_get  → RPC
format_df_for_widget           ◀── Arrow ───  DataFrame → Arrow IPC
```

* The child is **never forked from the API worker**. It is forked from a
  per-process fork server that was itself spawned with a scrubbed
  environment and holds only the imported libraries (see *Cost*), so it
  starts with no decrypted credentials, no ORM session, no encryption key.
* Its environment is built from scratch (`runner._child_environment`). Not
  one variable of the API process is inherited.
* Every data-source query the code issues is a request over a pipe. The
  `QueryCapturingClientWrapper` instances never leave the parent, so capture,
  per-query timeouts, rate limits, quotas and parameter rendering run exactly
  where they did before. The child's proxy exposes **only** `execute_query`
  (and its `query` alias); every other attribute of a real client is
  unreachable.
* Results come back as **Arrow IPC** bytes. The parent never unpickles data
  from the child — that would be arbitrary code execution in the API process.
  Parent → child payloads are pickle (the trusted direction).
* Errors raised on the trusted side (e.g. `QueryTimeoutError`,
  `UnsafeSQLError`, usage-limit errors) that propagate unchanged out of the
  generated code are re-raised in the parent **as the original objects**, so
  the retry loop's type-based decisions are unchanged.

## Enforced limits

| Limit | Mechanism | Default |
|---|---|---|
| Wall clock | parent SIGKILLs the child's process group | 600 s |
| Memory | `RLIMIT_AS` in the child | 4096 MB |
| CPU time | `RLIMIT_CPU` | = wall clock |
| Core dumps | `RLIMIT_CORE=0` | always |
| File size | `RLIMIT_FSIZE` | 256 MB |
| Privilege gain | `prctl(PR_SET_NO_NEW_PRIVS)` | always |
| Filesystem | Landlock: interpreter/libs read-only, this run's uploaded files read-only, one scratch dir read-write, nothing else (`/proc` is not exposed — `/proc/<pid>/environ` of the parent would leak the key) | when the kernel supports it |
| TCP | Landlock net (ABI ≥ 4, kernel ≥ 6.7): no bind, no connect | when the kernel supports it |
| stdout | capped at 2 MB in the child | always |
| Result payload | child checks Arrow/PPTX size; parent rejects oversized frames before reading them | 128 MB |
| Result decode | parent checks the Arrow stream's metadata before decoding: no compression, at most `BOW_SANDBOX_MAX_RESULT_CELLS` values, so a few bytes cannot make the API worker allocate gigabytes | ~16.7M values |
| Process creation | Linux seccomp denies fork/vfork/process clone; threads remain available | Linux |

Cancelling the tool (user stop, tool timeout) sets a cancel event the runner
polls; the child is killed instead of running on.

## What it does not guarantee

* **Kernel isolation.** The child shares the host kernel. A kernel exploit is
  out of scope for this layer; that is what gVisor / Kata / a microVM add.
* **Network on older kernels.** Without Landlock ABI 4 (kernel < 6.7) the
  child can open TCP sockets to whatever the container can reach. Production
  refuses to execute by default in this case. An operator can explicitly set
  `BOW_SANDBOX_REQUIRE_LANDLOCK=0` to permit weaker confinement, or deploy an
  executor with a no-egress network policy.
* **Landlock availability.** Requires kernel ≥ 5.13 with
  `CONFIG_SECURITY_LANDLOCK` and `landlock` in the LSM list (default on
  mainstream distro kernels; not on every cloud/VM kernel). Docker's default
  seccomp profile allows the Landlock syscalls since 20.10. When unavailable,
  production refuses to execute by default. Development logs a warning and
  continues; set `BOW_SANDBOX_REQUIRE_LANDLOCK=1` to require it there too.
  Production also requires the Linux process-creation filter so a forked child
  cannot leave the process group controlled by the runner.

## Choosing `BOW_SANDBOX_REQUIRE_LANDLOCK` (upgrade note)

The production default (`1`) runs generated code only where the kernel can
apply the full policy: Landlock filesystem **and** TCP confinement (Linux
6.7+, Landlock ABI 4) plus the process-creation filter. On any other host,
every create_data / inspect_data / refresh / scheduled rerun / PPTX run
fails until the operator acts:

* At startup each API worker logs, at ERROR, `Code execution is disabled on
  this host: …` with the kernel and Landlock ABI it found.
* Each refused run fails with a `SandboxUnavailable` error naming this
  setting. It is terminal: the coder is not asked to regenerate code, since
  no code change can fix the host.

The fix is to upgrade the kernel, or to set `BOW_SANDBOX_REQUIRE_LANDLOCK=0`.
`0` does not switch confinement off: the child always applies as much
Landlock as the kernel offers. It only stops refusing when part is missing:

| Host | With `0` |
|---|---|
| Linux 6.7+ (Landlock ABI ≥ 4) | Full policy, same as `1`. |
| Linux 5.13–6.6 with Landlock enabled (e.g. Ubuntu 22.04, Debian 12, AL2023) | Filesystem confinement still applies, so secrets stay out of reach. Generated code that escapes Python can open TCP connections (internal services, cloud metadata endpoints). |
| No Landlock (e.g. RHEL 8, or the LSM disabled) | Separate process, scrubbed environment, rlimits and the kill timeout only. Code that escapes Python can read files the API user can read, including `backend/.env` and the API worker's `/proc/<pid>/environ`. |

## Operator knobs (environment)

| Variable | Meaning | Default |
|---|---|---|
| `BOW_CODE_SANDBOX` | `subprocess` or `inprocess` (pre-sandbox behavior, debug only) | `subprocess` |
| `BOW_SANDBOX_TIMEOUT_SECONDS` | wall-clock budget per execution | `600` |
| `BOW_SANDBOX_MEMORY_MB` | `RLIMIT_AS` for the child, `0` disables | `4096` |
| `BOW_SANDBOX_CPU_SECONDS` | `RLIMIT_CPU`, `0` disables | = timeout |
| `BOW_SANDBOX_REQUIRE_LANDLOCK` | `1` → require Landlock filesystem and TCP confinement; `0` explicitly permits weaker confinement | `1` in production, `0` in development |
| `BOW_SANDBOX_MAX_RESULT_MB` | Maximum Arrow or PPTX payload accepted from the child | `128` |
| `BOW_SANDBOX_MAX_RESULT_CELLS` | Maximum values (rows × columns, nested included) in a returned DataFrame | one per 8 bytes of `BOW_SANDBOX_MAX_RESULT_MB` (~16.7M) |
| `BOW_SANDBOX_ZYGOTE` | `0` disables the fork server (each run then spawns a full interpreter, ~0.7 s) | `1` |

Thread-count hints (`OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`,
`MKL_NUM_THREADS`, `NUMEXPR_MAX_THREADS`) are the only variables copied from
the API process into the child.

## Cost

A cold interpreter with pandas/numpy/pyarrow imported costs ~0.7 s. To keep
that off every execution, each API process starts one **fork server**
(`sandbox/zygote.py`): a spawned interpreter with the scrubbed environment
that imports the libraries once and then only forks. Each execution is a
fork of that clean process (a few milliseconds), followed by the child's own
rlimits / no_new_privs / Landlock. The zygote never receives a job, a
credential or a result, so a forked child starts from the same state a fresh
spawn would. Finished children stay zombies until the runner reaps them
through the zygote, so a pid can never be recycled while the runner still
holds it (the SIGKILL on timeout cannot hit an unrelated process). If the
fork server cannot start, the runner spawns a full interpreter per run.
DataFrames cross the pipe as Arrow, which is a copy; very large results pay
that once.

## Observability

Span `code_execution.execute_code` carries `code_execution.sandbox`
(`subprocess` / `inprocess`), `sandbox_spawn_ms`, `sandbox_total_ms` and
`sandbox_landlock` (bool). The child's `ready` message (pid, rlimits, Landlock
report) is logged at DEBUG by `app.ai.code_execution.sandbox.runner`.

## Next step: a separate executor container

The pipe protocol is transport-agnostic. Moving the child into its own
container (same image, different entrypoint) with no secrets, no egress and a
`runtimeClassName` (gVisor / Kata) is an additive change on top of this
design: the parent-side broker and the child are already the two halves of
that service.
