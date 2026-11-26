"""Agent sandboxes: containers where a report's agent runs code.

A second, separate tier from the in-process code sandbox in
``app/ai/code_execution/sandbox/``. Everything here sits behind the deployment
flag read by :func:`app.services.sandbox.feature.sandbox_feature_enabled`.

Importing this package must stay cheap: it is imported by existing code at the
feature's connection points (settings payload, router, tool catalog, role
editor, scheduler) whether or not the feature is on. The SSH and gRPC
libraries are only ever imported lazily, by the driver registry and the
runtime client, when a sandbox is actually used.
"""
