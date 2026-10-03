#!/usr/bin/env python3
"""Synthetic ONTAP-shaped API for repeatable local verification, NOT ONTAP.

Binds loopback by default. Username/password come from environment. No mutation
routes, appliance calls, or production data. Supports representative RCA routes.
"""

import argparse
import base64
import json
import os
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit


def dataset(path):
    now = datetime.now(timezone.utc)
    if path == "/api/cluster":
        return {
            "uuid": "cluster-sim",
            "name": "SYNTHETIC ONTAP LAB",
            "version": {
                "generation": 9,
                "major": 14,
                "minor": 1,
                "full": "SIMULATED 9.14.1P9",
            },
        }
    if path == "/api/storage/volumes":
        return [
            {
                "uuid": f"volume-{i}",
                "name": f"production-{i}",
                "state": "online",
                "is_constituent": False,
                "svm": {"uuid": "svm-1", "name": "production"},
                "space": {
                    "size": 1000000000,
                    "used": i * 100000000,
                    "available": 1000000000 - i * 100000000,
                },
                "aggregates": [{"uuid": "aggr-1", "name": "ssd-tier"}],
                "metric": {"latency": {"total": 200 * i}, "iops": {"total": 500 * i}},
            }
            for i in range(1, 8)
        ]
    if path == "/api/svm/svms":
        return [{"uuid": "svm-1", "name": "production", "state": "running"}]
    if path == "/api/cluster/nodes":
        return [
            {
                "uuid": "node-1",
                "name": "controller-a",
                "model": "SIMULATED",
                "state": "up",
            }
        ]
    if path == "/api/storage/aggregates":
        return [
            {
                "uuid": "aggr-1",
                "name": "ssd-tier",
                "state": "online",
                "node": {"uuid": "node-1"},
                "space": {"block_storage": {"size": 10000000000, "used": 4000000000}},
            }
        ]
    if path == "/api/storage/luns":
        return [
            {
                "uuid": "lun-1",
                "name": "/vol/production-1/database",
                "serial_number": "SIM000000001",
                "location": {"volume": {"uuid": "volume-1"}},
                "space": {"size": 500000000},
            }
        ]
    if path == "/api/protocols/san/igroups":
        return [
            {"uuid": "igroup-1", "name": "database-hosts", "svm": {"uuid": "svm-1"}}
        ]
    if path == "/api/protocols/san/lun-maps":
        return [
            {
                "lun": {"uuid": "lun-1"},
                "igroup": {"uuid": "igroup-1"},
                "logical_unit_number": 0,
            }
        ]
    if path == "/api/network/fc/ports":
        return [
            {
                "uuid": "fc-port-1",
                "name": "0a",
                "node": {"uuid": "node-1"},
                "state": "online",
            }
        ]
    if path == "/api/network/fc/interfaces":
        return [
            {
                "uuid": "fc-lif-1",
                "name": "fcp-a",
                "svm": {"uuid": "svm-1"},
                "wwpn": "20:00:00:00:00:00:00:01",
            }
        ]
    if path == "/api/network/fc/logins":
        return [
            {
                "initiator": {"wwpn": "10:00:00:00:00:00:00:01"},
                "interface": {"uuid": "fc-lif-1"},
            }
        ]
    if path == "/api/support/ems/events":
        return [
            {
                "index": i,
                "time": (now - timedelta(minutes=i)).isoformat(),
                "node": {"uuid": "node-1", "name": "controller-a"},
                "message": {
                    "name": "synthetic.capacity.warning",
                    "severity": "warning",
                },
                "log_message": "Synthetic capacity warning for RCA test",
            }
            for i in range(1, 4)
        ]
    if path == "/api/snapmirror/relationships":
        return [
            {
                "uuid": "mirror-1",
                "healthy": False,
                "state": "snapmirrored",
                "unhealthy_reason": [{"message": "Synthetic transfer failure"}],
            }
        ]
    if path.endswith("/snapshots"):
        return [
            {
                "uuid": "snap-1",
                "name": "hourly-synthetic",
                "create_time": now.isoformat(),
            }
        ]
    if path == "/api/network/fc/fabrics/fabric-sim/switches":
        return [
            {
                "name": f"switch-{i}",
                "fabric": {"name": "fabric-sim"},
                "vendor": "SYNTHETIC",
            }
            for i in range(3)
        ]
    if path == "/api/network/fc/fabrics/fabric-sim/zones":
        return [{"name": "zone-sim", "fabric": {"name": "fabric-sim"}}]
    if path == "/api/network/fc/interfaces/fc-lif-1":
        return {"uuid": "fc-lif-1", "name": "fcp-a", "enabled": True}
    if path == "/api/cluster/sensors/node-1/5":
        return {"node": {"uuid": "node-1"}, "index": 5, "name": "synthetic-temperature"}
    if path == "/api/security/ssh":
        return {"max_instances": 10}
    if path == "/api/security/authentication/cluster/ad-proxy":
        return {"svm": {"uuid": "svm-1", "name": "production"}}
    if path == "/api/protocols/fpolicy/svm-1/connections/node-1/policy-sim/192.0.2.1":
        return {
            "state": "connected",
            "node": {"uuid": "node-1"},
            "svm": {"uuid": "svm-1"},
        }
    if path == "/api/protocols/nfs/services/svm-1/metrics":
        return [
            {
                "v4": {
                    "timestamp": (now - timedelta(minutes=i)).isoformat(),
                    "duration": "PT15S",
                    "status": "ok",
                    "iops": {"total": 100 + i},
                }
            }
            for i in range(1, 4)
        ]
    if path.endswith("/metrics"):
        return [
            {
                "timestamp": (now - timedelta(minutes=i * 5)).isoformat(),
                "duration": "PT5M",
                "status": "ok",
                "iops": {"total": 1200 + i * 50},
                "throughput": {"total": 8000000},
                "latency": {"total": 500 + i * 100},
            }
            for i in range(1, 7)
        ]
    if path == "/api/protocols/nfs/export-policies":
        return [{"id": 17, "name": "production-exports", "svm": {"uuid": "svm-1"}}]
    if path == "/api/protocols/nfs/export-policies/17/rules":
        return [
            {
                "index": 1,
                "clients": [{"match": "192.0.2.0/24"}],
                "ro_rule": ["sys"],
                "rw_rule": ["sys"],
            }
        ]
    if path == "/api/cluster/counter/tables":
        return [
            {"name": "synthetic_volume", "description": "Synthetic counter definitions"}
        ]
    if path == "/api/cluster/counter/tables/synthetic_volume":
        return {
            "name": "synthetic_volume",
            "counters": [{"name": "read_ops", "type": "rate", "unit": "per_sec"}],
        }
    if path == "/api/cluster/counter/tables/synthetic_volume/rows":
        return [{"id": "volume-1", "counters": [{"name": "read_ops", "value": 123456}]}]
    if path in {"/api/storage/disks", "/api/storage/shelves"}:
        return []
    return None


