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
validate_python_code (AST)                    python -m …sandbox.child
wrap_clients_for_capture      ── job ───────▶ scrubbed env, rlimits,
  QueryCapturingClientWrapper                 no_new_privs, Landlock
  (capture, timeout, quotas,   ◀── rpc ─────  exec(code); generate_df()
   rate limit, SQL validation) ── result ──▶    ds_clients[k].execute_query()
SafeHttpClient (SSRF policy)                    http.get / batch_get  → RPC
format_df_for_widget           ◀── Arrow ───  DataFrame → Arrow IPC
```

* The child is **spawned, never forked**. It starts with an empty heap: no
  decrypted credentials, no ORM session, no encryption key.
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

Cancelling the tool (user stop, tool timeout) sets a cancel event the runner
polls; the child is killed instead of running on.

## What it does not guarantee

* **Kernel isolation.** The child shares the host kernel. A kernel exploit is
  out of scope for this layer; that is what gVisor / Kata / a microVM add.
* **Network on older kernels.** Without Landlock ABI 4 (kernel < 6.7) the
  child can open sockets to whatever the container can reach. There are no
  credentials in the child to use, but SSRF-style reach into the customer's
  network is not prevented. Deploy the executor as a separate container with
  a no-egress network policy to close this, or run on a 6.7+ kernel.
* **Landlock availability.** Requires kernel ≥ 5.13 with
  `CONFIG_SECURITY_LANDLOCK` and `landlock` in the LSM list (default on
  mainstream distro kernels; not on every cloud/VM kernel). Docker's default
  seccomp profile allows the Landlock syscalls since 20.10. When unavailable
  the sandbox logs one warning and runs with everything else in the table;
  set `BOW_SANDBOX_REQUIRE_LANDLOCK=1` to refuse instead.

## Operator knobs (environment)

| Variable | Meaning | Default |
|---|---|---|
| `BOW_CODE_SANDBOX` | `subprocess` or `inprocess` (pre-sandbox behavior, debug only) | `subprocess` |
| `BOW_SANDBOX_TIMEOUT_SECONDS` | wall-clock budget per execution | `600` |
| `BOW_SANDBOX_MEMORY_MB` | `RLIMIT_AS` for the child, `0` disables | `4096` |
| `BOW_SANDBOX_CPU_SECONDS` | `RLIMIT_CPU`, `0` disables | = timeout |
| `BOW_SANDBOX_REQUIRE_LANDLOCK` | `1` → fail executions when Landlock cannot be applied | `0` |
| `BOW_SANDBOX_PREWARM` | `0` disables the one idle pre-spawned child per API process | `1` |

Thread-count hints (`OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`,
`MKL_NUM_THREADS`, `NUMEXPR_MAX_THREADS`) are the only variables copied from
the API process into the child.

## Cost

A cold child (interpreter + pandas/numpy/pyarrow import) costs ~0.7 s. Each
API process keeps one idle pre-spawned child so the next execution starts
immediately; it is reaped after 10 idle minutes. DataFrames cross the pipe as
Arrow, which is a copy; very large results pay that once.

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
