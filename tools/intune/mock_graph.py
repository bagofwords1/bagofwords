"""Mock Microsoft Graph for Intune — shapes per the Graph v1.0 API reference.

There is no Intune emulator or container: Intune is SaaS-only and needs a
licensed Entra tenant. Like tools/appdynamics/mock_controller.py, this serves
the exact surface `IntuneClient` touches, with JSON shapes taken from the
v1.0 docs' sample payloads, so the connector can be driven end to end
(connect form → Test → catalog → agent query) without a tenant:

  POST /{tenant}/oauth2/v2.0/token                                  (client credentials)
  GET  /v1.0/deviceManagement/managedDevices
  GET  /v1.0/deviceManagement/deviceCompliancePolicies
  GET  /v1.0/deviceManagement/deviceCompliancePolicies/{id}/deviceStatuses
  GET  /v1.0/deviceManagement/deviceConfigurations
  GET  /v1.0/deviceAppManagement/mobileApps
  GET  /v1.0/deviceManagement/detectedApps
  GET  /v1.0/deviceManagement/windowsAutopilotDeviceIdentities

Faithfulness rules enforced on purpose:
  - Bearer token required; permissions are per app registration. A call whose
    permission the app lacks is a 403 (the real Graph behaviour), which is how
    the client decides what to leave out of the catalog.
  - Paging through `@odata.nextLink` honouring `$top` (default page 100).
  - `$select` trims properties. WITHOUT it, mobileApps inline a base64
    `largeIcon` blob (as the real API does) — a client that forgets $select
    pays for it.
  - `$filter` supports eq / ne / gt / ge / lt / le / and / startswith() on a
    per-collection allowlist only; anything else → 400 BadRequest, mirroring
    Intune's partial OData support. NOTE: the real allowlist is not published
    per property — this one is an approximation; validate on a real tenant.
  - Sentinel dates ("never"): 0001-01-01T00:00:00Z and 9999-12-31T23:59:59Z.
  - Tenant `nolicense` → 400 "Request not applicable to target tenant."
  - INTUNE_MOCK_THROTTLE_EVERY=N → every Nth Graph call is a 429 with
    Retry-After: 1 (exercises the shared Graph throttle handling).
  - FAIL_COLLECTIONS (module set, for tests) → those collections answer 500,
    a non-permission failure that must surface instead of shrinking the catalog.

Run:  cd tools/intune && uv run --project ../../backend uvicorn mock_graph:app --port 8095
Point the backend at it (both env vars must be set before the backend starts):
      BOW_GRAPH_BASE_URL=http://localhost:8095/v1.0
      BOW_GRAPH_TOKEN_BASE_URL=http://localhost:8095
Apps (tenant id: any GUID, e.g. 11111111-1111-1111-1111-111111111111):
      full access     client_id intune-reader   / secret Reader123!
      no Autopilot    client_id intune-partial  / secret Partial123!
      nothing granted client_id intune-none     / secret None123!
"""
import base64
import os
import re
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse

ALL_PERMS = {
    "DeviceManagementManagedDevices.Read.All",
    "DeviceManagementConfiguration.Read.All",
    "DeviceManagementApps.Read.All",
    "DeviceManagementServiceConfig.Read.All",
}
APPS = {
    "intune-reader": ("Reader123!", ALL_PERMS),
    "intune-partial": ("Partial123!", ALL_PERMS - {"DeviceManagementServiceConfig.Read.All"}),
    "intune-none": ("None123!", set()),
}
THROTTLE_EVERY = int(os.environ.get("INTUNE_MOCK_THROTTLE_EVERY", "0") or 0)
FAIL_COLLECTIONS: set = set()

_tokens: dict[str, dict] = {}  # token -> {"perms", "tenant"}
_calls = {"n": 0}

app = FastAPI(title="Mock Microsoft Graph — Intune (v1.0 shapes)")

NEVER = "0001-01-01T00:00:00Z"
NO_GRACE = "9999-12-31T23:59:59Z"
NOW = datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _gid(prefix: int, i: int) -> str:
    return f"{prefix:08x}-{i:04x}-4{i:03x}-8000-{i:012x}"


# ── seeded estate: "Contoso" — 120 devices across Windows / iOS / Android / macOS ──

