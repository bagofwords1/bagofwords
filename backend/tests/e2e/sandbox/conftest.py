"""Fixtures shared by the agent-sandbox e2e tests."""
import pytest

# Reused RBAC cast builders (real HTTP routes only).
from tests.e2e.rbac.conftest import (  # noqa: F401  (pytest fixtures)
    _rbac_relaxed_signup_flags,
    assign_role,
    bootstrap_admin,
    create_role,
    enterprise_license,
    get_system_role,
    invite_user_to_org,
    list_roles,
)


def headers(token: str, org_id: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def sandbox_cast(bootstrap_admin, invite_user_to_org):
    """An org with its admin and one plain member: {org_id, admin, member} with ready headers."""
    admin = bootstrap_admin("sbx_admin")
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    return {
        "org_id": admin["org_id"],
        "admin": {**admin, "headers": headers(admin["token"], admin["org_id"])},
        "member": {**member, "headers": headers(member["token"], admin["org_id"])},
    }


# ── Calling the sandbox services directly ────────────────────────────────
#
# The service layer is async and the e2e suite is synchronous, so these tests
# run a coroutine to completion per step (the pattern other e2e modules use
# for service calls). Sandboxes run on the fake compute.

import asyncio
import uuid

from app.schemas.sandbox_schema import SandboxComputeCreate, SandboxEnvironmentCreate
from app.services.sandbox import access as access_module
from app.services.sandbox import computes as compute_service
from app.services.sandbox import environments as environment_service
from app.services.sandbox.drivers.fake import reset_fake_computes
from app.services.sandbox.service import ReportRef, SandboxService
from app.services.sandbox.settings import load_settings, save_settings


def run(coro):
    """Run one coroutine to completion on a fresh event loop."""
    return asyncio.run(coro)


class SandboxWorld:
    """An organization set up for sandboxes on the fake compute, with helpers
    that each do one thing through the service layer."""

    def __init__(self, org_id: str, admin_id: str):
        self.org_id = org_id
        self.admin_id = admin_id
        self.service = SandboxService()

    def session(self):
        from app.dependencies import async_session_maker

        return async_session_maker()

    def call(self, fn):
        """Run ``await fn(db)`` in its own session and return the result."""
        async def _go():
            async with self.session() as db:
                return await fn(db)
        return run(_go())

    def configure(self, **changes):
        async def _go(db):
            settings = await load_settings(db, self.org_id)
            for key, value in changes.items():
                setattr(settings, key, value)
            await save_settings(db, self.org_id, settings)
            await db.commit()
        self.call(_go)

    def add_compute(self, name=None, *, enabled=True, bootstrap=True, capacity=20, labels=(), **config):
        async def _go(db):
            compute = await compute_service.create_compute(db, self.org_id, self.admin_id, SandboxComputeCreate(
                name=name or f"fake-{uuid.uuid4().hex[:6]}", type="fake", config=config,
                labels=list(labels), capacity=capacity,
            ))
            if bootstrap:
                await compute_service.bootstrap_compute(db, self.org_id, self.admin_id, compute)
                if enabled and compute.bootstrap_status == "ready":
                    await compute_service.set_enabled(db, self.org_id, self.admin_id, compute, True)
            return compute.id
        return self.call(_go)

    def add_environment(self, name=None, *, grant_to=(), **fields):
        async def _go(db):
            env = await environment_service.create_environment(db, self.org_id, self.admin_id, SandboxEnvironmentCreate(
                name=name or f"env-{uuid.uuid4().hex[:6]}", image=fields.pop("image", "fake-image"),
                memory_mb=fields.pop("memory_mb", 1024), disk_mb=fields.pop("disk_mb", 1024), **fields,
            ))
            if grant_to:
                await access_module.set_environment_grants(db, self.org_id, self.admin_id, env, list(grant_to))
            return env.id
        return self.call(_go)

    def report_ref(self, report: dict, **extra) -> ReportRef:
        return ReportRef(id=report["id"], organization_id=self.org_id, title=report.get("title") or "", **extra)

    def run_code(self, report, actor_id, request, **kwargs):
        ref = report if isinstance(report, ReportRef) else self.report_ref(report)
        return self.call(lambda db: self.service.run(db, ref, actor_id, request, **kwargs))

    def sandbox_of(self, report):
        report_id = report["id"] if isinstance(report, dict) else report.id

        async def _go(db):
            sandbox = await self.service.get_for_report(db, report_id)
            if sandbox is not None:
                db.expunge(sandbox)
            return sandbox
        return self.call(_go)

    def rows(self, model, *where):
        from sqlalchemy import select

        async def _go(db):
            result = await db.execute(select(model).where(*where))
            items = list(result.scalars().all())
            for item in items:
                db.expunge(item)
            return items
        return self.call(_go)


@pytest.fixture
def sandbox_world(sandbox_cast, agent_sandboxes_on):
    """The cast's organization with the deployment flag and the organization
    switch on. Computes and environments are added by each test."""
    world = SandboxWorld(sandbox_cast["org_id"], sandbox_cast["admin"]["user_id"])
    world.cast = sandbox_cast
    world.configure(enabled=True)
    yield world
    reset_fake_computes()


@pytest.fixture(autouse=True)
def _short_claim_wait(monkeypatch):
    """A run waits briefly for a claim that is not a run (a pause or stop in
    flight). Tests that set ``busy`` by hand have nothing that will release it,
    so keep that wait short."""
    from app.services.sandbox import service as service_module

    monkeypatch.setattr(service_module, "BRIEF_CLAIM_WAIT_SECONDS", 1)
