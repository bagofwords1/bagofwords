#!/usr/bin/env python3
"""Create ~1,000 logs-load*-default data streams (2 small docs each) to test
schema discovery at scale. Streams auto-create via the serverless logs-*-*
template on op_type=create. Env: ES_URL, ES_API_KEY_ENCODED (ES_CA_BUNDLE optional)."""
import json
import os
import ssl
import time
import urllib.request

ES = os.environ["ES_URL"].rstrip("/")
KEY = os.environ["ES_API_KEY_ENCODED"]
CTX = ssl.create_default_context(cafile=os.environ.get("ES_CA_BUNDLE") or None)
N = 1000
PER_BULK = 100  # streams per bulk request (2 docs each)


def bulk(lines):
    body = ("\n".join(lines) + "\n").encode()
    req = urllib.request.Request(
        f"{ES}/_bulk", data=body, method="POST",
        headers={"Content-Type": "application/x-ndjson",
                 "Authorization": f"ApiKey {KEY}"})
    with urllib.request.urlopen(req, timeout=300, context=CTX) as resp:
        out = json.loads(resp.read())
    errs = [i for i in out.get("items", []) if i["create"].get("error")]
    return len(out.get("items", [])), errs


total, t0 = 0, time.time()
for start in range(0, N, PER_BULK):
    lines = []
    for i in range(start, min(start + PER_BULK, N)):
        stream = f"logs-load{i:04d}-default"
        for d in range(2):
            lines.append(json.dumps({"create": {"_index": stream}}))
            lines.append(json.dumps({
                "@timestamp": f"2026-07-14T1{d}:00:00Z",
                "message": f"load test doc {d} for stream {i}",
                "log.level": "info",
                "service.name": f"load{i:04d}",
                "host.name": "host-load",
            }))
    n, errs = bulk(lines)
    total += n
    if errs:
        print(f"bulk errors ({len(errs)}): {json.dumps(errs[:2])[:300]}")
        break
    print(f"  {start + PER_BULK}/{N} streams ({total} docs, {time.time()-t0:.0f}s)")
print(f"DONE: {total} docs across {N} new data streams in {time.time()-t0:.0f}s")
