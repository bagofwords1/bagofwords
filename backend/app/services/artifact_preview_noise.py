"""Preview error events that are not app code.

Shared by the readiness wait (ArtifactPreviewService.wait_ready) and the
automatic check classifier, so both ignore the same noise.
"""
from __future__ import annotations

HOST_NOISE_PREFIXES = ("Failed to load resource", "[vite]", "[nuxt]", "[Vue warn]", "[HMR]")
# Playwright's service_workers="block" (preview contexts, _browser_common.py)
# installs an init script that reads navigator.serviceWorker in every frame; in
# the sandboxed artifact iframe that read throws. Not app code.
SANDBOX_SW_NOISE = "Failed to read the 'serviceWorker' property from 'Navigator'"


def is_preview_noise_error(event: dict) -> bool:
    """True for an error event from the host console or the sandbox service-worker block."""
    if event.get("kind") != "error":
        return False
    message = str(event.get("message") or "")
    if event.get("source") == "console" and message.startswith(HOST_NOISE_PREFIXES):
        return True
    return SANDBOX_SW_NOISE in message
