#!/usr/bin/env python3
"""Mock Siemens Teamcenter (web tier + Security Services + file server): the test boundary for the
``teamcenter`` connector.

There is no public Teamcenter instance or image, so the shapes below are assembled from public sources:

  * Krusty84/Teamcenter_REST_API (a Postman collection captured against real servers): the
    ``/tc/JsonRestServices/<Library>-<YYYY>-<MM>-<Service>/<op>`` paths, the ``{"header": {"state", "policy"},
    "body"}`` envelope, ``Core-2011-06-Session/login`` with ``body.credentials``, the ``XSRF-TOKEN`` cookie
    echoed as ``X-Xsrf-Token``, ``getProperties`` answering ``ServiceData.modelObjects[uid].props[name]
    .dbValues/uiValues``, ``getSavedQueries`` answering ``queries[].{name, query.uid}``, ``/fms/fmsupload``;
  * spalk/tcsoapy and goenninger-b-t/tc-mcp: BOM windows under a revision rule, ``expandPSOneLevel`` with
    ``parent.bomLine`` / ``children[].bomLine|itemRevOfBOMLine``, where-used, saved-query execution, the
    Change Manager relations (problem / impacted / solution / reference items);
  * Siemens' Teamcenter 10.1 services reference (loginSSO under Session 2006-03 and 2011-06,
    getTypeDescriptions under Session 2011-06) and the Mendix connector docs (the TcSS login URL ending in
    ``/weblogin/login_redirect``, an application ID registered with a root URL the browser returns to).

Inferred (no public capture): the exact request/response field names of describeSavedQueries,
executeSavedQueries, getTypeDescriptions2, getFileReadTickets, whereUsed and the TcSS redirect
parameters (``TCSS_TOKEN`` / ``TCSS_USERID`` here; the connector detects any parameter whose name contains
"token" / "user", or uses the names the admin configures). The mock keeps Teamcenter's documented habits
on purpose: maps as ``[[keys], [values]]``, failures as an exception object with a ``.QName``, sessions in
cookies, objects a user may not read missing from ``modelObjects`` with a partial error.

Served:
  POST {CTX}/JsonRestServices/<service>/<op>     the SOA operations in ``OPS`` (unknown -> 404)
  GET  {CTX}/fms/fmsdownload/<name>?ticket=…     file bytes for a read ticket   (also /fms on the root)
  GET  /tcssol/weblogin/login_redirect?appID=…   TcSS login: Negotiate -> redirect with a token,
                                                 else an HTML password form
  POST /tcssol/weblogin/login                    the form's submit -> redirect to the app root URL

Test switches (STATE, reset()): profile ("current" | "legacy": only the 2006/2007 service versions),
sso_only (password login refused, as on an SSO-enforced site), expire_sessions (next call answers
InvalidUserException), kerberos_user (who a Negotiate header signs in as), app_roots {appID: root URL}.
REQUEST_LOG records (user, path). OPEN_WINDOWS counts BOM windows not yet closed.

Run standalone:  python tools/teamcenter/mock_teamcenter_server.py --port 8099
"""
from __future__ import annotations

import argparse
import fnmatch
import io
import json
import secrets
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urlsplit

DEFAULT_CONTEXT = "/tc"
TCSS_CONTEXT = "/tcssol"
QNAME = "http://teamcenter.com/Schemas/Soa/2006-03/Exceptions.{}"

USERS = {
    "infodba": {"password": "infodba-Secr3t", "name": "Teamcenter Admin", "group": "dba", "uid": "USR_infodba00001"},
    "jdoe": {"password": "jdoe-pw", "name": "Jane Doe", "group": "Engineering", "uid": "USR_jdoe000000001"},
    "asmith": {"password": "asmith-pw", "name": "Alex Smith", "group": "Engineering", "uid": "USR_asmith0000001"},
}
# Objects a user may not read (Teamcenter access rules): jdoe is not cleared for export-controlled parts.
HIDDEN = {"jdoe": {"REV_PRT-1003_A", "ITM_PRT-1003"}}

STATE: dict = {}
SESSIONS: dict = {}       # JSESSIONID -> {"user", "xsrf"}
TOKENS: dict = {}         # TcSS token -> user
TICKETS: dict = {}        # read ticket -> ImanFile uid
WINDOWS: dict = {}        # window uid -> {"lines": {line uid: {...}}}
REQUEST_LOG: list = []
OPEN_WINDOWS = {"count": 0}
_LOCK = threading.Lock()

# --------------------------------------------------------------------------- type model
# name -> (parent, {prop: (valueType, isArray, displayName)}). valueType: 2 date, 3 double, 5 int, 6 logical,
# 8 string, 9 typed reference, 13 typed relation (Teamcenter's PROP_* numbering).
_WSO = {
    "object_name": (8, False, "Name"), "object_desc": (8, False, "Description"), "object_type": (8, False, "Type"),
    "object_string": (8, False, "Object"), "owning_user": (9, False, "Owner"), "owning_group": (9, False, "Group ID"),
    "last_mod_user": (9, False, "Last Modifying User"), "creation_date": (2, False, "Date Created"),
    "last_mod_date": (2, False, "Date Modified"), "release_status_list": (9, True, "Release Status"),
    "date_released": (2, False, "Date Released"), "checked_out": (8, False, "Checked-Out"),
}
_REV = {**_WSO, "item_id": (8, False, "ID"), "item_revision_id": (8, False, "Revision"),
        "items_tag": (9, False, "Item"), "IMAN_specification": (13, True, "Specifications"),
        "IMAN_reference": (13, True, "References"), "IMAN_manifestation": (13, True, "Manifestations"),
        "IMAN_Rendering": (13, True, "Renderings"), "TC_Attaches": (13, True, "Attaches"),
        "fnd0InProcess": (6, False, "In Process"), "awp0CellProperties": (8, True, "Cell Properties")}
