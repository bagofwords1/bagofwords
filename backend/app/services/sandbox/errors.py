"""Errors the sandbox service raises. Each carries a stable ``code`` and a
message that is safe to show to the user or hand to the agent as a tool result."""
from __future__ import annotations

from typing import Optional


class SandboxError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 400, detail: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.detail = detail


# Codes, in one place so tools, routes and tests agree on them.
DISABLED = "sandbox.disabled"                      # the organization switch is off
FORBIDDEN = "sandbox.forbidden"                    # the user lacks use_agent_sandbox or report access
NO_ENVIRONMENT = "sandbox.no_environment"          # no environment the user may use
ENVIRONMENT_NOT_ALLOWED = "sandbox.environment_not_allowed"
ENVIRONMENT_NOT_FOUND = "sandbox.environment_not_found"
NO_COMPUTE = "sandbox.no_compute"                  # no enabled, healthy compute fits the environment
QUOTA_USER = "sandbox.quota_user"                  # too many running sandboxes for this user
QUOTA_TOTAL = "sandbox.quota_total"                # too many running sandboxes in the deployment
BUSY = "sandbox.busy"                              # a run is already in progress
NOT_FOUND = "sandbox.not_found"
INVALID = "sandbox.invalid_request"
PACKAGES_NEED_NETWORK = "sandbox.packages_need_network"
PACKAGE_INSTALL_FAILED = "sandbox.package_install_failed"
INPUT_NOT_ALLOWED = "sandbox.input_not_allowed"    # an input file is not one of the report's
UNAVAILABLE = "sandbox.unavailable"                # the compute or the sandbox could not be reached
INVALID_STATE = "sandbox.invalid_state"
COMPUTE_NOT_FOUND = "sandbox.compute_not_found"
COMPUTE_IN_USE = "sandbox.compute_in_use"
COMPUTE_NOT_READY = "sandbox.compute_not_ready"
ENVIRONMENT_IN_USE = "sandbox.environment_in_use"
ENVIRONMENT_BROKEN = "sandbox.environment_broken"  # its setup or start script keeps failing
BOOTSTRAP_RUNNING = "sandbox.bootstrap_running"
NAME_TAKEN = "sandbox.name_taken"


def disabled() -> SandboxError:
    return SandboxError(DISABLED, "Agent sandboxes are not enabled for this organization.", status=403)


def starting() -> SandboxError:
    return SandboxError(
        BUSY, "This report's sandbox is still being created by another run. Try again in a moment.",
        status=409,
    )


def busy() -> SandboxError:
    return SandboxError(
        BUSY, "This report's sandbox is already running something. One run at a time: wait for it to finish.",
        status=409,
    )
