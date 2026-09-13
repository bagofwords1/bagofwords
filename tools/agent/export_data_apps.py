"""Download HTML/PDF through authenticated export routes for the sandbox fixtures."""

import json, httpx, os
from pathlib import Path

run = Path(os.environ.get("BOW_DATA_APP_RUN", "/tmp/bow-data-app-run"))
s = json.loads((run / "session.json").read_text())
apps = json.loads((run / "apps/manifest.json").read_text())
apps["legacy"] = json.loads((run / "legacy-manifest.json").read_text())["legacy"]
c = httpx.Client(
    base_url=s["base_url"],
    headers={
        "Authorization": "Bearer " + s["admin"]["token"],
        "X-Organization-Id": s["organization"]["id"],
    },
    timeout=180,
)
for slug, app in apps.items():
    r = c.get("/api/artifacts/" + app["artifact_id"] + "/export/html")
    r.raise_for_status()
    (run / (slug + "-export.html")).write_bytes(r.content)
    print(slug, "HTML bytes", len(r.content), flush=True)
    if slug in ("commerce", "legacy"):
        r = c.get("/api/artifacts/" + app["artifact_id"] + "/export/pdf")
        r.raise_for_status()
        assert r.content.startswith(b"%PDF")
        (run / (slug + "-export.pdf")).write_bytes(r.content)
        print(slug, "PDF bytes", len(r.content), flush=True)