_CHANGE = {**_REV, "CMMaturity": (8, False, "Maturity"), "CMDisposition": (8, False, "Disposition"),
           "CMClosure": (8, False, "Closure"), "CMHasProblemItem": (13, True, "Problem Items"),
           "CMHasImpactedItem": (13, True, "Impacted Items"), "CMHasSolutionItem": (13, True, "Solution Items"),
           "CMReferences": (13, True, "Reference Items"), "cm0Analyst": (9, False, "Analyst")}
TYPES = {
    "WorkspaceObject": (None, _WSO),
    "Item": ("WorkspaceObject", {**_WSO, "item_id": (8, False, "ID"), "revision_list": (9, True, "Revisions")}),
    "ItemRevision": ("WorkspaceObject", _REV),
    "A2_PartRevision": ("ItemRevision", {**_REV, "a2_material": (8, False, "Material"),
                                         "a2_mass_kg": (3, False, "Mass (kg)"), "a2_make_buy": (8, False, "Make/Buy"),
                                         "a2_supplier": (8, False, "Supplier")}),
    "DocumentRevision": ("ItemRevision", _REV),
    "ChangeItemRevision": ("ItemRevision", _CHANGE),
    "ProblemReportRevision": ("ChangeItemRevision", _CHANGE),
    "ChangeRequestRevision": ("ChangeItemRevision", _CHANGE),
    "ChangeNoticeRevision": ("ChangeItemRevision", _CHANGE),
    "Dataset": ("WorkspaceObject", {**_WSO, "ref_list": (9, True, "References")}),
    "PDF": ("Dataset", {**_WSO, "ref_list": (9, True, "References")}),
    "MSWordX": ("Dataset", {**_WSO, "ref_list": (9, True, "References")}),
    "UGMASTER": ("Dataset", {**_WSO, "ref_list": (9, True, "References")}),
    "ImanFile": (None, {"original_file_name": (8, False, "Original File Name"), "byte_size": (5, False, "Size"),
                        "object_string": (8, False, "Object")}),
    "Folder": ("WorkspaceObject", {**_WSO, "contents": (9, True, "Contents")}),
    "User": (None, {"user_id": (8, False, "User ID"), "user_name": (8, False, "Person"),
                    "home_folder": (9, False, "Home Folder"), "object_string": (8, False, "Object")}),
    "Group": (None, {"name": (8, False, "Name"), "object_string": (8, False, "Object")}),
    "ReleaseStatus": (None, {"name": (8, False, "Name"), "object_string": (8, False, "Object")}),
    "RevisionRule": (None, {"object_name": (8, False, "Name"), "object_desc": (8, False, "Description"),
                            "object_string": (8, False, "Object")}),
    "BOMLine": (None, {"bl_quantity": (8, False, "Quantity"), "bl_sequence_no": (8, False, "Find No."),
                       "object_string": (8, False, "Object")}),
}
REVISION_CLASSES = {"ItemRevision", "A2_PartRevision", "DocumentRevision", "ChangeItemRevision",
                    "ProblemReportRevision", "ChangeRequestRevision", "ChangeNoticeRevision"}


def _props_of(type_name):
    parent, props = TYPES[type_name]
    return props


# --------------------------------------------------------------------------- documents
def _pdf(lines) -> bytes:
    """A minimal valid PDF with one page of text (pypdf extracts it)."""
    text = " ".join(f"({ln.replace('(', '[').replace(')', ']')}) Tj 0 -18 Td" for ln in lines)
    stream = f"BT /F1 12 Tf 50 750 Td {text} ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for i, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def _docx(paragraphs) -> bytes:
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r></w:p>' for p in paragraphs)
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f'<w:body>{body}</w:body></w:document>')
    types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
             '<Default Extension="xml" ContentType="application/xml"/>'
             '<Override PartName="/word/document.xml" '
             'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="word/document.xml"/></Relationships>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return buf.getvalue()


SPEC_CODE = "PUMP-ALPHA-77"
FILES = {
    "FILE_spec_pdf00001": ("PRT-1000_Pump_Assembly_Spec.pdf", _pdf([
        "Pump Assembly PRT-1000 rev B - Design Specification",
        "Max operating pressure: 16 bar. Design flow: 120 m3/h.",
        f"Design code: {SPEC_CODE}. Material of the impeller: duplex stainless steel.",
        "Qualification: 10,000 hour endurance test required before release."])),
    "FILE_report_docx01": ("PRT-1001_Impeller_Test_Report.docx", _docx([
        "Impeller PRT-1001 fatigue test report.",
        "Fatigue limit measured: 210 MPa at 10^7 cycles.",
        "Crack initiation observed at the blade root on rev A samples; rev B adds a 2 mm fillet."])),
    "FILE_model_prt0001": ("prt-1000_b.prt", b"\x00NXPART" + b"\x00" * 64),
}

# --------------------------------------------------------------------------- data
OBJECTS: dict = {}


def _obj(uid, type_name, class_name=None, **props):
    OBJECTS[uid] = {"uid": uid, "type": type_name, "className": class_name or type_name, "props": props}