_OS = [
    ("Windows", ["10.0.22631.4317", "10.0.19045.5011", "10.0.26100.2033"], [("Dell Inc.", "Latitude 7440"), ("LENOVO", "ThinkPad T14 Gen 4"), ("HP", "EliteBook 840 G10")]),
    ("iOS", ["17.6.1", "18.0.1"], [("Apple", "iPhone 15"), ("Apple", "iPhone 14 Pro")]),
    ("Android", ["14", "13"], [("samsung", "Galaxy S23"), ("Google", "Pixel 8")]),
    ("macOS", ["14.6.1", "15.0.1"], [("Apple", "MacBook Pro (14-inch, M3)")]),
]
_DEPTS = ["finance", "sales", "eng", "ops", "hr"]


def _devices() -> list[dict]:
    out = []
    for i in range(120):
        os_name, versions, hw = _OS[i % 4 if i % 10 else 0]  # Windows-heavy, like most fleets
        maker, model = hw[i % len(hw)]
        state = "compliant"
        if i % 7 == 0:
            state = "noncompliant"
        elif i % 11 == 0:
            state = "inGracePeriod"
        elif i % 29 == 0:
            state = "unknown"
        user = f"user{i:03d}"
        last_sync = NOW - timedelta(hours=(i * 13) % (24 * 45))
        out.append({
            "@odata.type": "#microsoft.graph.managedDevice",
            "id": _gid(0x705C034C, i),
            "userId": _gid(0xAB000000, i),
            "deviceName": f"{os_name[:3].upper()}-{_DEPTS[i % 5].upper()}-{i:03d}",
            "managedDeviceOwnerType": "personal" if i % 6 == 0 else "company",
            "managementState": "managed",
            "enrolledDateTime": _iso(NOW - timedelta(days=30 + (i * 5) % 600)),
            "lastSyncDateTime": _iso(last_sync),
            "operatingSystem": os_name,
            "complianceState": state,
            "jailBroken": "False" if os_name in ("iOS", "Android") else "Unknown",
            "managementAgent": "mdm" if os_name != "Windows" else ("configurationManagerClientMdm" if i % 9 == 0 else "mdm"),
            "osVersion": versions[i % len(versions)],
            "easActivated": False,
            "easDeviceId": "",
            "easActivationDateTime": NEVER,
            "azureADRegistered": True,
            "deviceEnrollmentType": "windowsAzureADJoin" if os_name == "Windows" else "userEnrollment",
            "activationLockBypassCode": "SECRET-BYPASS-CODE" if os_name == "iOS" else None,
            "emailAddress": f"{user}@contoso.com",
            "azureADDeviceId": _gid(0xAAD00000, i),
            "deviceRegistrationState": "registered",
            "deviceCategoryDisplayName": _DEPTS[i % 5].title(),
            "isSupervised": os_name == "iOS" and i % 2 == 0,
            "exchangeLastSuccessfulSyncDateTime": NEVER,
            "exchangeAccessState": "none",
            "exchangeAccessStateReason": "none",
            "isEncrypted": i % 13 != 0,
            "userPrincipalName": f"{user}@contoso.com",
            "model": model,
            "manufacturer": maker,
            "imei": f"35{i:013d}" if os_name in ("iOS", "Android") else "",
            "complianceGracePeriodExpirationDateTime": _iso(NOW + timedelta(days=3)) if state == "inGracePeriod" else NO_GRACE,
            "serialNumber": f"SN{i:06d}",
            "phoneNumber": "",
            "androidSecurityPatchLevel": "2026-09-01" if os_name == "Android" else "",
            "userDisplayName": f"User {i:03d}",
            "wiFiMacAddress": f"00:1a:2b:{i % 256:02x}:00:01",
            "subscriberCarrier": "",
            "meid": "",
            "totalStorageSpaceInBytes": 256 * 1024 ** 3 if os_name == "Windows" else 128 * 1024 ** 3,
            "freeStorageSpaceInBytes": ((i * 7919) % 100 + 5) * 1024 ** 3,
            "managedDeviceName": f"{user}_{os_name}_{i}",
            "partnerReportedThreatState": "secured" if i % 17 else "high",
            "managementCertificateExpirationDate": _iso(NOW + timedelta(days=200 + i)),
            "physicalMemoryInBytes": (16 if os_name in ("Windows", "macOS") else 8) * 1024 ** 3,
            "enrollmentProfileName": "Corporate Autopilot" if os_name == "Windows" else "",
            "notes": None,
        })
    return out


