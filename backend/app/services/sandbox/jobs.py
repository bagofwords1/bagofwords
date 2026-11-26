"""Scheduled jobs for agent sandboxes.

Registered on the scheduler leader, and only while the deployment flag is on:
with it off no sandbox job exists. The jobs themselves (lifecycle, backend
health, reconcile) are added in :func:`register_sandbox_jobs` as they land.
"""
from __future__ import annotations

import logging
from typing import List

from app.services.sandbox.feature import sandbox_feature_enabled

logger = logging.getLogger(__name__)

# Every sandbox job id starts with this, so tests and operators can tell them apart.
JOB_ID_PREFIX = "agent_sandbox_"


def register_sandbox_jobs(scheduler) -> List[str]:
    """Register the sandbox jobs on ``scheduler``; returns the job ids added.

    A no-op returning ``[]`` when the feature is off for this deployment.
    """
    if not sandbox_feature_enabled():
        return []
    job_ids: List[str] = []
    return job_ids