def _date(day, hour=9):
    return f"2026-{day[:2]}-{day[3:]}T{hour:02d}:00:00+00:00"


def _build():
    OBJECTS.clear()
    for user_id, u in USERS.items():
        home = f"FLD_home_{user_id}"[:18]
        _obj(u["uid"], "User", user_id=user_id, user_name=u["name"], home_folder=home,
             object_string=f"{u['name']} ({user_id})")
    for g in ("dba", "Engineering"):
        _obj(f"GRP_{g}", "Group", name=g, object_string=g)
    _obj("STS_released0001", "ReleaseStatus", name="TCM Released", object_string="TCM Released")
    _obj("STS_approved0001", "ReleaseStatus", name="Approved", object_string="Approved")
    for uid, name, desc in (("RUL_latestwork01", "Latest Working", "Latest working revision"),
                            ("RUL_latestrel001", "Latest Released", "Latest released revision"),
                            ("RUL_anystatus001", "Any Status; Working", "Released if any, else working")):
        _obj(uid, "RevisionRule", object_name=name, object_desc=desc, object_string=name)

    def item(item_id, rev, name, type_name, owner, group, created, modified, released=None, **extra):
        item_uid, rev_uid = f"ITM_{item_id}", f"REV_{item_id}_{rev}"
        if item_uid not in OBJECTS:
            _obj(item_uid, "Item", item_id=item_id, object_name=name, object_type="Item", revision_list=[],
                 owning_user=USERS[owner]["uid"], owning_group=f"GRP_{group}", creation_date=_date(created),
                 last_mod_date=_date(created), object_string=f"{item_id}-{name}")
        OBJECTS[item_uid]["props"]["revision_list"].append(rev_uid)
        props = dict(item_id=item_id, item_revision_id=rev, object_name=name, object_type=type_name,
                     object_desc=extra.pop("desc", None), owning_user=USERS[owner]["uid"],
                     owning_group=f"GRP_{group}", last_mod_user=USERS[owner]["uid"], creation_date=_date(created),
                     last_mod_date=_date(modified), release_status_list=["STS_released0001"] if released else [],
                     date_released=_date(released) if released else None, checked_out="",
                     items_tag=item_uid, object_string=f"{item_id}/{rev};1-{name}", fnd0InProcess=False,
                     IMAN_specification=[], IMAN_reference=[], IMAN_manifestation=[], IMAN_Rendering=[],
                     TC_Attaches=[], **extra)
        _obj(rev_uid, type_name, **props)
        return rev_uid

    part = "A2_PartRevision"
    item("PRT-1000", "A", "Pump Assembly", part, "jdoe", "Engineering", "01-10", "02-14", "02-14",
         a2_material="Assembly", a2_mass_kg=42.5, a2_make_buy="Make", a2_supplier=None)
    item("PRT-1000", "B", "Pump Assembly", part, "jdoe", "Engineering", "06-02", "10-28",
         desc="Rev B: new impeller", a2_material="Assembly", a2_mass_kg=41.9, a2_make_buy="Make", a2_supplier=None)
    item("PRT-1001", "A", "Impeller", part, "jdoe", "Engineering", "01-12", "02-10", "02-10",
         a2_material="Cast iron", a2_mass_kg=3.2, a2_make_buy="Make", a2_supplier=None)
    item("PRT-1001", "B", "Impeller", part, "asmith", "Engineering", "06-05", "11-02",
         a2_material="Duplex stainless steel", a2_mass_kg=3.0, a2_make_buy="Make", a2_supplier=None)
    item("PRT-1002", "A", "Hex Bolt M8x30", part, "asmith", "Engineering", "01-05", "01-20", "01-20",
         a2_material="A4-80 stainless", a2_mass_kg=0.02, a2_make_buy="Buy", a2_supplier="Fastenal")
    item("PRT-1003", "A", "Seal Kit (export controlled)", part, "infodba", "dba", "03-01", "03-15",
         a2_material="FKM", a2_mass_kg=0.3, a2_make_buy="Buy", a2_supplier="Parker")
    item("PRT-1004", "A", "Motor Housing", part, "asmith", "Engineering", "02-01", "09-30",
         a2_material="Aluminium A356", a2_mass_kg=8.4, a2_make_buy="Make", a2_supplier=None)
    item("PRT-1005", "A", "Impeller Blade", part, "jdoe", "Engineering", "01-15", "02-01", "02-01",
         a2_material="Duplex stainless steel", a2_mass_kg=0.25, a2_make_buy="Make", a2_supplier=None)
    item("PRT-2000", "A", "Valve Assembly", part, "asmith", "Engineering", "04-01", "05-20", "05-20",
         a2_material="Assembly", a2_mass_kg=6.1, a2_make_buy="Make", a2_supplier=None)
    item("000123", "A", "Test Fixture", "ItemRevision", "infodba", "dba", "02-20", "02-21")
    item("DOC-0001", "A", "Pump Family Design Guide", "DocumentRevision", "jdoe", "Engineering", "01-02", "01-03",
         "01-03")
    pr = item("PR-0001", "A", "Impeller cracks under load", "ProblemReportRevision", "jdoe", "Engineering",
              "05-10", "05-20", CMMaturity="Reviewing", CMDisposition="Investigate", CMClosure="Open",
              CMHasProblemItem=["REV_PRT-1001_A"], CMHasImpactedItem=[], CMHasSolutionItem=[], CMReferences=[])
    ecr = item("ECR-0001", "A", "Change impeller material to duplex", "ChangeRequestRevision", "jdoe",
               "Engineering", "05-25", "06-01", CMMaturity="Complete", CMDisposition="Approved", CMClosure="Closed",
               CMHasProblemItem=["REV_PRT-1001_A"], CMHasImpactedItem=["REV_PRT-1001_A", "REV_PRT-1000_A"],
               CMHasSolutionItem=[], CMReferences=[pr])
    item("ECN-0001", "A", "Release impeller rev B", "ChangeNoticeRevision", "asmith", "Engineering", "06-03",
         "11-03", CMMaturity="Executing", CMDisposition="Approved", CMClosure="Open", CMHasProblemItem=[],
         CMHasImpactedItem=["REV_PRT-1001_A", "REV_PRT-1000_A"],
         CMHasSolutionItem=["REV_PRT-1001_B", "REV_PRT-1000_B"], CMReferences=[ecr])
    item("ECN-0002", "A", "Bolt supplier change", "ChangeNoticeRevision", "asmith", "Engineering", "01-18",
         "01-20", "01-20", CMMaturity="Complete", CMDisposition="Approved", CMClosure="Closed",
         CMHasProblemItem=[], CMHasImpactedItem=[], CMHasSolutionItem=["REV_PRT-1002_A"], CMReferences=[])

    def dataset(uid, type_name, name, file_uid, owner, attach_to, relation, modified):
        fname, data = FILES[file_uid]
        _obj(file_uid, "ImanFile", original_file_name=fname, byte_size=len(data), object_string=fname)
        _obj(uid, type_name, object_name=name, object_type=type_name, ref_list=[file_uid],
             owning_user=USERS[owner]["uid"], owning_group="GRP_Engineering", last_mod_user=USERS[owner]["uid"],
             creation_date=_date(modified), last_mod_date=_date(modified), release_status_list=[],
             object_string=name, object_desc=None, date_released=None, checked_out="")
        OBJECTS[attach_to]["props"][relation].append(uid)

    dataset("DS_spec_pdf00001", "PDF", "PRT-1000 Pump Assembly Specification", "FILE_spec_pdf00001", "jdoe",
            "REV_PRT-1000_B", "IMAN_specification", "10-20")
    dataset("DS_report_docx01", "MSWordX", "PRT-1001 Impeller Test Report", "FILE_report_docx01", "asmith",
            "REV_PRT-1001_B", "IMAN_reference", "11-01")
    dataset("DS_model_prt0001", "UGMASTER", "prt-1000/B", "FILE_model_prt0001", "jdoe",
            "REV_PRT-1000_B", "IMAN_specification", "10-28")
    for user_id in USERS:
        home = f"FLD_home_{user_id}"[:18]
        _obj(home, "Folder", object_name="Home", object_type="Folder",
             contents=["FLD_projects0001", "REV_PRT-1000_B", "DS_spec_pdf00001"], object_string="Home")
    _obj("FLD_projects0001", "Folder", object_name="Pump Program", object_type="Folder",
         contents=["REV_PRT-1001_B", "DS_report_docx01"], object_string="Pump Program")