DEVICES = _devices()

POLICIES = [
    {"@odata.type": "#microsoft.graph.windows10CompliancePolicy", "id": _gid(0x4214B716, 1), "displayName": "Windows 10/11 baseline", "description": "BitLocker, Defender, min OS 10.0.19045"},
    {"@odata.type": "#microsoft.graph.iosCompliancePolicy", "id": _gid(0x4214B716, 2), "displayName": "iOS baseline", "description": "Passcode, min iOS 17"},
    {"@odata.type": "#microsoft.graph.androidCompliancePolicy", "id": _gid(0x4214B716, 3), "displayName": "Android baseline", "description": "Encryption, no root"},
    {"@odata.type": "#microsoft.graph.macOSCompliancePolicy", "id": _gid(0x4214B716, 4), "displayName": "macOS baseline", "description": "FileVault, firewall"},
]
for n, p in enumerate(POLICIES):
    p.update({"createdDateTime": _iso(NOW - timedelta(days=400 - n)), "lastModifiedDateTime": _iso(NOW - timedelta(days=20 + n)), "version": 3 + n})

_POLICY_OS = {POLICIES[0]["id"]: "Windows", POLICIES[1]["id"]: "iOS", POLICIES[2]["id"]: "Android", POLICIES[3]["id"]: "macOS"}
_STATUS = {"compliant": "compliant", "noncompliant": "nonCompliant", "inGracePeriod": "inGracePeriod", "unknown": "unknown"}


def _statuses(policy_id: str) -> list[dict]:
    os_name = _POLICY_OS[policy_id]
    out = []
    for d in DEVICES:
        if d["operatingSystem"] != os_name:
            continue
        out.append({
            "@odata.type": "#microsoft.graph.deviceComplianceDeviceStatus",
            "id": f"{policy_id[:8]}_{d['id']}",
            "deviceDisplayName": d["deviceName"],
            "userName": d["userDisplayName"],
            "deviceModel": d["model"],
            "complianceGracePeriodExpirationDateTime": d["complianceGracePeriodExpirationDateTime"],
            "status": _STATUS[d["complianceState"]],
            "lastReportedDateTime": d["lastSyncDateTime"],
            "userPrincipalName": d["userPrincipalName"],
        })
    return out


CONFIGS = [
    {"@odata.type": "#microsoft.graph.windows10GeneralConfiguration", "id": _gid(0xC0F10000, 1), "displayName": "Windows - Device restrictions", "description": "Block consumer features"},
    {"@odata.type": "#microsoft.graph.windows10EndpointProtectionConfiguration", "id": _gid(0xC0F10000, 2), "displayName": "Windows - Endpoint protection", "description": "BitLocker + firewall"},
    {"@odata.type": "#microsoft.graph.iosGeneralDeviceConfiguration", "id": _gid(0xC0F10000, 3), "displayName": "iOS - Restrictions", "description": ""},
    {"@odata.type": "#microsoft.graph.androidWorkProfileGeneralDeviceConfiguration", "id": _gid(0xC0F10000, 4), "displayName": "Android - Work profile", "description": ""},
    {"@odata.type": "#microsoft.graph.macOSGeneralDeviceConfiguration", "id": _gid(0xC0F10000, 5), "displayName": "macOS - Restrictions", "description": ""},
]
for n, c in enumerate(CONFIGS):
    c.update({"createdDateTime": _iso(NOW - timedelta(days=300 - n)), "lastModifiedDateTime": _iso(NOW - timedelta(days=10 + n)), "version": 1 + n})

