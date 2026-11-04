import sys, os, json, time, sqlite3; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
rid = json.load(open(os.path.join(S, "agent_run.json")))["report_id"]
DS = SEED["demo_data_source"]["data_source_id"]
UA = "audit-loop/access-coverage"; SPOOF = "203.0.113.77"
H = {**hdr(ADMIN["token"], ua=UA), "X-Forwarded-For": SPOOF}
base = stats()["deliveries"]; t_start = time.time(); out = []
def do(label, method, path, **kw):
    r = api.request(method, path, headers=kw.pop("headers", H), **kw); out.append((label, method, path, r.status_code)); return r
do("list reports", "GET", "/api/reports")
do("read report", "GET", f"/api/reports/{rid}")
do("read report completions", "GET", f"/api/reports/{rid}/completions")
do("read data source", "GET", f"/api/data_sources/{DS}")
do("read data source schema", "GET", f"/api/data_sources/{DS}/schema")
do("read data source tables", "GET", f"/api/data_sources/{DS}/tables")
q = do("create query", "POST", "/api/queries", json={"title": "audit loop query", "report_id": rid})
qid = q.json().get("id") if q.status_code == 200 else None
if qid:
    do("read query", "GET", f"/api/queries/{qid}")
    do("run query (builder)", "POST", f"/api/queries/{qid}/run", json={"code": "SELECT COUNT(*) AS n FROM Invoice", "title": "count"})
    do("run query (viewer)", "POST", f"/api/queries/{qid}/run", json={"mode": "viewer"})
    do("preview query", "POST", f"/api/queries/{qid}/preview", json={"code": "SELECT 1"})
f = do("upload file", "POST", "/api/files", files={"file": ("audit-loop.csv", b"a,b\n1,2\n", "text/csv")})
fid = f.json().get("id") if f.status_code == 200 else None
if fid:
    do("download file", "GET", f"/api/files/{fid}/content")
    do("download file again (cached read)", "GET", f"/api/files/{fid}/content")
do("publish report", "POST", f"/api/reports/{rid}/publish")
do("public report read (anonymous)", "GET", f"/api/r/{rid}", headers={"User-Agent": UA})
do("public report queries (anonymous)", "GET", f"/api/r/{rid}/queries", headers={"User-Agent": UA})
do("export report html (anonymous)", "GET", f"/api/r/{rid}/export_html", headers={"User-Agent": UA})
do("audit export json", "GET", "/api/enterprise/audit/export", params={"format": "json"})
k = do("create api key", "POST", "/api/api_keys", json={"name": "audit-loop-key"})
key_plain = (k.json() or {}).get("key") or (k.json() or {}).get("token") if k.status_code == 200 else None
do("list api keys", "GET", "/api/api_keys")
print("HTTP:"); [print(f"  {s:>3}  {m:6} {p:55} {l}") for l, m, p, s in out]
time.sleep(14)
got = [e for e in events(since=base) if not e["dup"]]
print(f"\ncollector new unique events: {len(got)}")
for e in got:
    ev = e["event"]; print(f"  {ev['action']:35} actor={ev['actor']['type']}:{ev['actor']['email']} ip={ev['context']['ip_address']} ua={ev['context']['user_agent']} targets={ev['targets']} meta={json.dumps(ev['metadata'])[:160]}")
# secret scan across everything the collector has
blob = open(os.path.join(S, "collector-state", "events.jsonl")).read()
scan = {"anthropic_api_key": os.environ["ANTHROPIC_KEY"] in blob, "admin_jwt": ADMIN["token"] in blob, "api_key_plaintext": bool(key_plain) and key_plain in blob,
        "collector_bearer": "collector-bearer-token" in blob, "hmac_secret": "collector-hmac-secret" in blob, "hec_token": "collector-hec-token" in blob,
        "literal 'password'": "assword" in blob, "member_new_password": "NewPassword456!" in blob}
print("\nsecret scan over all collected events:", scan)
json.dump({"http": out, "events": [e["event"] for e in got], "scan": scan, "query_id": qid, "file_id": fid}, open(os.path.join(S, "07_result.json"), "w"), indent=1, default=str)