# Product structure: revision -> [(child item id, quantity, find number)]; children resolve by revision rule.
STRUCTURE = {
    "REV_PRT-1000_A": [("PRT-1001", "1", "10"), ("PRT-1002", "4", "20")],
    "REV_PRT-1000_B": [("PRT-1001", "1", "10"), ("PRT-1002", "4", "20"), ("PRT-1004", "1", "30")],
    "REV_PRT-1001_A": [("PRT-1005", "6", "10")],
    "REV_PRT-1001_B": [("PRT-1005", "7", "10")],
    "REV_PRT-1004_A": [("PRT-1003", "1", "10"), ("PRT-1002", "6", "20")],
    "REV_PRT-2000_A": [("PRT-1002", "2", "10"), ("PRT-1005", "1", "20")],
}

SAVED_QUERIES = {
    "QRY_itemrev00001": ("Item Revision...", REVISION_CLASSES, [
        ("Item ID", "items_tag.item_id"), ("Revision", "item_revision_id"), ("Name", "object_name"),
        ("Type", "object_type"), ("Owning User", "owning_user.user_id"),
        ("Release Status", "release_status_list.name")]),
    "QRY_general00001": ("General...", None, [
        ("Name", "object_name"), ("Type", "object_type"), ("Owning User", "owning_user.user_id")]),
    "QRY_item00000001": ("Item...", {"Item"}, [("Item ID", "item_id"), ("Name", "object_name"), ("Type", "object_type")]),
    "QRY_dataset00001": ("Dataset...", {"PDF", "MSWordX", "UGMASTER", "Dataset"}, [
        ("Name", "object_name"), ("Dataset Type", "object_type")]),
}


def reset() -> None:
    with _LOCK:
        STATE.clear()
        STATE.update({"profile": "current", "sso_only": False, "expire_sessions": False, "kerberos_user": None,
                      "app_roots": {}, "case_sensitive": False})
        SESSIONS.clear()
        TOKENS.clear()
        TICKETS.clear()
        WINDOWS.clear()
        REQUEST_LOG.clear()
        OPEN_WINDOWS["count"] = 0
        _build()


reset()

# --------------------------------------------------------------------------- service helpers
_LEGACY_MISSING = {"Core-2011-06-Session/login", "Core-2011-06-Session/loginSSO",
                   "Core-2015-10-Session/getTypeDescriptions2", "Core-2011-06-Session/getTypeDescriptions",
                   "Query-2008-06-SavedQuery/executeSavedQueries", "Query-2007-06-SavedQuery/executeSavedQueries"}


