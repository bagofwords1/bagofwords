"""Live loop: upload an image on turn 1, ask text-only follow-ups, and record
per turn what the model said, which tools it used, and how many images the
planner request carried (from the backend log)."""
import io, json, os, re, sys, time, httpx
from PIL import Image, ImageDraw, ImageFont

BASE = "http://localhost:8000"
label, key_file, log_file, out = sys.argv[1:5]
key = open(key_file).read().strip()
c = httpx.Client(base_url=BASE, timeout=60)
tok = c.post("/api/auth/jwt/login", data={"username": "admin@example.com", "password": "Password123!"}).json()["access_token"]
org = c.get("/api/organizations", headers={"Authorization": f"Bearer {tok}"}).json()[0]["id"]
H = {"Authorization": f"Bearer {tok}", "X-Organization-Id": org}

provs = c.get("/api/llm/providers", headers=H).json()
if not any(p["provider_type"] == "anthropic" for p in provs):
    r = c.post("/api/llm/providers", headers=H, json={"name": "Anthropic", "provider_type": "anthropic",
               "credentials": {"api_key": key}, "models": []})
    assert r.status_code == 200, r.status_code
models = c.get("/api/llm/models", headers=H).json()
haiku = next(m for m in models if m["model_id"] == "claude-haiku-5-5")
if not haiku.get("is_enabled", True):
    c.post(f"/api/llm/models/{haiku['id']}/toggle", params={"enabled": "true"}, headers=H)
for small in ("false", "true"):
    r = c.post(f"/api/llm/models/{haiku['id']}/set_default?small={small}", headers=H); assert r.status_code == 200, r.text

rep = c.post("/api/reports", headers=H, json={"title": f"image followup {label}", "data_sources": []}).json()
rid = rep["id"]

img = Image.new("RGB", (900, 220), (255, 140, 0))
d = ImageDraw.Draw(img)
d.text((40, 70), "CODE: ZEBRA-7341", fill=(0, 0, 0), font=ImageFont.load_default(size=64))
buf = io.BytesIO(); img.save(buf, "PNG"); buf.seek(0)

TURNS = [
    (True,  "What text is written in this image? Reply with just the text."),
    (False, "What's the capital of France? One word."),
    (False, "Give me one fun fact about octopuses, in one sentence."),
    (False, "Did THIS message (the one you are reading right now) come with an image attached to it? Start your answer with 'ATTACHED: yes' or 'ATTACHED: no', then explain in one sentence."),
    (False, "What was the background color of the image I sent at the start of our chat? One word."),
]

def wait_done():
    for _ in range(240):
        cs = c.get(f"/api/reports/{rid}/completions", headers=H).json()
        items = cs.get("completions", cs) if isinstance(cs, dict) else cs
        last = items[-1]
        if last.get("role") != "user" and last.get("status") not in ("in_progress", "planning", "pending"):
            return last
        time.sleep(2)
    raise SystemExit("timeout")

results = []
for i, (with_img, prompt) in enumerate(TURNS, 1):
    if with_img:
        buf.seek(0)
        r = c.post("/api/files", headers=H, files={"file": ("zebra.png", buf, "image/png")}, data={"report_id": rid})
        assert r.status_code == 200, r.text
    log_start = os.path.getsize(log_file); t0 = time.time()
    r = c.post(f"/api/reports/{rid}/completions?background=true", headers=H, json={"prompt": {"content": prompt, "mentions": [{}]}})
    assert r.status_code == 200, r.text
    last = wait_done()
    time.sleep(1)
    with open(log_file, errors="replace") as f:
        f.seek(log_start); chunk = f.read()
    imgs = [int(x) for x in re.findall(r"\[transcript\] request est_tokens=\d+ images=(\d+)", chunk)]
    text, tools, reasoning = "", [], ""
    for b in last.get("completion_blocks") or []:
        text = (b.get("content") or text)
        reasoning += (b.get("reasoning") or "")
        te = b.get("tool_execution") or {}
        if te.get("tool_name"): tools.append(te["tool_name"])
    results.append({"turn": i, "uploaded_image": with_img, "prompt": prompt,
                    "images_per_llm_call": imgs, "tools": tools,
                    "answer": text.strip()[:300], "reasoning": reasoning.strip()[:400],
                    "status": last.get("status"), "t0": t0, "t1": time.time()})
    print(json.dumps(results[-1], ensure_ascii=False), flush=True)
plog = sys.argv[5] if len(sys.argv) > 5 else None
if plog:
    reqs = [json.loads(l) for l in open(plog)]
    for t in results:
        rs = [r for r in reqs if t["t0"] <= r["t"] <= t["t1"]]
        t["llm_requests"] = len(rs)
        t["requests_with_image"] = sum(1 for r in rs if r["images"])
        t["image_positions"] = sorted({(p["msg_from_end"], p["role"]) for r in rs for p in r["images"]})
        print("TURN", t["turn"], "upload" if t["uploaded_image"] else "no-upload", "| requests:", t["llm_requests"], "| with image:", t["requests_with_image"], t["image_positions"], "| tools:", t["tools"], "|", repr(t["answer"][:150]))
json.dump({"label": label, "report_id": rid, "turns": results}, open(out, "w"), indent=1, ensure_ascii=False)
