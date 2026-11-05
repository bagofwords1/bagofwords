#!/usr/bin/env python3
"""Mock KMS Lighthouse public API: the test boundary for the KMS connector.

No KMS Lighthouse tenant is available, so this simulates the surface
`KmsLighthouseClient` touches, with shapes taken from the KMS Lighthouse
Public API Documentation 5.0 and the public username/password login:

  POST {PREFIX}/login?username&password     cookie and/or token (STATE["login_mode"])
  GET  {PREFIX}/sessionInfo                 data.userId, data.userFullName, data.username
  GET  {PREFIX}/cm/tree/branch/view         ?parentId; ids and statuses are strings
  GET  {PREFIX}/search/full                 ?query&start&rows[&layoutId] -> regularSearch.results[]
  GET  {PREFIX}/public/item/{id}            the article body (ITEMS); HTTP 200 {"status": "ERROR"} for an
                                            unknown id or one this user may not see
  GET  {PREFIX}/ai_answers_item             ?itemId&version&language -> the AI-ready text (AI_ITEMS); 404
                                            for an unknown item, 400 when version or language do not match
  GET  {PREFIX}/public/scenario/{id}        a scenario (SCENARIOS); HTTP 200 {"status": "ERROR"} when unknown
  GET  {PREFIX}/public/scenario/{id}/step/{stepId}
                                            one step of a scenario as data.steps (SCENARIO_STEPS), its own
                                            children inline; HTTP 200 {"status": "ERROR"} when unknown,
                                            HTTP 404 when the stepId is empty
                                            Both scenario routes answer 406 unless the request sends
                                            "Accept: application/vnp.kms-api.v2+json" (the PDF's header).

Faithfulness rules enforced on purpose:
  - A rejected login answers a bare HTTP 400 with no body.
  - The tree's "error" answer is HTTP 200 {"status": "SUCCESS", "data": []}.
  - Every route other than /login needs a session cookie, a login token or a
    pasted API token (TOKENS); a missing or expired session answers 401, or a
    302 to the login page when STATE["expired_response"] == "302".
  - Each user sees only the tree nodes in their `visible` set, and only the
    search results whose `visible` set names them. The tree has a nested
    folder (Procedures/Billing), two sibling folders with one title (Forms),
    an offline item, an offline folder (OFFLINE_FOLDER), a scenario node,
    hostile folder titles (HOSTILE_FOLDER_TITLES) and a wide folder
    (WIDE_FOLDER) with more subfolders than the client's folder-call cap, two
    of them holding a deeper folder (DEEP_AT_CAP, DEEP_PAST_CAP).
  - Search does not rank or filter by the query: every query matches every
    result the user may see, paged by `start`/`rows`. There is no length rule
    on `query` (the PDF states none); a 400 with no body is the PDF's
    "message too long" answer (STATE["search_400"]).

Test-only switches and routes (not part of the KMS API):
  - STATE["session_expiry"] = "immediate": every session /login hands out is
    already expired when it is used (pasted API tokens stay valid).
  - STATE["hold_stale_401s"] = N: the first N requests that carry an expired
    session wait on one threading.Barrier(N, timeout=5) before they get their
    401/302, and are counted in STATE["stale_401s"]; a broken barrier answers
    500, so a test fails instead of hanging.
  - GET {PREFIX}/_mock/status_error/<id>  HTTP 200 {"status": "ERROR"} echoing the id
  - GET {PREFIX}/_mock/held               waits for HELD["release"] (sets HELD["arrived"] first),
                                          so a test can act while a KMS request is in flight
  - STATE["hold_login"] = True: POST /login waits the same way on HELD, so a
    test can act while a login is in flight.
  - STATE["hold_stale_session"] = True: a request carrying an expired session
    waits the same way on HELD before it gets its 401/302.
  - Search: every /search/full call is counted in STATE["search_count"], and
    its query, start, rows and layoutId (None when absent) are recorded in
    STATE["last_search_params"]. STATE["extra_results"] holds extra result
    records (visible to every user unless they carry a `visible` set).
    STATE["search_body"], when not None, is sent as the whole /search/full
    JSON body instead (an odd shape a test wants to see handled).
    STATE["hold_search_after_first"] = True: a /search/full call with
    start > 0 waits on HELD before it answers (a slow later page).
  - Tree: STATE["tree_status"] maps a parentId (a digit string) to an HTTP
    status (an int, 100-599); that tree call answers with it and no body (a
    branch the server fails to list), and its parentId is appended to
    STATE["tree_status_sent"].
  - Read: STATE["ai_text_available"] = False makes every /ai_answers_item
    answer 404; STATE["ai_missing_items"] (item ids) does so for those items
    only. Every /ai_answers_item call appends its itemId, version and
    language to STATE["ai_requests"]. STATE["ai_status"] = N (an int,
    200-599) makes every /ai_answers_item call answer HTTP N with no body;
    "empty" is a 200 with an empty body, "not_json" a 200 whose body is not
    JSON, and "error" a 200 {"status": "ERROR"}. Item 279 carries the planted secrets
    (ARTICLE_SECRETS) in its title, description and HTML (its AI text holds
    the same links as plain text, with a raw '&'), and its AI text
    adds an element (E_RESTRICTED, RESTRICTED_MARKER) that /public/item does
    not return. Item 21302 exists only as a scenario.
  - Scenarios: every request to a scenario route appends its path, v, plainText
    and Accept header to STATE["scenario_requests"] (also one answered 406).
    STATE["step_status"] maps a stepId (a digit string) to an HTTP status (an
    int, 200-599, answered with no body), "error" (a 200 {"status": "ERROR"})
    or "wrong_step" (the answer for another step). Scenario 21302 is shaped
    like the PDF (its "Yes" and "MasterCard" children need a step fetch, "No"
    and "Visa" are inline; image URLs and one step's plain text carry tokens,
    SCENARIO_SECRETS); 21400 comes back to its own steps (a cycle); 21500 is
    60 levels deep, all inline; 21501 has 60 children that all need a fetch;
    21502 has 450 inline children.
  - STATE["odd_shapes"] = "bad_elements": /public/item answers item 279 with
    an `elements` value that is a list, not a map of records.
  - STATE["login_status"] = N (an int, 200-599): POST /login answers HTTP N
    with no body (still counted in "login_count").
  - STATE["expire_sessions_at"] = "<parentId>", "ai_answers_item" or
    "scenario_step": while it is set, every tree call for that parentId, every
    /ai_answers_item call or every scenario step call (a client's retry after
    a re-login too), first expires every
    session /login opened (expire_sessions; pasted API tokens stay valid),
    so a userpass client's call answers 401, or 302 with expired_response.
  - GET {PREFIX}/_mock/empty              HTTP 200 with an empty body
  - GET {PREFIX}/_mock/oversized          HTTP 200 with a large JSON body (OVERSIZED_BYTES)

Users: alice, bob and svc (the system account used by e2e). Pasted API
tokens are in TOKENS.

Run:  python tools/kms_lighthouse/mock_kms_server.py --port 8096
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PREFIX = "/kms/lh/api"
SESSION_COOKIE = "JSESSIONID"
# Planted in unexpected response shapes; it must never reach client output.
ODD_MARKER = "kms-odd-marker-5c1e"

# Content-management tree: node id -> record. "parent" None is the top level
# (asked for with no parentId). "subType" and "updateDate" are sent when set.
TREE = {
    "78": {"parent": None, "title": "Accounts", "type": "FOLDER", "status": "2"},
    "10": {"parent": None, "title": "Procedures", "type": "FOLDER", "status": "2"},
    "11": {"parent": None, "title": "Restricted", "type": "FOLDER", "status": "2"},     # alice only
    "95": {"parent": None, "title": "Archive", "type": "FOLDER", "status": "2"},        # empty
    "279": {"parent": "78", "title": "Reset a customer password", "type": "ITEM", "status": "2"},
    "301": {"parent": "10", "title": "Billing dispute", "type": "ITEM", "status": "2",
            "subType": "T_PROCEDURE", "updateDate": "10/04/2024"},
    "305": {"parent": "10", "title": "Policy v2.1 / summary", "type": "ITEM", "status": "2"},
    "306": {"parent": "10", "title": "Draft refund rules", "type": "ITEM", "status": "1"},   # offline
    "12": {"parent": "10", "title": "Billing", "type": "FOLDER", "status": "2"},
    "13": {"parent": "10", "title": "Forms", "type": "FOLDER", "status": "2"},
    "14": {"parent": "10", "title": "Forms", "type": "FOLDER", "status": "2"},             # same title as 13
    "302": {"parent": "12", "title": "Refund a payment", "type": "ITEM", "status": "2"},
    "4401": {"parent": "12", "title": "Card blocked flow", "type": "ITEM", "subType": "T_SCENARIO_FLOW",
             "status": "2"},
    "303": {"parent": "13", "title": "Change of address form", "type": "ITEM", "status": "2"},
    "304": {"parent": "14", "title": "Card replacement form", "type": "ITEM", "status": "2"},
    "307": {"parent": "11", "title": "Escalation path", "type": "ITEM", "status": "2"},
}
# Hostile folder titles, each holding one item: a path segment must still round-trip.
HOSTILE_FOLDER_TITLES = {
    "20": "Long " + "x" * 245,
    "21": "tab\there\nnewline",
    "22": "..",
    "23": "folder:7 Archive",
    "24": "<b>Bold</b> &amp; HTML",
}
for _folder_id, _title in HOSTILE_FOLDER_TITLES.items():
    TREE[_folder_id] = {"parent": None, "title": _title, "type": "FOLDER", "status": "2"}
    TREE[str(int(_folder_id) + 300)] = {"parent": _folder_id, "title": "Inside " + _folder_id, "type": "ITEM",
                                        "status": "2"}
# A wide folder (listed last): more subfolders than the client's folder-call cap.
WIDE_FOLDER = "30"
WIDE_CHILDREN = 60
TREE[WIDE_FOLDER] = {"parent": None, "title": "Wide", "type": "FOLDER", "status": "2"}
for _n in range(WIDE_CHILDREN):
    TREE[str(1000 + _n)] = {"parent": WIDE_FOLDER, "title": f"Wide {_n}", "type": "FOLDER", "status": "2"}
    TREE[str(2000 + _n)] = {"parent": str(1000 + _n), "title": f"Wide item {_n}", "type": "ITEM", "status": "2"}
# An offline folder (with one item) under Procedures.
OFFLINE_FOLDER = "15"
TREE[OFFLINE_FOLDER] = {"parent": "10", "title": "Old forms", "type": "FOLDER", "status": "1"}
TREE["308"] = {"parent": OFFLINE_FOLDER, "title": "Old address form", "type": "ITEM", "status": "2"}
# Folders deep in the wide folder: from the root "30", finding DEEP_AT_CAP takes the whole folder-call
# cap (the root level and 39 wide children), and DEEP_PAST_CAP one call more.
DEEP_AT_CAP, DEEP_PAST_CAP = "5001", "5002"
for _deep_id, _wide_parent in ((DEEP_AT_CAP, "1038"), (DEEP_PAST_CAP, "1039")):
    TREE[_deep_id] = {"parent": _wide_parent, "title": "Deep " + _deep_id, "type": "FOLDER", "status": "2"}
    TREE[str(int(_deep_id) + 100)] = {"parent": _deep_id, "title": "Deep item", "type": "ITEM", "status": "2"}
_RESTRICTED = {"11", "307"}

# Planted in search titles and descriptions; neither may reach client output.
# Both are made-up test values (the JWT is a well-formed but unsigned dummy).
CONTENT_JWT = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJtb2NrLXNlYXJjaCJ9.bW9jay1zaWduYXR1cmU"
URL_TOKEN = "mock-url-token-3b9e71"

# /search/full results (PDF shape), plus who may see each one.
SEARCH_RESULTS = [
    {"id": "279", "subType": "T_PROCEDURE", "title": "Reset a customer <strong>password</strong>",
     "description": "Steps to <strong>reset</strong> a password from the agent desk.",
     "status": "Online", "statusCode": "2", "updateDate": "10/04/2024", "visible": {"alice", "svc"}},
    {"id": "301", "subType": "T_PROCEDURE", "title": "<strong>Billing</strong> dispute",
     "description": "Open a <strong>billing</strong> dispute &amp; track it.",
     "status": "Online", "statusCode": "2", "updatedDate": "2024-05-09 12:57:56.0",
     "visible": {"alice", "bob", "svc"}},
    {"id": "4400", "subType": "T_SCENARIO_FLOW", "title": "Card <strong>blocked</strong> flow",
     "description": "A decision tree for a blocked card.", "status": "Online", "statusCode": "2",
     "visible": {"alice", "bob", "svc"}},
    {"id": "4410", "subType": "T_PROCEDURE", "title": "Draft <strong>refund</strong> rules",
     "description": "Not published yet.", "status": "Offline", "statusCode": "1",
     "visible": {"alice", "bob", "svc"}},
    {"id": "4420", "subType": "T_PROCEDURE",
     "title": f"Token notes {CONTENT_JWT} https://kms.example.com/contents?token={URL_TOKEN}",
     "description": (f"See https://kms.example.com/contents?id=7&amp;token={URL_TOKEN}&amp;lang=en and "
                     f"<strong>bearer</strong> {CONTENT_JWT} for details."),
     "status": "Online", "statusCode": "2", "visible": {"alice", "bob", "svc"}},
    {"id": "4430", "subType": "T_PROCEDURE", "title": "Restricted <strong>escalation</strong> path",
     "description": "Managers only.", "status": "Online", "statusCode": "2", "visible": {"alice"}},
    {"id": "4440", "type": "FOLDER", "title": "A folder hit", "description": "", "status": "Online",
     "statusCode": "2", "visible": {"alice", "bob", "svc"}},
]

# Planted in article reads; none may reach client output. Made-up test values (unsigned dummy JWTs).
ARTICLE_SECRETS = {
    "title": "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ0aXRsZSJ9.bW9jay10aXRsZS1zaWc",
    "description": "eyJhbGciOiJub25lIn0.eyJzdWIiOiJkZXNjcmlwdGlvbiJ9.bW9jay1kZXNjLXNpZw",
    "body_jwt": "eyJhbGciOiJub25lIn0.eyJzdWIiOiJib2R5In0.bW9jay1ib2R5LXNpZw",
    "bare_url": "mock-bare-url-token-8d2f61",
    "entity_url": "mock-entity-url-token-41ac07",
    "img": "eyJhbGciOiJub25lIn0.eyJzdWIiOiJpbWFnZSJ9.bW9jay1pbWFnZS1zaWc",
}
RESTRICTED_MARKER = "kms-restricted-marker-7a90"
_S = ARTICLE_SECRETS
_LINKS_TEXT = (f"Form at https://kms.example.com/contents?token={_S['bare_url']} or "
               f"https://kms.example.com/contents?id=7&amp;token={_S['entity_url']}&amp;lang=en. "
               f"Signed {_S['body_jwt']}")
# The same words as ai element_text, which is plain text (API PDF): a raw '&', no character reference.
_AI_LINKS_TEXT = _LINKS_TEXT.replace("&amp;", "&")

# /public/item bodies (PDF shape: integer tabs[].id, string elements.*.tabId, plus a null and a -1).
ITEMS = {
    "279": {
        "id": 279, "type": "DOCUMENT", "subType": "T_PROCEDURE ", "version": 2, "language": "en",
        "title": f"Reset a customer password {_S['title']}",
        "description": f"How to reset a password from the agent desk. Ref {_S['description']}",
        "status": 2, "createDate": "01/02/2024", "updateDate": "10/04/2024", "relatedItems": [],
        "tabs": [{"id": 1, "caption": "Overview"}, {"id": 3, "caption": "Processes"}],
        "elements": {
            "ITEM_IMAGE": {"value": "b144e1e2-2d92-4e09-8d68-f0b2604da35d.gif", "tabId": None,
                           "title": "Item image", "showTitle": True, "type": "ITEM_IMAGE_FIELD"},
            "E_SUMMARY": {"value": "<div style=\"font-size: 14px;\">Item summary: verify the caller first.</div>",
                          "tabId": "1", "title": "Summary", "showTitle": True, "type": "EDITOR_FIELD"},
            "E_SCREEN": {"value": ("<p>Screen:</p><img src=\"https://kms.example.com/file-service/a.jpg"
                                   f"/contents?token={_S['img']}\">"),
                         "tabId": "1", "title": "Screenshot", "showTitle": True, "type": "EDITOR_FIELD"},
            "E_PROCESS": {"value": "<ol><li>Open the agent desk.</li><li>Choose Reset.</li></ol>",
                          "tabId": "3", "title": "Steps", "showTitle": True, "type": "EDITOR_FIELD"},
            "E_LINKS": {"value": f"<p>{_LINKS_TEXT}</p>", "tabId": "3", "title": "Links", "showTitle": True,
                        "type": "EDITOR_FIELD"},
            "E_NOTES": {"value": "<p>Untabbed note for every agent.</p>", "tabId": None, "title": "Notes",
                        "showTitle": True, "type": "EDITOR_FIELD"},
            "E_LEGACY": {"value": "<p>Legacy note on no tab.</p>", "tabId": -1, "title": "Legacy",
                         "showTitle": True, "type": "EDITOR_FIELD"},
        },
    },
    "301": {
        "id": 301, "type": "DOCUMENT", "subType": "T_PROCEDURE", "version": 1, "language": "en",
        "title": "Billing dispute", "description": "Open a billing dispute.", "status": 2,
        "updateDate": "10/04/2024", "tabs": [{"id": 1, "caption": "Overview"}],
        "elements": {"E_SUMMARY": {"value": "<p>Item text for a billing dispute.</p>", "tabId": "1",
                                   "title": "Summary", "type": "EDITOR_FIELD"}},
    },
    "306": {
        "id": 306, "type": "DOCUMENT", "subType": "T_PROCEDURE", "version": 1, "language": "en",
        "title": "Draft refund rules", "description": "Not published yet.", "status": 1,
        "tabs": [{"id": 1, "caption": "Overview"}],
        "elements": {"E_SUMMARY": {"value": "<p>Draft text.</p>", "tabId": "1", "title": "Summary",
                                   "type": "EDITOR_FIELD"}},
    },
}


def _ai_element(name, tab, caption, text):
    return {"permissions": [], "type": "DOCUMENT", "element_name": name, "element_caption": caption,
            "element_type": "EDITOR_FIELD", "tab_caption": tab, "element_text": text}


# /ai_answers_item bodies (PDF shape). Item 279 adds E_RESTRICTED, which its /public/item does not return.
AI_ITEMS = {
    "279": {"title": ITEMS["279"]["title"], "type": "DOCUMENT", "version": 2, "language": "en", "permissions": [1],
            "item_id": 279, "status": "Online", "status_id": 2, "elements": [
                _ai_element("E_SUMMARY", "Overview", "Summary", "AI summary: verify the caller, then reset."),
                _ai_element("E_LINKS", "Processes", "Links", "AI links. " + _AI_LINKS_TEXT),
                _ai_element("E_RESTRICTED", "Overview", "Managers only", RESTRICTED_MARKER + " escalation codes"),
            ]},
    "301": {"title": "Billing dispute", "type": "DOCUMENT", "version": 1, "language": "en", "permissions": [1],
            "item_id": 301, "status": "Online", "status_id": 2, "elements": [
                _ai_element("E_SUMMARY", "Overview", "Summary", "AI text for a billing dispute."),
            ]},
    "306": {"title": "Draft refund rules", "type": "DOCUMENT", "version": 1, "language": "en", "permissions": [1],
            "item_id": 306, "status": "Offline", "status_id": 1, "elements": [
                _ai_element("E_SUMMARY", "Overview", "Summary", "AI draft text."),
            ]},
}

# Planted in scenario image URLs and step text; none may reach client output. Made-up test values.
SCENARIO_SECRETS = {
    "root_image": "eyJhbGciOiJub25lIn0.eyJzdWIiOiJzY2VuYXJpby1yb290In0.bW9jay1yb290LWltYWdlLXNpZw",
    "step_image": "eyJhbGciOiJub25lIn0.eyJzdWIiOiJzY2VuYXJpby1zdGVwIn0.bW9jay1zdGVwLWltYWdlLXNpZw",
    "step_url": "mock-step-url-token-6e2a90",
}


def _step(step_id, name="", node="", text="", image="", decisions=None, children=None):
    """One scenario step (PDF shape); ``decisions`` keys are "<scenario>:2:<child stepId>"."""
    return {"text": text, "decisions": decisions or {}, "stepName": name, "stepRemark": "", "stepNodeText": node,
            "stepId": step_id, "image": image, "imageSvg": "", "selected": None, "selectCause": None,
            "links": {}, "children": children or {}}


def _image(name, token):
    return f"https://kms.example.com/file-service/archive/file/{name}.jpg/contents?token={token}"


def _scenario(scenario_id, title, steps):
    return {"id": scenario_id, "title": title, "notes": "", "description": None, "updatedBy": "290",
            "updatedDate": "2024-05-09 12:57:56.0", "steps": steps, "relatedItems": [], "choiceDisplay": "buttons"}


# Asked for with plainText=true (PDF: "content without HTML tags"), so step text is plain.
_NO_STEP_TEXT = ("Call the bank on 1-800-555-0100 to register.\n"
                 f"Form: https://kms.example.com/contents?id=7&token={SCENARIO_SECRETS['step_url']}")


def _deep_chain(levels):
    """Scenario 21500: one inline child per level, ``levels`` levels deep (built from the bottom up)."""
    step = _step(50000 + levels - 1, name=f"Level {levels - 1} question")
    for level in range(levels - 2, -1, -1):
        step = _step(50000 + level, name=f"Level {level} question",
                     decisions={f"21500:2:{50000 + level + 1}": f"Go deeper {level + 1}"},
                     children={str(50000 + level + 1): step})
    return step


# /public/scenario bodies; 21302 has no /public/item (a bare-digit read finds it only through the fallback).
SCENARIOS = {
    "21302": _scenario(21302, "How to Register for Advanced Credit Card Security", _step(
        23505, name="Have you registered for advanced security?",
        image=_image("47fe1d9f", SCENARIO_SECRETS["root_image"]),
        decisions={"21302:2:23506": "Yes", "21302:2:23515": "No"},
        children={"23515": _step(23515, node="No", text=_NO_STEP_TEXT)})),
    "21400": _scenario(21400, "Blocked card check", _step(
        30001, name="Is the card blocked?",
        decisions={"21400:2:30002": "Yes", "21400:2:30003": "Not sure", "21400:2:30001": "Start again"},
        children={"30002": _step(30002, name="Unblock the card", node="Yes",
                                 decisions={"21400:2:30001": "Back to the start"})})),
    "21500": _scenario(21500, "Deep scenario", _deep_chain(60)),
    "21501": _scenario(21501, "Wide fetched scenario", _step(
        51099, name="Pick a branch", decisions={f"21501:2:{51000 + n}": f"Branch {n}" for n in range(60)})),
    "21502": _scenario(21502, "Wide inline scenario", _step(
        52999, name="Pick an option", decisions={f"21502:2:{52000 + n}": f"Option {n}" for n in range(450)},
        children={str(52000 + n): _step(52000 + n, node=f"Option {n}", text=f"Option {n} step.")
                  for n in range(450)})),
}
# /public/scenario/{id}/step/{stepId} steps: scenario id -> stepId -> the step (its children inline).
SCENARIO_STEPS = {
    "21302": {
        "23506": _step(23506, node="Yes", children={"23496": _step(
            23496, name="Which type of card do you have?", image=_image("a506b7e1", SCENARIO_SECRETS["step_image"]),
            decisions={"21302:2:23497": "Visa", "21302:2:23498": "MasterCard"},
            children={"23497": _step(23497, node="Visa", text="Register the Visa card in the card portal.")})}),
        "23498": _step(23498, node="MasterCard", text="Call the MasterCard desk to register."),
    },
    "21400": {"30003": _step(30003, name="Check the card status", node="Not sure",
                             children={"30002": _step(30002, name="Unblock the card")})},
    "21501": {str(51000 + n): _step(51000 + n, node=f"Branch {n}", text=f"Branch {n} step.") for n in range(60)},
}
_SCENARIO_ACCEPT = "application/vnp.kms-api.v2+json"

# "session_username" is what sessionInfo reports; it differs from the login
# name on purpose, so a test can tell a reported identity from the typed one.
USERS = {
    "alice": {"password": "alice-kms-pass", "user_id": 11, "full_name": "Alice Agent",
              "session_username": "alice.agent", "visible": set(TREE)},
    "bob": {"password": "bob-kms-pass", "user_id": 12, "full_name": "Bob Billing",
            "session_username": "bob.billing", "visible": {"10", "301", "12", "302"}},
    "svc": {"password": "svc-kms-pass", "user_id": 13, "full_name": "BOW Service",
            "session_username": "bow.service", "visible": set(TREE) - _RESTRICTED},
}

# Pasted API tokens (the "token" auth variant) -> user.
TOKENS = {
    "alice-api-token-1": "alice",
    "bob-api-token-1": "bob",
    "svc-api-token-1": "svc",
}

# Size of the /_mock/oversized body (a test lowers the client's cap below it).
OVERSIZED_BYTES = 256 * 1024

# Allowed values of the switches a test sets; anything else is a typo and raises.
_SWITCHES = {
    "login_mode": ("cookie", "token", "both", "none"),
    "expired_response": ("401", "302"),
    "session_expiry": ("never", "immediate"),
    "odd_shapes": (False, "list", "error", "bad_id", "bad_username", "bad_elements"),
    "ai_text_available": (True, False),
    "top_level_requires_parent": (True, False),
    "hold_login": (True, False),
    "hold_stale_session": (True, False),
    "search_400": (True, False),
    "hold_search_after_first": (True, False),
}
# The odd 200 answers STATE["ai_status"] can name besides an HTTP status.
_AI_ANSWERS = ("empty", "not_json", "error")
# Counters and sizes: non-negative integers (bool is not an int here).
_COUNTERS = ("login_count", "accepted_logins", "hold_stale_401s", "stale_401s", "search_count")
_DEFAULTS = {
    "login_mode": "cookie",          # cookie | token | both | none (SUCCESS, no session)
    "expired_response": "401",       # 401 | 302
    "session_expiry": "never",       # never | immediate (login sessions are dead on arrival)
    "ai_text_available": True,
    "top_level_requires_parent": False,
    "hold_login": False,             # True: POST /login waits on HELD like /_mock/held
    "login_count": 0,                # every login attempt
    "accepted_logins": 0,            # logins that returned SUCCESS
    "hold_stale_401s": 0,            # N > 0: the first N stale-session requests meet at a barrier
    "stale_401s": 0,                 # requests that carried an expired session
    "hold_stale_session": False,     # True: a stale-session request waits on HELD before its 401/302
    "search_400": False,             # True: /search/full answers 400 with no body ("message too long")
    "hold_search_after_first": False,  # True: a /search/full past the first page waits on HELD
    "search_count": 0,               # /search/full requests
    "last_search_params": None,      # {"query", "start", "rows", "layoutId"} of the last /search/full
    "extra_results": [],
    "search_body": None,             # None | a JSON value sent as the whole /search/full body
    "odd_shapes": False,             # False | "list" | "error" | "bad_id" | "bad_username" | "bad_elements"
    "tree_status": {},               # parentId -> HTTP status: that tree call answers it with no body
    "tree_status_sent": [],          # the parentId of every tree call answered from "tree_status", in order
    "ai_missing_items": [],          # item ids whose /ai_answers_item answers 404
    "ai_requests": [],               # {"itemId", "version", "language"} of every /ai_answers_item call, in order
    "ai_status": None,               # None | an HTTP status (int 200-599) | "empty" | "not_json" | "error"
    "login_status": None,            # None | an HTTP status (int 200-599): POST /login answers it with no body
    "expire_sessions_at": None,      # None | a tree parentId (digit string) | "ai_answers_item" | "scenario_step"
    "scenario_requests": [],         # {"path", "v", "plainText", "accept"} of every scenario route call, in order
    "step_status": {},               # stepId -> HTTP status | "error" | "wrong_step": that step call answers it
}
_STEP_ANSWERS = ("error", "wrong_step")
_STALE = {"barrier": None}           # the barrier for STATE["hold_stale_401s"]
# GET {PREFIX}/_mock/held sets "arrived", then waits up to 5 s for "release".
HELD = {"arrived": threading.Event(), "release": threading.Event()}


def _is_digits(value) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9]+", value) is not None


def _is_status(value) -> bool:
    """An int HTTP status a final answer can carry, 200-599 (bool is not an int here)."""
    return type(value) is int and 200 <= value <= 599


def _valid_tree_status(value) -> bool:
    """A dict of ASCII digit-string folder ids to int HTTP statuses."""
    return type(value) is dict and all(
        type(key) is str and re.fullmatch(r"[0-9]+", key) is not None and _is_status(status)
        for key, status in value.items())


class _State(dict):
    def __setitem__(self, key, value):
        if key not in _DEFAULTS:
            raise KeyError(f"STATE has no switch {key!r}")
        allowed = _SWITCHES.get(key)
        # `True == 1` would pass a plain `in` check, so compare types too.
        if allowed is not None and not any(type(value) is type(a) and value == a for a in allowed):
            raise ValueError(f"STATE[{key!r}] must be one of {allowed!r}, not {value!r}")
        if key in _COUNTERS and (type(value) is not int or value < 0):
            raise ValueError(f"STATE[{key!r}] must be a non-negative int, not {value!r}")
        if key == "tree_status" and not _valid_tree_status(value):
            raise ValueError(f"STATE[{key!r}] must map folder ids (digit strings) to HTTP statuses "
                             f"(ints 200-599), not {value!r}")
        if key == "ai_missing_items" and not (type(value) is list and all(_is_digits(v) for v in value)):
            raise ValueError(f"STATE[{key!r}] must be a list of item ids (digit strings), not {value!r}")
        if key == "login_status" and not (value is None or _is_status(value)):
            raise ValueError(f"STATE[{key!r}] must be None or an HTTP status (int 200-599), not {value!r}")
        if key == "ai_status" and not (value is None or _is_status(value) or value in _AI_ANSWERS):
            raise ValueError(f"STATE[{key!r}] must be None, an HTTP status (int 200-599) or one of "
                             f"{_AI_ANSWERS!r}, not {value!r}")
        if key == "expire_sessions_at" and not (value is None or value in ("ai_answers_item", "scenario_step")
                                                or _is_digits(value)):
            raise ValueError(f"STATE[{key!r}] must be None, a parentId, 'ai_answers_item' or 'scenario_step', "
                             f"not {value!r}")
        if key == "step_status" and not (type(value) is dict and all(
                _is_digits(k) and (_is_status(v) or v in _STEP_ANSWERS) for k, v in value.items())):
            raise ValueError(f"STATE[{key!r}] must map step ids (digit strings) to HTTP statuses (ints 200-599) "
                             f"or one of {_STEP_ANSWERS!r}, not {value!r}")
        if key == "hold_stale_401s":
            old = _STALE["barrier"]
            if old is not None:
                old.abort()  # release any thread still waiting on the old barrier
            _STALE["barrier"] = threading.Barrier(value, timeout=5) if value else None
        super().__setitem__(key, value)

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]


STATE: dict = _State()
REQUEST_LOG: list = []
_SESSIONS: dict = {}        # session cookie value -> user
_LOGIN_TOKENS: dict = {}    # token issued by /login -> user
_LOCK = threading.Lock()


def reset():
    """Restore the default state; called before every test."""
    with _LOCK:
        STATE.clear()
        STATE.update({key: (type(value)(value) if isinstance(value, (list, dict)) else value)
                      for key, value in _DEFAULTS.items()})
        REQUEST_LOG.clear()
        _SESSIONS.clear()
        _LOGIN_TOKENS.clear()
        for event in HELD.values():
            event.clear()


def expire_sessions():
    """Invalidate every session opened by /login (pasted API tokens stay valid)."""
    with _LOCK:
        _SESSIONS.clear()
        _LOGIN_TOKENS.clear()


reset()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep test output quiet
        pass

    # ------------------------------------------------------------ plumbing
    def _send_json(self, status, body, headers=None):
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def _send_empty(self, status, headers=None):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _identity(self):
        auth = self.headers.get("Authorization") or ""
        login_sessions_live = STATE["session_expiry"] == "never"
        if auth.startswith("Bearer "):
            token = auth[len("Bearer "):]
            user = TOKENS.get(token) or (_LOGIN_TOKENS.get(token) if login_sessions_live else None)
            if user:
                return user
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE and value in _SESSIONS and login_sessions_live:
                return _SESSIONS[value]
        return None

    def _carries_session(self):
        """True when the request sends a session (cookie or Bearer) that was not accepted."""
        cookie = self.headers.get("Cookie") or ""
        has_cookie = any(part.strip().startswith(SESSION_COOKIE + "=") for part in cookie.split(";"))
        return has_cookie or (self.headers.get("Authorization") or "").startswith("Bearer ")

    def _unauthenticated(self):
        if self._carries_session():
            with _LOCK:
                index = STATE["stale_401s"]
                STATE["stale_401s"] = index + 1
                barrier = _STALE["barrier"] if index < STATE["hold_stale_401s"] else None
            if barrier is not None:
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    return self._send_empty(500)
            if STATE["hold_stale_session"]:
                HELD["arrived"].set()
                if not HELD["release"].wait(5):
                    return self._send_empty(500)   # never released: fail instead of hanging
        if STATE["expired_response"] == "302":
            self._send_empty(302, {"Location": "/kms/lh/login"})
        else:
            self._send_empty(401)

    # ------------------------------------------------------------ routes
    def do_POST(self):
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        if parts.path != PREFIX + "/login":
            REQUEST_LOG.append((self._identity(), "POST", parts.path))
            return self._send_empty(404)
        username = (query.get("username") or [""])[0]
        password = (query.get("password") or [""])[0]
        if STATE["hold_login"]:
            HELD["arrived"].set()
            if not HELD["release"].wait(5):
                return self._send_empty(500)   # never released: fail instead of hanging
        with _LOCK:
            REQUEST_LOG.append((username or None, "POST", parts.path))
            STATE["login_count"] += 1
            record = USERS.get(username)
            forced = STATE["login_status"]
            if forced is not None:
                accepted = None
            elif not record or record["password"] != password:
                accepted = False
            else:
                accepted = True
                STATE["accepted_logins"] += 1
                mode = STATE["login_mode"]
                headers, data = {}, {"userId": record["user_id"]}
                if mode in ("cookie", "both"):
                    sid = secrets.token_hex(16)
                    _SESSIONS[sid] = username
                    headers["Set-Cookie"] = f"{SESSION_COOKIE}={sid}; Path=/; HttpOnly"
                if mode in ("token", "both"):
                    token = "lt-" + secrets.token_hex(16)
                    _LOGIN_TOKENS[token] = username
                    data["token"] = token
        if accepted is None:
            return self._send_empty(forced)
        if not accepted:
            return self._send_empty(400)
        return self._send_json(200, {"status": "SUCCESS", "data": data}, headers)

    def do_GET(self):
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        expire_at = STATE["expire_sessions_at"]
        if expire_at == "ai_answers_item":
            expiring = parts.path == PREFIX + "/ai_answers_item"
        elif expire_at == "scenario_step":
            expiring = parts.path.startswith(PREFIX + "/public/scenario/") and "/step/" in parts.path
        else:
            expiring = (expire_at is not None and parts.path == PREFIX + "/cm/tree/branch/view"
                        and (query.get("parentId") or [None])[0] == expire_at)
        if expiring:
            expire_sessions()
        user = self._identity()
        REQUEST_LOG.append((user, "GET", parts.path))
        if not parts.path.startswith(PREFIX + "/"):
            return self._send_empty(404)
        if user is None:
            return self._unauthenticated()
        route = parts.path[len(PREFIX):]
        odd = STATE["odd_shapes"]
        if route == "/sessionInfo":
            if odd == "list":
                return self._send_json(200, [ODD_MARKER])
            if odd == "error":
                return self._send_json(200, {"status": "ERROR", "exception": ODD_MARKER, "message": ODD_MARKER})
            if odd == "bad_id":
                return self._send_json(200, {"status": "SUCCESS", "data": [ODD_MARKER]})
            record = USERS[user]
            username = {"name": ODD_MARKER} if odd == "bad_username" else record["session_username"]
            return self._send_json(200, {"status": "SUCCESS", "data": {
                "userId": record["user_id"], "userFullName": record["full_name"], "username": username}})
        if route == "/cm/tree/branch/view":
            if odd == "list":
                return self._send_json(200, [ODD_MARKER])
            if odd == "error":
                return self._send_json(200, {"status": "ERROR", "exception": ODD_MARKER})
            if odd == "bad_id":
                return self._send_json(200, {"status": "SUCCESS", "data": [
                    {"id": "x-" + ODD_MARKER, "title": ODD_MARKER, "type": "FOLDER", "status": "2"}]})
            parent = (query.get("parentId") or [None])[0]
            if parent in STATE["tree_status"]:
                with _LOCK:
                    STATE["tree_status_sent"].append(parent)
                return self._send_empty(STATE["tree_status"][parent])
            if parent is None and STATE["top_level_requires_parent"]:
                return self._send_json(200, {"status": "SUCCESS", "data": []})
            visible = USERS[user]["visible"]
            data = [
                {"id": node_id, "language": "en", "title": node["title"], "type": node["type"],
                 "version": "1", "status": node["status"], "state": "ACTIVE",
                 "sons": str(sum(1 for n in TREE.values() if n["parent"] == node_id)),
                 **{key: node[key] for key in ("subType", "updateDate") if key in node}}
                for node_id, node in TREE.items()
                if node["parent"] == parent and node_id in visible
            ]
            return self._send_json(200, {"status": "SUCCESS", "data": data})
        if route == "/search/full":
            return self._search(user, query, odd)
        if route.startswith("/public/item/"):
            return self._item(user, route[len("/public/item/"):], odd)
        if route == "/ai_answers_item":
            return self._ai_text(user, query)
        if route.startswith("/public/scenario/"):
            return self._scenario(parts.path, route[len("/public/scenario/"):], query)
        if route.startswith("/_mock/status_error/"):
            item_id = route[len("/_mock/status_error/"):]
            return self._send_json(200, {"status": "ERROR", "exception": f"Item {item_id} was not found",
                                         "message": f"no item {item_id}"})
        if route == "/_mock/held":
            HELD["arrived"].set()
            if not HELD["release"].wait(5):
                return self._send_empty(500)   # never released: fail instead of hanging
            return self._send_json(200, {"status": "SUCCESS", "data": []})
        if route == "/_mock/empty":
            return self._send_empty(200)
        if route == "/_mock/oversized":
            filler = "x" * OVERSIZED_BYTES
            return self._send_json(200, {"status": "SUCCESS", "data": filler})
        return self._send_empty(404)

    @staticmethod
    def _item_visible(user, item_id):
        """A tree node follows the tree's visibility; an item outside the tree is visible to everyone."""
        return item_id not in TREE or item_id in USERS[user]["visible"]

    def _item(self, user, item_id, odd):
        if odd == "list":
            return self._send_json(200, [ODD_MARKER])
        if odd == "error":
            return self._send_json(200, {"status": "ERROR", "exception": ODD_MARKER, "data": None})
        if odd == "bad_id":
            return self._send_json(200, {"id": "x-" + ODD_MARKER, "title": ODD_MARKER, "status": 2,
                                         "elements": {"E_SUMMARY": {"value": ODD_MARKER, "tabId": "1"}}})
        item = ITEMS.get(item_id)
        if odd == "bad_elements" and item_id == "279":
            return self._send_json(200, {**item, "elements": [ODD_MARKER]})
        if item is None or not self._item_visible(user, item_id):
            # The PDF's not-found shape.
            return self._send_json(200, {"status": "ERROR", "exception": "Cannot get property 'id' on null object",
                                         "data": None})
        return self._send_json(200, item)

    def _scenario(self, path, rest, query):
        """``rest`` is "<id>" (the scenario) or "<id>/step/<stepId>" (one step, as data.steps)."""
        accept = self.headers.get("Accept")
        with _LOCK:
            STATE["scenario_requests"].append({"path": path, "v": (query.get("v") or [None])[0],
                                               "plainText": (query.get("plainText") or [None])[0], "accept": accept})
        if accept != _SCENARIO_ACCEPT:
            return self._send_empty(406)
        scenario_id, step_route, step_id = rest.partition("/step/")
        if step_route and not step_id:
            return self._send_empty(404)
        scenario = SCENARIOS.get(scenario_id)
        missing = {"status": "ERROR", "exception": "Can not find scenario", "data": None}
        if not step_id:
            return self._send_json(200, missing if scenario is None else {"status": "SUCCESS", "data": scenario})
        forced = STATE["step_status"].get(step_id)
        if forced == "error":
            return self._send_json(200, missing)
        if forced is not None and forced != "wrong_step":
            return self._send_empty(forced)
        step = SCENARIO_STEPS.get(scenario_id, {}).get(step_id)
        if scenario is None or step is None:
            return self._send_json(200, missing)
        if forced == "wrong_step":
            step = _step(int(step_id) + 1, name=ODD_MARKER, text=ODD_MARKER)
        return self._send_json(200, {"status": "SUCCESS", "data": {**scenario, "steps": step}})

    def _ai_text(self, user, query):
        params = {key: (query.get(key) or [None])[0] for key in ("itemId", "version", "language")}
        with _LOCK:
            STATE["ai_requests"].append(params)
        forced = STATE["ai_status"]
        if forced == "empty":
            return self._send_empty(200)
        if forced == "not_json":
            raw = b"<html>not json</html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return None
        if forced == "error":
            return self._send_json(200, {"status": "ERROR", "exception": "AI text failed", "data": None})
        if forced is not None:
            return self._send_empty(forced)
        item_id = params["itemId"] or ""
        body = AI_ITEMS.get(item_id)
        if (not STATE["ai_text_available"] or item_id in STATE["ai_missing_items"] or body is None
                or not self._item_visible(user, item_id)):
            return self._send_empty(404)
        if params["version"] != str(body["version"]) or params["language"] != body["language"]:
            return self._send_empty(400)
        return self._send_json(200, body)

    def _search(self, user, query, odd):
        params = {key: (query.get(key) or [None])[0] for key in ("query", "start", "rows", "layoutId")}
        with _LOCK:
            STATE["search_count"] += 1
            STATE["last_search_params"] = params
        if STATE["search_400"]:
            return self._send_empty(400)
        if STATE["hold_search_after_first"] and params["start"] not in (None, "0"):
            HELD["arrived"].set()
            if not HELD["release"].wait(5):
                return self._send_empty(500)   # never released: fail instead of hanging
            self.close_connection = True       # the client has usually given up by now
            try:
                return self._send_json(200, {"regularSearch": {"query": params["query"], "results": []}})
            except (BrokenPipeError, ConnectionResetError):
                return None
        if STATE["search_body"] is not None:
            return self._send_json(200, STATE["search_body"])
        if odd == "list":
            return self._send_json(200, [ODD_MARKER])
        if odd == "error":
            return self._send_json(200, {"status": "ERROR", "message": ODD_MARKER, "params": {}})
        if odd == "bad_id":
            return self._send_json(200, {"regularSearch": {"query": ODD_MARKER, "results": [
                {"id": "x-" + ODD_MARKER, "title": ODD_MARKER, "description": ODD_MARKER, "status": "Online"}]}})
        try:
            start, rows = int(params["start"] or 0), int(params["rows"] or 10)
        except ValueError:
            return self._send_empty(400)
        visible = [r for r in SEARCH_RESULTS + list(STATE["extra_results"]) if user in r.get("visible", (user,))]
        page = [{k: v for k, v in r.items() if k != "visible"} for r in visible[start:start + rows]]
        return self._send_json(200, {"regularSearch": {
            "query": params["query"], "suggestedQueryAlternative": None, "results": page}})


def make_server(port: int = 0, host: str = "127.0.0.1"):
    return ThreadingHTTPServer((host, port), Handler)


def main():
    parser = argparse.ArgumentParser(description="Mock KMS Lighthouse public API")
    parser.add_argument("--port", type=int, default=8096)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    server = make_server(args.port, args.host)
    print(f"Mock KMS Lighthouse listening on http://{args.host}:{args.port}{PREFIX}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
