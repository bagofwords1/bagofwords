"""Seed the Postgres scale test DB: 100 users, 500 reports (asyncpg)."""
import asyncio, uuid, datetime
import asyncpg

DSN = 'postgresql://bow@127.0.0.1:5433/bow_load'
N_USERS, N_REPORTS = 100, 500


async def main():
    conn = await asyncpg.connect(DSN)
    org_id = await conn.fetchval("SELECT id FROM organizations LIMIT 1")
    pw_hash = await conn.fetchval("SELECT hashed_password FROM users WHERE email='loadadmin@test.com'")
    now = datetime.datetime.utcnow()
    old = now - datetime.timedelta(days=10)

    user_ids = []
    for i in range(N_USERS):
        uid = str(uuid.uuid4())
        await conn.execute(
            "INSERT INTO users (id, name, email, hashed_password, is_active, is_superuser, is_verified, is_service_account) "
            "VALUES ($1, $2, $3, $4, true, false, true, false)",
            uid, f"Load User {i}", f"load_u{i}@test.com", pw_hash)
        await conn.execute(
            "INSERT INTO memberships (id, user_id, organization_id, role, email, created_at, updated_at) "
            "VALUES ($1, $2, $3, 'member', $4, $5, $6)",
            str(uuid.uuid4()), uid, org_id, f"load_u{i}@test.com", now, now)
        user_ids.append(uid)

    report_ids = []
    for i in range(N_REPORTS):
        rid = str(uuid.uuid4())
        await conn.execute(
            "INSERT INTO reports (id, title, slug, status, report_type, mode, conversation_share_enabled, "
            "artifact_visibility, conversation_visibility, shared_run_identity, refresh_on_view, "
            "user_id, organization_id, created_at, updated_at, last_activity_at) "
            "VALUES ($1, $2, $3, 'published', 'regular', 'chat', false, 'none', 'none', 'viewer', false, $4, $5, $6, $7, $8)",
            rid, f"Load Report {i}", f"load-report-{i}-{rid[:8]}", user_ids[i % N_USERS], org_id, old, old, old)
        report_ids.append(rid)

    await conn.close()
    print(f"org={org_id} users={len(user_ids)} reports={len(report_ids)}")
    with open('scale_ids.txt', 'w') as f:
        f.write(str(org_id) + '\n' + ','.join(report_ids) + '\n')

if __name__ == '__main__':
    asyncio.run(main())
