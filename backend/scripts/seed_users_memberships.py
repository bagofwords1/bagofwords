"""Seed many users, orgs, memberships and RBAC rows to reproduce the
GET /api/users/whoami slowness.

whoami -> organization_service.get_user_organizations loops over EVERY org the
signed-in user belongs to and, per org, runs resolve_permissions (group +
role-assignment + resource-grant queries) plus a usage-quota summary. So the two
levers that make whoami slow are:

  1. the NUMBER of orgs the sandbox user is a member of (the per-org loop), and
  2. the RBAC row volume per org (groups / roles / role_assignments /
     resource_grants that resolve_permissions has to union).

This script maxes out both, and also creates a large pool of unrelated users +
memberships so the shared tables are realistically big.

Usage:
  python scripts/seed_users_memberships.py \
      --orgs 30 --users 300 --groups-per-org 4 --roles-per-org 5 \
      --grants-per-org 200 --user-memberships-per-user 4

The sandbox user (sandbox@bow.dev) is made a member of ALL --orgs (plus its
existing Main Org), added to groups, given role assignments and resource grants
in each, so its whoami pays the full per-org cost for every one.

Reads BOW_DATABASE_URL from env (postgres or sqlite). Idempotent-ish: all rows
are tagged with a short run id so re-runs add a fresh batch.
"""
import os
import sys
import uuid
import random
import asyncio
import argparse
from datetime import datetime

os.environ.setdefault("BOW_DATABASE_URL", "sqlite:///db/app.db")
os.environ.setdefault("BOW_SMTP_PASSWORD", "dummy")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select, insert

import app.models  # noqa
import pkgutil, importlib
for _, modname, _ in pkgutil.iter_modules(app.models.__path__):
    if modname != "application":
        importlib.import_module(f"app.models.{modname}")

from app.models.user import User
from app.models.organization import Organization
from app.models.organization_settings import OrganizationSettings
from app.models.membership import Membership
from app.models.group import Group
from app.models.group_membership import GroupMembership
from app.models.role import Role
from app.models.role_assignment import RoleAssignment
from app.models.resource_grant import ResourceGrant

# Reuse the sandbox user's argon2 hash so seeded users are valid rows (they
# never need to log in interactively; this just satisfies NOT NULL).
FAKE_HASH = "$argon2id$v=19$m=65536,t=3,p=4$IpvMMNUXS7jwa1bdKwHTYw$U+tuai2i51hyTEIptplaceholderplaceholderplaceholder"

PERMS = ["view_reports", "create_reports", "run_queries", "view_schema",
         "manage_agents", "manage_connections", "view_dashboards"]
GRANT_PERMS = ["query", "view_schema", "manage"]


def _uid():
    return str(uuid.uuid4())


async def _bulk(db, table, rows, chunk=2000):
    for i in range(0, len(rows), chunk):
        await db.execute(insert(table), rows[i:i + chunk])
    await db.commit()