def extract(row, key):
    for part in key.split("."):
        if not isinstance(row, dict):
            return None
        row = row.get(part)
    return row


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        expected = (
            "Basic "
            + base64.b64encode(
                f"{os.environ.get('NETAPP_USERNAME', '')}:{os.environ.get('NETAPP_PASSWORD', '')}".encode()
            ).decode()
        )
        if (
            not os.environ.get("NETAPP_PASSWORD")
            or self.headers.get("Authorization") != expected
        ):
            return self.reply(
                401, {"error": {"code": "401", "message": "Authentication required"}}
            )
        u = urlsplit(self.path)
        params = parse_qs(u.query)
        if (
            u.path
            in {
                "/api/security/ssh",
                "/api/security/authentication/cluster/ad-proxy",
                "/api/protocols/fpolicy/svm-1/connections/node-1/policy-sim/192.0.2.1",
            }
            and params
        ):
            return self.reply(400, {"error": {"code": "unsupported_control"}})
        if (
            u.path == "/api/protocols/nfs/services/svm-1/metrics"
            and not {"timestamp", "interval"} <= params.keys()
        ):
            return self.reply(400, {"error": {"code": "missing_time_bounds"}})
        data = dataset(u.path)
        if data is None:
            return self.reply(
                404,
                {
                    "error": {
                        "code": "404",
                        "message": "Not represented by the simulator",
                    }
                },
            )
        if not isinstance(data, list):
            return self.reply(200, data)
        for key in ["state", "svm.name", "name", "is_constituent"]:
            if key in params:
                val = params[key][0].strip('"')
                data = [r for r in data if str(extract(r, key)).lower() == val.lower()]
        start = int(params.get("start", ["0"])[0])
        size = min(2, int(params.get("max_records", ["2"])[0]))
        rows = data[start : start + size]
        body = {"records": rows, "num_records": len(rows)}
        if start + size < len(data):
            nxt = {k: v[0] for k, v in params.items()}
            nxt["start"] = start + size
            body["_links"] = {"next": {"href": u.path + "?" + urlencode(nxt)}}
        self.reply(200, body)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=18091)
    a = p.parse_args()
    if not os.environ.get("NETAPP_USERNAME") or not os.environ.get("NETAPP_PASSWORD"):
        p.error("Set NETAPP_USERNAME and NETAPP_PASSWORD locally.")
    print(f"Synthetic API listening on 127.0.0.1:{a.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()
