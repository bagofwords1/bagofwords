#!/usr/bin/env python3
"""Mock SAP servers for UI / e2e verification of the BusinessObjects and BW
(XMLA) connectors WITHOUT a licensed SAP system.

- BusinessObjects  : a /biprws REST server on :6405 (logon token, universes,
                     universe outline, query results).
- SAP BW (XMLA)    : an XMLA SOAP server on :8410 (Discover catalogs/cubes/
                     hierarchies/measures, Execute MDX -> rowset).

This is the stubbed boundary for Loop A of the connector feedback loop: it lets
the real app perform Test Connection -> indexing -> query end to end. It serves
canned data only; it is NOT a real SAP implementation.

Run:  python tools/agent/mock_sap_servers.py     (Ctrl-C to stop)
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
import json
import threading


# ---------------------------------------------------------------------------
# BusinessObjects /biprws
# ---------------------------------------------------------------------------

BO_UNIVERSES = {
    "universes": {
        "universe": [
            {"id": "101", "name": "eFashion", "type": "unx", "folderName": "Webi Universes"},
            {"id": "102", "name": "Sales Analytics", "type": "unx", "folderName": "Finance"},
        ]
    }
}

BO_OUTLINES = {
    "101": {"universe": {"id": "101", "name": "eFashion", "outline": {"folder": [
        {"name": "Geography", "item": [
            {"id": "o1", "name": "Country", "type": "dimension", "dataType": "String"},
            {"id": "o2", "name": "Store name", "type": "detail", "dataType": "String"},
        ]},
        {"name": "Measures", "item": [
            {"id": "o3", "name": "Sales revenue", "type": "measure", "dataType": "Numeric"},
            {"id": "o4", "name": "Quantity sold", "type": "measure", "dataType": "Numeric"},
        ]},
    ]}}},
    "102": {"universe": {"id": "102", "name": "Sales Analytics", "outline": {"folder": [
        {"name": "Time", "item": [
            {"id": "p1", "name": "Year", "type": "dimension", "dataType": "String"},
        ]},
        {"name": "Measures", "item": {"id": "p2", "name": "Margin", "type": "measure", "dataType": "Numeric"}},
    ]}}},
}

BO_QUERY_ROWS = {"rows": [
    {"Country": "US", "Sales revenue": 3500000},
    {"Country": "France", "Sales revenue": 1200000},
    {"Country": "Japan", "Sales revenue": 980000},
]}


class BusinessObjectsHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[BO]", self.command, self.path, fmt % args)

    def _send(self, code=200, body=None, headers=None):
        payload = json.dumps(body if body is not None else {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        path = urlparse(self.path).path
        # Drain the request body so keep-alive stays healthy.
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if path.endswith("/logon/long"):
            self._send(200, {}, headers={"X-SAP-LogonToken": "mock-logon-token-123"})
        elif path.endswith("/logoff"):
            self._send(200, {})
        elif path.endswith("/sl/v1/queries"):
            self._send(200, BO_QUERY_ROWS)
        else:
            self._send(404, {"error": "not found"})

    def do_GET(self):
        path = urlparse(self.path).path
        if path.endswith("/sl/v1/universes"):
            self._send(200, BO_UNIVERSES)
            return
        for uid, outline in BO_OUTLINES.items():
            if path.endswith(f"/sl/v1/universes/{uid}"):
                self._send(200, outline)
                return
        self._send(404, {"error": "not found"})


# ---------------------------------------------------------------------------
# SAP BW XMLA SOAP
# ---------------------------------------------------------------------------

XMLA_NS = "urn:schemas-microsoft-com:xml-analysis"
ROWSET_NS = "urn:schemas-microsoft-com:xml-analysis:rowset"


def _discover_envelope(rows_xml: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soap:Body>"
        f'<DiscoverResponse xmlns="{XMLA_NS}"><return>'
        f'<root xmlns="{ROWSET_NS}">{rows_xml}</root>'
        "</return></DiscoverResponse>"
        "</soap:Body></soap:Envelope>"
    ).encode()


def _execute_envelope(rows_xml: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soap:Body>"
        f'<ExecuteResponse xmlns="{XMLA_NS}"><return>'
        f'<root xmlns="{ROWSET_NS}">{rows_xml}</root>'
        "</return></ExecuteResponse>"
        "</soap:Body></soap:Envelope>"
    ).encode()


BW_CATALOGS = _discover_envelope("<row><CATALOG_NAME>0D_NW_C01</CATALOG_NAME></row>")
BW_CUBES = _discover_envelope(
    "<row><CUBE_NAME>0D_NW_C01_Q001</CUBE_NAME>"
    "<CUBE_CAPTION>Net Weight Query</CUBE_CAPTION><CUBE_TYPE>CUBE</CUBE_TYPE></row>"
)
BW_HIERARCHIES = _discover_envelope(
    "<row><HIERARCHY_NAME>Country</HIERARCHY_NAME>"
    "<HIERARCHY_UNIQUE_NAME>[0D_NW_C01__ZCOUNTRY]</HIERARCHY_UNIQUE_NAME>"
    "<HIERARCHY_CAPTION>Country</HIERARCHY_CAPTION>"
    "<DIMENSION_UNIQUE_NAME>[0D_NW_C01__ZCOUNTRY]</DIMENSION_UNIQUE_NAME></row>"
)
BW_MEASURES = _discover_envelope(
    "<row><MEASURE_NAME>Net Weight</MEASURE_NAME>"
    "<MEASURE_UNIQUE_NAME>[Measures].[4GBQ8]</MEASURE_UNIQUE_NAME>"
    "<MEASURE_CAPTION>Net Weight</MEASURE_CAPTION></row>"
)
BW_EXECUTE = _execute_envelope(
    "<row><Country>US</Country><Net_x0020_Weight>900</Net_x0020_Weight></row>"
    "<row><Country>DE</Country><Net_x0020_Weight>120</Net_x0020_Weight></row>"
)


class BwXmlaHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[BW]", self.command, self.path, fmt % args)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        text = body.decode("utf-8", errors="ignore")
        action = (self.headers.get("SOAPAction") or "").strip('"')

        if "Discover" in action or "<Discover" in text:
            if "DBSCHEMA_CATALOGS" in text:
                out = BW_CATALOGS
            elif "MDSCHEMA_CUBES" in text:
                out = BW_CUBES
            elif "MDSCHEMA_HIERARCHIES" in text:
                out = BW_HIERARCHIES
            elif "MDSCHEMA_MEASURES" in text:
                out = BW_MEASURES
            else:
                out = _discover_envelope("")
        else:
            out = BW_EXECUTE

        self.send_response(200)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        # A GET (e.g. ?wsdl probe) just returns 200 so connectivity checks pass.
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.end_headers()
        self.wfile.write(b"<wsdl/>")


def _serve(handler, port, label):
    httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
    print(f"{label} mock listening on :{port}")
    httpd.serve_forever()


if __name__ == "__main__":
    threads = [
        threading.Thread(target=_serve, args=(BusinessObjectsHandler, 6405, "BusinessObjects"), daemon=True),
        threading.Thread(target=_serve, args=(BwXmlaHandler, 8410, "SAP BW XMLA"), daemon=True),
    ]
    for t in threads:
        t.start()
    print("Mock SAP servers running. Ctrl-C to stop.")
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        print("stopping")
