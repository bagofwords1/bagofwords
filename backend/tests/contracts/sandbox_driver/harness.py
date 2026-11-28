"""What a driver's test module supplies to the contract suite."""
from __future__ import annotations

import dataclasses
import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional, Sequence

from app.services.sandbox.drivers.base import (
    ComputeContext, SandboxDriver, SandboxHandle, SandboxSpec,
)
from app.services.sandbox.runtime_client import SandboxRuntimeClient


@dataclass
class DriverHarness:
    # A driver for a compute that has been prepared ("bootstrapped").
    driver: SandboxDriver
    # A second driver instance for the same compute, as another worker would build it.
    make_driver: Callable[[], SandboxDriver]
    # The image sandboxes are created from.
    image: str
    # The command that, run inside a sandbox, exits 0 when `domain` is
    # reachable under the sandbox's network policy and non-zero when it is not.
    probe_command: Callable[[str], Sequence[str]]
    # Domains to probe: one on the registry list, one public domain off every list.
    registry_domain: str
    other_domain: str
    # Put a sandbox on the compute that BoW does not know about, owned by
    # `instance_id` (this instance when None); returns its id.
    plant_orphan: Callable[[Optional[str]], Awaitable[str]]
    # Whether a sandbox with this id exists on the compute, whoever owns it.
    exists_on_compute: Callable[[str], Awaitable[bool]]
    # Remove a planted sandbox from the compute, whoever owns it (test cleanup).
    remove_from_compute: Callable[[str], Awaitable[None]]
    memory_mb: int = 1024
    disk_mb: int = 1024
    created: List[SandboxHandle] = field(default_factory=list)

    def spec(self, **overrides) -> tuple:
        """A sandbox spec and the plaintext token whose hash it carries."""
        token = uuid.uuid4().hex
        values = dict(
            sandbox_id=uuid.uuid4().hex[:12],
            image=self.image,
            token_sha256=hashlib.sha256(token.encode()).hexdigest(),
            memory_mb=self.memory_mb,
            disk_mb=self.disk_mb,
        )
        values.update(overrides)
        return SandboxSpec(**values), token

    async def create(self, **overrides) -> tuple:
        spec, token = self.spec(**overrides)
        handle = await self.driver.create(spec)
        self.created.append(handle)
        return handle, token

    def runtime(self, handle: SandboxHandle, token: str, driver: Optional[SandboxDriver] = None) -> SandboxRuntimeClient:
        endpoint = dataclasses.replace((driver or self.driver).endpoint(handle), token=token)
        return SandboxRuntimeClient(endpoint)

    async def cleanup(self) -> None:
        for handle in self.created:
            try:
                await self.driver.delete(handle)
            except Exception:
                pass
        self.created.clear()
