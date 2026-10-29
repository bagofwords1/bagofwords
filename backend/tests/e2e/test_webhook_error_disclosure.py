"""Inbound platform webhooks are unauthenticated until the platform's
signature is checked, so a failure must not echo exception text (parser
messages, driver errors, internal state) back to the caller."""
import pytest

WEBHOOKS = [
    "/api/settings/integrations/teams/webhook",
    "/api/settings/integrations/slack/webhook",
    "/api/settings/integrations/whatsapp/webhook",
]
# Bodies that make a handler raise before it reaches any platform logic.
MALFORMED = [b"{not json", b"[1, 2, 3]", b'"just a string"']


@pytest.mark.e2e
@pytest.mark.parametrize("path", WEBHOOKS)
@pytest.mark.parametrize("body", MALFORMED)
def test_webhook_failure_does_not_echo_exception_text(test_client, path, body):
    r = test_client.post(path, content=body, headers={"Content-Type": "application/json"})
    if r.status_code < 500:
        return  # handled cleanly (e.g. 400 "Invalid JSON body"): nothing leaked
    detail = r.json().get("detail", "")
    assert detail == "Webhook processing failed"
    for leak in ("Expecting", "object has no attribute", "Traceback", "line 1 column"):
        assert leak not in r.text
