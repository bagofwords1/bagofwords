"""Extend seed_org.py --sqlite-sources 1 output with a deterministic ERD fixture.

Run with the backend venv. The JSON seed file (auth credentials) stays outside
of the repository. No LLM or external database is used.
"""
import argparse
import json
import sqlite3
import httpx
from seed_org import register, login, pending_invite_token

p = argparse.ArgumentParser()
p.add_argument('--seed', required=True)
p.add_argument('--app-db', required=True)
p.add_argument('--extra-tables', type=int, default=120)
a = p.parse_args()
s = json.load(open(a.seed))
ds = s['sqlite_sources'][0]
with httpx.Client(base_url=s['base_url']) as client:
    s['admin']['token'] = login(client, s['admin']['email'], s['admin']['password'])
with sqlite3.connect(ds['db_file']) as db:
    db.executescript('''
    CREATE TABLE IF NOT EXISTS customers (id INTEGER PRIMARY KEY, name TEXT, email TEXT);
    CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id), total REAL, created_at TEXT);
    CREATE TABLE IF NOT EXISTS line_items (id INTEGER PRIMARY KEY, order_id INTEGER REFERENCES orders(id), product_id INTEGER REFERENCES products(id), quantity INTEGER);
    CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY, name TEXT, price REAL);
    CREATE TABLE IF NOT EXISTS employees (id INTEGER PRIMARY KEY, manager_id INTEGER REFERENCES employees(id), name TEXT);
    CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY, order_id INTEGER REFERENCES orders(id), amount REAL);
    ''')
    for i in range(a.extra_tables):
        db.execute(f'CREATE TABLE IF NOT EXISTS archive_{i:03d} (id INTEGER PRIMARY KEY, value TEXT)')
headers = {'Authorization': 'Bearer ' + s['admin']['token'], 'X-Organization-Id': s['organization']['id']}
with httpx.Client(base_url=s['base_url'], headers=headers, timeout=120) as c:
    c.get(f"/api/data_sources/{ds['id']}/refresh_schema").raise_for_status()
    c.put('/api/organization/onboarding', json={'dismissed': True, 'completed': False}).raise_for_status()
    c.put(f"/api/data_sources/{ds['id']}", json={'name': 'Commerce', 'is_public': True}).raise_for_status()
    email = 'erd-reader@example.com'
    invite = c.post(f"/api/organizations/{s['organization']['id']}/members", json={'organization_id': s['organization']['id'], 'email': email, 'role': 'member'})
    if invite.status_code not in (200, 400, 409): invite.raise_for_status()
    register(c, 'ERD reader', email, s['admin']['password'], pending_invite_token(a.app_db, email))
    s['reader'] = {'email': email, 'token': login(c, email, s['admin']['password'])}
    c.post(f"/api/data_sources/{ds['id']}/bulk_update_tables", json={'action': 'deactivate'}).raise_for_status()
    c.put(f"/api/data_sources/{ds['id']}/update_tables_status", json={'activate': ['orders'], 'deactivate': []}).raise_for_status()
# SQLite's connector currently emits no FK metadata, and the table-status API
# cannot write relationships. Seed this otherwise unreachable metadata state in
# the isolated app DB only; schema loading, permissions and Save remain real.
relations = {'orders': [('customer_id', 'customers')], 'line_items': [('order_id', 'orders'), ('product_id', 'products')], 'payments': [('order_id', 'orders')], 'employees': [('manager_id', 'employees')]}
with sqlite3.connect(a.app_db) as db:
    for name, links in relations.items():
        fks = [{'column': {'name': col}, 'references_name': target, 'references_column': {'name': 'id'}} for col, target in links]
        db.execute('UPDATE datasource_tables SET fks=? WHERE datasource_id=? AND name=?', (json.dumps(fks), ds['id'], name))
    # Synthetic historical rollups cannot be authored through the table API.
    # Seed them only in this isolated fixture, so overlay semantics are testable.
    import uuid
    table_id = db.execute('SELECT id FROM datasource_tables WHERE datasource_id=? AND name=?', (ds['id'], 'orders')).fetchone()[0]
    db.execute('DELETE FROM table_stats WHERE data_source_id=?', (ds['id'],))
    stats = dict(id=str(uuid.uuid4()), org_id=s['organization']['id'], data_source_id=ds['id'], table_fqn='orders', datasource_table_id=table_id,
                 usage_count=24, success_count=22, failure_count=2, pos_feedback_count=8, neg_feedback_count=1,
                 weighted_usage_count=24, weighted_pos_feedback=8, weighted_neg_feedback=1, unique_users=3, trusted_usage_count=20,
                 last_used_at='2026-09-01 10:00:00', updated_at_stats='2026-09-01 10:00:00')
    db.execute(f"INSERT INTO table_stats ({','.join(stats)}) VALUES ({','.join('?' for _ in stats)})", list(stats.values()))
s['archive_count'] = a.extra_tables
json.dump(s, open(a.seed, 'w'), indent=2)
print('Seeded Commerce ERD fixture with', a.extra_tables, 'archive tables')