class SoaError(Exception):
    def __init__(self, exc, message, code=0):
        super().__init__(message)
        self.exc, self.message, self.code = exc, message, code


def _visible(user, uid) -> bool:
    return uid in OBJECTS and uid not in HIDDEN.get(user, set())


def _display(uid) -> str:
    obj = OBJECTS.get(uid)
    return (obj or {}).get("props", {}).get("object_string") or uid


def _prop_json(obj, name):
    value = obj["props"].get(name)
    vt, is_array, _ = _props_of(obj["type"]).get(name, (8, False, name))
    values = value if isinstance(value, list) else ([] if value in (None, "") else [value])
    if vt in (9, 13):
        return {"dbValues": values, "uiValues": [_display(v) for v in values]}
    if vt == 6:
        return {"dbValues": ["1" if v else "0" for v in values], "uiValues": ["True" if v else "False" for v in values]}
    if vt == 2:
        return {"dbValues": values, "uiValues": [v[:10] for v in values]}
    return {"dbValues": [str(v) for v in values], "uiValues": [str(v) for v in values]}


def _model(uid, props=None) -> dict:
    obj = OBJECTS[uid]
    out = {"objectID": uid, "uid": uid, "className": obj["className"], "type": obj["type"]}
    if props:
        out["props"] = {p: _prop_json(obj, p) for p in props}
    return out


def _service_data(user, uids, props=None, errors=None) -> dict:
    model, plain, partial = {}, [], list(errors or [])
    for uid in uids:
        if not _visible(user, uid):
            partial.append({"uid": uid, "errorValues": [{"code": 515001, "level": 3,
                                                          "message": "The object could not be read: no access."}]})
            continue
        known = _props_of(OBJECTS[uid]["type"])
        wanted = [p for p in (props or []) if p in known]
        missing = [p for p in (props or []) if p not in known]
        model[uid] = _model(uid, wanted)
        plain.append(uid)
        if missing:
            partial.append({"uid": uid, "errorValues": [{"code": 38015, "level": 1,
                                                          "message": f"Invalid property: {m}"} for m in missing]})
    return {".QName": "http://teamcenter.com/Schemas/Soa/2006-03/Base.ServiceData", "plain": plain,
            "modelObjects": model, "partialErrors": partial}


def _attr_value(uid, attr):
    """The stored value(s) a saved-query entry compares, following one reference (``owning_user.user_id``)."""
    obj = OBJECTS[uid]
    head, _, tail = attr.partition(".")
    value = obj["props"].get(head)
    if tail:
        refs = value if isinstance(value, list) else [value]
        return [OBJECTS[r]["props"].get(tail) for r in refs if r in OBJECTS]
    return value if isinstance(value, list) else [value]


def _like(value, pattern) -> bool:
    if value is None:
        return False
    if STATE["case_sensitive"]:
        return fnmatch.fnmatchcase(str(value), pattern)
    return fnmatch.fnmatchcase(str(value).lower(), pattern.lower())


def _run_saved_query(user, quid, entries, values, limit):
    if quid not in SAVED_QUERIES:
        raise SoaError("ServiceException", "The saved query does not exist.", 141001)
    _, classes, fields = SAVED_QUERIES[quid]
    attr_of = {e.lower(): a for e, a in fields}
    criteria = []
    for e, v in zip(entries, values):
        if e.lower() not in attr_of:
            raise SoaError("ServiceException", "Invalid list of user entries.", 3031)
        criteria.append((attr_of[e.lower()], v))
    if not criteria:
        raise SoaError("ServiceException", "Invalid query: at least one entry is required.", 3006)
    found = []
    for uid, obj in OBJECTS.items():
        if classes is not None and obj["className"] not in classes:
            continue
        if classes is None and obj["type"] not in TYPES or obj["type"] in ("User", "Group", "ReleaseStatus",
                                                                            "RevisionRule", "ImanFile", "BOMLine"):
            continue
        if not _visible(user, uid):
            continue
        if all(any(_like(v, pat) for v in _attr_value(uid, attr)) for attr, pat in criteria):
            found.append(uid)
    return found[: max(0, int(limit or 0)) or len(found)]


def _resolve_child(item_id, rule_uid):
    revs = [u for u in OBJECTS.get(f"ITM_{item_id}", {}).get("props", {}).get("revision_list", [])]
    name = OBJECTS.get(rule_uid, {}).get("props", {}).get("object_name", "Latest Working")
    if name == "Latest Released":
        revs = [r for r in revs if OBJECTS[r]["props"]["release_status_list"]]
    elif name.startswith("Any Status"):
        released = [r for r in revs if OBJECTS[r]["props"]["release_status_list"]]
        revs = released or revs
    return revs[-1] if revs else None


def _new_line(window, rev_uid, qty="", seq=""):
    line = f"BLN_{secrets.token_hex(6)}"
    WINDOWS[window]["lines"][line] = {"rev": rev_uid}
    rev = OBJECTS[rev_uid]["props"]
    OBJECTS[line] = {"uid": line, "type": "BOMLine", "className": "BOMLine",
                     "props": {"bl_quantity": qty, "bl_sequence_no": seq,
                               "object_string": f"{rev['item_id']}/{rev['item_revision_id']}"}}
    return line


def _parents(rev_uid):
    item_id = OBJECTS[rev_uid]["props"]["item_id"]
    return [p for p, children in STRUCTURE.items() if any(c[0] == item_id for c in children)]


