"""A small, stateful fake of Microsoft 365 for connector-action tests and the sandbox.

One router (``FakeM365.__call__(httpx.Request) -> httpx.Response``) serves:

- the Microsoft identity platform: ``/{tenant}/oauth2/v2.0/authorize`` (auto
  sign-in, redirects back with a code) and ``/{tenant}/oauth2/v2.0/token``
  (authorization_code / refresh_token / client_credentials). Access tokens are
  unsigned JWTs whose ``scp`` (delegated) or ``roles`` (app) claim carries the
  scopes that were requested — so a test can sign in without Mail.Send and see
  the send refused;
- Graph v1.0 mail: folders, message listing / reading, PATCH, move,
  draft / createReply / createReplyAll / createForward, send, sendMail;
- Graph v1.0 Teams: joined teams, channels, chats, channel posts, replies
  and chat messages.

Every write is appended to ``self.writes`` so a test (or the sandbox
inspector at ``GET /_fake/writes``) can assert exactly what reached Microsoft.
Use in-process with ``httpx.MockTransport(FakeM365())``, or serve it with
``tools/agent/mock_graph_server.py``.
"""
from __future__ import annotations

import base64
import itertools
import json
import re
import time
import urllib.parse
from typing import Any, Dict, List, Optional

import httpx

ME = {"id": "u-dana", "displayName": "Dana Analyst", "userPrincipalName": "dana@contoso.test", "mail": "dana@contoso.test"}


def make_token(scopes: List[str], *, app: bool = False, upn: str = ME["userPrincipalName"]) -> str:
    def b64(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    claims: Dict[str, Any] = {"aud": "https://graph.microsoft.com", "exp": int(time.time()) + 3600, "upn": upn}
    if app:
        claims["roles"] = list(scopes)
    else:
        claims["scp"] = " ".join(scopes)
    return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(claims)}.fake"


_GRAPH_SCOPE_PREFIX = "https://graph.microsoft.com/"
_OIDC = {"openid", "profile", "offline_access", "email"}


def _graph_scopes(scope_param: str) -> List[str]:
    out = []
    for s in (scope_param or "").split():
        s = s[len(_GRAPH_SCOPE_PREFIX):] if s.startswith(_GRAPH_SCOPE_PREFIX) else s
        if s and s not in _OIDC and s != ".default":
            out.append(s)
    return out