_ICON = {"@odata.type": "microsoft.graph.mimeContent", "type": "image/png", "value": base64.b64encode(b"\x89PNG" + b"\0" * 4000).decode()}
MOBILE_APPS = []
for n, (kind, name, pub) in enumerate([
    ("win32LobApp", "Company Portal", "Microsoft Corporation"),
    ("win32LobApp", "7-Zip", "Igor Pavlov"),
    ("officeSuiteApp", "Microsoft 365 Apps for enterprise", "Microsoft"),
    ("iosStoreApp", "Microsoft Outlook", "Microsoft Corporation"),
    ("iosStoreApp", "Microsoft Teams", "Microsoft Corporation"),
    ("androidManagedStoreApp", "Microsoft Authenticator", "Microsoft Corporation"),
    ("webApp", "Contoso Intranet", "Contoso IT"),
    ("macOSLobApp", "Contoso VPN", "Contoso IT"),
]):
    MOBILE_APPS.append({
        "@odata.type": f"#microsoft.graph.{kind}", "id": _gid(0x0177548A, n), "displayName": name,
        "description": f"{name} managed by Intune", "publisher": pub, "largeIcon": _ICON,
        "createdDateTime": _iso(NOW - timedelta(days=200 + n)), "lastModifiedDateTime": _iso(NOW - timedelta(days=5 + n)),
        "isFeatured": n == 0, "privacyInformationUrl": None, "informationUrl": None,
        "owner": "IT", "developer": pub, "notes": "", "publishingState": "published",
    })

DETECTED = []
for n, (name, ver, pub, plat, count) in enumerate([
    ("Google Chrome", "130.0.6723.70", "Google LLC", "windows", 64),
    ("Google Chrome", "129.0.6668.101", "Google LLC", "windows", 21),
    ("Mozilla Firefox", "131.0.3", "Mozilla", "windows", 12),
    ("Zoom Workplace", "6.2.5", "Zoom Video Communications, Inc.", "windows", 40),
    ("Slack", "4.40.128", "Slack Technologies", "macOS", 9),
    ("WhatsApp", "24.20.79", "WhatsApp Inc.", "ios", 18),
    ("TikTok", "36.5.0", "TikTok Ltd.", "androidOSP", 3),
    ("Python 3.12.7 (64-bit)", "3.12.7150.0", "Python Software Foundation", "windows", 15),
]):
    DETECTED.append({"@odata.type": "#microsoft.graph.detectedApp", "id": _gid(0xCAF60DB6, n), "displayName": name, "version": ver, "sizeInByte": 100_000_000 + n, "deviceCount": count, "publisher": pub, "platform": plat})

AUTOPILOT = []
for n, d in enumerate([d for d in DEVICES if d["operatingSystem"] == "Windows"][:30]):
    AUTOPILOT.append({
        "@odata.type": "#microsoft.graph.windowsAutopilotDeviceIdentity", "id": _gid(0xFAC6F0B1, n),
        "groupTag": "CORP" if n % 3 else "KIOSK", "purchaseOrderIdentifier": f"PO-{2026000 + n // 10}",
        "serialNumber": d["serialNumber"], "productKey": "XXXXX-SECRET-KEY", "manufacturer": d["manufacturer"],
        "model": d["model"], "enrollmentState": "enrolled", "lastContactedDateTime": d["lastSyncDateTime"],
        "addressableUserName": d["userDisplayName"], "userPrincipalName": d["userPrincipalName"],
        "resourceName": "", "skuNumber": "", "systemFamily": "", "azureActiveDirectoryDeviceId": d["azureADDeviceId"],
        "managedDeviceId": d["id"], "displayName": d["deviceName"],
    })
for n in range(30, 36):  # registered, never enrolled
    AUTOPILOT.append({
        "@odata.type": "#microsoft.graph.windowsAutopilotDeviceIdentity", "id": _gid(0xFAC6F0B1, n),
        "groupTag": "CORP", "purchaseOrderIdentifier": "PO-2026009", "serialNumber": f"NEW{n:05d}",
        "productKey": "", "manufacturer": "Dell Inc.", "model": "Latitude 7450", "enrollmentState": "notContacted",
        "lastContactedDateTime": NEVER, "addressableUserName": "", "userPrincipalName": "", "resourceName": "",
        "skuNumber": "", "systemFamily": "", "azureActiveDirectoryDeviceId": "00000000-0000-0000-0000-000000000000",
        "managedDeviceId": "00000000-0000-0000-0000-000000000000", "displayName": "",
    })