# --------------------------------------------------------------------------- operations
def op_login(user_unused, body, path, handler):
    if path.startswith("Core-2011-06"):
        creds = body.get("credentials") or {}
        user, password = creds.get("user"), creds.get("password")
    else:
        user, password = body.get("username"), body.get("password")
    if STATE["sso_only"]:
        raise SoaError("InvalidCredentialsException", "Password login is disabled; use Security Services.", 515143)
    if user not in USERS or USERS[user]["password"] != password:
        raise SoaError("InvalidCredentialsException", "The login attempt failed: either the user ID or the "
                       "password is invalid.", 515143)
    return handler.start_session(user)


def op_login_sso(user_unused, body, path, handler):
    if path.startswith("Core-2011-06"):
        creds = body.get("credentials") or {}
        user, token = creds.get("user"), creds.get("password")
    else:
        user, token = body.get("username"), body.get("ssoCredentials")
    owner = TOKENS.get(token)
    if not owner or (user and user != owner):
        raise SoaError("InvalidCredentialsException", "The SSO token is invalid or expired.", 515143)
    return handler.start_session(owner)


def op_logout(user, body, path, handler):
    handler.end_session()
    return {}


def op_session_info(user, body, path, handler):
    u = USERS[user]
    return {"user": _model(u["uid"]), "group": _model(f"GRP_{u['group']}"),
            "extraInfo": [["TCServerVersion", "TcServerID"], ["V14000.3.0.20250310", "1"]],
            "ServiceData": _service_data(user, [u["uid"]])}


def op_type_descriptions(user, body, path, handler):
    types = []
    for name in body.get("typeNames") or []:
        if name not in TYPES:
            continue
        parent, props = TYPES[name]
        chain, p = [], parent
        while p:
            chain.append(p)
            p = TYPES[p][0]
        types.append({"name": name, "displayName": name.replace("A2_", "").replace("Revision", " Revision").strip(),
                      "parentTypeName": parent, "typeHierarchy": chain,
                      "propertyDescriptors": {k: {"name": k, "displayName": d, "valueType": vt, "isArray": arr}
                                              for k, (vt, arr, d) in props.items()}})
    return {"types": types, "ServiceData": _service_data(user, [])}


def op_get_properties(user, body, path, handler):
    uids = [o.get("uid") for o in body.get("objects") or [] if isinstance(o, dict)]
    return _service_data(user, uids, body.get("attributes") or [])


def op_saved_queries(user, body, path, handler):
    return {"queries": [{"name": n, "description": "", "query": {"uid": q, "type": "ImanQuery"}}
                        for q, (n, _, _) in SAVED_QUERIES.items()], "ServiceData": _service_data(user, [])}


def op_describe_queries(user, body, path, handler):
    lists = []
    for q in body.get("queries") or []:
        fields = SAVED_QUERIES.get((q or {}).get("uid"), (None, None, []))[2]
        lists.append({"fields": [{"entryName": e, "attributeName": a, "logicalOperation": "AND",
                                  "mathOperation": "=", "value": ""} for e, a in fields]})
    return {"fieldLists": lists, "ServiceData": _service_data(user, [])}


def op_execute_query(user, body, path, handler):
    if path.startswith("Query-2006-03"):
        uids = _run_saved_query(user, (body.get("query") or {}).get("uid"), body.get("entries") or [],
                                body.get("values") or [], body.get("limit"))
        return {"nFound": len(uids), "objects": [_model(u) for u in uids], "ServiceData": _service_data(user, uids)}
    results = []
    for item in body.get("input") or []:
        uids = _run_saved_query(user, (item.get("query") or {}).get("uid"), item.get("entries") or [],
                                item.get("values") or [], item.get("maxNumToReturn"))
        results.append({"objectUIDS": uids, "numOfObjects": len(uids)})
    return {"arrayOfResults": results, "serviceData": _service_data(user, [])}


def op_revision_rules(user, body, path, handler):
    rules = [u for u, o in OBJECTS.items() if o["type"] == "RevisionRule"]
    return {"output": [{"revRule": _model(u), "overrideFolders": []} for u in rules],
            "serviceData": _service_data(user, rules)}


def op_create_bom_windows(user, body, path, handler):
    out = []
    for info in body.get("info") or []:
        rev = (info.get("itemRev") or {}).get("uid")
        rule = ((info.get("revRuleConfigInfo") or {}).get("revRule") or {}).get("uid")
        if not _visible(user, rev) or rev not in OBJECTS or OBJECTS[rev]["className"] not in REVISION_CLASSES:
            raise SoaError("ServiceException", "The item revision could not be found.", 46001)
        window = f"WIN_{secrets.token_hex(6)}"
        WINDOWS[window] = {"lines": {}, "rule": rule, "user": user}
        OPEN_WINDOWS["count"] += 1
        out.append({"clientId": info.get("clientId"), "bomWindow": {"uid": window, "type": "BOMWindow"},
                    "bomLine": {"uid": _new_line(window, rev), "type": "BOMLine"}})
    return {"output": out, "serviceData": _service_data(user, [])}


