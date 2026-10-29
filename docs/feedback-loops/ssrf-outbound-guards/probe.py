import json, sys, httpx
d = json.load(open(sys.argv[1]))
H = {"Authorization": f"Bearer {d['admin']['token']}", "X-Organization-Id": d["organization"]["id"]}
c = httpx.Client(base_url="http://localhost:8000/api", headers=H, timeout=60)
def show(label, r):
    try: body = r.json()
    except Exception: body = r.text
    print(f"{label:58s} -> {r.status_code} {json.dumps(body)[:150]}")
for url in ["http://169.254.169.254/v1", "http://[fd00:ec2::254]/v1", "http://metadata.google.internal/v1", "file:///etc/passwd", "http://127.0.0.1:9999/llm-private-ok/v1"]:
    show(f"LLM custom base_url {url}", c.post("/llm/test_connection", json={"name":"t","provider_type":"custom","credentials":{"base_url":url,"api_key":"x"},"models":[{"model_id":"m","name":"m","is_custom":True,"is_enabled":True}]}))
for url in ["file:///etc", "/home/user/bagofwords", "ext::sh -c touch% /tmp/pwned", "https://169.254.169.254/x.git", "http://127.0.0.1:9999/git-private-ok.git"]:
    show(f"git test {url}", c.post("/git/repositories/test", json={"provider":"github","repo_url":url,"branch":"main"}))
for url in ["https://169.254.169.254/latest/meta-data", "http://127.0.0.1:9999/audit-private-ok"]:
    show(f"audit stream test {url}", c.post("/enterprise/audit/streams/test", json={"destination":"https","config":{"url":url}}))
