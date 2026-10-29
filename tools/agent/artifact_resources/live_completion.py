"""Paid full-completion browser fixtures. Requires explicit approval and an env key.

No app source, resource schemas, SDK hints or expected outcomes are supplied to
completions. Generated reports stay in the disposable running sandbox for UI QA.
"""

import asyncio
import json
import os
import time
from pathlib import Path

import httpx

PROMPTS = {
    "reading": "Can you make me a little reading list? I keep losing articles I want to read. Let me save them and mark them done.",
    "notes": "Make a little app where I can jot down project notes and come back to them later.",
    "documents": "I want a small app: drop in a document, get an AI summary as it writes, and keep the summaries I like.",
    "blog": "Make me a simple blog site. I want to write posts, keep drafts, and publish when ready.",
}


async def main():
    if os.getenv("ARTIFACT_LIVE_EVAL_APPROVED") != "true":
        raise RuntimeError("Explicit real-model evaluation approval is required")
    key = os.environ.get("OPENAI_API_KEY_TEST", "")
    provider_id = os.environ.get("ARTIFACT_LIVE_PROVIDER_ID")
    if not provider_id and not key:
        raise RuntimeError(
            "Supply a test key or an existing dedicated evaluation provider"
        )
    cases = os.getenv("ARTIFACT_LIVE_CASES", "documents").split(",")
    out = Path(os.getenv("ARTIFACT_LIVE_OUTPUT", "/tmp/artifact-live-browser.json"))
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8108", timeout=900
    ) as client:
        login = await client.post(
            "/api/auth/jwt/login",
            data={
                "username": "artifact-author@example.com",
                "password": "Password123!",
            },
        )
        login.raise_for_status()
        headers = {"Authorization": "Bearer " + login.json()["access_token"]}
        who = await client.get("/api/users/whoami", headers=headers)
        who.raise_for_status()
        headers["X-Organization-Id"] = who.json()["organizations"][0]["id"]
        if not provider_id:
            response = await client.post(
                "/api/llm/providers",
                headers=headers,
                json={
                    "name": "Live artifact evaluation " + str(time.time_ns()),
                    "provider_type": "openai",
                    "credentials": {"api_key": key},
                    "models": [
                        {"model_id": model, "name": model, "is_custom": True}
                        for model in ("gpt-6-luna", "gpt-6-sol")
                    ],
                },
            )
            response.raise_for_status()
            provider_id = response.json()["id"]
        response = await client.get("/api/llm/models", headers=headers)
        response.raise_for_status()
        models = [m for m in response.json() if m["provider_id"] == provider_id]
        results = []

        async def run(model, case):
            model_row = next(m for m in models if m["model_id"] == model)
            r = await client.post(
                "/api/reports",
                headers=headers,
                json={
                    "title": f"{model} · {case} · live evaluation",
                    "files": [],
                    "data_sources": [],
                    "model_id": model_row["id"],
                },
            )
            r.raise_for_status()
            report = r.json()
            result = {
                "model": model,
                "case": case,
                "prompt": PROMPTS[case],
                "report_id": report["id"],
                "status": "running",
            }
            results.append(result)
            out.write_text(json.dumps(results, indent=2))
            start = time.monotonic()
            try:
                r = await client.post(
                    f"/api/reports/{report['id']}/completions",
                    params={"background": "false"},
                    headers=headers,
                    json={
                        "prompt": {
                            "content": PROMPTS[case],
                            "widget_id": None,
                            "step_id": None,
                            "mentions": [{}],
                        }
                    },
                )
                r.raise_for_status()
                result["completion"] = r.json()
                assert all(c["status"] != "error" for c in r.json()["completions"]), (
                    "Completion failed; inspect its recorded result"
                )
                r = await client.get(
                    f"/api/artifacts/report/{report['id']}/latest", headers=headers
                )
                r.raise_for_status()
                result["artifact"] = r.json()
                base = f"/api/artifacts/{result['artifact']['artifact_id']}/runtime"
                r = await client.get(base + "/resources", headers=headers)
                r.raise_for_status()
                result["resources"] = r.json()["items"]
                assert result["artifact"]["status"] == "completed"
                assert result["artifact"]["content"].get("sdk_version") == 1
                expected = (
                    {"collection", "files", "ai"}
                    if case == "documents"
                    else {"collection"}
                )
                assert expected <= {d["kind"] for d in result["resources"]}
                result["status"] = "generated"
            except Exception as exc:
                result["status"] = "failed"
                result["error"] = (
                    str(exc).replace(key, "[REDACTED]") if key else str(exc)
                )
            result["elapsed_seconds"] = round(time.monotonic() - start, 1)
            out.write_text(json.dumps(results, indent=2))
            print(model, case, result["status"], result["elapsed_seconds"], flush=True)

        # Two independent reports; completion/planner/tools remain fully normal.
        for case in cases:
            await asyncio.gather(
                *(run(model, case) for model in ("gpt-6-luna", "gpt-6-sol"))
            )
        assert all(r["status"] == "generated" for r in results), (
            "One or more completion cases failed"
        )


if __name__ == "__main__":
    asyncio.run(main())