class FakeM365:
    def __init__(self, *, app_roles: Optional[List[str]] = None):
        self._ids = itertools.count(1)
        self.requests: List[str] = []
        self.writes: List[Dict[str, Any]] = []
        self.app_roles = app_roles or []
        self.codes: Dict[str, List[str]] = {}
        self.refresh: Dict[str, List[str]] = {}
        self.folders = {
            "inbox": {"id": "fld-inbox", "displayName": "Inbox"},
            "archive": {"id": "fld-archive", "displayName": "Archive"},
            "drafts": {"id": "fld-drafts", "displayName": "Drafts"},
            "sentitems": {"id": "fld-sent", "displayName": "Sent Items"},
            "deleteditems": {"id": "fld-deleted", "displayName": "Deleted Items"},
        }
        self.child_folders = {"fld-inbox": [{"id": "fld-invoices", "displayName": "Invoices"}]}
        self.messages: Dict[str, Dict[str, Any]] = {}
        for subject, sender, folder, body in (
            ("Invoice INV-2041 overdue", "billing@vendor.test", "fld-inbox",
             "Hi Dana, invoice INV-2041 for $12,400 is 15 days overdue. Please confirm payment date."),
            ("Q3 security review", "ciso@contoso.test", "fld-inbox",
             "Reminder: the Q3 security review is Thursday 10:00. Bring the open-detections list."),
            ("Lunch?", "lee@contoso.test", "fld-inbox", "Tacos at noon?"),
        ):
            self._add_message(subject, sender, folder, body)
        self.team = {"id": "team-soc", "displayName": "SOC"}
        self.channels = {"chan-alerts": "Alerts", "chan-general": "General"}
        self.channel_posts: Dict[str, List[Dict[str, Any]]] = {c: [] for c in self.channels}
        self.chats = {"chat-lee": {"chatType": "oneOnOne", "topic": None,
                                   "members": [ME["id"], "u-lee"], "names": [ME["displayName"], "Lee Ops"]}}
        self.chat_msgs: Dict[str, List[Dict[str, Any]]] = {"chat-lee": []}
        self.channel_posts["chan-alerts"].append(self._chat_msg("Morning: 3 new high-severity detections overnight.", "u-lee", "Lee Ops"))

    # ------------------------------------------------------------ helpers

    def _nid(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids)}"

    def _add_message(self, subject, sender, folder, body, *, draft=False, to=None) -> Dict[str, Any]:
        mid = self._nid("msg")
        m = {
            "id": mid, "subject": subject, "isRead": False, "isDraft": draft,
            "from": {"emailAddress": {"address": sender}},
            "toRecipients": to or [{"emailAddress": {"address": ME["mail"]}}],
            "receivedDateTime": "2026-11-20T08:%02d:00Z" % (len(self.messages) % 60),
            "bodyPreview": body[:255], "body": {"contentType": "text", "content": body},
            "parentFolderId": folder, "flag": {"flagStatus": "notFlagged"},
            "webLink": f"https://outlook.office.com/mail/id/{mid}",
        }
        self.messages[mid] = m
        return m

    def _chat_msg(self, text, uid=ME["id"], name=ME["displayName"]) -> Dict[str, Any]:
        mid = str(1700000000000 + next(self._ids))
        return {"id": mid, "messageType": "message", "createdDateTime": "2026-11-20T09:00:00Z",
                "lastModifiedDateTime": "2026-11-20T09:00:00Z",
                "from": {"user": {"id": uid, "displayName": name}},
                "body": {"contentType": "text", "content": text}, "replies": [],
                "webUrl": f"https://teams.microsoft.com/l/message/{mid}"}

    @staticmethod
    def _json(status: int, body: Any = None) -> httpx.Response:
        if body is None:
            return httpx.Response(status)
        return httpx.Response(status, json=body)

    def _err(self, status: int, code: str, message: str) -> httpx.Response:
        return self._json(status, {"error": {"code": code, "message": message}})

    def _scopes(self, request: httpx.Request) -> set:
        auth = request.headers.get("authorization", "")
        tok = auth.split(" ", 1)[1] if " " in auth else ""
        try:
            payload = tok.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
        except Exception:
            return set()
        return set(str(claims.get("scp") or "").split()) | set(claims.get("roles") or [])

    def _need(self, request, *alts) -> Optional[httpx.Response]:
        if not ({a.lower() for a in alts} & {s.lower() for s in self._scopes(request)}):
            return self._err(403, "ErrorAccessDenied", f"Access is denied. Requires {alts[0]}.")
        return None

    def _folder_id(self, ref: str) -> Optional[str]:
        if ref in self.folders:
            return self.folders[ref]["id"]
        all_ids = {f["id"] for f in self.folders.values()} | {c["id"] for cs in self.child_folders.values() for c in cs}
        return ref if ref in all_ids else None

    # ------------------------------------------------------------- router

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url}")
        path = request.url.path
        q = dict(urllib.parse.parse_qsl(request.url.query.decode()))
        m = re.fullmatch(r"/([^/]+)/oauth2/v2\.0/(authorize|token)", path)
        if m:
            return self._identity(request, m.group(2), q)
        if path.startswith("/_fake/"):
            return self._json(200, {"writes": self.writes})
        if "/v1.0" in path:
            path = path.split("/v1.0", 1)[1]
        body = {}
        if request.content:
            try:
                body = json.loads(request.content)
            except Exception:
                body = {}
        return self._graph(request, request.method, path, q, body)

    # -------------------------------------------------------- identity

    def _identity(self, request, kind, q) -> httpx.Response:
        if kind == "authorize":
            code = self._nid("code")
            self.codes[code] = _graph_scopes(q.get("scope", ""))
            dest = q["redirect_uri"] + "?" + urllib.parse.urlencode({"code": code, "state": q.get("state", "")})
            return httpx.Response(302, headers={"location": dest})
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        grant = form.get("grant_type")
        if grant == "client_credentials":
            return self._json(200, {"access_token": make_token(self.app_roles, app=True),
                                    "token_type": "Bearer", "expires_in": 3600})
        if grant == "authorization_code":
            scopes = self.codes.pop(form.get("code", ""), None)
            if scopes is None:
                return self._json(400, {"error": "invalid_grant"})
        elif grant == "refresh_token":
            scopes = self.refresh.get(form.get("refresh_token", ""))
            if scopes is None:
                return self._json(400, {"error": "invalid_grant"})
        else:
            return self._json(400, {"error": "unsupported_grant_type"})
        rt = self._nid("rt")
        self.refresh[rt] = scopes
        return self._json(200, {"access_token": make_token(scopes), "refresh_token": rt,
                                "token_type": "Bearer", "expires_in": 3600, "scope": " ".join(scopes)})

    # ----------------------------------------------------------- graph

    def _graph(self, request, method, path, q, body) -> httpx.Response:  # noqa: C901 - a router
        if path == "/me":
            return self._json(200, ME)
        # ---- mail folders
        if path == "/me/mailFolders" and method == "GET":
            return self._json(200, {"value": [
                {**f, "parentFolderId": None, "childFolderCount": len(self.child_folders.get(f["id"], [])),
                 "unreadItemCount": sum(1 for m in self.messages.values() if m["parentFolderId"] == f["id"] and not m["isRead"]),
                 "totalItemCount": sum(1 for m in self.messages.values() if m["parentFolderId"] == f["id"])}
                for f in self.folders.values()]})
        mm = re.fullmatch(r"/me/mailFolders/([^/]+)/childFolders", path)
        if mm:
            return self._json(200, {"value": [{**c, "parentFolderId": mm.group(1), "childFolderCount": 0,
                                               "unreadItemCount": 0, "totalItemCount": 0}
                                              for c in self.child_folders.get(mm.group(1), [])]})
        mm = re.fullmatch(r"/me/mailFolders/([^/]+)", path)
        if mm and method == "GET":
            fid = self._folder_id(urllib.parse.unquote(mm.group(1)))
            f = next((x for x in self.folders.values() if x["id"] == fid), None)
            return self._json(200, {**f, "unreadItemCount": 0, "totalItemCount": 0}) if f else self._err(404, "ErrorItemNotFound", "folder")
        mm = re.fullmatch(r"/me/mailFolders/([^/]+)/messages", path)
        if mm and method == "GET":
            fid = self._folder_id(urllib.parse.unquote(mm.group(1)))
            rows = [m for m in self.messages.values() if m["parentFolderId"] == fid and not m["isDraft"]]
            return self._json(200, {"value": sorted(rows, key=lambda m: m["receivedDateTime"], reverse=True)})
        # ---- messages
        if path == "/me/messages" and method == "GET":
            rows = [m for m in self.messages.values() if not m["isDraft"]]
            return self._json(200, {"value": sorted(rows, key=lambda m: m["receivedDateTime"], reverse=True)})
        if path == "/me/messages" and method == "POST":
            if (r := self._need(request, "Mail.ReadWrite")):
                return r
            m = self._add_message(body.get("subject") or "", ME["mail"], "fld-drafts",
                                  (body.get("body") or {}).get("content", ""), draft=True,
                                  to=body.get("toRecipients") or [])
            self.writes.append({"op": "create_draft", "id": m["id"], "subject": m["subject"], "to": m["toRecipients"]})
            return self._json(201, m)
        if path == "/me/sendMail" and method == "POST":
            if (r := self._need(request, "Mail.Send")):
                return r
            msg = body.get("message") or {}
            self.writes.append({"op": "send_mail", "subject": msg.get("subject"),
                                "to": [x["emailAddress"]["address"] for x in msg.get("toRecipients") or []]})
            return httpx.Response(202)
        mm = re.fullmatch(r"/me/messages/([^/]+)(?:/(move|send|createReply|createReplyAll|createForward))?", path)
        if mm:
            mid, verb = urllib.parse.unquote(mm.group(1)), mm.group(2)
            msg = self.messages.get(mid)
            if msg is None:
                return self._err(404, "ErrorItemNotFound", "The specified object was not found in the store.")
            if verb is None and method == "GET":
                return self._json(200, msg)
            if verb is None and method == "PATCH":
                if (r := self._need(request, "Mail.ReadWrite")):
                    return r
                for k in ("isRead", "flag", "categories", "subject"):
                    if k in body:
                        msg[k] = body[k]
                self.writes.append({"op": "update", "id": mid, "changes": body})
                return self._json(200, msg)
            if verb == "move":
                if (r := self._need(request, "Mail.ReadWrite")):
                    return r
                fid = self._folder_id(body.get("destinationId", ""))
                if not fid:
                    return self._err(400, "ErrorInvalidIdMalformed", "destinationId")
                del self.messages[mid]
                new = {**msg, "id": self._nid("msg"), "parentFolderId": fid}
                self.messages[new["id"]] = new
                self.writes.append({"op": "move", "id": mid, "new_id": new["id"], "folder": fid})
                return self._json(201, new)
            if verb == "send":
                if (r := self._need(request, "Mail.Send")):
                    return r
                if not msg["isDraft"]:
                    return self._err(400, "ErrorInvalidRequest", "Only drafts can be sent.")
                msg["isDraft"], msg["parentFolderId"] = False, "fld-sent"
                self.writes.append({"op": "send_draft", "id": mid, "subject": msg["subject"],
                                    "to": [x["emailAddress"]["address"] for x in msg["toRecipients"]]})
                return httpx.Response(202)
            if verb in ("createReply", "createReplyAll", "createForward"):
                if (r := self._need(request, "Mail.ReadWrite")):
                    return r
                prefix = "FW: " if verb == "createForward" else "RE: "
                to = ((body.get("message") or {}).get("toRecipients")
                      or ([] if verb == "createForward" else [msg["from"]]))
                d = self._add_message(prefix + msg["subject"], ME["mail"], "fld-drafts",
                                      ((body.get("message") or {}).get("body") or {}).get("content", ""),
                                      draft=True, to=to)
                self.writes.append({"op": verb, "id": mid, "draft_id": d["id"]})
                return self._json(201, d)
        # ---- teams
        if path == "/me/joinedTeams":
            return self._json(200, {"value": [self.team]})
        if path == f"/teams/{self.team['id']}/channels":
            return self._json(200, {"value": [{"id": c, "displayName": n, "webUrl": f"https://teams/c/{c}"}
                                              for c, n in self.channels.items()]})
        if path == "/me/chats":
            return self._json(200, {"value": [
                {"id": cid, "chatType": c["chatType"], "topic": c["topic"], "lastUpdatedDateTime": "2026-11-20T09:00:00Z",
                 "webUrl": f"https://teams/chat/{cid}",
                 "members": [{"userId": u, "displayName": n} for u, n in zip(c["members"], c["names"])]}
                for cid, c in self.chats.items()]})
        mm = re.fullmatch(r"/teams/([^/]+)/channels/([^/]+)/messages(?:/([^/]+)/replies)?", path)
        if mm:
            chan, root = mm.group(2), mm.group(3)
            if chan not in self.channels:
                return self._err(404, "NotFound", "channel")
            if method == "GET":
                rows = list(reversed(self.channel_posts[chan]))
                if root:
                    rows = next((list(reversed(r.get("replies") or [])) for r in self.channel_posts[chan] if r["id"] == root), [])
                return self._json(200, {"value": rows})
            if (r := self._need(request, "ChannelMessage.Send", "Group.ReadWrite.All")):
                return r
            msg = self._chat_msg((body.get("body") or {}).get("content", ""))
            if root:
                parent = next((r for r in self.channel_posts[chan] if r["id"] == root), None)
                if parent is None:
                    return self._err(404, "NotFound", "message")
                parent.setdefault("replies", []).append(msg)
            else:
                self.channel_posts[chan].append(msg)
            self.writes.append({"op": "channel_post", "channel": self.channels[chan], "reply_to": root,
                                "text": msg["body"]["content"]})
            return self._json(201, msg)
        mm = re.fullmatch(r"/chats/([^/]+)/messages", path)
        if mm:
            cid = mm.group(1)
            if cid not in self.chats:
                return self._err(404, "NotFound", "chat")
            if method == "GET":
                return self._json(200, {"value": list(reversed(self.chat_msgs[cid]))})
            if (r := self._need(request, "ChatMessage.Send", "Chat.ReadWrite")):
                return r
            msg = self._chat_msg((body.get("body") or {}).get("content", ""))
            self.chat_msgs[cid].append(msg)
            self.writes.append({"op": "chat_message", "chat": cid, "text": msg["body"]["content"]})
            return self._json(201, msg)
        if path == "/search/query":
            return self._json(200, {"value": [{"hitsContainers": [{"total": 0, "moreResultsAvailable": False, "hits": []}]}]})
        return self._err(404, "NotImplementedInFake", f"{method} {path}")
