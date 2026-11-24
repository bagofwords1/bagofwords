"""An in-memory SharePoint Server REST provider for lists (2013+ `/_api/`).

Used by the unit tests of `sharepoint_onprem_lists` and served over HTTP with a
real NTLM handshake by `tools/agent/mock_sharepoint_lists_server.py`. It answers
only the read requests the connector makes, but answers them the way SharePoint
Server does, including the parts that make on-premises lists hard:

* the List View Threshold — on a list with more items than `threshold`, any
  `$filter` or `$orderby` on a column without an index is refused with
  SPQueryThrottledException (HTTP 500), however few rows it would return;
* the lookup column threshold — more than `lookup_threshold` person/lookup
  joins in one query is refused the same way;
* `$top` capped at 5,000, default 100; `$skiptoken=Paged=TRUE&p_ID=n` paging and
  a `__next` link while more items remain; `$skip` ignored;
* `$select` validation: unknown properties and person/lookup sub-fields that are
  not `$expand`ed are HTTP 400;
* projecting `EMail` from a person column fails the whole query when one of the
  users has no email address;
* verbose JSON (`{"d": {"results": [...]}}`, multi-values as `{"results": []}`)
  or `odata=nometadata` (`{"value": [...]}`, bare arrays), per the Accept header;
* REST property names are `EntityPropertyName`s (`OData__x0032_017`);
* taxonomy values whose `Label` is the term's WssId, with the readable labels in
  a hidden companion note field;
* folders inside a list come back as items with FileSystemObjectType 1.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

SITE_PATH = "/sites/ops"
THROTTLED_CODE = "-2147024860, Microsoft.SharePoint.SPQueryThrottledException"


def _guid(seed: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "bow-fake-sp:" + seed))


@dataclass
class FakeField:
    internal: str
    title: str
    type_name: str
    entity: Optional[str] = None
    hidden: bool = False
    read_only: bool = False
    indexed: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.entity is None:
            self.entity = ("OData_" + self.internal) if self.internal.startswith("_") else self.internal
        self.id = _guid("field:" + self.internal + ":" + self.title)

    @property
    def is_lookup(self) -> bool:
        return self.type_name in ("Lookup", "LookupMulti", "User", "UserMulti")

    @property
    def multi(self) -> bool:
        return self.type_name in ("LookupMulti", "UserMulti", "MultiChoice", "TaxonomyFieldTypeMulti")

    def as_json(self) -> dict:
        body = {
            "__metadata": {"type": f"SP.Field{self.type_name}"},
            "Id": self.id, "InternalName": self.internal, "StaticName": self.internal,
            "EntityPropertyName": self.entity, "Title": self.title, "TypeAsString": self.type_name,
            "Hidden": self.hidden, "ReadOnlyField": self.read_only, "Indexed": self.indexed,
        }
        body.update(self.extra)
        return body


@dataclass
class FakeList:
    title: str
    fields: List[FakeField]
    items: List[Dict[str, Any]]
    base_type: int = 0
    base_template: int = 100
    hidden: bool = False
    root: Optional[str] = None
    description: str = ""
    visible_to: Optional[set] = None   # principals; None = everyone

    def __post_init__(self):
        self.id = _guid("list:" + self.title)
        if self.root is None:
            self.root = f"{SITE_PATH}/Lists/{self.title.replace(' ', '')}"

    def field_by_entity(self, name: str) -> Optional[FakeField]:
        low = name.casefold()
        return next((f for f in self.fields if f.entity.casefold() == low), None)


class FakeSharePointLists:
    """`handle(method, url, params, accept, principal)` → (status, body bytes)."""

    def __init__(self, lists: List[FakeList], users: Dict[int, Tuple[str, Optional[str]]], *,
                 origin: str = "https://sharepoint.corp.example", site_path: str = SITE_PATH,
                 threshold: int = 5000, lookup_threshold: int = 12):
        self.lists = lists
        self.users = users
        self.origin, self.site_path = origin, site_path
        self.threshold, self.lookup_threshold = threshold, lookup_threshold
        self.requests: List[dict] = []
        self.fail_next: List[Tuple[int, dict]] = []   # queued (status, error body) responses
        self.link_pages = True   # emit __next / odata.nextLink while more items remain

    # ------------------------------------------------------------ plumbing

    def handle(self, method: str, url: str, params: Optional[dict] = None, *, accept: str = "",
               principal: Optional[str] = None) -> Tuple[int, bytes]:
        parts = urlsplit(url)
        query = {k: v[-1] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        query.update({k: str(v) for k, v in (params or {}).items()})
        path = unquote(parts.path)
        self.requests.append({"method": method, "path": path, "params": dict(query), "principal": principal})
        verbose = "nometadata" not in accept
        if self.fail_next:
            status, body = self.fail_next.pop(0)
            return status, json.dumps(body).encode()
        prefix = self.site_path + "/_api/"
        if method != "GET" or not path.startswith(prefix):
            return 404, b""
        endpoint = path[len(prefix):]
        try:
            return 200, json.dumps(self._route(endpoint, query, verbose, principal)).encode()
        except _Fail as fail:
            return fail.status, json.dumps({"error": {"code": fail.code, "message": {"lang": "en-US", "value": fail.message}}}).encode()

    def _wrap_one(self, body: dict, verbose: bool) -> dict:
        return {"d": body} if verbose else body

    def _wrap_many(self, rows: List[dict], verbose: bool, next_url: Optional[str] = None) -> dict:
        if verbose:
            d = {"results": rows}
            if next_url:
                d["__next"] = next_url
            return {"d": d}
        out = {"value": rows}
        if next_url:
            out["odata.nextLink"] = next_url
        return out

    def _visible(self, principal: Optional[str]) -> List[FakeList]:
        return [l for l in self.lists if l.visible_to is None or principal in l.visible_to]

    def _route(self, endpoint: str, q: dict, verbose: bool, principal: Optional[str]):
        if endpoint == "web":
            return self._wrap_one({"Title": "Operations", "Url": self.origin + self.site_path}, verbose)
        if endpoint == "web/currentuser":
            return self._wrap_one({"LoginName": f"i:0#.w|corp\\{principal or 'svc-sharepoint'}"}, verbose)
        if endpoint == "web/lists":
            lists = self._visible(principal)
            m = re.fullmatch(r"\s*BaseType eq (\d+)\s*", q.get("$filter", "") or "") if q.get("$filter") else None
            if q.get("$filter") and not m:
                raise _Fail(400, "Microsoft.SharePoint.SPException", "Unsupported filter in fake.")
            if m:
                lists = [l for l in lists if l.base_type == int(m.group(1))]
            return self._wrap_many([self._list_json(l) for l in lists], verbose)
        m = re.fullmatch(r"web/lists\(guid'([0-9a-f-]{36})'\)(/fields|/items)?", endpoint)
        if not m:
            raise _Fail(404, "System.IO.FileNotFoundException", "Not found.")
        target = next((l for l in self._visible(principal) if l.id == m.group(1)), None)
        if target is None:
            raise _Fail(404, "System.IO.FileNotFoundException", "List does not exist.")
        if m.group(2) == "/fields":
            return self._wrap_many([f.as_json() for f in target.fields], verbose)
        if m.group(2) == "/items":
            return self._items(target, q, verbose)
        return self._wrap_one(self._list_json(target), verbose)

    def _list_json(self, l: FakeList) -> dict:
        return {"__metadata": {"type": "SP.List"}, "Id": l.id, "Title": l.title, "Description": l.description,
                "ItemCount": len(l.items), "Hidden": l.hidden, "BaseTemplate": l.base_template,
                "BaseType": l.base_type, "RootFolder": {"__metadata": {"type": "SP.Folder"}, "ServerRelativeUrl": l.root}}

    # --------------------------------------------------------------- items

    def _items(self, l: FakeList, q: dict, verbose: bool) -> dict:
        select = [s.strip() for s in (q.get("$select") or "").split(",") if s.strip()]
        expand = [s.strip() for s in (q.get("$expand") or "").split(",") if s.strip()]
        top = min(int(q.get("$top") or 100), 5000)
        after = 0
        token = q.get("$skiptoken") or ""
        tm = re.search(r"p_ID=(\d+)", unquote(token))
        if tm:
            after = int(tm.group(1))

        # Threshold: a filter/sort on any non-indexed column of a large list.
        referenced = set()
        for expr in (q.get("$filter") or "", q.get("$orderby") or ""):
            for word in re.findall(r"[A-Za-z_][\w]*", re.sub(r"'[^']*'", "", expr)):
                f = l.field_by_entity(word)
                if f is not None:
                    referenced.add(f)
                elif word not in ("eq", "ne", "gt", "lt", "ge", "le", "and", "or", "not", "asc", "desc",
                                  "datetime", "true", "false", "null", "substringof", "startswith"):
                    raise _Fail(400, "Microsoft.SharePoint.SPException", f"The field or property '{word}' does not exist.")
        if len(l.items) > self.threshold and any(not f.indexed for f in referenced):
            raise _Fail(500, THROTTLED_CODE,
                        "The attempted operation is prohibited because it exceeds the list view threshold enforced by the administrator.")
        if q.get("$filter") or q.get("$orderby"):
            raise _Fail(400, "Microsoft.SharePoint.SPException", "The fake does not evaluate filters; the connector must not send them.")

        # Expansion and projection rules.
        joins = 0
        expanded = set()
        for name in expand:
            f = l.field_by_entity(name)
            if f is None or not f.is_lookup:
                raise _Fail(400, "Microsoft.SharePoint.SPException", f"The field or property '{name}' does not exist.")
            expanded.add(f.entity)
            joins += 1
        if joins > self.lookup_threshold:
            raise _Fail(500, THROTTLED_CODE,
                        "The query cannot be completed because the number of lookup columns it contains exceeds the lookup column threshold enforced by the administrator.")
        projections: List[Tuple[str, Optional[FakeField], Optional[str]]] = []
        for s in select:
            if s in ("ID", "Id", "FileSystemObjectType"):
                projections.append((s, None, None))
                continue
            if "/" in s:
                head, sub = s.split("/", 1)
                f = l.field_by_entity(head)
                if f is None or not f.is_lookup:
                    raise _Fail(400, "Microsoft.SharePoint.SPException", f"The field or property '{head}' does not exist.")
                if f.entity not in expanded:
                    raise _Fail(400, "Microsoft.SharePoint.SPException",
                                f"The query to field '{f.entity}/{sub}' is not valid. The $select query string must specify the target fields and the $expand query string must contains {f.entity}.")
                projections.append((s, f, sub))
                continue
            if s.endswith("Id") and l.field_by_entity(s[:-2]) is not None and l.field_by_entity(s[:-2]).is_lookup:
                projections.append((s, l.field_by_entity(s[:-2]), "#id"))
                continue
            f = l.field_by_entity(s)
            if f is None:
                raise _Fail(400, "Microsoft.SharePoint.SPException", f"The field or property '{s}' does not exist.")
            projections.append((s, f, None))

        rows = sorted((it for it in l.items if it["ID"] > after), key=lambda it: it["ID"])
        page, more = rows[:top], len(rows) > top
        out = [self._item_json(l, it, projections, expanded, verbose) for it in page]
        next_url = None
        if more and self.link_pages:
            params = {k: v for k, v in q.items() if k != "$skiptoken"}
            params["$skiptoken"] = f"Paged=TRUE&p_ID={page[-1]['ID']}"
            next_url = (f"{self.origin}{self.site_path}/_api/web/lists(guid'{l.id}')/items?"
                        + urlencode(params, quote_via=quote, safe=""))
        return self._wrap_many(out, verbose, next_url)

    def _user(self, uid: int, sub: str, verbose: bool):
        title, email = self.users[uid]
        if sub == "EMail" and not email:
            raise _Fail(400, "Microsoft.SharePoint.SPException", "Cannot get value for projected field Approver_x005f_EMail.")
        body = {"Title": title, "EMail": email, "Id": uid}
        out = {sub: body.get(sub)}
        if verbose:
            out["__metadata"] = {"type": "SP.Data.UserInfoItem"}
        return out

    def _lookup(self, f: FakeField, ref, sub: str, verbose: bool):
        if f.type_name.startswith("User"):
            return self._user(ref, sub, verbose)
        target = next(l for l in self.lists if l.title == f.extra["_target"])
        row = next(it for it in target.items if it["ID"] == ref)
        out = {sub: row.get(target.field_by_entity(sub).internal if target.field_by_entity(sub) else sub)}
        if verbose:
            out["__metadata"] = {"type": "SP.Data.Item"}
        return out

    def _many(self, values: list, verbose: bool, type_name: str = "Collection(Edm.String)"):
        return {"__metadata": {"type": type_name}, "results": values} if verbose else values

    def _item_json(self, l: FakeList, it: dict, projections, expanded, verbose: bool) -> dict:
        row: Dict[str, Any] = {}
        if verbose:
            row["__metadata"] = {"type": f"SP.Data.{l.title.replace(' ', '')}ListItem", "id": f"Item{it['ID']}"}
        for name, f, sub in projections:
            if f is None:
                row[name] = it.get("FileSystemObjectType", 0) if name == "FileSystemObjectType" else it["ID"]
                continue
            value = it.get(f.internal)
            if sub == "#id":
                if f.multi:
                    row[name] = self._many(list(value or []), verbose, "Collection(Edm.Int32)")
                else:
                    row[name] = value
            elif sub is not None:
                if f.multi:
                    row[f.entity] = self._many([self._lookup(f, v, sub, verbose) for v in (value or [])], verbose, "Collection(SP.Data.Item)")
                elif value is None:
                    row[f.entity] = {"__metadata": {"type": "SP.Data.Item"}} if verbose else {}
                else:
                    row[f.entity] = self._lookup(f, value, sub, verbose)
            elif f.type_name in ("MultiChoice",):
                row[name] = self._many(list(value or []), verbose) if value is not None else None
            elif f.type_name == "URL":
                row[name] = None if value is None else (
                    {"__metadata": {"type": "SP.FieldUrlValue"}, "Description": value[1], "Url": value[0]}
                    if verbose else {"Description": value[1], "Url": value[0]})
            elif f.type_name.startswith("Taxonomy"):
                # SharePoint's REST quirk: Label is the WssId, not the term name.
                terms = value or []
                single = lambda t: {"Label": str(t[2]), "TermGuid": t[1], "WssId": t[2]}
                row[name] = (self._many([single(t) for t in terms], verbose, "Collection(SP.Taxonomy.TaxonomyFieldValue)")
                             if f.multi else (single(terms[0]) if terms else None))
            else:
                row[name] = value
        if verbose:
            # Unexpanded person/lookup columns appear as deferred links in verbose mode.
            for f in l.fields:
                if f.is_lookup and f.entity not in expanded and f.entity not in row:
                    row[f.entity] = {"__deferred": {"uri": f"{self.origin}{self.site_path}/_api/Web/Lists(guid'{l.id}')/Items({it['ID']})/{f.entity}"}}
        return row


class _Fail(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


# ----------------------------------------------------------------- the world

USERS = {
    7: ("Dana Levi", "dana@corp.example"),
    8: ("Noa Cohen", "noa@corp.example"),
    9: ("Avi Mizrahi", None),          # no email: projecting EMail would fail
    1073741823: ("System Account", None),
}


def _system_fields() -> List[FakeField]:
    return [
        FakeField("ID", "ID", "Counter", read_only=True, indexed=True),
        FakeField("ContentType", "Content Type", "Computed"),
        FakeField("Modified", "Modified", "DateTime", read_only=True),
        FakeField("Created", "Created", "DateTime", read_only=True),
        FakeField("Author", "Created By", "User", read_only=True),
        FakeField("Editor", "Modified By", "User", read_only=True),
        FakeField("_UIVersionString", "Version", "Text", read_only=True),
        FakeField("Attachments", "Attachments", "Attachments"),
        FakeField("Edit", "Edit", "Computed", read_only=True),
        FakeField("LinkTitle", "Title", "Computed", read_only=True),
        FakeField("FileLeafRef", "Name", "File", hidden=True),
        FakeField("_ModerationStatus", "Approval Status", "ModStat", hidden=True),
        FakeField("owshiddenversion", "owshiddenversion", "Integer", hidden=True),
        FakeField("AppAuthor", "App Created By", "Lookup", read_only=True),
    ]


def build_world(*, big_items: int = 12000, threshold: int = 5000) -> FakeSharePointLists:
    """The lists a lab site holds; dates are fixed so results are reproducible."""
    projects = FakeList("Projects", [
        *_system_fields(),
        FakeField("Title", "Title", "Text"),
        FakeField("Code", "Project Code", "Text"),
    ], [
        {"ID": 1, "Title": "Data Center Move", "Code": "DCM", "Created": "2026-01-02T09:00:00Z", "Modified": "2026-01-02T09:00:00Z", "Author": 7, "Editor": 7},
        {"ID": 2, "Title": "VPN Upgrade", "Code": "VPN", "Created": "2026-01-03T09:00:00Z", "Modified": "2026-01-03T09:00:00Z", "Author": 7, "Editor": 7},
    ])

    region_text = FakeField("cf3a9b1e0d4c4a6f8b2e7d9c1a5f3e2b", "Region_0", "Note", hidden=True)
    expense_fields = [
        *_system_fields(),
        FakeField("Title", "Title", "Text"),
        FakeField("Category", "Category", "Choice"),
        FakeField("field_2", "Amount", "Currency"),
        FakeField("_x0032_017", "2017", "Number"),
        FakeField("Submitted", "Submitted", "DateTime"),
        FakeField("Approved", "Approved", "Boolean"),
        FakeField("Approver", "Approver", "User"),
        FakeField("Reviewers", "Reviewers", "UserMulti", extra={"AllowMultipleValues": True}),
        FakeField("Tags", "Tags", "MultiChoice"),
        FakeField("Notes", "Notes", "Note", extra={"RichText": True}),
        FakeField("Project", "Project", "Lookup", extra={"LookupField": "Title", "_target": "Projects"}),
        FakeField("Receipt", "Receipt", "URL"),
        FakeField("Total", "Total incl. VAT", "Calculated", read_only=True, extra={"OutputType": 10}),
        FakeField("Region", "Region", "TaxonomyFieldType", extra={"TextField": region_text.id}),
        region_text,
    ]
    terms = {"North": ("North", "5b3f0b3e-0000-4000-8000-000000000001", 11),
             "South": ("South", "5b3f0b3e-0000-4000-8000-000000000002", 12)}
    expense_rows = [
        # id, title, category, amount, 2017, submitted, approved, approver, reviewers, tags, project, region
        (1, "Flight to Berlin", "Travel", 420.0, 12.0, "2026-03-02T08:00:00Z", True, 7, [8], ["Q1", "Conference"], 1, "North"),
        (2, "Hotel Berlin", "Travel", 610.5, 3.0, "2026-03-03T08:00:00Z", True, 7, [8, 9], ["Q1"], 1, "North"),
        (3, "Team lunch", "Meals", 95.0, None, "2026-04-11T11:30:00Z", False, None, [], [], None, "South"),
        (5, "O'Brien taxi", "Travel", 38.25, 1.0, "2026-04-20T22:15:00Z", True, 8, [7], ["Q2"], 2, "South"),
        (6, "Laptop", "Equipment", 1899.0, None, "2026-05-05T09:00:00Z", None, 9, [], ["Q2", "Capex"], 2, None),
        (8, "Router", "Equipment", 240.0, 4.0, "2026-06-30T23:59:59Z", True, 7, [], ["Q2"], 2, "North"),
    ]
    expense_items = []
    for (iid, title, cat, amount, y2017, submitted, approved, approver, reviewers, tags, project, region) in expense_rows:
        expense_items.append({
            "ID": iid, "Title": title, "Category": cat, "field_2": amount, "_x0032_017": y2017,
            "Submitted": submitted, "Approved": approved, "Approver": approver, "Reviewers": reviewers,
            "Tags": tags, "Project": project, "Region": [(terms[region][0], terms[region][1], terms[region][2])] if region else [],
            region_text.internal: f"{terms[region][0]}|{terms[region][1]}" if region else None,
            "Notes": f"<div class=\"ExternalClass1\"><p>Receipt for <b>{title}</b></p><p>Paid &amp; filed</p></div>",
            "Receipt": (f"https://receipts.corp.example/{iid}", f"Receipt {iid}"),
            "Total": f"float;#{amount * 1.17:.4f}",
            "Created": submitted, "Modified": submitted, "Author": approver or 7, "Editor": 7,
        })
    # A folder inside the list: an item, but not a row.
    expense_items.append({"ID": 4, "Title": "Archive", "FileSystemObjectType": 1, "Created": "2026-04-01T00:00:00Z",
                          "Modified": "2026-04-01T00:00:00Z", "Author": 7, "Editor": 7})
    expenses = FakeList("Expenses", expense_fields, sorted(expense_items, key=lambda it: it["ID"]),
                        description="Employee expense claims")

    statuses = ["Open", "In Progress", "Closed", "Closed"]
    tickets = FakeList("Tickets", [
        *_system_fields(),
        FakeField("Title", "Title", "Text"),
        FakeField("Status", "Status", "Choice"),
        FakeField("Priority", "Priority", "Number"),
        FakeField("Opened", "Opened", "DateTime"),
        FakeField("AssignedTo", "Assigned To", "User"),
    ], [
        {"ID": i, "Title": f"Ticket {i}" + (" VPN drops" if i % 997 == 0 else ""),
         "Status": statuses[i % 4], "Priority": float(i % 5 + 1),
         "Opened": f"2026-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}T10:00:00Z",
         "AssignedTo": 7 if i % 2 else 8,
         "Created": "2026-01-01T00:00:00Z", "Modified": "2026-01-01T00:00:00Z", "Author": 7, "Editor": 8}
        for i in range(1, big_items + 1)
    ], description="IT helpdesk tickets")

    wide_people = [FakeField(f"Person{i}", f"Person {i}", "User") for i in range(1, 15)]
    wide = FakeList("Rota", [*_system_fields(), FakeField("Title", "Title", "Text"), *wide_people], [
        {"ID": 1, "Title": "Week 1", **{f"Person{i}": 7 if i % 2 else 8 for i in range(1, 15)},
         "Created": "2026-01-01T00:00:00Z", "Modified": "2026-01-01T00:00:00Z", "Author": 7, "Editor": 7},
    ])

    hidden = FakeList("TaxonomyHiddenList", [*_system_fields(), FakeField("Title", "Title", "Text")], [], hidden=True)
    history = FakeList("Workflow History", [*_system_fields(), FakeField("Title", "Title", "Text")], [], base_template=140)
    library = FakeList("Documents", [*_system_fields(), FakeField("Title", "Title", "Text")], [],
                       base_type=1, base_template=101, root=f"{SITE_PATH}/Shared Documents")
    elsewhere = FakeList("Foreign", [*_system_fields(), FakeField("Title", "Title", "Text")], [],
                         root="/sites/other/Lists/Foreign")
    restricted = FakeList("Salaries", [*_system_fields(), FakeField("Title", "Title", "Text"),
                                       FakeField("Salary", "Salary", "Currency")],
                          [{"ID": 1, "Title": "Dana", "Salary": 100.0, "Created": "2026-01-01T00:00:00Z",
                            "Modified": "2026-01-01T00:00:00Z", "Author": 7, "Editor": 7}],
                          visible_to={"dana", "svc-sharepoint"})
    return FakeSharePointLists(
        [expenses, tickets, projects, wide, hidden, history, library, elsewhere, restricted],
        USERS, threshold=threshold,
    )
