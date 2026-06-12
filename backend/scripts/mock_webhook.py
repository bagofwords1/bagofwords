#!/usr/bin/env python3
"""Mock inbound-webhook sender — the reference signer for testing BOW webhooks.

Simulates GitHub / Jira / generic sources hitting the receiver with a correctly
signed (or token-authed) body, so the full path can be exercised without a real
external system.

Examples:
  # generic HMAC
  python scripts/mock_webhook.py --url http://localhost:8000/webhooks/whk_xxx --secret whsec_yyy
  # github HMAC
  python scripts/mock_webhook.py --url ... --secret ... --source github
  # token mode (Jira Cloud / legacy)
  python scripts/mock_webhook.py --url ... --secret ... --auth-mode token
  # url_token mode (Jira Server / dumb POST)
  python scripts/mock_webhook.py --url ... --secret ... --auth-mode url_token
"""
import argparse
import hashlib
import hmac
import json
import sys
import time
import urllib.request


def build_pr_payload(action: str, title: str) -> dict:
    return {
        "action": action,
        "pull_request": {
            "title": title,
            "html_url": "https://github.com/acme/app/pull/42",
            "user": {"login": "alice"},
            "body": "Refresh tokens were expiring early. This patches the clock skew.",
            "base": {"ref": "main"},
            "head": {"ref": "fix/auth"},
        },
        "repository": {"full_name": "acme/app"},
    }


def build_generic_payload(action: str, title: str) -> dict:
    return {"type": action, "title": title, "detail": "generic webhook event for testing"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--secret", required=True)
    ap.add_argument("--source", default="generic", choices=["github", "jira", "generic"])
    ap.add_argument("--auth-mode", default="hmac", choices=["hmac", "token", "url_token"])
    ap.add_argument("--action", default="opened")
    ap.add_argument("--title", default="Fix auth timeout on token refresh")
    ap.add_argument("--delivery", default="mock-delivery-0001")  # dedup key
    ap.add_argument("--tamper", action="store_true", help="corrupt body after signing (should 401)")
    args = ap.parse_args()

    if args.source == "github":
        payload = build_pr_payload(args.action, args.title)
    else:
        payload = build_generic_payload(args.action, args.title)
    body = json.dumps(payload).encode()

    headers = {"Content-Type": "application/json", "X-BOW-Delivery": args.delivery}

    if args.auth_mode == "token":
        headers["Authorization"] = f"Bearer {args.secret}"
    elif args.auth_mode == "url_token":
        pass  # secret is the URL token itself
    elif args.source == "github":
        sig = hmac.new(args.secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Hub-Signature-256"] = f"sha256={sig}"
        headers["X-GitHub-Event"] = "pull_request"
        headers["X-GitHub-Delivery"] = args.delivery
    else:  # generic HMAC over "{timestamp}.{body}"
        ts = str(int(time.time()))
        signed = f"{ts}.".encode() + body
        sig = hmac.new(args.secret.encode(), signed, hashlib.sha256).hexdigest()
        headers["X-BOW-Signature-256"] = f"sha256={sig}"
        headers["X-BOW-Timestamp"] = ts

    if args.tamper:
        body = body + b" "  # invalidate the signature

    req = urllib.request.Request(args.url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            print(resp.status, resp.read().decode())
    except urllib.error.HTTPError as e:
        print(e.code, e.read().decode())
        return 0  # report status; non-2xx is expected in negative tests


if __name__ == "__main__":
    sys.exit(main())
