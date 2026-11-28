"""How much of a compute its sandboxes may use, and how much they use now.

A compute is not full after a fixed number of sandboxes: a 4 GB environment
takes four times the memory of a 1 GB one. Placement therefore counts the
limits each sandbox was created with against what the compute has:

* **CPU** is shared, so it may be overcommitted (default 4 sandboxes' worth of
  limit per real CPU): sandboxes are idle most of the time.
* **Memory** is not shared: by default the limits must fit in what the server
  has, less a reserve for the system and BoW's own containers.
* **Disk** is held by every sandbox that still exists on the compute, running
  or stopped, because a stopped sandbox keeps its workspace.

A resource the compute did not report is not counted. The plain sandbox count
(``capacity``) stays as an upper bound on top of this.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from pydantic import BaseModel, ConfigDict, Field

RESOURCES = ("cpus", "memory_mb", "disk_mb")
# States in which a sandbox's container is (or is about to be) running: it holds CPU and memory.
RUNNING_STATES = ("provisioning", "ready", "busy", "idle")
# States in which the sandbox still exists on the compute: it holds disk.
EXISTING_STATES = RUNNING_STATES + ("stopped", "failed")


class Allocation(BaseModel):
    """An admin's policy for one compute. Every field has a safe default."""

    model_config = ConfigDict(extra="forbid")

    cpu_overcommit: float = Field(
        default=4.0, ge=1.0, le=32.0, title="CPU overcommit",
        description="How many CPUs' worth of sandbox limits may be placed per real CPU.",
    )
    memory_overcommit: float = Field(
        default=1.0, ge=1.0, le=4.0, title="Memory overcommit",
        description="1 means sandbox memory limits must fit in the server's memory. Above 1 risks out-of-memory kills.",
    )
    reserve_memory_mb: int = Field(
        default=1024, ge=0, le=1024 * 1024, title="Memory kept free (MB)",
        description="Left for the operating system and BoW's own services on the compute.",
    )
    reserve_disk_mb: int = Field(
        default=5 * 1024, ge=0, le=100 * 1024 * 1024, title="Disk kept free (MB)",
        description="Left for images, logs and the system on the workspace disk.",
    )


def allocation_of(compute) -> Allocation:
    try:
        return Allocation(**(compute.allocation or {}))
    except Exception:  # noqa: BLE001 - a stored value from an older shape must not break placement
        return Allocation()


def allocatable(compute) -> Dict[str, float]:
    """What sandboxes may be given on this compute, per resource it reported."""
    total = {k: float(v) for k, v in (compute.resources or {}).items() if k in RESOURCES and v}
    policy = allocation_of(compute)
    out: Dict[str, float] = {}
    if "cpus" in total:
        out["cpus"] = round(total["cpus"] * policy.cpu_overcommit, 2)
    if "memory_mb" in total:
        out["memory_mb"] = max(0.0, round((total["memory_mb"] - policy.reserve_memory_mb) * policy.memory_overcommit))
    if "disk_mb" in total:
        out["disk_mb"] = max(0.0, total["disk_mb"] - policy.reserve_disk_mb)
    return out


@dataclass
class Usage:
    """What the sandboxes on one compute hold, by their limits."""

    cpus: float = 0.0
    memory_mb: float = 0.0
    disk_mb: float = 0.0
    running: int = 0
    existing: int = 0

    def as_dict(self) -> Dict[str, float]:
        return {"cpus": round(self.cpus, 2), "memory_mb": self.memory_mb, "disk_mb": self.disk_mb,
                "running": self.running, "existing": self.existing}


def _limit(limits: Optional[Dict[str, Any]], key: str) -> float:
    try:
        return float((limits or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def usage_by_compute(sandboxes: Iterable) -> Dict[str, Usage]:
    """Add up the limits of the given sandbox rows (id, compute_id, state, limits)."""
    out: Dict[str, Usage] = {}
    for sandbox in sandboxes:
        if not sandbox.compute_id or sandbox.state not in EXISTING_STATES:
            continue
        usage = out.setdefault(sandbox.compute_id, Usage())
        usage.existing += 1
        usage.disk_mb += _limit(sandbox.limits, "disk_mb")
        if sandbox.state in RUNNING_STATES:
            usage.running += 1
            usage.cpus += _limit(sandbox.limits, "cpus")
            usage.memory_mb += _limit(sandbox.limits, "memory_mb")
    return out


@dataclass
class Fit:
    ok: bool
    # Resources there is not enough of, e.g. ["memory_mb"]; "sandboxes" for the count.
    short: List[str] = field(default_factory=list)
    # The fullest resource after placing it, 0..1 (for choosing the least loaded).
    load: float = 0.0


def fits(compute, usage: Optional[Usage], need: Dict[str, Any], *, resume: bool = False) -> Fit:
    """Whether a sandbox with limits ``need`` can be placed on ``compute``.

    ``resume``: the sandbox already exists there (stopped), so its disk is
    already counted and only CPU and memory are needed again."""
    usage = usage or Usage()
    room = allocatable(compute)
    short: List[str] = []
    load = 0.0
    capacity = int(compute.capacity or 0)
    if usage.running + 1 > capacity:
        short.append("sandboxes")
    if capacity:
        load = max(load, (usage.running + 1) / capacity)
    for key in RESOURCES:
        if key not in room:
            continue
        if key == "disk_mb" and resume:
            continue
        wanted = getattr(usage, key) + _limit(need, key)
        if wanted > room[key] + 1e-9:
            short.append(key)
        if room[key] > 0:
            load = max(load, wanted / room[key])
    return Fit(ok=not short, short=short, load=round(load, 4))


_NAMES = {"cpus": "CPU", "memory_mb": "memory", "disk_mb": "disk space", "sandboxes": "sandbox slots"}


def describe_short(short: Iterable[str]) -> str:
    """"memory and disk space", for an error message."""
    names = [_NAMES.get(k, k) for k in dict.fromkeys(short)]
    return " and ".join([", ".join(names[:-1]), names[-1]] if len(names) > 1 else names)