def op_expand_one_level(user, body, path, handler):
    out = []
    exp_rev = bool((body.get("pref") or {}).get("expItemRev"))
    for parent in (body.get("input") or {}).get("parentBomLines") or []:
        line = (parent or {}).get("uid")
        window = next((w for w, d in WINDOWS.items() if line in d["lines"]), None)
        if window is None:
            raise SoaError("ServiceException", "The BOM line is no longer valid.", 46010)
        rev = WINDOWS[window]["lines"][line]["rev"]
        children = []
        for item_id, qty, seq in STRUCTURE.get(rev, []):
            child_rev = _resolve_child(item_id, WINDOWS[window]["rule"])
            if not child_rev or not _visible(user, child_rev):
                continue   # unconfigured, or a line the user may not read
            child_line = _new_line(window, child_rev, qty, seq)
            entry = {"bomLine": {"uid": child_line, "type": "BOMLine"}}
            if exp_rev:
                entry["itemRevOfBOMLine"] = {"uid": child_rev, "type": OBJECTS[child_rev]["type"]}
            children.append(entry)
        out.append({"parent": {"bomLine": {"uid": line}, "itemRevOfBOMLine": {"uid": rev}}, "children": children})
    return {"output": out, "serviceData": _service_data(user, [])}


def op_close_bom_windows(user, body, path, handler):
    for w in body.get("bomWindows") or []:
        data = WINDOWS.pop((w or {}).get("uid"), None)
        if data:
            OPEN_WINDOWS["count"] -= 1
            for line in data["lines"]:
                OBJECTS.pop(line, None)
    return {"serviceData": _service_data(user, [])}


def op_where_used(user, body, path, handler):
    if path.startswith("Core-2012-02"):
        targets = [(i.get("inputObject") or {}).get("uid") for i in body.get("input") or []]
        levels = ((body.get("configParams") or {}).get("intMap") or {}).get("numLevels", 1)
        key = "parentObject"
    else:
        targets = [(o or {}).get("uid") for o in body.get("objects") or []]
        levels = body.get("numLevels", 1)
        key = "parentItemRev"
    out = []
    for target in targets:
        if not _visible(user, target):
            raise SoaError("ServiceException", "The object could not be read.", 515001)
        info, frontier, level = [], [target], 0
        while frontier and (levels == -1 or level < int(levels)):
            level += 1
            nxt = []
            for t in frontier:
                for p in _parents(t):
                    if _visible(user, p):
                        info.append({key: {"uid": p}, "level": level})
                        nxt.append(p)
            frontier = nxt
        out.append({"inputObject": {"uid": target}, "info": info})
    return {"output": out, "serviceData": _service_data(user, [])}


def op_read_tickets(user, body, path, handler):
    keys, values = [], []
    for f in body.get("files") or []:
        uid = (f or {}).get("uid")
        if uid in FILES and any(uid in (o["props"].get("ref_list") or []) and _visible(user, u)
                                for u, o in OBJECTS.items()):
            ticket = secrets.token_hex(24)
            TICKETS[ticket] = uid
            keys.append({"uid": uid, "type": "ImanFile"})
            values.append(ticket)
    return {"tickets": [keys, values], "serviceData": _service_data(user, [])}


OPS = {
    "Core-2011-06-Session/login": op_login, "Core-2006-03-Session/login": op_login,
    "Core-2011-06-Session/loginSSO": op_login_sso, "Core-2006-03-Session/loginSSO": op_login_sso,
    "Core-2006-03-Session/logout": op_logout,
    "Core-2007-01-Session/getTCSessionInfo": op_session_info,
    "Core-2015-10-Session/getTypeDescriptions2": op_type_descriptions,
    "Core-2011-06-Session/getTypeDescriptions": op_type_descriptions,
    "Core-2006-03-DataManagement/getProperties": op_get_properties,
    "Query-2006-03-SavedQuery/getSavedQueries": op_saved_queries,
    "Query-2006-03-SavedQuery/describeSavedQueries": op_describe_queries,
    "Query-2008-06-SavedQuery/executeSavedQueries": op_execute_query,
    "Query-2007-06-SavedQuery/executeSavedQueries": op_execute_query,
    "Query-2006-03-SavedQuery/executeSavedQuery": op_execute_query,
    "Cad-2007-01-StructureManagement/getRevisionRules": op_revision_rules,
    "Cad-2007-01-StructureManagement/createBOMWindows": op_create_bom_windows,
    "Cad-2007-01-StructureManagement/expandPSOneLevel": op_expand_one_level,
    "Cad-2007-01-StructureManagement/closeBOMWindows": op_close_bom_windows,
    "Core-2007-01-DataManagement/whereUsed": op_where_used,
    "Core-2012-02-DataManagement/whereUsed": op_where_used,
    "Core-2006-03-FileManagement/getFileReadTickets": op_read_tickets,
}
_NO_SESSION = {"Core-2011-06-Session/login", "Core-2006-03-Session/login", "Core-2011-06-Session/loginSSO",
               "Core-2006-03-Session/loginSSO"}


