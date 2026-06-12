from typing import Optional

from .base import WebhookAdapter


class GitHubAdapter(WebhookAdapter):
    """GitHub repo/org webhooks. Native HMAC via X-Hub-Signature-256 over the raw body."""

    source = "github"

    def is_handshake(self, headers: dict, payload: dict) -> bool:
        return (headers.get("x-github-event") or "").lower() == "ping"

    def verify_hmac(self, secret: str, raw_body: bytes, headers: dict) -> bool:
        sig = headers.get("x-hub-signature-256") or ""
        expected = "sha256=" + self._hexsig(secret, raw_body)
        return self._ct_eq(sig, expected)

    def event_id(self, headers: dict, payload: dict) -> Optional[str]:
        return headers.get("x-github-delivery")

    def normalize(self, headers: dict, payload: dict) -> dict:
        event = (headers.get("x-github-event") or "event").lower()
        action = payload.get("action") or ""
        event_key = f"{event}.{action}" if action else event
        repo = (payload.get("repository") or {}).get("full_name") or "?"

        summary = f"GitHub {event_key}"
        details_lines = [f"event: {event_key}", f"repo: {repo}"]

        pr = payload.get("pull_request")
        issue = payload.get("issue")
        if pr:
            author = (pr.get("user") or {}).get("login") or "?"
            title = pr.get("title") or ""
            summary = f"PR {action or event}: {title} — by {author}"
            details_lines += [
                f"pr_title: {title}",
                f"pr_author: {author}",
                f"pr_url: {pr.get('html_url') or ''}",
                f"base: {(pr.get('base') or {}).get('ref') or ''}",
                f"head: {(pr.get('head') or {}).get('ref') or ''}",
                f"body: {(pr.get('body') or '')[:1500]}",
            ]
        elif issue:
            author = (issue.get("user") or {}).get("login") or "?"
            title = issue.get("title") or ""
            summary = f"Issue {action or event}: {title} — by {author}"
            details_lines += [
                f"issue_title: {title}",
                f"issue_author: {author}",
                f"issue_url: {issue.get('html_url') or ''}",
                f"body: {(issue.get('body') or '')[:1500]}",
            ]
        elif event == "push":
            pusher = (payload.get("pusher") or {}).get("name") or "?"
            ref = payload.get("ref") or ""
            n = len(payload.get("commits") or [])
            summary = f"Push to {ref} by {pusher} ({n} commits)"
            details_lines += [f"ref: {ref}", f"pusher: {pusher}", f"commits: {n}"]

        return {
            "summary": summary,
            "details": "\n".join(details_lines),
            "raw": payload,
            "event_key": event_key,
        }