def _url():
    u = os.environ["BOW_DATABASE_URL"]
    if u.startswith("postgresql://"):
        return u.replace("postgresql://", "postgresql+asyncpg://", 1)
    if u.startswith("sqlite:///"):
        return u.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    return u


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--orgs", type=int, default=30,
                    help="orgs the sandbox user is added to (the whoami per-org loop)")
    ap.add_argument("--users", type=int, default=300, help="extra unrelated users")
    ap.add_argument("--groups-per-org", type=int, default=4)
    ap.add_argument("--roles-per-org", type=int, default=5)
    ap.add_argument("--grants-per-org", type=int, default=200,
                    help="resource_grants per org (split across user/group/role principals)")
    ap.add_argument("--user-memberships-per-user", type=int, default=4,
                    help="how many orgs each extra user joins")
    args = ap.parse_args()

    now = datetime.utcnow()
    tag = uuid.uuid4().hex[:6]
    engine = create_async_engine(_url(), future=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as db:
        sandbox = (await db.execute(
            select(User).where(User.email == "sandbox@bow.dev"))).scalars().first()
        assert sandbox, "run signup first (sandbox@bow.dev)"
        sid = str(sandbox.id)

        # ---- extra users --------------------------------------------------
        user_ids = [_uid() for _ in range(args.users)]
        await _bulk(db, User.__table__, [
            {"id": uid, "email": f"user-{tag}-{i}@bow.dev", "name": f"User {tag} {i}",
             "hashed_password": FAKE_HASH, "is_active": True, "is_superuser": False,
             "is_verified": True, "is_service_account": False,
             "created_at": now, "updated_at": now}
            for i, uid in enumerate(user_ids)
        ])
        print(f"[seed:{tag}] users={len(user_ids)}")

        # ---- orgs (+settings) the sandbox user joins ----------------------
        org_ids = [_uid() for _ in range(args.orgs)]
        await _bulk(db, Organization.__table__, [
            {"id": oid, "name": f"Org {tag} {i}", "description": f"seeded org {i}",
             "created_at": now, "updated_at": now}
            for i, oid in enumerate(org_ids)
        ])
        await _bulk(db, OrganizationSettings.__table__, [
            {"id": _uid(), "organization_id": oid, "config": {},
             "created_at": now, "updated_at": now}
            for oid in org_ids
        ])
        print(f"  orgs={len(org_ids)} (+settings)")

        # ---- sandbox membership in every org ------------------------------
        await _bulk(db, Membership.__table__, [
            {"id": _uid(), "user_id": sid, "organization_id": oid, "role": "admin",
             "created_at": now, "updated_at": now}
            for oid in org_ids
        ])

        # ---- per-org RBAC (groups, roles, assignments, grants) ------------
        group_rows, gm_rows = [], []
        role_rows, ra_rows = [], []
        grant_rows = []
        # track group/role ids per org so sandbox gets group memberships + role
        # assignments and grants land on real principals.
        for oid in org_ids:
            gids = [_uid() for _ in range(args.groups_per_org)]
            for gi, gid in enumerate(gids):
                group_rows.append({
                    "id": gid, "organization_id": oid, "name": f"grp-{tag}-{gi}",
                    "description": "seeded", "created_at": now, "updated_at": now})
                # sandbox is in every group
                gm_rows.append({"id": _uid(), "group_id": gid, "user_id": sid,
                                "created_at": now, "updated_at": now})

            rids = [_uid() for _ in range(args.roles_per_org)]
            for ri, rid in enumerate(rids):
                role_rows.append({
                    "id": rid, "organization_id": oid, "name": f"role-{tag}-{ri}",
                    "description": "seeded",
                    "permissions": random.sample(PERMS, k=random.randint(2, len(PERMS))),
                    "is_system": False, "created_at": now, "updated_at": now})
                # assign role to sandbox directly AND to a group principal
                ra_rows.append({"id": _uid(), "organization_id": oid, "role_id": rid,
                                "principal_type": "user", "principal_id": sid,
                                "created_at": now, "updated_at": now})
                ra_rows.append({"id": _uid(), "organization_id": oid, "role_id": rid,
                                "principal_type": "group", "principal_id": random.choice(gids),
                                "created_at": now, "updated_at": now})

            # resource grants: split across user (sandbox), group, role principals
            for k in range(args.grants_per_org):
                mod = k % 3
                if mod == 0:
                    ptype, pid = "user", sid
                elif mod == 1:
                    ptype, pid = "group", random.choice(gids)
                else:
                    ptype, pid = "role", random.choice(rids)
                grant_rows.append({
                    "id": _uid(), "organization_id": oid,
                    "resource_type": random.choice(["data_source", "connection"]),
                    "resource_id": _uid(), "principal_type": ptype, "principal_id": pid,
                    "permissions": random.sample(GRANT_PERMS, k=random.randint(1, 3)),
                    "created_at": now, "updated_at": now})

        await _bulk(db, Group.__table__, group_rows)
        await _bulk(db, GroupMembership.__table__, gm_rows)
        await _bulk(db, Role.__table__, role_rows)
        await _bulk(db, RoleAssignment.__table__, ra_rows)
        await _bulk(db, ResourceGrant.__table__, grant_rows)
        print(f"  groups={len(group_rows)} group_memberships={len(gm_rows)} "
              f"roles={len(role_rows)} role_assignments={len(ra_rows)} "
              f"resource_grants={len(grant_rows)}")

        # ---- spread extra users across orgs (bloat shared tables) ---------
        extra_mem = []
        for uid in user_ids:
            for oid in random.sample(org_ids, k=min(args.user_memberships_per_user, len(org_ids))):
                extra_mem.append({"id": _uid(), "user_id": uid, "organization_id": oid,
                                  "role": "member", "created_at": now, "updated_at": now})
        await _bulk(db, Membership.__table__, extra_mem)
        print(f"  extra memberships={len(extra_mem)}")

        # summary of what sandbox whoami now traverses
        total_orgs = (await db.execute(
            select(Membership).where(Membership.user_id == sid))).scalars().all()
        print(f"[seed:{tag}] DONE. sandbox is now a member of {len(total_orgs)} orgs "
              f"(whoami loops over each).")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
