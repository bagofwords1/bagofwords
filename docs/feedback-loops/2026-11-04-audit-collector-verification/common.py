import json, os, time, httpx
S = os.path.dirname(os.path.abspath(__file__))
SEED = json.load(open(os.path.join(S, "seed.json")))
BASE = "http://localhost:8000"
COLL = "http://127.0.0.1:8790"
ORG = SEED["organization"]["id"]
ADMIN = SEED["admin"]
MEMBER = SEED["members"][0]
def hdr(token, org=ORG, ua="audit-loop/1"):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": org, "User-Agent": ua}
api = httpx.Client(base_url=BASE, timeout=120)
coll = httpx.Client(base_url=COLL, timeout=60)
def login(email, password):
    r = api.post("/api/auth/jwt/login", data={"username": email, "password": password}); r.raise_for_status(); return r.json()["access_token"]
def stats(): return coll.get("/_control/stats").json()
def events(prefix="", since=0): return coll.get("/_control/events", params={"action": prefix, "since": since}).json()
def wait_for(pred, timeout=60, every=1.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = pred()
        if v: return v
        time.sleep(every)
    return None
def wait_action(action, n=1, timeout=45, since=0):
    return wait_for(lambda: (lambda ev: ev if len(ev) >= n else None)([e for e in events(action, since) if not e["dup"]]), timeout)
def pp(x): print(json.dumps(x, indent=1, default=str))
