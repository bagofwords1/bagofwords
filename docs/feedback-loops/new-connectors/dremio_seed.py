"""Bootstrap Dremio OSS: first admin user, a space with a CTAS table in $scratch + a curated view."""
import requests, time, sys
B = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9047"
S = requests.Session(); S.trust_env = False
r = S.put(f"{B}/apiv2/bootstrap/firstuser", json={"userName": "admin", "firstName": "a", "lastName": "b", "email": "a@b.co", "createdAt": int(time.time()*1000), "password": "Passw0rd!x"}, headers={"Authorization": "_dremionull"})
print("bootstrap", r.status_code)
tok = S.post(f"{B}/apiv2/login", json={"userName": "admin", "password": "Passw0rd!x"}).json()["token"]
S.headers.update({"Authorization": f"_dremio{tok}", "Content-Type": "application/json"})
def sql(q):
    jid = S.post(f"{B}/api/v3/sql", json={"sql": q}).json()["id"]
    while True:
        j = S.get(f"{B}/api/v3/job/{jid}").json()
        if j["jobState"] in ("COMPLETED", "FAILED", "CANCELED"): break
        time.sleep(0.3)
    if j["jobState"] != "COMPLETED": print("FAILED", q[:60], j.get("errorMessage")); 
    return j
S.post(f"{B}/api/v3/catalog", json={"entityType": "space", "name": "Sales"})
vals = ", ".join(f"({i}, 'c-{i%6}', '{['EMEA','AMER','APAC'][i%3]}', {10+i*1.5}, DATE '2025-0{1+i%6}-{1+i%27:02d}')" for i in range(900))
sql('DROP TABLE IF EXISTS "$scratch".orders')
sql(f'CREATE TABLE "$scratch".orders AS SELECT * FROM (VALUES {vals}) AS t(order_id, customer_id, region, amount, order_date)')
sql('CREATE OR REPLACE VIEW "Sales"."orders_by_region" AS SELECT region, COUNT(*) AS n, SUM(amount) AS revenue FROM "$scratch".orders GROUP BY region')
sql('CREATE OR REPLACE VIEW "Sales"."orders" AS SELECT * FROM "$scratch".orders')
print("seeded")
