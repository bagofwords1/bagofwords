"""Scoped transport for an internal browser preview using the real app endpoints.

The browser never receives a user token. Its allowlisted requests are fulfilled
by this server-side broker; all normal API permission/viewer policies still run.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx

from app.ai.code_execution.query_params import date_range_issue, param_values_equal
from app.schemas.param_schema import parse_param_specs


class PreviewUnavailableError(RuntimeError):
    """The access check could not complete; protected evidence stays withheld."""


class ArtifactPreviewService:
    def __init__(self, runtime_ctx: dict):
        self.ctx = runtime_ctx
        self.report_id = str(runtime_ctx["report"].id)
        self.org_id = str(runtime_ctx["organization"].id)
        self.user_id = str(runtime_ctx["user"].id)
        self.execution_id = str(runtime_ctx.get("agent_execution_id") or "")
        self.artifact = {}
        self.queries = []
        self.query_ids = set()
        self.file_ids = set()
        self.file_urls = set()
        self.events = []
        self.cursor = 0
        self.delivered = 0
        self.action_id = None
        self.actions = {}
        self.expectations = {}
        self.revisions = {}
        self.pending = set()
        self.changed = asyncio.Event()
        self.evidence_id = str(uuid4())
        self.client = None
        self.token = None
        self.operations = 0

    async def open(self, artifact_id: str):
        from app.core.auth import get_jwt_strategy
        from app.settings.config import settings
        from app.services.artifact_verification_policy import require_browser_access
        await require_browser_access(self.ctx, artifact_id)
        # Short-lived token is only kept in this server-side HTTP client. It is
        # never placed in page headers, cookies, URLs, JS, or tool observations.
        strategy = get_jwt_strategy()
        strategy.lifetime_seconds = 600
        self.token = await strategy.write_token(self.ctx["user"])
        # Dispatch through the running application: internal API reads must not
        # depend on the development/container listening port. Import lazily so
        # application startup can finish before any preview is opened.
        from main import app
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://artifact-internal",
            headers={"Authorization": f"Bearer {self.token}", "X-Organization-Id": self.org_id},
            timeout=60, follow_redirects=False, trust_env=False,
        )
        artifact = await self._get(f"/api/artifacts/{artifact_id}")
        if str(artifact.get("report_id")) != self.report_id or artifact.get("mode") != "page":
            raise PermissionError("Preview requires a page artifact in the current report")
        if str(artifact.get("organization_id")) != self.org_id:
            raise PermissionError("Artifact is outside the current organization")
        self.artifact = artifact
        self.file_ids = {str(f.get("id")) for f in artifact.get("content", {}).get("files", [])}
        self.queries = await self._get(f"/api/queries?report_id={self.report_id}&artifact_id={artifact_id}")
        self.query_ids = {str(q["id"]) for q in self.queries}
        self.datasets = [dict(query_id=q["id"], title=q.get("title"),
                              **self.row_counts((q.get("default_step") or {}).get("data") or {})) for q in self.queries]
        option_ids = {str(p["options_source"]["query_id"]) for q in self.queries
                      for p in q.get("parameters", []) or [] if (p.get("options_source") or {}).get("query_id")}
        if option_ids:
            report_queries = await self._get(f"/api/queries?report_id={self.report_id}")
            self.options = [q for q in report_queries if str(q["id"]) in option_ids or q.get("title") in option_ids]
        else:
            self.options = []
        base = os.getenv("BOW_ARTIFACT_PREVIEW_URL") or settings.bow_config.base_url or "http://localhost:3000"
        self.origin = base.rstrip("/").replace("0.0.0.0", "localhost")
        self.url = f"{self.origin}/artifact-preview/{artifact_id}"
        return self

    @staticmethod
    def row_counts(data):
        returned = len(data.get("rows") or [])
        total = (data.get("info") or {}).get("total_rows")
        return {"returned_rows": returned, "total_rows": total,
                "result_truncated": isinstance(total, int) and total > returned}

    async def _get(self, path):
        try:
            response = await self.client.get(path)
        except httpx.RequestError as exc:
            raise PreviewUnavailableError("Preview backend is unavailable; retry verification") from exc
        if response.status_code in {401, 403, 404}:
            raise PermissionError(f"Preview access denied (HTTP {response.status_code})")
        if response.status_code != 200:
            raise PreviewUnavailableError(f"Preview backend is unavailable (HTTP {response.status_code}); retry verification")
        return response.json()

    def record(self, kind: str, **fields):
        self.cursor += 1
        # Bound and sanitize before persistence/model projection.
        raw = json.dumps(fields, default=str)
        if self.token:
            raw = raw.replace(self.token, "[redacted]")
        raw = re.sub(r'(?i)(bearer\s+)[A-Za-z0-9._-]+', r'\1[redacted]', raw)
        if len(raw) > 8000:
            fields = {"message": "Diagnostic exceeded size limit", "truncated": True}
        else:
            fields = json.loads(raw)
        event = {"cursor": self.cursor, "kind": kind, "action_id": self.action_id, **fields}
        if kind == "data_sent":
            self.revisions[fields.get("data_revision")] = set(fields.get("request_ids") or [])
            self.revisions = dict(list(self.revisions.items())[-100:])
        self.events.append(event)
        self.events = self.events[-1000:]
        self.changed.set()
        return event

    def begin_action(self):
        if self.pending:
            raise ValueError("A previous query is still pending; use browser_snapshot with its action ID before another action")
        self.action_id = str(uuid4())
        self.actions[self.action_id] = self.cursor
        return self.action_id

    async def route(self, route):
        request = route.request
        parsed = urlparse(request.url)
        path = parsed.path
        while path.startswith("/api/api/"):
            path = path[4:]
        method = request.method
        if request.url in self.file_urls and method == "GET":
            return await route.continue_()
        if f"{parsed.scheme}://{parsed.netloc}" != self.origin:
            self.record("blocked", path=path, message="External resource is outside preview scope")
            return await route.abort()
        if path.startswith("/api/") and not path.startswith("/api/_nuxt_icon/"):
            try:
                return await self._api(route, path, parse_qs(parsed.query))
            except Exception as e:
                self.record("error", source="transport", message=str(e)[:1500])
                return await route.fulfill(status=502, json={"detail": "Preview request failed"})
        # Only the preview document and app assets; no arbitrary same-origin
        # navigation, write endpoint, service worker, or popup escape.
        allowed = path == f"/artifact-preview/{self.artifact['id']}" or path.startswith(
            ("/_nuxt/", "/__nuxt", "/libs/", "/fonts/", "/api/_nuxt_icon/", "/data_sources_icons/"))
        if method == "GET" and allowed and (not request.is_navigation_request() or
                path == f"/artifact-preview/{self.artifact['id']}"):
            return await route.continue_()
        return await route.fulfill(status=403, json={"detail": "Outside artifact preview scope"})

    async def _api(self, route, path, query):
        req = route.request
        aid = self.artifact["id"]
        result = None
        if req.method == "GET":
            if path == f"/api/artifacts/{aid}":
                result = self.artifact
            elif path == f"/api/artifacts/report/{self.report_id}":
                result = [self.artifact]
            elif path == "/api/queries" and query.get("report_id") == [self.report_id]:
                if query.get("artifact_id") not in (None, [aid]):
                    return await route.fulfill(status=403, json={"detail": "Wrong artifact"})
                result = self.queries if query.get("artifact_id") else self.queries + self.options
            elif path in {f"/api/reports/{self.report_id}", "/api/users/me/viewer_context", "/api/settings", "/api/config/i18n"}:
                result = await self._get(path)
            elif any(path == f"/api/files/{fid}/embed_token" for fid in self.file_ids):
                result = await self._get(path)
                if result.get("url"):
                    from urllib.parse import urljoin
                    self.file_urls.add(urljoin(self.origin, result["url"]))
            if result is not None:
                return await route.fulfill(json=result)
        match = re.fullmatch(r"/api/queries/([^/]+)/run", path)
        if req.method == "POST" and match and match[1] in self.query_ids:
            body = req.post_data_json or {}
            if set(body) - {"mode", "params", "force_refresh"} or body.get("mode") != "viewer":
                return await route.fulfill(status=403, json={"detail": "Only declared viewer parameter runs are permitted"})
            self.operations += 1
            if self.operations > 40:
                return await route.fulfill(status=429, json={"detail": "Preview query budget exceeded"})
            request_id = str(uuid4())
            action_id = self.action_id
            self.pending.add(request_id)
            self.record("query_started", query_id=match[1], request_id=request_id, submitted_params=body.get("params", {}))
            started = time.monotonic()
            try:
                response = await self.client.post(path, json=body, headers={"X-Request-ID": request_id})
                result = response.json()
                self.record("query", action_id=action_id, query_id=match[1], request_id=request_id,
                            operation_kind="query", execution_mode="viewer_query",
                            submitted_params=body.get("params", {}), applied_params=result.get("applied_params"),
                            status=result.get("status", "error"), http_status=response.status_code,
                            cached=result.get("cached"), step_id=result.get("step_id"),
                            **self.row_counts(result.get("data") or {}),
                            duration_ms=round((time.monotonic() - started) * 1000),
                            error=result.get("error") or result.get("detail"))
                result["verification_request_id"] = request_id
                return await route.fulfill(status=response.status_code, json=result)
            finally:
                self.pending.discard(request_id)
                self.changed.set()
        self.record("blocked", path=path, message="Operation is outside read-only preview scope")
        return await route.fulfill(status=403, json={"detail": "Operation is outside read-only preview scope"})

    async def wait_ready(self, timeout=10):
        deadline = time.monotonic() + timeout
        while not any(e["kind"] == "data_received" for e in self.events):
            if any(e["kind"] == "error" for e in self.events):
                return "errors_observed"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "pending"
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), remaining)
            except asyncio.TimeoutError:
                return "pending"
        return "ready"

    def validate_expectation(self, expectation):
        params = expectation.get("params", {})
        if not isinstance(params, dict):
            raise ValueError("Expected params must be an object")
        if "query_ids" in expectation:
            ids = set(expectation.get("query_ids") or [])
        else:
            # Derive affected IDs from the authorized manifest. The model can
            # specify the intended filter values without retyping UUIDs.
            declared = {q["id"]: {p["name"] for p in q.get("parameters", []) or []
                                    if p.get("source") != "identity"} for q in self.queries}
            names = set(params)
            known = set().union(*declared.values()) if declared else set()
            if not names or not names <= known:
                raise ValueError("Expected params must name declared input parameters")
            ids = {qid for qid, fields in declared.items() if names & fields}
        if not ids or not ids <= self.query_ids:
            raise ValueError("Expected query IDs must belong to this artifact")
        return {"query_ids": sorted(ids), "params": params}

    async def wait(self, action_id=None, expectation=None, timeout=15, since=None):
        # Explicit action checks retain their history; bare snapshots describe
        # only the evidence they will deliver, not every past page failure.
        since = self.actions[action_id] if action_id else (self.delivered if since is None else since)
        if expectation is not None:
            self.expectations[action_id] = self.validate_expectation(expectation)
        expectation = self.expectations.get(action_id) or {}
        ids = set(expectation.get("query_ids") or [])
        expected_params = expectation.get("params") or {}
        deadline = time.monotonic() + timeout
        # Give debounced controls an opportunity to commit, using the event
        # signal thereafter. This is browser settling, not a model polling loop.
        grace = time.monotonic() + (0.7 if action_id else 0)
        while True:
            events = [e for e in self.events if e["cursor"] > since and (not action_id or e.get("action_id") == action_id)]
            completed = {e["query_id"] for e in events if e["kind"] == "query"}
            committed = any(e["kind"] in {"params_commit", "query_started"} for e in events)
            queries = [e for e in events if e["kind"] == "query" and (not ids or e["query_id"] in ids)]
            declared = {q["id"]: {p.name: p for p in parse_param_specs(q.get("parameters"))} for q in self.queries}
            mismatched = False
            for event in queries:
                specs = declared.get(event["query_id"])
                applied = event.get("applied_params") or {}
                for name, expected in expected_params.items():
                    if specs is not None and name not in specs:
                        continue
                    spec = (specs or {}).get(name)
                    matches = param_values_equal(spec, expected, applied.get(name)) if spec else applied.get(name) == expected
                    if name not in applied or not matches:
                        mismatched = True
            acknowledged = set().union(*(self.revisions.get(e.get("data_revision"), set())
                                         for e in events if e["kind"] == "data_received"))
            received = bool(queries) and all(e.get("request_id") in acknowledged for e in queries)
            failed = any(e["kind"] == "error" or (e["kind"] == "query" and e.get("status") != "success") for e in events)
            if failed:
                return "failed"
            if mismatched:
                return "parameter_mismatch"
            if not self.pending and ids <= completed and received:
                return "data_received"
            if not ids and not committed and time.monotonic() >= grace and not self.pending:
                return "no_query_observed"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if self.pending:
                    return "pending"
                return "no_expected_request" if ids - completed or not completed else "data_not_acknowledged"
            self.changed.clear()
            try:
                await asyncio.wait_for(self.changed.wait(), min(remaining, max(0.05, grace-time.monotonic())) if not ids and not committed else remaining)
            except asyncio.TimeoutError:
                pass

    def evidence(self, since=None, update_status=None):
        start = self.delivered if since is None else since
        delta = [e for e in self.events if e["cursor"] > start]
        self.delivered = self.cursor
        result_checks = []
        date_names = {q["id"]: {p["name"] for p in q.get("parameters", []) or []
                               if p.get("type") in {"date", "date_range"}} for q in self.queries}
        range_names = {q["id"]: {p["name"] for p in q.get("parameters", []) or []
                                if p.get("type") == "date_range"} for q in self.queries}
        range_messages = {
            "mixed_date_range": "Range bounds mix calendar dates, naive timestamps or timezone-aware timestamps. Confirm the source timezone and endpoint semantics before verifying; do not infer conversions.",
            "reversed_date_range": "Range starts after it ends. Check for an intermediate picker state and verify the final range against known data; do not silently swap bounds.",
            "invalid_date_range": "Range does not contain valid ISO bounds. Inspect the declared parameter and submitted values before verifying.",
        }
        for event in delta:
            if event["kind"] != "query":
                continue
            # Execution remains backward compatible; verification independently
            # diagnoses questionable ranges, even when they return nonempty data.
            values = event.get("applied_params")
            if values is None:
                values = event.get("submitted_params") or {}
            for name in sorted(range_names.get(event["query_id"], set())):
                issue = date_range_issue(values.get(name))
                if issue:
                    result_checks.append({"code": issue, "status": "inconclusive", "query_id": event["query_id"],
                                          "parameters": [name], "message": range_messages[issue]})
            if event.get("status") != "success":
                continue
            if event.get("result_truncated"):
                result_checks.append({"code": "partial_result", "status": "inconclusive", "query_id": event["query_id"],
                                      "message": "Returned rows are partial. Do not verify full totals from these rows; use backend aggregates or label the app's scope."})
            active_dates = [name for name in date_names.get(event["query_id"], set())
                            if (event.get("applied_params") or {}).get(name) is not None]
            if active_dates and event.get("returned_rows") == 0:
                result_checks.append({"code": "empty_date_result", "status": "inconclusive", "query_id": event["query_id"],
                                      "parameters": sorted(active_dates),
                                      "message": "A date-filtered query returned zero rows. This does not prove the filter works. Test a range containing a known record and inspect the query's date-bound handling before claiming success."})
        return {
            "result_checks": result_checks,
            "warnings": [e for e in delta if e["kind"] == "warning"][-10:],
            "queries": [e for e in delta if e["kind"] == "query"][-20:],
            "errors": [e for e in delta if e["kind"] in {"error", "blocked"}][-10:],
            "runtime": [e for e in delta if e["kind"] in {"params_commit", "data_sent", "data_received", "params_status"}][-20:],
            "pending": sorted(self.pending), "update_status": update_status,
            "truncated": len(delta) > 50 or (bool(self.events) and start < self.events[0]["cursor"] - 1),
        }

    def identity(self):
        code = self.artifact.get("content", {}).get("code", "")
        return {"id": self.artifact["id"], "version": self.artifact["version"],
                "code_hash": hashlib.sha256(code.encode()).hexdigest()}

    async def close(self):
        if self.client:
            await self.client.aclose()
        self.token = None
