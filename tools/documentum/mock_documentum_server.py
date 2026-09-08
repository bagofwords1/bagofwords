#!/usr/bin/env python3
"""Mock OpenText Documentum REST Services (`/dctm-rest`) + OTDS (`/otdsws`).

No Documentum can be downloaded without an OpenText entitlement (see
docs/documentum-lab-access.md), so this simulates the exact surface
`DocumentumClient` touches, with response shapes taken from the public REST
reference clients, the REST 25.4 javadocs and recorded community responses:

  GET  /dctm-rest/services                                  home document
  GET  /dctm-rest/repositories                              feed
  GET  /dctm-rest/repositories/{repo}                       repository + links
  GET  /dctm-rest/repositories/{repo}?dql=...               DQL (SELECT only)
  GET  /dctm-rest/repositories/{repo}/currentuser
  GET  /dctm-rest/repositories/{repo}/cabinets
  GET  /dctm-rest/repositories/{repo}/folders/{id}
  GET  /dctm-rest/repositories/{repo}/folders/{id}/folders
  GET  /dctm-rest/repositories/{repo}/folders/{id}/documents[?object-type=]
  GET  /dctm-rest/repositories/{repo}/objects/{id}
  GET  /dctm-rest/repositories/{repo}/objects/{id}/contents/content
  GET  /dctm-rest/repositories/{repo}/objects/{id}/content-media
  GET  /dctm-rest/repositories/{repo}/search?q=...
  GET  /dctm-rest/repositories/{repo}/formats/{name}
  POST /otdsws/oauth2/token   client_credentials | password | token-exchange
                              (impersonation) | authorization_code | refresh_token
  GET  /otdsws/oauth2/auth    authorization-code flow (auto-consent with
                              login_hint, or a tiny login form)

Faithfulness rules enforced on purpose:
  - Feeds page with `page` (1-based) / `items-per-page`, expose `total` only
    with include-total=true and always carry `next`/`previous` links.
  - Documentum ACLs are enforced: a user without Browse on an object gets a
    403 on direct access and never sees it in listings, search or DQL.
  - Only CURRENT versions are returned unless the DQL says `(ALL)`.
  - `a_content_type` is a Documentum FORMAT name (msw12, excel12book,
    crtext, pdf, csv), not a MIME type — the client must map it.
  - Bearer tokens come only from the OTDS endpoints and expire
    (DCTM_MOCK_TOKEN_TTL seconds, default 3600).
  - Impersonation via RFC 8693 token exchange works only for the OAuth
    client that has "Allow impersonation" (bow); `noimp` gets invalid_grant.

Users (Basic or OTDS password grant, partition "Corp"):
  dmadmin / dmadmin123   superuser, sees everything
  alice   / alice123     groups finance + hr
  bob     / bob123       group finance
  carol   / carol123     no group (only /Engineering + /Temp)
OAuth clients: bow / bow-secret (impersonation allowed, service user svc_bow),
               noimp / noimp-secret (no impersonation).

Run:  python tools/documentum/mock_documentum_server.py   (port DCTM_MOCK_PORT, default 8081)
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import secrets
import struct
import threading
import time
import zipfile
import zlib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

REPO = "bow_demo"
PARTITION = "Corp"
DCTM_JSON = "application/vnd.emc.documentum+json"
LINKREL = "http://identifiers.emc.com/linkrel/"
TOKEN_TTL = int(os.environ.get("DCTM_MOCK_TOKEN_TTL", "3600"))
DEFAULT_PAGE_SIZE = int(os.environ.get("DCTM_MOCK_PAGE_SIZE", "100"))
MAX_PAGE_SIZE = 1000

USERS = {
    # user_login_name -> record. dmadmin is the superuser.
    "dmadmin": {"password": "dmadmin123", "user_name": "dmadmin", "user_address": "dmadmin@example.com",
                "groups": {"finance", "hr", "engineering"}, "superuser": True},
    "alice": {"password": "alice123", "user_name": "Alice Cohen", "user_address": "alice@example.com",
              "groups": {"finance", "hr"}, "superuser": False},
    "bob": {"password": "bob123", "user_name": "Bob Levi", "user_address": "bob@example.com",
            "groups": {"finance"}, "superuser": False},
    "carol": {"password": "carol123", "user_name": "Carol Adam", "user_address": "carol@example.com",
              "groups": set(), "superuser": False},
    "svc_bow": {"password": None, "user_name": "svc_bow", "user_address": "svc_bow@example.com",
                "groups": {"finance", "hr", "engineering"}, "superuser": True},
}
OAUTH_CLIENTS = {
    "bow": {"secret": "bow-secret", "allow_impersonation": True, "service_user": "svc_bow"},
    "noimp": {"secret": "noimp-secret", "allow_impersonation": False, "service_user": "svc_bow"},
}
# acl_name -> groups allowed to Browse/Read. "everyone" = all authenticated users.
ACLS = {
    "dm_world": {"everyone"},
    "finance_acl": {"finance"},
    "hr_acl": {"hr"},
    "engineering_acl": {"everyone"},
}
FORMATS = {
    "csv": ("text/csv", "csv"), "excel12book": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
    "pdf": ("application/pdf", "pdf"), "msw12": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
    "crtext": ("text/plain", "txt"), "text": ("text/plain", "txt"), "json": ("application/json", "json"),
    "unknown": ("application/octet-stream", "bin"),
}


# ---------------------------------------------------------------------------
# Tiny OOXML / PDF generators (stdlib only — the mock also runs in a bare
# python:3.12-slim container).
# ---------------------------------------------------------------------------

def make_docx(paragraphs):
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;")
    body = "".join(f"<w:p><w:r><w:t>{esc(p)}</w:t></w:r></w:p>" for p in paragraphs)
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           f'<w:body>{body}</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                   '</Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                   '</Relationships>')
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


def make_xlsx(sheets):
    """sheets: {name: [[cell, ...], ...]} — inline strings and numbers only."""
    def col(n):
        s = ""
        while n:
            n, r = divmod(n - 1, 26)
            s = chr(65 + r) + s
        return s
    esc = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        names = list(sheets)
        overrides = "".join(f'<Override PartName="/xl/worksheets/sheet{i+1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(len(names)))
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' + overrides + '</Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        sheet_tags = "".join(f'<sheet name="{esc(n)}" sheetId="{i+1}" r:id="rId{i+1}"/>' for i, n in enumerate(names))
        z.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   f'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>{sheet_tags}</sheets></workbook>')
        rels = "".join(f'<Relationship Id="rId{i+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i+1}.xml"/>' for i in range(len(names)))
        z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + rels + '</Relationships>')
        for i, n in enumerate(names):
            rows = []
            for r, row in enumerate(sheets[n], start=1):
                cells = []
                for c, v in enumerate(row, start=1):
                    ref = f"{col(c)}{r}"
                    if isinstance(v, (int, float)):
                        cells.append(f'<c r="{ref}"><v>{v}</v></c>')
                    else:
                        cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{esc(v)}</t></is></c>')
                rows.append(f'<row r="{r}">{"".join(cells)}</row>')
            z.writestr(f"xl/worksheets/sheet{i+1}.xml", '<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                       f'<sheetData>{"".join(rows)}</sheetData></worksheet>')
    return buf.getvalue()


def make_pdf(lines_per_page):
    """A minimal multi-page PDF with Helvetica text (pypdf extracts it)."""
    objs = []  # (id, bytes)
    font_id = 3
    page_ids = []
    next_id = 4
    content_ids = []
    for lines in lines_per_page:
        page_ids.append(next_id); content_ids.append(next_id + 1); next_id += 2
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    objs.append((1, b"<< /Type /Catalog /Pages 2 0 R >>"))
    objs.append((2, f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()))
    objs.append((3, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"))
    for pid, cid, lines in zip(page_ids, content_ids, lines_per_page):
        text = "BT /F1 12 Tf 50 750 Td 16 TL " + " ".join(
            "(" + l.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") Tj T*" for l in lines) + " ET"
        stream = text.encode("latin-1")
        objs.append((pid, f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {cid} 0 R /Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()))
        objs.append((cid, f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"))
    objs.sort()
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = {}
    for oid, body in objs:
        offsets[oid] = out.tell()
        out.write(f"{oid} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    n = max(offsets) + 1
    out.write(f"xref\n0 {n}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for i in range(1, n):
        out.write(f"{offsets[i]:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


# ---------------------------------------------------------------------------
# Repository seed
# ---------------------------------------------------------------------------

def _oid(kind: str, n: int) -> str:
    # Documentum ids: 2-hex type tag + 6-hex docbase id + 8-hex counter.
    tag = {"cabinet": "0c", "folder": "0b", "document": "09", "user": "11"}[kind]
    return f"{tag}0180aa{n:08x}"


class Repo:
    def __init__(self):
        self.objects = {}  # id -> dict
        self.children = {}  # folder id -> [ids]
        self._n = 0x1000
        now = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)
        self.now = now
        self._seed()

    def _new(self, kind, **props):
        self._n += 1
        oid = _oid(kind, self._n)
        rec = {"r_object_id": oid, "r_object_type": {"cabinet": "dm_cabinet", "folder": "dm_folder", "document": "dm_document"}[kind],
               "r_creation_date": "2026-01-15T09:00:00.000+00:00", "r_modify_date": "2026-08-01T10:00:00.000+00:00",
               "owner_name": "dmadmin", "acl_name": "dm_world", "acl_domain": REPO, "title": "", "subject": "",
               "i_folder_id": [], "r_version_label": ["1.0", "CURRENT"], "i_chronicle_id": oid, "_kind": kind}
        rec.update(props)
        self.objects[oid] = rec
        return rec

    def _folder(self, name, parent=None, acl="dm_world"):
        kind = "cabinet" if parent is None else "folder"
        path = f"/{name}" if parent is None else f"{parent['r_folder_path'][0]}/{name}"
        rec = self._new(kind, object_name=name, r_folder_path=[path], acl_name=acl,
                        i_folder_id=[parent["r_object_id"]] if parent else [])
        self.children.setdefault(rec["r_object_id"], [])
        if parent:
            self.children[parent["r_object_id"]].append(rec["r_object_id"])
        return rec

    def _doc(self, folder, name, fmt, data, acl=None, otype="dm_document", title="", modified=None, extra_folders=(), old_versions=()):
        rec = self._new("document", object_name=name, a_content_type=fmt, r_full_content_size=len(data), r_content_size=len(data),
                        acl_name=acl or folder["acl_name"], r_object_type=otype, title=title,
                        i_folder_id=[folder["r_object_id"]] + [f["r_object_id"] for f in extra_folders],
                        r_modify_date=modified or "2026-08-01T10:00:00.000+00:00")
        rec["_content"] = data
        self.children[folder["r_object_id"]].append(rec["r_object_id"])
        for f in extra_folders:
            self.children[f["r_object_id"]].append(rec["r_object_id"])
        for i, (label, old) in enumerate(old_versions, start=1):
            v = self._new("document", object_name=name, a_content_type=fmt, r_full_content_size=len(old), r_content_size=len(old),
                          acl_name=rec["acl_name"], r_object_type=otype, title=title, i_folder_id=list(rec["i_folder_id"]),
                          r_version_label=[label], i_chronicle_id=rec["r_object_id"], r_modify_date="2026-03-01T10:00:00.000+00:00")
            v["_content"] = old
        return rec

    def _seed(self):
        finance = self._folder("Finance", acl="finance_acl")
        reports = self._folder("Reports", finance, acl="finance_acl")
        y2026 = self._folder("2026", reports, acl="finance_acl")
        invoices = self._folder("Invoices", finance, acl="finance_acl")
        hr = self._folder("HR", acl="hr_acl")
        policies = self._folder("Policies", hr, acl="hr_acl")
        eng = self._folder("Engineering", acl="engineering_acl")
        specs = self._folder("Specs", eng, acl="engineering_acl")
        temp = self._folder("Temp")

        q1 = ("region,product,revenue,units\nNorth,Widgets,120000,480\nSouth,Widgets,80000,320\n"
              "North,Gadgets,64000,160\nSouth,Gadgets,45500,130\nEast,Widgets,98000,392\n").encode()
        q1_old = "region,product,revenue,units\nNorth,Widgets,100000,400\n".encode()
        q2 = ("region,product,revenue,units\nNorth,Widgets,131000,524\nSouth,Widgets,90500,362\n"
              "North,Gadgets,71000,178\nSouth,Gadgets,52000,149\nEast,Widgets,104000,416\nWest,Gadgets,38000,95\n").encode()
        self._doc(y2026, "Q1_revenue.csv", "csv", q1, title="Q1 2026 revenue by region", old_versions=[("1.0", q1_old)])
        self.objects[[k for k, v in self.objects.items() if v["object_name"] == "Q1_revenue.csv" and "CURRENT" in v["r_version_label"]][0]]["r_version_label"] = ["2.0", "CURRENT"]
        self._doc(y2026, "Q2_revenue.csv", "csv", q2, title="Q2 2026 revenue by region")
        self._doc(y2026, "regional_targets.xlsx", "excel12book", make_xlsx({
            "Targets": [["region", "target_2026", "owner"], ["North", 500000, "Alice Cohen"], ["South", 350000, "Bob Levi"], ["East", 400000, "Alice Cohen"], ["West", 150000, "Bob Levi"]],
            "Notes": [["note"], ["Targets approved by the board in January 2026"]],
        }), title="Regional targets 2026")
        self._doc(y2026, "board_summary.txt", "crtext",
                  b"Board summary H1 2026: revenue grew 12 percent year over year. Widgets remain the strongest product line. "
                  b"Travel spending is capped at 500 euros per trip under the new travel policy.",
                  title="Board summary H1 2026", extra_folders=[policies])
        for n, (num, amount, vendor) in enumerate([("1001", "12,400.00", "Acme Logistics"), ("1002", "3,150.50", "Blue Bird Catering")]):
            self._doc(invoices, f"INV-{num}.pdf", "pdf", make_pdf([
                [f"INVOICE {num}", f"Vendor: {vendor}", f"Amount due: EUR {amount}", "Payment terms: 30 days"],
                ["Page 2", "Line items", "Freight and handling", "Thank you for your business"],
            ]), otype="bow_invoice", title=f"Invoice {num} {vendor}")
        self._doc(policies, "Travel_Policy.docx", "msw12", make_docx([
            "Travel Policy", "Employees may expense travel up to 500 euros per trip.",
            "Business class is permitted for flights longer than six hours.", "All expenses require a receipt.",
        ]), title="Travel policy")
        self._doc(policies, "Salary_Bands.csv", "csv",
                  b"band,min_salary,max_salary\nL1,45000,60000\nL2,60000,80000\nL3,80000,110000\n", title="Salary bands (confidential)")
        self._doc(specs, "architecture.md", "crtext",
                  b"# Platform architecture\n\nThe ingestion service reads from Documentum REST Services and writes to the lake.\n",
                  title="Platform architecture")
        self._doc(specs, "release_notes.txt", "crtext", b"Release 26.2: added Documentum connector, OTDS impersonation, and faster search.",
                  title="Release notes 26.2")
        self._doc(temp, "scratch.json", "json", json.dumps({"kind": "scratch", "items": [1, 2, 3]}).encode())
        self._doc(temp, "big.bin", "unknown", os.urandom(1_600_000))  # > 1 MB to exercise download limits
        self.root_ids = [o["r_object_id"] for o in self.objects.values() if o["_kind"] == "cabinet"]

    # ---- access control ---------------------------------------------------

    def can_read(self, user, obj):
        u = USERS[user]
        if u["superuser"]:
            return True
        allowed = ACLS.get(obj["acl_name"], set())
        return "everyone" in allowed or bool(allowed & u["groups"])

    def is_current(self, obj):
        return "CURRENT" in obj["r_version_label"]

    def folder_paths(self, obj):
        if obj["_kind"] != "document":
            return list(obj["r_folder_path"])
        return [self.objects[f]["r_folder_path"][0] for f in obj["i_folder_id"] if f in self.objects]

    def type_matches(self, obj, dql_type):
        t = obj["r_object_type"]
        if dql_type == "dm_sysobject":
            return True
        if dql_type == "dm_document":
            return t in ("dm_document", "bow_invoice")
        if dql_type == "dm_folder":
            return t in ("dm_folder", "dm_cabinet")
        return t == dql_type


REPO_DATA = Repo()
TOKENS = {}   # access token -> {user, exp, client}
REFRESH = {}  # refresh token -> {user, client}
CODES = {}    # auth code -> {user, client, redirect_uri, exp}
LOCK = threading.Lock()
# Test hooks: recent (user, path) pairs, and a switch that makes the content
# resource point at an ACS server even for media-url-policy=local (a client
# must refuse to follow off-REST links).
REQUEST_LOG = []
FORCE_ACS_LINKS = False


# ---------------------------------------------------------------------------
# DQL (SELECT subset)
# ---------------------------------------------------------------------------

_DQL_RE = re.compile(r"^\s*SELECT\s+(?P<cols>.+?)\s+FROM\s+(?P<type>[a-z_]\w*)\s*(?P<all>\(ALL\))?\s*(?:WHERE\s+(?P<where>.+?))?\s*(?:ORDER\s+BY\s+(?P<order>[\w\s,]+?))?\s*(?:ENABLE\s*\(.*\))?\s*$", re.I | re.S)


def _unq(s):
    return s.strip()[1:-1].replace("''", "'")


def _cond(obj, repo, cond):
    cond = cond.strip()
    if cond.startswith("(") and cond.endswith(")"):
        inner = cond[1:-1]
        return any(_cond(obj, repo, c) for c in re.split(r"\s+OR\s+", inner, flags=re.I))
    m = re.match(r"^FOLDER\s*\(\s*('(?:[^']|'')*')\s*(,\s*DESCEND)?\s*\)$", cond, re.I)
    if m:
        path, descend = _unq(m.group(1)).rstrip("/"), bool(m.group(2))
        if path == "" and descend:  # FOLDER('/', DESCEND) — the whole repository
            return bool(repo.folder_paths(obj)) or obj["_kind"] == "cabinet"
        for p in repo.folder_paths(obj):
            parent = p.rsplit("/", 1)[0] if obj["_kind"] == "document" else p.rsplit("/", 1)[0]
            if obj["_kind"] == "document":
                if p == path or (descend and p.startswith(path + "/")):
                    return True
            else:
                if parent == path or (descend and parent.startswith(path + "/")):
                    return True
        return False
    m = re.match(r"^(ANY\s+)?(\w+)\s*(=|LIKE)\s*('(?:[^']|'')*')$", cond, re.I)
    if m:
        attr, op, val = m.group(2), m.group(3).upper(), _unq(m.group(4))
        return _match_attr(obj, repo, attr, op, val, lower=False)
    m = re.match(r"^LOWER\s*\(\s*(\w+)\s*\)\s*(=|LIKE)\s*('(?:[^']|'')*')$", cond, re.I)
    if m:
        return _match_attr(obj, repo, m.group(1), m.group(2).upper(), _unq(m.group(3)), lower=True)
    raise ValueError(f"DQL predicate not supported by the mock: {cond}")


def _match_attr(obj, repo, attr, op, val, lower):
    values = obj.get(attr)
    if attr == "r_folder_path" and obj["_kind"] == "document":
        values = repo.folder_paths(obj)
    if values is None:
        return False
    if not isinstance(values, list):
        values = [values]
    values = [str(v) for v in values]
    if lower:
        values = [v.lower() for v in values]
        val = val.lower()
    if op == "=":
        return val in values
    rx = re.compile("^" + re.escape(val).replace("%", ".*").replace("_", ".") + "$", re.S)
    return any(rx.match(v) for v in values)


def run_dql(repo, user, dql):
    m = _DQL_RE.match(dql)
    if not m:
        raise ValueError("Only SELECT statements are supported")
    cols = [c.strip() for c in m.group("cols").split(",")]
    dql_type = m.group("type").lower()
    all_versions = bool(m.group("all"))
    where = m.group("where")
    rows = []
    if dql_type == "dm_user":
        for login, u in USERS.items():
            rec = {"user_login_name": login, "user_name": u["user_name"], "user_address": u["user_address"], "user_source": "OTDS", "r_object_id": _oid("user", hash(login) & 0xFFFF)}
            conds = re.split(r"\s+AND\s+(?![^(]*\))", where, flags=re.I) if where else []
            if all(_cond(rec | {"_kind": "user"}, repo, c) for c in conds):
                rows.append({c: rec.get(c.split()[-1], None) for c in cols} if cols != ["*"] else rec)
        return rows
    conds = re.split(r"\s+AND\s+(?![^(]*\))", where, flags=re.I) if where else []
    for obj in repo.objects.values():
        if not repo.type_matches(obj, dql_type):
            continue
        if obj["_kind"] == "document" and not all_versions and not repo.is_current(obj):
            continue
        if not repo.can_read(user, obj):
            continue
        if all(_cond(obj, repo, c) for c in conds):
            rows.append(public_props(obj) if cols == ["*"] else {c: public_props(obj).get(c) for c in cols})
    order = m.group("order")
    if order:
        key = order.split(",")[0].split()[0]
        rows.sort(key=lambda r: str(r.get(key) or ""))
    return rows


def public_props(obj):
    return {k: v for k, v in obj.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "DocumentumMock/1.0"
    protocol_version = "HTTP/1.1"
    # Buffer each response and disable Nagle: headers + body leave in one
    # segment, otherwise every keep-alive request pays a ~40 ms delayed-ACK.
    wbufsize = 1 << 16
    disable_nagle_algorithm = True

    def log_message(self, fmt, *args):  # quieter default
        if os.environ.get("DCTM_MOCK_VERBOSE"):
            super().log_message(fmt, *args)

    # ---- helpers ----------------------------------------------------------
    @property
    def base(self):
        return f"http://{self.headers.get('Host', 'localhost')}"

    def repo_url(self):
        return f"{self.base}/dctm-rest/repositories/{REPO}"

    def send_json(self, status, body, ctype=DCTM_JSON, extra=None):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send_bytes(self, status, data, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def error(self, status, code, message):
        self.send_json(status, {"status": status, "code": code, "message": message, "details": message, "id": secrets.token_hex(8)})

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def authenticate(self, query):
        """Basic (repository user) or Bearer/`access_token` (OTDS)."""
        auth = self.headers.get("Authorization") or ""
        token = None
        if auth.lower().startswith("basic "):
            try:
                user, pw = base64.b64decode(auth.split(None, 1)[1]).decode().split(":", 1)
            except Exception:
                return None
            u = USERS.get(user)
            if u and u["password"] and pw == u["password"]:
                return user
            return None
        if auth.lower().startswith("bearer "):
            token = auth.split(None, 1)[1].strip()
        elif query.get("access_token"):
            token = query["access_token"][0]
        if token:
            with LOCK:
                rec = TOKENS.get(token)
            if rec and rec["exp"] > time.time():
                return rec["user"]
        return None

    def feed(self, title, path, entries, query, total_count):
        page = max(1, int(query.get("page", ["1"])[0]))
        size = min(MAX_PAGE_SIZE, max(1, int(query.get("items-per-page", [str(DEFAULT_PAGE_SIZE)])[0])))
        start = (page - 1) * size
        chunk = entries[start:start + size]
        self_q = {k: v[0] for k, v in query.items()}
        def link(p):
            q = dict(self_q); q["page"] = str(p); q["items-per-page"] = str(size)
            return f"{self.base}{path}?{urlencode(q)}"
        links = [{"rel": "self", "href": link(page)}]
        if start + size < len(entries):
            links.append({"rel": "next", "href": link(page + 1)})
        if page > 1:
            links.append({"rel": "previous", "href": link(page - 1)})
        body = {"id": f"{self.base}{path}", "title": title, "author": [{"name": "OpenText Documentum"}],
                "updated": REPO_DATA.now.isoformat(), "page": page, "items-per-page": size, "links": links, "entries": chunk}
        if query.get("include-total", ["false"])[0].lower() == "true":
            body["total"] = total_count
        return body

    def obj_links(self, obj):
        ru = self.repo_url()
        oid = obj["r_object_id"]
        if obj["_kind"] == "document":
            return [{"rel": "self", "href": f"{ru}/objects/{oid}"}, {"rel": "edit", "href": f"{ru}/objects/{oid}"},
                    {"rel": LINKREL + "primary-content", "href": f"{ru}/objects/{oid}/contents/content"},
                    {"rel": LINKREL + "content-media", "href": f"{ru}/objects/{oid}/content-media"},
                    {"rel": LINKREL + "parent-links", "href": f"{ru}/objects/{oid}/parent-links"},
                    {"rel": "version-history", "href": f"{ru}/objects/{oid}/versions"}]
        base = "cabinets" if obj["_kind"] == "cabinet" else "folders"
        return [{"rel": "self", "href": f"{ru}/{base}/{oid}"},
                {"rel": LINKREL + "folders", "href": f"{ru}/folders/{oid}/folders"},
                {"rel": LINKREL + "documents", "href": f"{ru}/folders/{oid}/documents"},
                {"rel": LINKREL + "objects", "href": f"{ru}/folders/{oid}/objects"}]

    def entry(self, obj, inline):
        e = {"id": self.obj_links(obj)[0]["href"], "title": obj["object_name"], "summary": obj.get("title") or obj["object_name"],
             "updated": obj["r_modify_date"], "published": obj["r_creation_date"], "links": self.obj_links(obj)}
        if inline:
            e["content"] = self.object_body(obj)
        return e

    def object_body(self, obj):
        name = {"document": "document", "folder": "folder", "cabinet": "cabinet"}[obj["_kind"]]
        return {"name": name, "type": obj["r_object_type"], "definition": f"{self.repo_url()}/types/{obj['r_object_type']}",
                "properties": public_props(obj), "links": self.obj_links(obj)}

    # ---- routing ----------------------------------------------------------
    def do_GET(self):
        parts = urlsplit(self.path)
        path, query = unquote(parts.path), parse_qs(parts.query, keep_blank_values=True)
        if path.startswith("/otdsws/"):
            return self.otds_get(path, query)
        if path == "/health":
            return self.send_json(200, {"ok": True}, "application/json")
        if not path.startswith("/dctm-rest"):
            return self.error(404, "E_RESOURCE_NOT_FOUND", "not found")
        if path in ("/dctm-rest/services", "/dctm-rest/services.json"):
            return self.send_json(200, {"resources": {"repositories": {"href": f"{self.base}/dctm-rest/repositories"},
                                                        "about": {"href": f"{self.base}/dctm-rest/about"}}}, "application/home+json")
        if path == "/dctm-rest/about":
            return self.send_json(200, {"name": "Documentum REST Services", "product_version": "26.2.0000.0100", "properties": {}})
        user = self.authenticate(query)
        with LOCK:
            REQUEST_LOG.append((user, path))
            del REQUEST_LOG[:-500]
        if not user:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="com.emc.documentum.rest"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/dctm-rest/repositories":
            entries = [{"id": self.repo_url(), "title": REPO, "summary": "BOW demo repository", "updated": REPO_DATA.now.isoformat(),
                        "links": [{"rel": "edit", "href": self.repo_url()}]}]
            return self.send_json(200, self.feed("Repositories", "/dctm-rest/repositories", entries, query, 1))
        m = re.match(r"^/dctm-rest/repositories/([^/]+)(/.*)?$", path)
        if not m or m.group(1) != REPO:
            return self.error(404, "E_RESOURCE_NOT_FOUND", f"Repository {m.group(1) if m else ''} not found")
        sub = m.group(2) or ""
        return self.repo_get(user, sub, query, path)

    def repo_get(self, user, sub, query, path):
        repo, ru = REPO_DATA, self.repo_url()
        if sub in ("", "/"):
            if "dql" in query:
                try:
                    rows = run_dql(repo, user, query["dql"][0])
                except ValueError as exc:
                    return self.error(400, "E_INPUT_ILLEGAL_ARGUMENTS", str(exc))
                entries = [{"id": f"{ru}?dql-row={i}", "title": f"row {i}", "content": {"properties": r}} for i, r in enumerate(rows)]
                return self.send_json(200, self.feed("DQL query results", path, entries, query, len(rows)))
            return self.send_json(200, {"id": REPO, "name": REPO, "description": "BOW demo repository", "servers": [{"name": REPO, "host": "dctm.example.com", "version": "26.2.0000.0100 Linux64.Postgres", "docbroker": "dctm.example.com"}],
                                        "links": [{"rel": "self", "href": ru}, {"rel": LINKREL + "cabinets", "href": f"{ru}/cabinets"},
                                                  {"rel": LINKREL + "folders", "href": f"{ru}/folders"}, {"rel": LINKREL + "dql", "href": ru + "{?dql,items-per-page,page,inline,include-total}", "hreftemplate": True},
                                                  {"rel": LINKREL + "search", "href": ru + "/search{?q,inline,items-per-page,page,include-total}", "hreftemplate": True},
                                                  {"rel": LINKREL + "current-user", "href": f"{ru}/currentuser"}, {"rel": LINKREL + "formats", "href": f"{ru}/formats"},
                                                  {"rel": LINKREL + "types", "href": f"{ru}/types"}]})
        if sub == "/currentuser":
            u = USERS[user]
            return self.send_json(200, {"name": "user", "type": "dm_user", "properties": {"user_name": u["user_name"], "user_login_name": user, "user_address": u["user_address"], "user_source": "OTDS", "user_privileges": 16 if u["superuser"] else 0}, "links": [{"rel": "self", "href": f"{ru}/currentuser"}]})
        if sub == "/cabinets":
            cabs = [repo.objects[i] for i in repo.root_ids if repo.can_read(user, repo.objects[i])]
            inline = query.get("inline", ["false"])[0].lower() == "true"
            return self.send_json(200, self.feed("Cabinets", path, [self.entry(o, inline) for o in cabs], query, len(cabs)))
        m = re.match(r"^/formats/([^/]+)$", sub)
        if m:
            fmt = m.group(1)
            if fmt not in FORMATS:
                return self.error(404, "E_RESOURCE_NOT_FOUND", f"Format {fmt} not found")
            mime, ext = FORMATS[fmt]
            return self.send_json(200, {"name": "format", "type": "dm_format", "properties": {"name": fmt, "mime_type": mime, "dos_extension": ext, "description": fmt}})
        if sub == "/formats":
            entries = [{"id": f"{ru}/formats/{f}", "title": f, "content": {"properties": {"name": f, "mime_type": mi, "dos_extension": ex}}} for f, (mi, ex) in FORMATS.items()]
            return self.send_json(200, self.feed("Formats", path, entries, query, len(entries)))
        if sub == "/types":
            names = ["dm_sysobject", "dm_document", "bow_invoice", "dm_folder", "dm_cabinet"]
            return self.send_json(200, self.feed("Types", path, [{"id": f"{ru}/types/{n}", "title": n} for n in names], query, len(names)))
        if sub == "/search":
            q = (query.get("q") or [""])[0].strip().lower()
            if not q:
                return self.error(400, "E_INPUT_ILLEGAL_ARGUMENTS", "q is required")
            hits = []
            for o in repo.objects.values():
                if o["_kind"] != "document" or not repo.is_current(o) or not repo.can_read(user, o):
                    continue
                hay = " ".join([o["object_name"], o.get("title") or "", o.get("subject") or ""]).lower()
                text = o["_content"][:200_000] if o["a_content_type"] in ("crtext", "text", "csv", "json") else b""
                if q in hay or (text and q in text.decode("utf-8", "ignore").lower()) or (o["a_content_type"] in ("pdf", "msw12", "excel12book") and q in _office_text(o).lower()):
                    hits.append(o)
            inline = query.get("inline", ["false"])[0].lower() == "true"
            return self.send_json(200, self.feed("Search results", path, [self.entry(o, inline) for o in hits], query, len(hits)))
        m = re.match(r"^/(folders|cabinets|objects|documents)/([0-9a-f]{16})(?:/(.*))?$", sub)
        if not m:
            return self.error(404, "E_RESOURCE_NOT_FOUND", "Unknown resource")
        kind, oid, tail = m.group(1), m.group(2), m.group(3) or ""
        obj = repo.objects.get(oid)
        if not obj:
            return self.error(404, "E_RESOURCE_NOT_FOUND", f"Object {oid} not found")
        if not repo.can_read(user, obj):
            return self.error(403, "E_ACCESS_DENIED", "You do not have permission to access this object")
        if not tail:
            return self.send_json(200, self.object_body(obj))
        inline = query.get("inline", ["false"])[0].lower() == "true"
        if tail in ("folders", "documents", "objects") and obj["_kind"] in ("folder", "cabinet"):
            kids = [repo.objects[i] for i in repo.children.get(oid, [])]
            if tail == "folders":
                kids = [k for k in kids if k["_kind"] != "document"]
            elif tail == "documents":
                kids = [k for k in kids if k["_kind"] == "document"]
            otype = (query.get("object-type") or [None])[0]
            if otype:
                kids = [k for k in kids if repo.type_matches(k, otype.lower())]
            kids = [k for k in kids if repo.can_read(user, k) and (k["_kind"] != "document" or repo.is_current(k))]
            return self.send_json(200, self.feed(tail.title(), path, [self.entry(k, inline) for k in kids], query, len(kids)))
        if tail == "parent-links":
            parents = [repo.objects[f] for f in obj["i_folder_id"]]
            entries = [{"id": f"{ru}/objects/{oid}/parent-links/{p['r_object_id']}", "title": p["r_folder_path"][0], "content": {"properties": {"r_object_id": p["r_object_id"], "r_folder_path": p["r_folder_path"]}},
                        "links": [{"rel": LINKREL + "parent", "href": f"{ru}/folders/{p['r_object_id']}"}]} for p in parents]
            return self.send_json(200, self.feed("Parent links", path, entries, query, len(entries)))
        if obj["_kind"] == "document" and tail == "contents/content":
            mime, _ = FORMATS.get(obj["a_content_type"], FORMATS["unknown"])
            policy = (query.get("media-url-policy") or ["all"])[0]
            href = f"{ru}/objects/{oid}/content-media" if policy == "local" and not FORCE_ACS_LINKS else f"{self.base}/ACS/servlet/ACS?command=read&objectid={oid}&format={obj['a_content_type']}&signature=mock"
            return self.send_json(200, {"name": "content", "type": "dmr_content", "properties": {"format_name": obj["a_content_type"], "full_format": obj["a_content_type"], "content_size": len(obj["_content"]), "r_object_id": oid, "page": 0, "rendition": 0, "full_content_size": len(obj["_content"]), "mime_type": mime},
                                        "links": [{"rel": "self", "href": f"{ru}/objects/{oid}/contents/content"}, {"rel": "enclosure", "href": href, "title": "content"}]})
        if obj["_kind"] == "document" and tail == "content-media":
            mime, _ = FORMATS.get(obj["a_content_type"], FORMATS["unknown"])
            return self.send_bytes(200, obj["_content"], mime)
        return self.error(404, "E_RESOURCE_NOT_FOUND", "Unknown resource")

    # ---- OTDS ---------------------------------------------------------------
    def otds_get(self, path, query):
        if path == "/otdsws/oauth2/auth":
            client = (query.get("client_id") or [""])[0]
            redirect = (query.get("redirect_uri") or [""])[0]
            state = (query.get("state") or [""])[0]
            if client not in OAUTH_CLIENTS or not redirect:
                return self.error(400, "invalid_request", "client_id and redirect_uri are required")
            hint = (query.get("login_hint") or [""])[0]
            if hint and hint in USERS:
                return self.redirect_with_code(hint, client, redirect, state)
            page = (f'<html><body><h1>OTDS Sign In</h1><form method="post" action="/otdsws/oauth2/auth">'
                    f'<input type="hidden" name="client_id" value="{client}"/><input type="hidden" name="redirect_uri" value="{redirect}"/>'
                    f'<input type="hidden" name="state" value="{state}"/>'
                    '<label>Username <input id="otds-user" name="username"/></label>'
                    '<label>Password <input id="otds-password" name="password" type="password"/></label>'
                    '<button id="otds-signin" type="submit">Sign in</button></form></body></html>').encode()
            return self.send_bytes(200, page, "text/html; charset=utf-8")
        if path == "/otdsws/oauth2/.well-known/openid-configuration" or path == "/otdsws/.well-known/openid-configuration":
            return self.send_json(200, {"issuer": f"{self.base}/otdsws", "authorization_endpoint": f"{self.base}/otdsws/oauth2/auth",
                                        "token_endpoint": f"{self.base}/otdsws/oauth2/token", "jwks_uri": f"{self.base}/otdsws/oauth2/jwks",
                                        "grant_types_supported": ["authorization_code", "client_credentials", "password", "refresh_token", "urn:ietf:params:oauth:grant-type:token-exchange"]}, "application/json")
        if path == "/otdsws/rest/authentication/oauth/tokeninfo" or path == "/otdsws/oauth2/tokeninfo":
            token = (query.get("token") or [""])[0]
            with LOCK:
                rec = TOKENS.get(token)
            if not rec:
                return self.send_json(401, {"error": "invalid_token"}, "application/json")
            if rec["exp"] <= time.time():
                return self.send_json(410, {"error": "expired"}, "application/json")
            return self.send_json(200, {"user": rec["user"] + "@" + PARTITION, "client_id": rec["client"], "expires_in": int(rec["exp"] - time.time())}, "application/json")
        return self.error(404, "E_RESOURCE_NOT_FOUND", "not found")

    def redirect_with_code(self, user, client, redirect, state):
        code = secrets.token_urlsafe(24)
        with LOCK:
            CODES[code] = {"user": user, "client": client, "redirect_uri": redirect, "exp": time.time() + 300}
        sep = "&" if "?" in redirect else "?"
        self.send_response(302)
        self.send_header("Location", f"{redirect}{sep}{urlencode({'code': code, 'state': state})}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        parts = urlsplit(self.path)
        path = unquote(parts.path)
        body = self.read_body()
        form = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "ignore")).items()}
        if path == "/otdsws/oauth2/auth":
            user = form.get("username", "")
            u = USERS.get(user)
            if not u or not u["password"] or form.get("password") != u["password"]:
                return self.send_bytes(401, b"<html><body><p id='otds-error'>Invalid credentials</p></body></html>", "text/html; charset=utf-8")
            return self.redirect_with_code(user, form.get("client_id", ""), form.get("redirect_uri", ""), form.get("state", ""))
        if path in ("/otdsws/oauth2/token", "/otdsws/login"):
            return self.token_endpoint(form)
        if path.startswith("/dctm-rest/"):
            return self.error(405, "E_METHOD_NOT_ALLOWED", "The mock is read-only")
        return self.error(404, "E_RESOURCE_NOT_FOUND", "not found")

    def client_auth(self, form):
        auth = self.headers.get("Authorization") or ""
        cid, secret = form.get("client_id"), form.get("client_secret")
        if auth.lower().startswith("basic "):
            try:
                cid, secret = base64.b64decode(auth.split(None, 1)[1]).decode().split(":", 1)
            except Exception:
                return None
        c = OAUTH_CLIENTS.get(cid or "")
        if c and c["secret"] == secret:
            return cid
        return None

    def token_endpoint(self, form):
        grant = form.get("grant_type", "")
        client = self.client_auth(form)
        if not client:
            return self.send_json(401, {"error": "invalid_client", "error_description": "Client authentication failed"}, "application/json")
        c = OAUTH_CLIENTS[client]
        user = None
        if grant == "client_credentials":
            user = c["service_user"]
        elif grant == "password":
            login = form.get("username", "").split("@")[0]
            u = USERS.get(login)
            if not u or not u["password"] or form.get("password") != u["password"]:
                return self.send_json(400, {"error": "invalid_grant", "error_description": "Invalid user credentials"}, "application/json")
            user = login
        elif grant == "urn:ietf:params:oauth:grant-type:token-exchange":
            if form.get("subject_token_type") != "urn:opentext.com:oauth:string:user_id":
                return self.send_json(400, {"error": "invalid_request", "error_description": "Unsupported subject_token_type"}, "application/json")
            if not c["allow_impersonation"]:
                return self.send_json(400, {"error": "invalid_grant", "error_description": "Impersonation not enabled for this client"}, "application/json")
            subject = form.get("subject_token", "")
            login, _, partition = subject.partition("@")
            if partition and partition != PARTITION:
                return self.send_json(400, {"error": "invalid_grant", "error_description": f"Unknown partition {partition}"}, "application/json")
            if login not in USERS:
                return self.send_json(400, {"error": "invalid_grant", "error_description": f"User {subject} not found"}, "application/json")
            user = login
        elif grant == "authorization_code":
            with LOCK:
                rec = CODES.pop(form.get("code", ""), None)
            if not rec or rec["exp"] < time.time() or rec["client"] != client or (form.get("redirect_uri") and form["redirect_uri"] != rec["redirect_uri"]):
                return self.send_json(400, {"error": "invalid_grant", "error_description": "Invalid or expired authorization code"}, "application/json")
            user = rec["user"]
        elif grant == "refresh_token":
            with LOCK:
                rec = REFRESH.pop(form.get("refresh_token", ""), None)
            if not rec or rec["client"] != client:
                return self.send_json(400, {"error": "invalid_grant", "error_description": "Invalid refresh token"}, "application/json")
            user = rec["user"]
        else:
            return self.send_json(400, {"error": "unsupported_grant_type"}, "application/json")
        token, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with LOCK:
            TOKENS[token] = {"user": user, "exp": time.time() + TOKEN_TTL, "client": client}
            REFRESH[refresh] = {"user": user, "client": client}
        body = {"access_token": token, "token_type": "Bearer", "expires_in": TOKEN_TTL, "scope": "otds:roles"}
        if grant in ("authorization_code", "refresh_token", "password"):
            body["refresh_token"] = refresh
        return self.send_json(200, body, "application/json")


def _office_text(obj):
    """Best-effort text of the seeded office/PDF docs for the search endpoint."""
    fmt = obj["a_content_type"]
    data = obj["_content"]
    try:
        if fmt == "pdf":
            return " ".join(re.findall(rb"\((.*?)\) Tj", data)).__str__()
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = b"".join(z.read(n) for n in z.namelist() if n.endswith(".xml"))
        return re.sub(r"<[^>]+>", " ", xml.decode("utf-8", "ignore"))
    except Exception:
        return ""


def make_server(port=0, host="127.0.0.1"):
    return ThreadingHTTPServer((host, port), Handler)


def main():
    port = int(os.environ.get("DCTM_MOCK_PORT", "8081"))
    server = make_server(port, os.environ.get("DCTM_MOCK_HOST", "0.0.0.0"))
    print(f"Mock Documentum REST + OTDS listening on http://0.0.0.0:{port}/dctm-rest  (repository {REPO})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