# collection -> (rows provider, permission, filterable props, orderable props)
COLLECTIONS: dict[str, tuple] = {
    "deviceManagement/managedDevices": (
        lambda: DEVICES, "DeviceManagementManagedDevices.Read.All",
        {"operatingSystem", "complianceState", "deviceName", "managementAgent", "lastSyncDateTime",
         "enrolledDateTime", "managedDeviceOwnerType", "userPrincipalName", "deviceEnrollmentType", "isEncrypted"},
        {"deviceName", "lastSyncDateTime", "enrolledDateTime"},
    ),
    "deviceManagement/deviceCompliancePolicies": (
        lambda: POLICIES, "DeviceManagementConfiguration.Read.All", {"displayName"}, {"displayName"},
    ),
    "deviceManagement/deviceConfigurations": (
        lambda: CONFIGS, "DeviceManagementConfiguration.Read.All", {"displayName"}, {"displayName"},
    ),
    "deviceAppManagement/mobileApps": (
        lambda: MOBILE_APPS, "DeviceManagementApps.Read.All", {"displayName", "publisher", "publishingState"}, {"displayName"},
    ),
    "deviceManagement/detectedApps": (
        lambda: DETECTED, "DeviceManagementManagedDevices.Read.All", {"displayName", "platform", "publisher"},
        {"displayName", "deviceCount"},
    ),
    "deviceManagement/windowsAutopilotDeviceIdentities": (
        lambda: AUTOPILOT, "DeviceManagementServiceConfig.Read.All", {"enrollmentState", "groupTag", "serialNumber"},
        {"serialNumber"},
    ),
}
STATUS_FILTERABLE = {"status", "deviceDisplayName", "userPrincipalName"}


# ── OData helpers ─────────────────────────────────────────────────────────────

def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, "innerError": {"request-id": secrets.token_hex(8)}}})


_CMP = re.compile(r"^\s*(\w+)\s+(eq|ne|gt|ge|lt|le)\s+('(?:[^']|'')*'|true|false|null|[\d\-:TZ.]+)\s*$", re.I)
_STARTS = re.compile(r"^\s*startswith\(\s*(\w+)\s*,\s*'((?:[^']|'')*)'\s*\)\s*$", re.I)


def _literal(raw: str):
    if raw.startswith("'"):
        return raw[1:-1].replace("''", "'")
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    if low == "null":
        return None
    return raw  # numbers / datetimes compared as strings (ISO sorts lexically)


def _compile_filter(expr: str, allowed: set) -> Callable[[dict], bool]:
    clauses = re.split(r"\s+and\s+", expr.strip(), flags=re.I)
    preds = []
    for clause in clauses:
        m = _STARTS.match(clause)
        if m:
            prop, val = m.group(1), m.group(2).replace("''", "'")
            if prop not in allowed:
                raise ValueError(f"Filter on property '{prop}' is not supported.")
            preds.append(lambda r, p=prop, v=val: str(r.get(p) or "").lower().startswith(v.lower()))
            continue
        m = _CMP.match(clause)
        if not m:
            raise ValueError(f"Invalid filter clause: {clause}")
        prop, op, raw = m.group(1), m.group(2).lower(), m.group(3)
        if prop not in allowed:
            raise ValueError(f"Filter on property '{prop}' is not supported.")
        val = _literal(raw)

        def pred(r, p=prop, o=op, v=val):
            cur = r.get(p)
            if isinstance(cur, (int, float)) and not isinstance(cur, bool) and isinstance(v, str):
                try:
                    v = float(v)
                except ValueError:
                    pass
            if o == "eq":
                return (str(cur).lower() == str(v).lower()) if isinstance(v, str) else cur == v
            if o == "ne":
                return (str(cur).lower() != str(v).lower()) if isinstance(v, str) else cur != v
            if cur is None:
                return False
            return {"gt": cur > v, "ge": cur >= v, "lt": cur < v, "le": cur <= v}[o]
        preds.append(pred)
    return lambda r: all(p(r) for p in preds)


