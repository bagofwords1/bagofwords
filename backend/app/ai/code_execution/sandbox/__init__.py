"""Process-level sandbox for model-generated code.

Generated Python no longer runs inside the API process. Each execution spawns
a fresh interpreter (``spawn``, never ``fork``, so it inherits nothing from the
parent's memory: no decrypted credentials, no live client objects, no
encryption key) with a scrubbed environment, rlimits, a wall-clock kill, and a
Landlock filesystem/network policy where the kernel supports it.

Everything the code needs from the trusted side (database queries, web
fetches) is brokered back to the parent over a pipe. The parent keeps the
`QueryCapturingClientWrapper` instances, so capture, timeouts, rate limits,
quotas and SQL validation run exactly where they did before — on the trusted
side of the boundary.

Modules:
    config    — env-driven knobs (mode, timeout, memory, landlock policy)
    landlock  — ctypes bindings for the Landlock LSM (fail-open by default)
    protocol  — pipe framing and DataFrame (Arrow IPC) serialization
    child     — the untrusted-side entrypoint (``python -m …sandbox.child``)
    runner    — the trusted-side driver: spawn, broker RPC, enforce limits
"""
