#!/usr/bin/env bash
# Live proof against the running backend: a real SQLite connection, the real
# /custom-queries/preview route, and the DB file checked afterwards.
set -u
B=http://localhost:8000
TOK=$(curl -s -X POST $B/api/auth/jwt/login -d 'username=admin@example.com&password=Password123!' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
ORG=$(curl -s $B/api/organizations -H "Authorization: Bearer $TOK" | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["id"])')
H=(-H "Authorization: Bearer $TOK" -H "X-Organization-Id: $ORG" -H "Content-Type: application/json")
DB=${DB:-/tmp/bow-shop.db}; rm -f $DB
python3 -c "import sqlite3;c=sqlite3.connect('$DB');c.execute('CREATE TABLE orders(id INTEGER PRIMARY KEY, amount REAL)');c.executemany('INSERT INTO orders VALUES(?,?)',[(i,i*10.0) for i in range(1,6)]);c.commit()"
curl -s -X PUT $B/api/organization/settings "${H[@]}" -d '{"config":{"enable_custom_queries":{"value":true}}}' >/dev/null
CID=$(curl -s -X POST $B/api/connections "${H[@]}" -d "{\"name\":\"shop-$RANDOM\",\"type\":\"sqlite\",\"config\":{\"database\":\"$DB\"},\"credentials\":{},\"auth_policy\":\"system_only\"}" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("id") or d)')
count(){ python3 -c "import sqlite3;print(sqlite3.connect('$DB').execute('SELECT COUNT(*) FROM orders').fetchone()[0])" 2>&1 | tail -1; }
echo "orders rows before: $(count)"
for SQL in "SELECT id, amount FROM orders ORDER BY id" "SELECT 1; DELETE FROM orders" "DELETE FROM orders" "DROP TABLE orders"; do
  R=$(curl -s -w ' [HTTP %{http_code}]' -X POST "$B/api/connections/$CID/custom-queries/preview" "${H[@]}" -d "{\"definition_sql\":\"$SQL\"}")
  echo "POST preview  $SQL"
  echo "  -> ${R:0:170}"
  echo "  orders rows now: $(count)"
done
