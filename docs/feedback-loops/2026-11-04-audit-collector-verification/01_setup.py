import sys, os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
H = hdr(ADMIN["token"])
# LLM provider: anthropic, only haiku
r = api.post("/api/llm/providers", headers=H, json={
    "name": "Anthropic-Haiku", "provider_type": "anthropic",
    "credentials": {"api_key": os.environ["ANTHROPIC_KEY"]},
    "models": [{"name": "Claude 4.5 Haiku", "model_id": "claude-haiku-4-5-20251001", "is_default": True, "is_small_default": True}],
})
print("provider", r.status_code, r.text[:200])
# streams
for name, dest, cfg, sec in [
    ("collector-webhook", "https", {"url": f"{COLL}/webhook"}, {"auth_header_value": "Bearer collector-bearer-token", "hmac_secret": "collector-hmac-secret"}),
    ("collector-hec", "splunk", {"hec_url": f"{COLL}/splunk", "index": "bow", "verify_tls": False}, {"token": "collector-hec-token"}),
]:
    r = api.post("/api/enterprise/audit/streams/test", headers=H, json={"destination": dest, "config": cfg, "secrets": sec})
    print("test", dest, r.status_code, r.text[:200])
    r = api.post("/api/enterprise/audit/streams", headers=H, json={"name": name, "destination": dest, "config": cfg, "secrets": sec, "start_from": "beginning", "activate": True})
    print("create", dest, r.status_code, r.text[:300])