def _auth(request: Request, permission: str) -> JSONResponse | None:
    if THROTTLE_EVERY:
        _calls["n"] += 1
        if _calls["n"] % THROTTLE_EVERY == 0:
            return JSONResponse(status_code=429, headers={"Retry-After": "1"}, content={"error": {"code": "TooManyRequests", "message": "Too many requests"}})
    header = request.headers.get("authorization", "")
    tok = _tokens.get(header[7:]) if header.lower().startswith("bearer ") else None
    if not tok:
        return _err(401, "InvalidAuthenticationToken", "Access token is empty or invalid.")
    if tok["tenant"] == "nolicense":
        return _err(400, "BadRequest", "Request not applicable to target tenant.")
    if permission not in tok["perms"]:
        return _err(403, "Forbidden", f"Application is not authorized to perform this operation. Application must have one of the following scopes: {permission}")
    return None


def _respond(request: Request, rows: list[dict], filterable: set, orderable: set) -> JSONResponse:
    q = request.query_params
    if q.get("$filter"):
        try:
            keep = _compile_filter(q["$filter"], filterable)
        except ValueError as e:
            return _err(400, "BadRequest", str(e))
        rows = [r for r in rows if keep(r)]
    if q.get("$orderby"):
        prop, _, direction = q["$orderby"].strip().partition(" ")
        if prop not in orderable:
            return _err(400, "BadRequest", f"OrderBy on property '{prop}' is not supported.")
        rows = sorted(rows, key=lambda r: (r.get(prop) is None, r.get(prop)), reverse=direction.strip().lower() == "desc")
    if q.get("$select"):
        wanted = [s.strip() for s in q["$select"].split(",") if s.strip()]
        rows = [{"@odata.type": r.get("@odata.type"), **{k: r.get(k) for k in wanted}} for r in rows]
    try:
        top = max(1, min(int(q.get("$top") or 100), 1000))
        skip = max(0, int(q.get("$skiptoken") or 0))
    except ValueError:
        return _err(400, "BadRequest", "Invalid $top / $skiptoken.")
    page = rows[skip:skip + top]
    body: dict = {"@odata.context": str(request.url).split("?")[0], "value": page}
    if skip + top < len(rows):
        params = dict(q)
        params["$skiptoken"] = str(skip + top)
        params["$top"] = str(top)
        from urllib.parse import urlencode
        body["@odata.nextLink"] = str(request.url).split("?")[0] + "?" + urlencode(params)
    return JSONResponse(body)


# ── routes ────────────────────────────────────────────────────────────────────

@app.post("/{tenant}/oauth2/v2.0/token")
def token(tenant: str, grant_type: str = Form(...), client_id: str = Form(...), client_secret: str = Form(""), scope: str = Form("")):
    if grant_type != "client_credentials":
        return JSONResponse(status_code=400, content={"error": "unsupported_grant_type"})
    app_reg = APPS.get(client_id)
    if not app_reg or app_reg[0] != client_secret:
        return JSONResponse(status_code=401, content={"error": "invalid_client", "error_description": "AADSTS7000215: Invalid client secret provided."})
    if scope != "https://graph.microsoft.com/.default":
        return JSONResponse(status_code=400, content={"error": "invalid_scope"})
    tok = secrets.token_urlsafe(24)
    _tokens[tok] = {"perms": app_reg[1], "tenant": tenant}
    return {"token_type": "Bearer", "expires_in": 3599, "access_token": tok}


@app.get("/v1.0/deviceManagement/deviceCompliancePolicies/{policy_id}/deviceStatuses")
def device_statuses(policy_id: str, request: Request):
    denied = _auth(request, "DeviceManagementConfiguration.Read.All")
    if denied:
        return denied
    if policy_id not in _POLICY_OS:
        return _err(404, "ResourceNotFound", "Policy not found")
    return _respond(request, _statuses(policy_id), STATUS_FILTERABLE, {"deviceDisplayName"})


@app.get("/v1.0/{collection:path}")
def collection(collection: str, request: Request):
    spec = COLLECTIONS.get(collection)
    if not spec:
        return _err(404, "ResourceNotFound", f"Resource not found for the segment '{collection}'.")
    rows_fn, perm, filterable, orderable = spec
    denied = _auth(request, perm)
    if denied:
        return denied
    if collection in FAIL_COLLECTIONS:
        return _err(500, "InternalServerError", "An internal server error occurred.")
    return _respond(request, rows_fn(), filterable, orderable)


@app.get("/healthz")
def healthz():
    return {"ok": True, "devices": len(DEVICES)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("INTUNE_MOCK_PORT", "8095")))
