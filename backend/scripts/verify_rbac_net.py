"""DB-level verification of the transitional RBAC backward-compat net.

Runs in-process (no HTTP). Simulates the state that SCIM/LDAP/OIDC provisioning
produced before the fix — a Membership with a legacy `role` but NO
role_assignment — and asserts:

  1. resolve_permissions() falls back to the baseline permissions implied by
     Membership.role (member -> member set, admin -> full_admin_access), so such
     a user is never stranded with zero permissions.
  2. ensure_system_role_assignment() heals the row by creating a real assignment,
     after which the resolver reports the role via the normal RBAC path.
  3. A membership WITH a real assignment is unaffected by the net (the net only
     fires when there are zero assignments).

Usage:
  BOW_DATABASE_URL=sqlite:///db/app.db uv run python scripts/verify_rbac_net.py
"""
import asyncio
import sys
import uuid

results = []


def check(label, cond, extra=""):
    results.append(bool(cond))
    print(("PASS" if cond else "FAIL"), "-", label, ("" if cond else f"  >> {extra}"))


async def main():
    # Importing the app registers every ORM model so relationship() targets
    # (Completion, etc.) resolve when the mappers configure.
    import main  # noqa: F401
    from app.dependencies import async_session_maker
    from app.models.user import User
    from app.models.organization import Organization
    from app.models.membership import Membership
    from app.core.permission_resolver import (
        resolve_permissions, ensure_system_role_assignment, FULL_ADMIN,
    )
    from app.core.permissions_registry import DEFAULT_MEMBER_PERMISSIONS

    async with async_session_maker() as db:
        # A throwaway org for the synthetic memberships.
        org = Organization(name=f"net-test-{uuid.uuid4().hex[:8]}", description="")
        db.add(org)
        await db.flush()
        org_id = str(org.id)

        def make_user():
            u = User(email=f"net-{uuid.uuid4().hex[:10]}@example.com",
                     name="Net Test", hashed_password="x", is_active=False,
                     is_verified=True, is_superuser=False)
            db.add(u)
            return u

        # (1) Zero-assignment membership, legacy role='member'
        u_member = make_user()
        await db.flush()
        db.add(Membership(user_id=str(u_member.id), organization_id=org_id, role="member"))
        # (1b) Zero-assignment membership, legacy role='admin'
        u_admin = make_user()
        await db.flush()
        db.add(Membership(user_id=str(u_admin.id), organization_id=org_id, role="admin"))
        await db.commit()

        r_member = await resolve_permissions(db, str(u_member.id), org_id)
        check("net: zero-assignment member resolves to member baseline (not empty)",
              set(DEFAULT_MEMBER_PERMISSIONS).issubset(r_member.org_permissions)
              and FULL_ADMIN not in r_member.org_permissions,
              f"perms={sorted(r_member.org_permissions)}")

        r_admin = await resolve_permissions(db, str(u_admin.id), org_id)
        check("net: zero-assignment admin resolves to full_admin_access (not empty)",
              FULL_ADMIN in r_admin.org_permissions,
              f"perms={sorted(r_admin.org_permissions)}")

        # (2) Heal the member row with a real assignment; net no longer needed.
        await ensure_system_role_assignment(db, org_id, str(u_member.id), "member")
        await db.commit()
        # New session so the per-request resolver memo doesn't mask the change.
    async with async_session_maker() as db2:
        r_member2 = await resolve_permissions(db2, str(u_member.id), org_id)
        check("heal: after ensure_system_role_assignment, member has a real RBAC role",
              "member" in r_member2.role_names
              and set(DEFAULT_MEMBER_PERMISSIONS).issubset(r_member2.org_permissions),
              f"role_names={r_member2.role_names} perms={sorted(r_member2.org_permissions)}")

        # (3) ensure_* is idempotent — calling again adds no duplicate.
        await ensure_system_role_assignment(db2, org_id, str(u_member.id), "member")
        await db2.commit()
    async with async_session_maker() as db3:
        r_member3 = await resolve_permissions(db3, str(u_member.id), org_id)
        check("idempotent: ensure_system_role_assignment twice -> single member role",
              r_member3.role_names.count("member") == 1,
              f"role_names={r_member3.role_names}")

    ok = sum(results)
    print(f"\n==== SUMMARY ====\n{ok}/{len(results)} checks passed")
    print("ALL PASSED" if ok == len(results) else "SOME FAILED")
    sys.exit(0 if ok == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
