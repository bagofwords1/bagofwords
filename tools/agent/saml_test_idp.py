#!/usr/bin/env python3
"""Local-only test IdP. Run via the SAML sandbox runner; never deploy this app."""

import base64
import html
import os
import zlib
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from defusedxml.ElementTree import fromstring
from tests.mocks.saml_idp import response_xml

app = FastAPI()
state = Path(os.environ["BOW_SAML_TEST_DIR"])
issuer = "https://localhost:9443/idp"


@app.get("/sso", response_class=HTMLResponse)
async def sso(request: Request):
    req = fromstring(
        zlib.decompress(base64.b64decode(request.query_params["SAMLRequest"]), -15)
    )
    acs = req.get("AssertionConsumerServiceURL")
    if acs != "https://localhost:3000/api/auth/saml/local-saml/acs":
        return HTMLResponse("Unknown service provider", status_code=400)
    response = response_xml(
        req.get("ID"),
        acs,
        req.find("{urn:oasis:names:tc:SAML:2.0:assertion}Issuer").text,
        issuer,
        (state / "idp-key.pem").read_text(),
        (state / "idp-cert.pem").read_text(),
        subject="local-employee-007",
        attributes={
            "urn:example:mail": "local-saml@example.com",
            "urn:example:display": "Local SAML Member",
        },
        signature="response",
    )
    relay = request.query_params["RelayState"]
    return f'''<!doctype html><html><body><h1>Local test identity provider</h1>
    <p>Sign in as Local SAML Member</p><form method="post" action="{html.escape(acs)}">
    <input type="hidden" name="SAMLResponse" value="{html.escape(response)}">
    <input type="hidden" name="RelayState" value="{html.escape(relay)}">
    <button type="submit">Continue to BOW</button></form></body></html>'''