# --------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet
        pass

    def _send(self, status, body=b"", ctype="application/json;charset=UTF-8", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _cookies(self) -> dict:
        out = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k:
                out[k] = v
        return out

    def start_session(self, user):
        sid, xsrf = secrets.token_hex(16), secrets.token_hex(12)
        SESSIONS[sid] = {"user": user, "xsrf": xsrf}
        self._new_cookies = [("Set-Cookie", f"JSESSIONID={sid}; Path={self.server.context}; HttpOnly"),
                             ("Set-Cookie", f"XSRF-TOKEN={xsrf}; Path=/")]
        return {"serverInfo": {"DisplayVersion": "Teamcenter 14.3", "HostName": "tcserver", "UserID": user,
                               "TcServerID": sid[:8]}, "ServiceData": _service_data(user, [])}

    def end_session(self):
        SESSIONS.pop(self._cookies().get("JSESSIONID"), None)

    # ---- SOA
    def do_POST(self):  # noqa: C901
        path = urlsplit(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if path.startswith(TCSS_CONTEXT):
            return self._tcss_post(path, raw)
        prefix = self.server.context + "/JsonRestServices/"
        if not path.startswith(prefix):
            return self._send(404, b"<html><body>Not Found</body></html>", "text/html")
        op = unquote(path[len(prefix):])
        if op not in OPS or (STATE["profile"] == "legacy" and op in _LEGACY_MISSING):
            return self._send(404, b"<html><body>HTTP Status 404 - Not Found</body></html>", "text/html")
        try:
            envelope = json.loads(raw or b"{}")
            body = envelope.get("body") or {}
        except ValueError:
            return self._send(400, b"<html>Bad Request</html>", "text/html")
        self._new_cookies = []
        user = None
        with _LOCK:
            try:
                if op not in _NO_SESSION:
                    cookies = self._cookies()
                    session = SESSIONS.get(cookies.get("JSESSIONID"))
                    if STATE["expire_sessions"] and session:
                        SESSIONS.pop(cookies.get("JSESSIONID"), None)
                        STATE["expire_sessions"] = False
                        session = None
                    if session is None:
                        raise SoaError("InvalidUserException", "The user session has expired or does not exist.",
                                       515024)
                    if self.headers.get("X-XSRF-TOKEN") != session["xsrf"]:
                        return self._send(403, b'{"message": "XSRF token mismatch"}')
                    user = session["user"]
                REQUEST_LOG.append((user, op))
                result = OPS[op](user, body, op, self)
                if op in _NO_SESSION:
                    REQUEST_LOG[-1] = (result["serverInfo"]["UserID"], op)
            except SoaError as exc:
                REQUEST_LOG.append((user, op)) if not REQUEST_LOG or REQUEST_LOG[-1] != (user, op) else None
                payload = {".QName": QNAME.format(exc.exc), "code": exc.code, "level": 3, "message": exc.message}
                return self._send(200, json.dumps(payload).encode())
        return self._send(200, json.dumps(result).encode(), headers=self._new_cookies)

    # ---- file server + TcSS
    def do_GET(self):
        parts = urlsplit(self.path)
        path, query = parts.path, dict(parse_qsl(parts.query))
        if "/fms/fmsdownload/" in path:
            uid = TICKETS.get(query.get("ticket") or "")
            if uid is None:
                return self._send(403, b"<html>Invalid ticket</html>", "text/html")
            data = FILES[uid][1]
            return self._send(200, data, "application/octet-stream")
        if path == f"{TCSS_CONTEXT}/weblogin/login_redirect":
            app = query.get("appID")
            if app not in STATE["app_roots"]:
                return self._send(400, b"<html><body>Unknown application ID</body></html>", "text/html")
            auth = self.headers.get("Authorization") or ""
            if auth.startswith("Negotiate ") and STATE["kerberos_user"]:
                return self._token_redirect(app, STATE["kerberos_user"])
            return self._login_form(app)
        return self._send(404, b"<html><body>Not Found</body></html>", "text/html")

    def _login_form(self, app, error=""):
        html = (f"<html><body><h1>Teamcenter Security Services</h1><p>{error}</p>"
                f'<form method="post" action="{TCSS_CONTEXT}/weblogin/login">'
                f'<input type="hidden" name="appID" value="{app}">'
                '<input name="username" placeholder="User ID"> <input name="password" type="password">'
                '<button type="submit">Log in</button></form></body></html>')
        return self._send(200, html.encode(), "text/html;charset=UTF-8")

    def _token_redirect(self, app, user):
        token = secrets.token_urlsafe(24)
        TOKENS[token] = user
        root = STATE["app_roots"][app]
        sep = "&" if "?" in root else "?"
        target = f"{root}{sep}{urlencode({'TCSS_TOKEN': token, 'TCSS_USERID': user})}"
        return self._send(302, b"", "text/html", headers=[("Location", target)])

    def _tcss_post(self, path, raw):
        if path != f"{TCSS_CONTEXT}/weblogin/login":
            return self._send(404, b"<html>Not Found</html>", "text/html")
        form = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
        app = form.get("appID")
        if app not in STATE["app_roots"]:
            return self._send(400, b"<html><body>Unknown application ID</body></html>", "text/html")
        user = form.get("username")
        if user not in USERS or USERS[user]["password"] != form.get("password"):
            return self._login_form(app, "Invalid user ID or password.")
        return self._token_redirect(app, user)


class MockServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, context):
        super().__init__(addr, Handler)
        self.context = context

    def handle_error(self, request, client_address):
        import sys
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            super().handle_error(request, client_address)


def make_server(port: int = 0, host: str = "127.0.0.1", context: str = DEFAULT_CONTEXT) -> MockServer:
    return MockServer((host, port), context.rstrip("/"))


def main():
    parser = argparse.ArgumentParser(description="Mock Siemens Teamcenter (SOA JSON, TcSS, file server)")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--app-root", action="append", default=[],
                        help="APPID=URL: an application registered with the TcSS identity service")
    args = parser.parse_args()
    server = make_server(args.port, args.host)
    for pair in args.app_root:
        app, _, url = pair.partition("=")
        STATE["app_roots"][app] = url
    print(f"Mock Teamcenter listening on http://{args.host}:{args.port}{server.context} "
          f"(TcSS: http://{args.host}:{args.port}{TCSS_CONTEXT}/weblogin/login_redirect)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
