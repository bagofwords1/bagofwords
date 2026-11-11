#!/usr/bin/env python3
"""Synthetic F5 BIG-IP iControl REST (TMOS 17.1.1) for repeatable local verification. NOT a BIG-IP.

Binds loopback by default. Username/password come from F5_USERNAME / F5_PASSWORD. The
handshake, headers, paging parameters, envelopes and error bodies follow the iControl
REST User Guide (v16.1+) and real f5-ansible fixtures; see SOURCES.md. Every
mutating verb on /mgmt/tm is refused with 403, so this server can only answer GETs,
token login (POST), token timeout extension (PATCH) and token deletion (DELETE).
"""

import argparse
import base64
import hashlib
import json
import os
import secrets
import string
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

VER = "17.1.1"
LOCAL = "https://localhost"
START = datetime.now(UTC).replace(microsecond=0)
STATE = {"token_timeout": 1200, "tokens": {}, "lock": threading.Lock(), "started": time.time()}


# --------------------------------------------------------------------------- helpers
def h(*parts):
    """Deterministic small integer from a name, so stats are stable between runs."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:4], "big")


def obj_path(full_path):
    return full_path.replace("/", "~")


def self_link(uri, obj=None):
    return f"{LOCAL}{uri}" + (f"/{obj_path(obj)}" if obj else "") + f"?ver={VER}"


def tmos_date(dt):
    return dt.strftime("%b %d %Y %H:%M:%S GMT")


def stat_value(v):
    return {"value": v} if isinstance(v, int) and not isinstance(v, bool) else {"description": str(v)}


# --------------------------------------------------------------------------- estate
NODES = [
    # fullPath, address, state, session, monitor
    ("/Common/web01", "10.1.10.11", "up", "monitor-enabled", "/Common/icmp"),
    ("/Common/web02", "10.1.10.12", "up", "monitor-enabled", "/Common/icmp"),
    ("/Common/web03", "10.1.10.13", "down", "monitor-enabled", "/Common/icmp"),
    ("/Common/api01", "10.1.20.21", "up", "monitor-enabled", "/Common/icmp"),
    ("/Common/api02", "10.1.20.22", "up", "monitor-enabled", "/Common/icmp"),
    ("/Common/db01", "10.1.30.31", "up", "monitor-enabled", "/Common/icmp"),
    ("/Common/db02", "10.1.30.32", "user-down", "user-disabled", "/Common/icmp"),
    ("/App1/app1-web01", "10.2.10.11", "up", "monitor-enabled", "/Common/icmp"),
    ("/App1/app1-web02", "10.2.10.12", "up", "user-disabled", "/Common/icmp"),
    ("/App1/app1-cache01", "10.2.40.41", "unchecked", "user-enabled", "default"),
]

# pool fullPath -> (lb mode, monitor, description, members[(node fullPath, port, state, session)])
POOLS = {
    "/Common/web_pool": ("round-robin", "/Common/http_web_check", "Web tier HTTP", [
        ("/Common/web01", 80, "up", "monitor-enabled"),
        ("/Common/web02", 80, "up", "monitor-enabled"),
        ("/Common/web03", 80, "down", "monitor-enabled"),
    ]),
    "/Common/ssl_pool": ("least-connections-member", "/Common/https", "Web tier TLS", [
        ("/Common/web01", 443, "up", "monitor-enabled"),
        ("/Common/web02", 443, "up", "monitor-enabled"),
        ("/Common/web03", 443, "down", "monitor-enabled"),
    ]),
    "/Common/api_pool": ("round-robin", "/Common/http", "API tier", [
        ("/Common/api01", 8080, "up", "monitor-enabled"),
        ("/Common/api02", 8080, "up", "monitor-enabled"),
        ("/Common/api01", 8443, "up", "monitor-enabled"),
    ]),
    "/Common/db_pool": ("round-robin", "/Common/tcp", "PostgreSQL", [
        ("/Common/db01", 5432, "up", "monitor-enabled"),
        ("/Common/db02", 5432, "user-down", "user-disabled"),
    ]),
    "/App1/app1_web_pool": ("ratio-member", "/App1/app1_http", "App1 web tier", [
        ("/App1/app1-web01", 8080, "up", "monitor-enabled"),
        ("/App1/app1-web02", 8080, "up", "user-disabled"),
    ]),
    "/App1/app1_cache_pool": ("round-robin", "none", "App1 cache (no monitor)", [
        ("/App1/app1-cache01", 6379, "unchecked", "user-enabled"),
        ("/App1/app1-web01", 6379, "unchecked", "user-enabled"),
    ]),
}

# fullPath -> (destination, pool, enabled, ipProtocol, profiles, rules, description)
VIRTUALS = {
    "/Common/vs_web_http": ("/Common/10.1.1.10:80", "/Common/web_pool", True, "tcp",
                            ["/Common/tcp", "/Common/http-xff"], ["/Common/redirect_to_https"], "Public web HTTP"),
    "/Common/vs_web_https": ("/Common/10.1.1.10:443", "/Common/ssl_pool", True, "tcp",
                             ["/Common/tcp", "/Common/http-xff", "/Common/wildcard_clientssl"], ["/Common/maintenance_page"], "Public web HTTPS"),
    "/Common/vs_api": ("/Common/10.1.1.20:8080", "/Common/api_pool", True, "tcp",
                       ["/Common/tcp", "/Common/http"], [], "Internal API"),
    "/Common/vs_db": ("/Common/10.1.1.30:5432", "/Common/db_pool", True, "tcp", ["/Common/fastL4"], [], "PostgreSQL front"),
    "/App1/vs_app1_web": ("/App1/10.2.1.10:80", "/App1/app1_web_pool", True, "tcp",
                          ["/Common/tcp", "/App1/app1_http"], ["/App1/app1_header_insert"], "App1 web"),
    "/App1/vs_app1_cache": ("/App1/10.2.1.40:6379", "/App1/app1_cache_pool", False, "tcp", ["/Common/fastL4"], [], "App1 cache (disabled)"),
}

RULES = {
    "/Common/redirect_to_https": 'when HTTP_REQUEST {\n    HTTP::respond 301 Location "https://[HTTP::host][HTTP::uri]"\n}',
    "/Common/maintenance_page": 'when LB_FAILED {\n    HTTP::respond 503 content "Maintenance" "Retry-After" "120"\n}',
    "/App1/app1_header_insert": 'when HTTP_REQUEST {\n    HTTP::header insert X-App1-Edge [IP::client_addr]\n}',
}

CERTS = [
    # fullPath, subject CN, issuer, expiry (relative to START), key size, system-ish
    ("/Common/default.crt", "localhost.localdomain", "localhost.localdomain", START + timedelta(days=3650), 2048),
    ("/Common/wildcard.example.com.crt", "*.example.com", "Example Issuing CA", START + timedelta(days=10), 2048),
    ("/Common/legacy-api.crt", "legacy-api.example.com", "Example Issuing CA", START - timedelta(days=30), 2048),
    ("/App1/app1.example.com.crt", "app1.example.com", "Example Issuing CA", START + timedelta(days=365), 4096),
]

CLIENT_SSL = {
    "/Common/clientssl": ("/Common/default.crt", "/Common/default.key", "none", "none"),
    "/Common/wildcard_clientssl": ("/Common/wildcard.example.com.crt", "/Common/wildcard.example.com.key", "/Common/clientssl", "/Common/ca-bundle.crt"),
    "/Common/legacy_clientssl": ("/Common/legacy-api.crt", "/Common/legacy-api.key", "/Common/clientssl", "none"),
}

HTTP_PROFILES = {
    "/Common/http": ("none", "disabled"),
    "/Common/http-xff": ("/Common/http", "enabled"),
    "/App1/app1_http": ("/Common/http", "enabled"),
}

MONITORS_HTTP = {
    "/Common/http": ("none", "GET /\\r\\n", "", 5, 16, "*:*"),
    "/Common/http_web_check": ("/Common/http", "GET /health HTTP/1.1\\r\\nHost: web.example.com\\r\\nConnection: Close\\r\\n\\r\\n", "200 OK", 5, 16, "*:*"),
    "/App1/app1_http": ("/Common/http", "GET /healthz HTTP/1.0\\r\\n\\r\\n", "ok", 10, 31, "*:8080"),
}

DEVICES = [
    ("/Common/bigip-a.example.com", "bigip-a.example.com", "192.0.2.11", "active", "true", "10.1.250.11"),
    ("/Common/bigip-b.example.com", "bigip-b.example.com", "192.0.2.12", "standby", "false", "10.1.250.12"),
]

VLANS = {"/Common/external": (1010, ["1.1"]), "/Common/internal": (1020, ["1.2"]), "/Common/ha": (4093, ["1.3"])}
SELFS = [
    ("/Common/10.1.1.2/24", "10.1.1.2/24", "/Common/external", "/Common/traffic-group-local-only", "none"),
    ("/Common/10.1.10.2/24", "10.1.10.2/24", "/Common/internal", "/Common/traffic-group-local-only", "default"),
    ("/Common/10.1.1.1/24", "10.1.1.1/24", "/Common/external", "/Common/traffic-group-1", "none"),
    ("/Common/192.168.255.1/30", "192.168.255.1/30", "/Common/ha", "/Common/traffic-group-local-only", "default"),
]
INTERFACES = [("1.1", True, "10000SR-FD", "up"), ("1.2", True, "10000SR-FD", "up"), ("1.3", True, "1000T-FD", "up"),
              ("1.4", False, "none", "uninitialized"), ("mgmt", True, "1000T-FD", "up")]
PARTITIONS = ["Common", "App1"]


def split(full_path):
    partition, name = full_path.rsplit("/", 1)
    return partition.lstrip("/"), name


def base_item(kind, full_path, uri):
    partition, name = split(full_path)
    return {"kind": kind, "name": name, "partition": partition, "fullPath": full_path,
            "generation": 100 + h(full_path) % 900, "selfLink": self_link(uri, full_path)}


def node_items():
    items = []
    for full_path, address, state, session, monitor in NODES:
        item = base_item("tm:ltm:node:nodestate", full_path, "/mgmt/tm/ltm/node")
        item.update({"address": address, "connectionLimit": 0, "dynamicRatio": 1, "ephemeral": "false",
                     "fqdn": {"addressFamily": "ipv4", "autopopulate": "disabled", "downInterval": 5, "interval": "3600"},
                     "logging": "disabled", "monitor": monitor, "rateLimit": "disabled", "ratio": 1,
                     "session": session, "state": state})
        items.append(item)
    return items


def member_items(pool):
    items = []
    for node, port, state, session in POOLS[pool][3]:
        partition, node_name = split(node)
        full_path = f"{node}:{port}"
        address = next(n[1] for n in NODES if n[0] == node)
        item = base_item("tm:ltm:pool:members:membersstate", full_path, f"/mgmt/tm/ltm/pool/{obj_path(pool)}/members")
        item.update({"address": address, "connectionLimit": 0, "dynamicRatio": 1, "ephemeral": "false",
                     "fqdn": {"autopopulate": "disabled"}, "inheritProfile": "enabled", "logging": "disabled",
                     "monitor": "default", "priorityGroup": 0, "rateLimit": "disabled", "ratio": 1,
                     "session": session, "state": state})
        items.append(item)
    return items


def pool_items(expand):
    items = []
    for full_path, (lb, monitor, description, _) in POOLS.items():
        item = base_item("tm:ltm:pool:poolstate", full_path, "/mgmt/tm/ltm/pool")
        item.update({"allowNat": "yes", "allowSnat": "yes", "description": description, "ignorePersistedWeight": "disabled",
                     "ipTosToClient": "pass-through", "ipTosToServer": "pass-through", "linkQosToClient": "pass-through",
                     "linkQosToServer": "pass-through", "loadBalancingMode": lb, "minActiveMembers": 0, "minUpMembers": 0,
                     "minUpMembersAction": "failover", "minUpMembersChecking": "disabled", "monitor": monitor,
                     "queueDepthLimit": 0, "queueOnConnectionLimit": "disabled", "queueTimeLimit": 0, "reselectTries": 0,
                     "serviceDownAction": "none", "slowRampTime": 10})
        ref = {"link": f"{LOCAL}/mgmt/tm/ltm/pool/{obj_path(full_path)}/members?ver={VER}", "isSubcollection": True}
        if expand:
            ref["items"] = member_items(full_path)
        item["membersReference"] = ref
        items.append(item)
    return items


def virtual_items(expand):
    items = []
    for full_path, (dest, pool, enabled, proto, profiles, rules, description) in VIRTUALS.items():
        item = base_item("tm:ltm:virtual:virtualstate", full_path, "/mgmt/tm/ltm/virtual")
        item.update({"addressStatus": "yes", "autoLasthop": "default", "cmpEnabled": "yes", "connectionLimit": 0,
                     "description": description, "destination": dest, "ipProtocol": proto, "mask": "255.255.255.255",
                     "mirror": "disabled", "mobileAppTunnel": "disabled", "nat64": "disabled", "pool": pool,
                     "poolReference": {"link": self_link("/mgmt/tm/ltm/pool", pool)}, "rateLimit": "disabled",
                     "rateLimitDstMask": 0, "rateLimitMode": "object", "rateLimitSrcMask": 0,
                     "serviceDownImmediateAction": "none", "source": "0.0.0.0/0",
                     "sourceAddressTranslation": {"type": "automap"}, "sourcePort": "preserve",
                     "synCookieStatus": "not-activated", "translateAddress": "enabled", "translatePort": "enabled",
                     "vlansDisabled": True, "vsIndex": 2 + list(VIRTUALS).index(full_path)})
        item["enabled" if enabled else "disabled"] = True
        if rules:
            item["rules"] = rules
            item["rulesReference"] = [{"link": self_link("/mgmt/tm/ltm/rule", r)} for r in rules]
        base = f"/mgmt/tm/ltm/virtual/{obj_path(full_path)}"
        item["policiesReference"] = {"link": f"{LOCAL}{base}/policies?ver={VER}", "isSubcollection": True}
        pref = {"link": f"{LOCAL}{base}/profiles?ver={VER}", "isSubcollection": True}
        if expand:
            pref["items"] = []
            for p in profiles:
                pi = base_item("tm:ltm:virtual:profiles:profilesstate", p, base + "/profiles")
                kind = "client-ssl" if "clientssl" in p else "http" if "http" in p else "fastl4" if "fastL4" in p else "tcp"
                pi.update({"context": "clientside" if kind == "client-ssl" else "all",
                           "nameReference": {"link": self_link(f"/mgmt/tm/ltm/profile/{kind}", p)}})
                pref["items"].append(pi)
        item["profilesReference"] = pref
        items.append(item)
    return items


def rule_items():
    items = []
    for full_path, body in RULES.items():
        item = base_item("tm:ltm:rule:rulestate", full_path, "/mgmt/tm/ltm/rule")
        item.update({"apiAnonymous": body})
        items.append(item)
    return items


def cert_items():
    items = []
    for full_path, cn, issuer, expiry, bits in CERTS:
        item = base_item("tm:sys:file:ssl-cert:ssl-certstate", full_path, "/mgmt/tm/sys/file/ssl-cert")
        partition, name = split(full_path)
        item.update({"certificateKeyCurveName": "none", "certificateKeySize": bits,
                     "checksum": f"SHA1:1395:{hashlib.sha1(full_path.encode()).hexdigest()}",
                     "createTime": (START - timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%SZ"), "createdBy": "admin",
                     "expirationDate": int(expiry.timestamp()), "expirationString": tmos_date(expiry),
                     "fingerprint": "SHA256/" + ":".join(f"{b:02X}" for b in hashlib.sha256(full_path.encode()).digest()),
                     "isBundle": "false", "issuer": f"CN={issuer},O=Example,C=US", "keyType": "rsa-public",
                     "lastUpdateTime": (START - timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": 33188,
                     "revision": 1, "serialNumber": str(h(full_path, "serial")), "size": 1200 + h(full_path) % 300,
                     "sourcePath": f"file:///config/ssl/ssl.crt/{name}",
                     "subject": f"CN={cn},O=Example,C=US", "subjectAlternativeName": f"DNS:{cn}", "systemPath": f"/config/ssl/ssl.crt/{name}",
                     "updatedBy": "admin", "version": 3,
                     "bundleCertificatesReference": {"link": f"{LOCAL}/mgmt/tm/sys/file/ssl-cert/{obj_path(full_path)}/bundle-certificates?ver={VER}", "isSubcollection": True},
                     "certValidatorsReference": {"link": f"{LOCAL}/mgmt/tm/sys/file/ssl-cert/{obj_path(full_path)}/cert-validators?ver={VER}", "isSubcollection": True}})
        items.append(item)
    return items


def client_ssl_items():
    items = []
    for full_path, (cert, key, parent, chain) in CLIENT_SSL.items():
        item = base_item("tm:ltm:profile:client-ssl:client-sslstate", full_path, "/mgmt/tm/ltm/profile/client-ssl")
        item.update({"alertTimeout": "indefinite", "allowDynamicRecordSizing": "disabled", "allowExpiredCrl": "disabled",
                     "allowNonSsl": "disabled", "appService": "none", "authenticate": "once", "authenticateDepth": 9,
                     "cacheSize": 262144, "cacheTimeout": 3600, "cert": cert,
                     "certReference": {"link": self_link("/mgmt/tm/sys/file/ssl-cert", cert)}, "chain": chain,
                     "cipherGroup": "none", "ciphers": "DEFAULT", "defaultsFrom": parent, "handshakeTimeout": "10",
                     "inheritCertkeychain": "true", "key": key, "keyReference": {"link": self_link("/mgmt/tm/sys/file/ssl-key", key)},
                     "mode": "enabled", "ocspStapling": "disabled", "peerCertMode": "ignore", "renegotiation": "enabled",
                     "secureRenegotiation": "require", "serverName": "none", "sniDefault": "false", "sniRequire": "false",
                     "sslForwardProxy": "disabled", "strictResume": "disabled", "uncleanShutdown": "enabled",
                     "certKeyChain": [{"name": "default", "cert": cert, "chain": chain, "key": key}]})
        items.append(item)
    return items


def http_profile_items():
    items = []
    for full_path, (parent, xff) in HTTP_PROFILES.items():
        item = base_item("tm:ltm:profile:http:httpstate", full_path, "/mgmt/tm/ltm/profile/http")
        item.update({"acceptXff": "disabled", "appService": "none", "basicAuthRealm": "none", "defaultsFrom": parent,
                     "description": "none", "encryptCookies": [], "enforcement": {"maxHeaderCount": 64, "maxHeaderSize": 32768,
                     "pipeline": "allow", "unknownMethod": "allow"}, "fallbackHost": "none", "fallbackStatusCodes": [],
                     "headerErase": "none", "headerInsert": "none", "insertXforwardedFor": xff, "lwsSeparator": "none",
                     "lwsWidth": 80, "oneconnectTransformations": "enabled", "proxyType": "reverse", "redirectRewrite": "none",
                     "requestChunking": "sustain", "responseChunking": "sustain", "responseHeadersPermitted": [],
                     "serverAgentName": "BigIP", "viaRequest": "preserve", "viaResponse": "preserve", "xffAlternativeNames": []})
        items.append(item)
    return items


def monitor_http_items():
    items = []
    for full_path, (parent, send, recv, interval, timeout, dest) in MONITORS_HTTP.items():
        item = base_item("tm:ltm:monitor:http:httpstate", full_path, "/mgmt/tm/ltm/monitor/http")
        item.update({"adaptive": "disabled", "adaptiveDivergenceType": "relative", "adaptiveDivergenceValue": 25,
                     "adaptiveLimit": 200, "adaptiveSamplingTimespan": 300, "appService": "none", "defaultsFrom": parent,
                     "description": "none", "destination": dest, "interval": interval, "ipDscp": 0, "manualResume": "disabled",
                     "recv": recv, "recvDisable": "none", "reverse": "disabled", "send": send, "timeUntilUp": 0,
                     "timeout": timeout, "transparent": "disabled", "upInterval": 0})
        if full_path == "/App1/app1_http":
            # Real monitors echo the encrypted password; the connector must strip it.
            item.update({"username": "healthcheck", "password": "$M$7T$DpzEUVHt5rKkAfqrBh1PTA=="})
        items.append(item)
    return items


def device_items():
    items = []
    for full_path, hostname, mgmt_ip, failover, self_device, cs_ip in DEVICES:
        item = base_item("tm:cm:device:devicestate", full_path, "/mgmt/tm/cm/device")
        item.update({"activeModules": ["BIG-IP, VE-10G, HIYRKDP-ABCDEFG|...|LTM, VE|..."], "baseMac": "00:50:56:a1:" + f"{h(hostname) % 256:02x}:01",
                     "build": "0.0.2", "cert": "/Common/dtdi.crt", "chassisId": f"4d3e2b1a-{h(hostname) % 10**4:04d}-sim",
                     "chassisType": "individual", "configsyncIp": cs_ip, "edition": "Point Release 1",
                     "failoverState": failover, "haCapacity": 0, "hostname": hostname, "key": "/Common/dtdi.key",
                     "managementIp": mgmt_ip, "marketingName": "BIG-IP Virtual Edition", "mirrorIp": cs_ip,
                     "mirrorSecondaryIp": "any6", "multicastIp": "any6", "multicastPort": 0,
                     "optionalModules": ["APM, Base, VE GBB (500 CCU, 2500 Access Sessions)", "ASM, VE"],
                     "platformId": "Z100", "product": "BIG-IP", "selfDevice": self_device, "timeLimitedModules": [],
                     "timeZone": "UTC", "unicastAddress": [{"effectiveIp": cs_ip, "effectivePort": 1026, "ip": cs_ip, "port": 1026},
                                                           {"effectiveIp": "management-ip", "effectivePort": 1026, "ip": "management-ip", "port": 1026}],
                     "version": VER})
        items.append(item)
    return items


def vlan_items():
    items = []
    for full_path, (tag, _interfaces) in VLANS.items():
        item = base_item("tm:net:vlan:vlanstate", full_path, "/mgmt/tm/net/vlan")
        item.update({"autoLasthop": "default", "cmpHash": "default", "dagRoundRobin": "disabled", "dagTunnel": "outer",
                     "failsafe": "disabled", "failsafeAction": "failover-restart-tm", "failsafeTimeout": 90,
                     "hardwareSyncookie": "disabled", "ifIndex": 100 + tag % 100, "learning": "enable-forward", "mtu": 1500,
                     "sflow": {"pollInterval": 0, "pollIntervalGlobal": "yes", "samplingRate": 0, "samplingRateGlobal": "yes"},
                     "sourceChecking": "disabled", "synFloodRateLimit": 1000, "syncacheThreshold": 30000, "tag": tag,
                     "interfacesReference": {"link": f"{LOCAL}/mgmt/tm/net/vlan/{obj_path(full_path)}/interfaces?ver={VER}", "isSubcollection": True}})
        items.append(item)
    return items


def self_items():
    items = []
    for full_path, address, vlan, tg, allow in SELFS:
        item = base_item("tm:net:self:selfstate", full_path, "/mgmt/tm/net/self")
        item.update({"address": address, "addressSource": "from-user", "floating": "enabled" if "traffic-group-1" in tg else "disabled",
                     "inheritedTrafficGroup": "false", "trafficGroup": tg, "trafficGroupReference": {"link": self_link("/mgmt/tm/cm/traffic-group", tg)},
                     "unit": 1 if "traffic-group-1" in tg else 0, "vlan": vlan, "vlanReference": {"link": self_link("/mgmt/tm/net/vlan", vlan)},
                     "allowService": [] if allow == "none" else ["default"]})
        items.append(item)
    return items


def interface_items():
    items = []
    for name, enabled, media, _ in INTERFACES:
        item = {"kind": "tm:net:interface:interfacestate", "name": name, "fullPath": name, "generation": 7000 + h(name) % 900,
                "selfLink": f"{LOCAL}/mgmt/tm/net/interface/{name}?ver={VER}", "bundle": "not-supported", "bundleSpeed": "not-supported",
                "enabled": enabled, "flowControl": "tx-rx", "forceGigabitFiber": "disabled", "forwardErrorCorrection": "not-supported",
                "ifIndex": 32 + INTERFACES.index((name, enabled, media, _)), "lldpAdmin": "txonly", "lldpTlvmap": 130943,
                "macAddress": f"00:50:56:a1:{h(name) % 256:02x}:{10 + INTERFACES.index((name, enabled, media, _)):02x}",
                "mediaActive": media, "mediaFixed": "auto", "mediaMax": "auto", "mediaSfp": "auto", "mtu": 9198,
                "portFwdMode": "l3", "preferPort": "sfp", "qinqEthertype": "0x8100", "sflow": {"pollInterval": 0, "pollIntervalGlobal": "yes"},
                "stp": "enabled", "stpAutoEdgePort": "enabled", "stpEdgePort": "true", "stpLinkType": "auto"}
        if not enabled:
            item["disabled"] = True
            del item["enabled"]
        items.append(item)
    return items


def partition_items():
    items = []
    for p in PARTITIONS:
        items.append({"kind": "tm:auth:partition:partitionstate", "name": p, "fullPath": p, "generation": 1 + PARTITIONS.index(p),
                      "selfLink": f"{LOCAL}/mgmt/tm/auth/partition/{p}?ver={VER}", "defaultRouteDomain": 0,
                      "description": "Repository for system objects and shared objects." if p == "Common" else "App1 tenant"})
    return items


# --------------------------------------------------------------------------- stats
def status_block(avail, enabled, reason):
    return {"status.availabilityState": {"description": avail}, "status.enabledState": {"description": enabled},
            "status.statusReason": {"description": reason}}


def serverside(seed, scale=1):
    base = h(seed)
    cur = (base % 50) * scale
    return {"serverside.bitsIn": {"value": (base % 10**7) * 8 * scale}, "serverside.bitsOut": {"value": (base % (3 * 10**7)) * 8 * scale},
            "serverside.curConns": {"value": cur}, "serverside.maxConns": {"value": cur + 40 * scale},
            "serverside.pktsIn": {"value": (base % 10**5) * scale}, "serverside.pktsOut": {"value": (base % (2 * 10**5)) * scale},
            "serverside.totConns": {"value": (base % 10**6) * scale}}


def member_status(state, session):
    if state == "down":
        return ("offline", "enabled", "Pool member has been marked down by a monitor", "down", "enabled")
    if state == "user-down":
        return ("offline", "disabled", "Forced down", "forced-down", "user-disabled")
    if state == "unchecked":
        return ("unknown", "enabled", "Pool member does not have service checking enabled", "unchecked", "enabled")
    if session == "user-disabled":
        return ("available", "disabled", "Pool member is available, user disabled", "up", "user-disabled")
    return ("available", "enabled", "Pool member is available", "up", "enabled")


def member_stats_entries(pool, node, port, state, session):
    avail, enabled, reason, mon, sess = member_status(state, session)
    e = {"addr": {"description": next(n[1] for n in NODES if n[0] == node)}, "connq.ageEdm": {"value": 0}, "connq.ageEma": {"value": 0},
         "connq.ageHead": {"value": 0}, "connq.ageMax": {"value": 0}, "connq.depth": {"value": 0}, "connq.serviced": {"value": 0},
         "curSessions": {"value": 0 if avail != "available" else h(pool, node, port) % 20},
         "monitorRule": {"description": ("/Common/icmp (default node monitor)" if POOLS[pool][1] == "none" else f"{POOLS[pool][1]} (pool monitor)")},
         "monitorStatus": {"description": mon}, "nodeName": {"description": node}, "poolName": {"description": pool},
         "port": {"value": port}, "sessionStatus": {"description": sess}}
    e.update(status_block(avail, enabled, reason))
    e.update(serverside((pool, node, port), 0 if avail != "available" else 1))
    e["totRequests"] = {"value": 0 if avail != "available" else h(pool, node, port, "req") % 10**6}
    return e


def members_stats_response(pool, member=None):
    base = f"/mgmt/tm/ltm/pool/{obj_path(pool)}/members"
    entries = {}
    for node, port, state, session in POOLS[pool][3]:
        full = f"{node}:{port}"
        if member and full != member:
            continue
        key = f"{LOCAL}{base}/{obj_path(full)}/stats"
        entries[key] = {"nestedStats": {"kind": "tm:ltm:pool:members:membersstats", "selfLink": key + f"?ver={VER}",
                                        "entries": member_stats_entries(pool, node, port, state, session)}}
    if member:
        key = f"{LOCAL}{base}/{obj_path(member)}/stats"
        return {"kind": "tm:ltm:pool:members:membersstats", "generation": 100 + h(pool, member) % 900, "selfLink": key + f"?ver={VER}",
                "entries": {f"{LOCAL}{base}/{obj_path(member)}/{obj_path(member)}/stats": entries[key]}}
    return {"kind": "tm:ltm:pool:members:memberscollectionstats", "selfLink": f"{LOCAL}{base}/stats?ver={VER}", "entries": entries}


def pool_stats_entries(pool):
    members = POOLS[pool][3]
    statuses = [member_status(s, ss) for _, _, s, ss in members]
    active = sum(1 for a, en, *_ in statuses if a == "available" and en == "enabled")
    available = sum(1 for a, *_ in statuses if a == "available")
    if active == 0 and any(a == "unknown" for a, *_ in statuses):
        avail, reason = "unknown", "The children pool member(s) either don't have service checking enabled, or service check results are not available yet"
    elif active == 0:
        avail, reason = "offline", "The children pool member(s) are down"
    else:
        avail, reason = "available", "The pool is available"
    e = {"activeMemberCnt": {"value": active}, "availableMemberCnt": {"value": available}, "connq.ageEdm": {"value": 0}, "connq.ageEma": {"value": 0},
         "connq.ageHead": {"value": 0}, "connq.ageMax": {"value": 0}, "connq.depth": {"value": 0}, "connq.serviced": {"value": 0},
         "connqAll.ageEdm": {"value": 0}, "connqAll.ageEma": {"value": 0}, "connqAll.ageHead": {"value": 0}, "connqAll.ageMax": {"value": 0},
         "connqAll.depth": {"value": 0}, "connqAll.serviced": {"value": 0}, "curSessions": {"value": h(pool, "sess") % 50 if active else 0},
         "memberCnt": {"value": len(members)}, "minActiveMembers": {"value": 0},
         "monitorRule": {"description": "none" if POOLS[pool][1] == "none" else f"{POOLS[pool][1]} (pool monitor)"}}
    e.update(serverside(("pool", pool), 1 if active else 0))
    e.update(status_block(avail, "enabled", reason))
    e["tmName"] = {"description": pool}
    e["totRequests"] = {"value": h(pool, "req") % 10**7 if active else 0}
    return e


def virtual_stats_entries(vs):
    dest, pool, enabled, *_ = VIRTUALS[vs]
    pool_avail = pool_stats_entries(pool)["status.availabilityState"]["description"]
    if not enabled:
        avail, en, reason = pool_avail, "disabled", "The virtual server is available" if pool_avail == "available" else "The children pool member(s) are down"
    elif pool_avail == "offline":
        avail, en, reason = "offline", "enabled", "The children pool member(s) are down"
    elif pool_avail == "unknown":
        avail, en, reason = "unknown", "enabled", "The children pool member(s) either don't have service checking enabled, or service check results are not available yet"
    else:
        avail, en, reason = "available", "enabled", "The virtual server is available"
    base = h(vs)
    scale = 1 if avail == "available" and enabled else 0
    cur = (base % 300) * scale
    e = {"clientside.bitsIn": {"value": (base % 10**8) * 8 * scale}, "clientside.bitsOut": {"value": (base % (4 * 10**8)) * 8 * scale},
         "clientside.curConns": {"value": cur}, "clientside.evictedConns": {"value": 0}, "clientside.maxConns": {"value": cur + 120 * scale},
         "clientside.pktsIn": {"value": (base % 10**6) * scale}, "clientside.pktsOut": {"value": (base % (2 * 10**6)) * scale},
         "clientside.slowKilled": {"value": 0}, "clientside.totConns": {"value": (base % 10**7) * scale},
         "cmpEnableMode": {"description": "all-cpus"}, "cmpEnabled": {"description": "enabled"},
         "csMaxConnDur": {"value": 120000 * scale}, "csMeanConnDur": {"value": 1800 * scale}, "csMinConnDur": {"value": 12 * scale},
         "destination": {"description": dest.rsplit("/", 1)[-1]},
         "ephemeral.bitsIn": {"value": 0}, "ephemeral.bitsOut": {"value": 0}, "ephemeral.curConns": {"value": 0}, "ephemeral.evictedConns": {"value": 0},
         "ephemeral.maxConns": {"value": 0}, "ephemeral.pktsIn": {"value": 0}, "ephemeral.pktsOut": {"value": 0}, "ephemeral.slowKilled": {"value": 0},
         "ephemeral.totConns": {"value": 0}, "fiveMinAvgUsageRatio": {"value": (base % 7) * scale}, "fiveSecAvgUsageRatio": {"value": (base % 11) * scale},
         "oneMinAvgUsageRatio": {"value": (base % 9) * scale}}
    e.update(status_block(avail, en, reason))
    e.update({"syncookieStatus": {"description": "not-activated"}, "syncookie.accepts": {"value": 0}, "syncookie.hwAccepts": {"value": 0},
              "syncookie.hwSyncookies": {"value": 0}, "syncookie.hwsyncookieInstance": {"value": 0}, "syncookie.rejects": {"value": 0},
              "syncookie.swsyncookieInstance": {"value": 0}, "syncookie.syncacheCurr": {"value": 0}, "syncookie.syncacheOver": {"value": 0},
              "syncookie.syncookies": {"value": 0}, "tmName": {"description": vs}, "totRequests": {"value": (base % 10**8) * scale}})
    return e


def node_stats_entries(node):
    full_path, address, state, session, monitor = next(n for n in NODES if n[0] == node)
    avail, enabled, reason, mon, sess = member_status(state, session)
    reason = reason.replace("Pool member", "Node")
    e = {"addr": {"description": address}, "curSessions": {"value": h(node, "s") % 40 if avail == "available" else 0},
         "monitorRule": {"description": "/Common/icmp (default node monitor)" if monitor == "default" else f"{monitor} (node monitor)"},
         "monitorStatus": {"description": mon}}
    e.update(serverside(("node", node), 1 if avail == "available" else 0))
    e["sessionStatus"] = {"description": sess}
    e.update(status_block(avail, enabled, reason))
    e["tmName"] = {"description": node}
    e["totRequests"] = {"value": h(node, "r") % 10**6 if avail == "available" else 0}
    return e


def stats_collection(uri, kind_prefix, names, builder, single=None):
    """/stats on a collection keys entries by object stats URL; a single object nests one more segment."""
    entries = {}
    for name in names:
        if single and name != single:
            continue
        key = f"{LOCAL}{uri}/{obj_path(name)}/stats"
        if single:
            key = f"{LOCAL}{uri}/{obj_path(name)}/{obj_path(name)}/stats"
        entries[key] = {"nestedStats": {"kind": f"{kind_prefix}stats", "selfLink": f"{LOCAL}{uri}/{obj_path(name)}/stats?ver={VER}", "entries": builder(name)}}
    if single:
        return {"kind": f"{kind_prefix}stats", "generation": 100 + h(single) % 900, "selfLink": f"{LOCAL}{uri}/{obj_path(single)}/stats?ver={VER}", "entries": entries}
    return {"kind": f"{kind_prefix}collectionstats", "selfLink": f"{LOCAL}{uri}/stats?ver={VER}", "entries": entries}


def nested(uri, groups):
    """Build the sys/cm 'unnamed resource' shape: entries keyed by URL -> nestedStats -> entries."""
    return {"kind": None, "selfLink": f"{LOCAL}{uri}?ver={VER}", "entries": {f"{LOCAL}{uri}/{k}": {"nestedStats": {"entries": v}} for k, v in groups.items()}}


def sys_version():
    body = nested("/mgmt/tm/sys/version", {"0": {"Build": {"description": "0.0.2"}, "Date": {"description": "Thu Oct 26 11:14:44 PDT 2023"},
                                                "Edition": {"description": "Point Release 1"}, "Product": {"description": "BIG-IP"},
                                                "Title": {"description": "Main Package"}, "Version": {"description": VER}}})
    body["kind"] = "tm:sys:version:versionstats"
    return body


def sys_clock():
    body = nested("/mgmt/tm/sys/clock", {"0": {"fullDate": {"description": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}}})
    body["kind"] = "tm:sys:clock:clockstats"
    return body


def sys_hardware():
    uri = "/mgmt/tm/sys/hardware"
    groups = {
        "hardware-version": {"https://localhost/mgmt/tm/sys/hardware/hardware-version/cpus": {"nestedStats": {"entries": {
            "model": {"description": "Intel(R) Xeon(R) Platinum 8259CL CPU @ 2.50GHz"},
            "https://localhost/mgmt/tm/sys/hardware/hardware-version/cpus/versions": {"nestedStats": {"entries": {
                "https://localhost/mgmt/tm/sys/hardware/hardware-version/cpus/versions/0": {"nestedStats": {"entries": {"tmName": {"description": "cache size"}, "version": {"description": "36608 KB"}}}},
                "https://localhost/mgmt/tm/sys/hardware/hardware-version/cpus/versions/1": {"nestedStats": {"entries": {"tmName": {"description": "cores"}, "version": {"description": "4  (physical:4)"}}}},
                "https://localhost/mgmt/tm/sys/hardware/hardware-version/cpus/versions/2": {"nestedStats": {"entries": {"tmName": {"description": "cpu MHz"}, "version": {"description": "2499.998"}}}}}}}}}}},
        "platform": {"https://localhost/mgmt/tm/sys/hardware/platform/0": {"nestedStats": {"entries": {
            "baseMac": {"description": "00:50:56:a1:1a:01"}, "biosRev": {"description": "VMware 6.00"},
            "marketingName": {"description": "BIG-IP Virtual Edition"}, "pvaVersion": {"description": "none"}}}}},
        "system-info": {"https://localhost/mgmt/tm/sys/hardware/system-info/0": {"nestedStats": {"entries": {
            "bigipChassisSerialNum": {"description": "564d1a2b-c3d4-e5f6-sim0001"}, "hostBoardPartRevNum": {"description": " "},
            "hostBoardSerialNum": {"description": " "}, "platform": {"description": "Z100"},
            "switchBoardPartRevNum": {"description": " "}, "switchBoardSerialNum": {"description": " "}}}}},
    }
    body = nested(uri, groups)
    body["kind"] = "tm:sys:hardware:hardwarestats"
    return body


def sys_cpu():
    uri = "/mgmt/tm/sys/cpu"
    cpus = {}
    for i in range(4):
        idle = 70 + (h("cpu", i) % 25)
        if i == 1:
            idle = 8  # one hot core for RCA
        user = (100 - idle) * 2 // 3
        system = 100 - idle - user
        cpus[f"{LOCAL}{uri}/0/cpuInfo/{i}"] = {"nestedStats": {"entries": {
            "cpuId": {"value": i}, "fiveMinAvgIdle": {"value": idle}, "fiveMinAvgIowait": {"value": 0}, "fiveMinAvgIrq": {"value": 0},
            "fiveMinAvgNiced": {"value": 0}, "fiveMinAvgRatio": {"value": 100 - idle}, "fiveMinAvgSoftirq": {"value": 0}, "fiveMinAvgStolen": {"value": 0},
            "fiveMinAvgSystem": {"value": system}, "fiveMinAvgUser": {"value": user},
            "fiveSecAvgIdle": {"value": idle}, "fiveSecAvgIowait": {"value": 0}, "fiveSecAvgIrq": {"value": 0}, "fiveSecAvgNiced": {"value": 0},
            "fiveSecAvgRatio": {"value": 100 - idle}, "fiveSecAvgSoftirq": {"value": 0}, "fiveSecAvgStolen": {"value": 0},
            "fiveSecAvgSystem": {"value": system}, "fiveSecAvgUser": {"value": user},
            "idle": {"value": 10**8 + h("idle", i) % 10**6}, "iowait": {"value": 12345}, "irq": {"value": 0}, "niced": {"value": 0},
            "oneMinAvgIdle": {"value": idle}, "oneMinAvgIowait": {"value": 0}, "oneMinAvgIrq": {"value": 0}, "oneMinAvgNiced": {"value": 0},
            "oneMinAvgRatio": {"value": 100 - idle}, "oneMinAvgSoftirq": {"value": 0}, "oneMinAvgStolen": {"value": 0},
            "oneMinAvgSystem": {"value": system}, "oneMinAvgUser": {"value": user},
            "ratio": {"value": 100 - idle}, "softirq": {"value": 0}, "stolen": {"value": 0}, "system": {"value": 10**6 + system}, "usageRatio": {"value": 100 - idle},
            "user": {"value": 10**6 + user}}}}
    body = {"kind": "tm:sys:cpu:cpustats", "selfLink": f"{LOCAL}{uri}?ver={VER}", "entries": {
        f"{LOCAL}{uri}/0": {"nestedStats": {"entries": {f"{LOCAL}{uri}/0/cpuInfo": {"nestedStats": {"entries": cpus}}, "hostId": {"description": "0"}}}}}}
    return body


def sys_memory():
    uri = "/mgmt/tm/sys/memory"
    total = 16 * 1024**3
    tmm_used = 6 * 1024**3 + 123456789
    other = 5 * 1024**3 + 98765432
    host = {f"{LOCAL}{uri}/memory-host/0": {"nestedStats": {"entries": {
        "hostId": {"description": "0"}, "memoryTotal": {"value": total}, "memoryUsed": {"value": tmm_used + other},
        "otherMemoryTotal": {"value": total - 8 * 1024**3}, "otherMemoryUsed": {"value": other},
        "swapTotal": {"value": 1024**3}, "swapUsed": {"value": 0}, "tmmMemoryTotal": {"value": 8 * 1024**3}, "tmmMemoryUsed": {"value": tmm_used}}}}}
    tmm = {}
    for i in range(2):
        tmm[f"{LOCAL}{uri}/memory-tmm/0.{i}"] = {"nestedStats": {"entries": {"memoryTotal": {"value": 4 * 1024**3}, "memoryUsed": {"value": 3 * 1024**3 + i * 10**7 + 61728394},
                                                                               "tmmId": {"description": f"0.{i}"}}}}
    return {"kind": "tm:sys:memory:memorystats", "selfLink": f"{LOCAL}{uri}?ver={VER}", "entries": {
        f"{LOCAL}{uri}/memory-host": {"nestedStats": {"entries": host}}, f"{LOCAL}{uri}/memory-tmm": {"nestedStats": {"entries": tmm}}}}


def cm_failover_status():
    uri = "/mgmt/tm/cm/failover-status"
    return {"kind": "tm:cm:failover-status:failover-statusstats", "selfLink": f"{LOCAL}{uri}?ver={VER}", "entries": {
        f"{LOCAL}{uri}/0": {"nestedStats": {"entries": {
            "color": {"description": "green"},
            "https://localhost/mgmt/tm/cm/failoverStatus/0/details": {"nestedStats": {"entries": {
                "https://localhost/mgmt/tm/cm/failoverStatus/0/details/0": {"nestedStats": {"entries": {"details": {"description": "active for /Common/traffic-group-1"}}}}}}},
            "status": {"description": "ACTIVE"}, "summary": {"description": "1/1 active"}}}}}}


def cm_sync_status():
    uri = "/mgmt/tm/cm/sync-status"
    return {"kind": "tm:cm:sync-status:sync-statusstats", "selfLink": f"{LOCAL}{uri}?ver={VER}", "entries": {
        f"{LOCAL}{uri}/0": {"nestedStats": {"entries": {
            "color": {"description": "green"},
            "https://localhost/mgmt/tm/cm/syncStatus/0/details": {"nestedStats": {"entries": {
                "https://localhost/mgmt/tm/cm/syncStatus/0/details/0": {"nestedStats": {"entries": {"details": {"description": "device-group-ha-pair (In Sync): All devices in the device group are in sync"}}}}}}},
            "mode": {"description": "high-availability"}, "status": {"description": "In Sync"},
            "summary": {"description": "All devices in the device group are in sync"}}}}}}


COLLECTIONS = {
    # uri: (kind prefix, items builder (expand) , not-found noun)
    "/mgmt/tm/ltm/virtual": ("tm:ltm:virtual:virtual", virtual_items, "Virtual Server"),
    "/mgmt/tm/ltm/pool": ("tm:ltm:pool:pool", pool_items, "Pool"),
    "/mgmt/tm/ltm/node": ("tm:ltm:node:node", lambda expand: node_items(), "Node"),
    "/mgmt/tm/ltm/rule": ("tm:ltm:rule:rule", lambda expand: rule_items(), "Rule"),
    "/mgmt/tm/ltm/profile/http": ("tm:ltm:profile:http:http", lambda expand: http_profile_items(), "HTTP Profile"),
    "/mgmt/tm/ltm/profile/client-ssl": ("tm:ltm:profile:client-ssl:client-ssl", lambda expand: client_ssl_items(), "Client SSL Profile"),
    "/mgmt/tm/ltm/monitor/http": ("tm:ltm:monitor:http:http", lambda expand: monitor_http_items(), "Monitor"),
    "/mgmt/tm/sys/file/ssl-cert": ("tm:sys:file:ssl-cert:ssl-cert", lambda expand: cert_items(), "Certificate"),
    "/mgmt/tm/cm/device": ("tm:cm:device:device", lambda expand: device_items(), "Device"),
    "/mgmt/tm/net/vlan": ("tm:net:vlan:vlan", lambda expand: vlan_items(), "VLAN"),
    "/mgmt/tm/net/self": ("tm:net:self:self", lambda expand: self_items(), "Self IP"),
    "/mgmt/tm/net/interface": ("tm:net:interface:interface", lambda expand: interface_items(), "Interface"),
    "/mgmt/tm/auth/partition": ("tm:auth:partition:partition", lambda expand: partition_items(), "Partition"),
}
STATS = {
    "/mgmt/tm/ltm/virtual": ("tm:ltm:virtual:virtual", list(VIRTUALS), virtual_stats_entries),
    "/mgmt/tm/ltm/pool": ("tm:ltm:pool:pool", list(POOLS), pool_stats_entries),
    "/mgmt/tm/ltm/node": ("tm:ltm:node:node", [n[0] for n in NODES], node_stats_entries),
}
SINGLETONS = {"/mgmt/tm/sys/version": sys_version, "/mgmt/tm/sys/clock": sys_clock, "/mgmt/tm/sys/hardware": sys_hardware,
              "/mgmt/tm/sys/cpu": sys_cpu, "/mgmt/tm/sys/memory": sys_memory, "/mgmt/tm/cm/failover-status": cm_failover_status,
              "/mgmt/tm/cm/sync-status": cm_sync_status}


# --------------------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "BigIP-Simulator/" + VER
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        if os.environ.get("F5_SIM_VERBOSE"):
            super().log_message(*args)

    # -- plumbing
    def reply(self, status, body, headers=None):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=UTF-8")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def error(self, status, message, api_error=None, stack=True):
        body = {"code": status, "message": message}
        if stack:
            body["errorStack"] = []
        if api_error is not None:
            body["apiError"] = api_error
        if status in (401, 404) and not stack:
            body.update({"referer": self.client_address[0], "restOperationId": 10000 + int(time.time() * 1000) % 90000, "kind": ":resterrorresponse"})
        self.reply(status, body)

    def body_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return None

    def credentials_ok(self, username, password):
        return bool(password) and username == os.environ.get("F5_USERNAME", "") and password == os.environ.get("F5_PASSWORD", "")

    def authenticate(self):
        """Token (X-F5-Auth-Token) or HTTP Basic, as on a real BIG-IP. Returns None when rejected."""
        token = self.headers.get("X-F5-Auth-Token")
        if token:
            with STATE["lock"]:
                entry = STATE["tokens"].get(token)
                if not entry:
                    self.error(401, "X-F5-Auth-Token does not exist.", stack=False)
                    return None
                if time.time() > entry["expires"]:
                    del STATE["tokens"][token]
                    self.error(401, "X-F5-Auth-Token has expired.", stack=False)
                    return None
                return entry["user"]
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                user, _, password = base64.b64decode(auth[6:]).decode().partition(":")
            except Exception:
                user = password = ""
            if self.credentials_ok(user, password):
                return user
            self.error(401, "Authorization failed: user " + user + " is not permitted to access this resource.", stack=False)
            return None
        self.error(401, f"Authorization failed: no user authentication header or token detected. Uri:http://localhost:8100{self.path} Referrer:{self.client_address[0]} Sender:{self.client_address[0]}", stack=False)
        return None

    # -- verbs
    def do_POST(self):
        u = urlsplit(self.path)
        if u.path == "/mgmt/shared/authn/login":
            return self.login()
        if u.path.startswith("/mgmt/tm"):
            if self.authenticate() is None:
                return
            return self.error(403, "The simulator is read-only: mutating requests on /mgmt/tm are refused.", api_error=26214401)
        self.error(404, "Public URI path not registered: " + u.path, stack=False)

    def do_PUT(self):
        self.do_POST()

    def do_PATCH(self):
        u = urlsplit(self.path)
        if u.path.startswith("/mgmt/shared/authz/tokens/"):
            return self.patch_token(u.path.rsplit("/", 1)[-1])
        self.do_POST()

    def do_DELETE(self):
        u = urlsplit(self.path)
        if u.path.startswith("/mgmt/shared/authz/tokens/"):
            return self.delete_token(u.path.rsplit("/", 1)[-1])
        self.do_POST()

    def do_HEAD(self):
        self.error(405, "Method not allowed")

    def do_OPTIONS(self):
        self.error(405, "Method not allowed")

    # -- token lifecycle
    def login(self):
        data = self.body_json()
        if not isinstance(data, dict) or not isinstance(data.get("username"), str) or not isinstance(data.get("password"), str):
            return self.error(400, "Bad Request: username and password are required.", stack=False)
        provider = data.get("loginProviderName", "tmos")
        if provider != "tmos":
            return self.error(400, f"Bad Request: Login provider '{provider}' not found.", stack=False)
        if not self.credentials_ok(data["username"], data["password"]):
            return self.error(401, "Authentication failed.", stack=False)
        with STATE["lock"]:
            if sum(1 for t in STATE["tokens"].values() if t["user"] == data["username"]) >= 100:
                return self.error(400, f"Bad Request: user {data['username']} has reached maximum active login tokens", stack=False)
            token = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(26))
            now = time.time()
            timeout = STATE["token_timeout"]
            STATE["tokens"][token] = {"user": data["username"], "expires": now + timeout, "timeout": timeout, "start": now}
        body = {"username": data["username"],
                "loginReference": {"link": "https://localhost/mgmt/cm/system/authn/providers/tmos/1f44a60e-11a7-3c51-a49f-82983026b41b/login"},
                "loginProviderName": "tmos", "token": self.token_body(token), "generation": 0, "lastUpdateMicros": 0}
        self.reply(200, body)

    def token_body(self, token):
        entry = STATE["tokens"][token]
        return {"token": token, "name": token, "userName": entry["user"], "authProviderName": "tmos",
                "user": {"link": f"https://localhost/mgmt/shared/authz/users/{entry['user']}"}, "groupReferences": [],
                "timeout": entry["timeout"], "startTime": datetime.fromtimestamp(entry["start"], UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "+0000",
                "address": self.client_address[0], "partition": "[All]", "generation": 1,
                "lastUpdateMicros": int(entry["start"] * 10**6), "expirationMicros": int(entry["expires"] * 10**6),
                "kind": "shared:authz:tokens:authtokenitemstate", "selfLink": f"https://localhost/mgmt/shared/authz/tokens/{token}"}

    def patch_token(self, token):
        if self.authenticate() is None:
            return
        data = self.body_json()
        with STATE["lock"]:
            entry = STATE["tokens"].get(token)
            if not entry:
                return self.error(404, "Public URI path not registered: /mgmt/shared/authz/tokens/" + token, stack=False)
            timeout = (data or {}).get("timeout")
            if not isinstance(timeout, int) or timeout <= 0:
                return self.error(400, "Bad Request: timeout must be a positive integer", stack=False)
            if timeout > 36000:
                return self.error(406, "Auth-token absolute timeout (36000 seconds) exceeded", stack=False)
            entry["timeout"] = timeout
            entry["expires"] = entry["start"] + timeout
            body = self.token_body(token)
        self.reply(200, body)

    def delete_token(self, token):
        if self.authenticate() is None:
            return
        with STATE["lock"]:
            entry = STATE["tokens"].get(token)
            if not entry:
                return self.error(404, "Public URI path not registered: /mgmt/shared/authz/tokens/" + token, stack=False)
            body = self.token_body(token)
            del STATE["tokens"][token]
        self.reply(200, body)

    # -- GET routing
    def do_GET(self):
        u = urlsplit(self.path)
        path = unquote(u.path).rstrip("/")
        params = {k: v[-1] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
        if path.startswith("/mgmt/shared/authz/tokens/"):
            if self.authenticate() is None:
                return
            token = path.rsplit("/", 1)[-1]
            with STATE["lock"]:
                if token not in STATE["tokens"]:
                    return self.error(404, "Public URI path not registered: " + path, stack=False)
                body = self.token_body(token)
            return self.reply(200, body)
        if not path.startswith("/mgmt/tm/"):
            return self.error(404, "Public URI path not registered: " + path, stack=False)
        if self.authenticate() is None:
            return
        if path in SINGLETONS:
            return self.reply(200, SINGLETONS[path]())
        # collection stats, object, object stats, subcollections
        for uri, (kind, builder, noun) in sorted(COLLECTIONS.items(), key=lambda kv: -len(kv[0])):
            if path == uri:
                return self.collection(uri, kind, builder, params)
            if path == uri + "/stats" and uri in STATS:
                k, names, b = STATS[uri]
                return self.reply(200, stats_collection(uri, k, names, b))
            if path.startswith(uri + "/"):
                return self.object(uri, kind, builder, noun, path[len(uri) + 1:], params)
        self.error(404, "Public URI path not registered: " + path, stack=False)

    def collection(self, uri, kind, builder, params, items=None):
        items = builder("expandSubcollections" in params and params["expandSubcollections"] == "true") if items is None else items
        flt = params.get("$filter")
        if flt is not None:
            parts = flt.replace("+", " ").split()
            if len(parts) != 3 or parts[0] != "partition" or parts[1] != "eq":
                return self.error(400, "Bad Request: $filter supports only 'partition eq <name>'.")
            items = [i for i in items if i.get("partition") == parts[2]]
        if "$select" in params:
            keep = [s.strip() for s in params["$select"].split(",") if s.strip()]
            items = [{k: v for k, v in i.items() if k in keep} for i in items]
        body = {"kind": kind + "collectionstate", "selfLink": f"{LOCAL}{uri}?ver={VER}"}
        total = len(items)
        if "$top" in params:
            try:
                top, skip = int(params["$top"]), int(params.get("$skip", "0"))
                if top < 1 or skip < 0:
                    raise ValueError
            except ValueError:
                return self.error(400, "Bad Request: $top and $skip must be non-negative integers.")
            page = items[skip:skip + top]
            def link(s):
                q = {}
                if flt is not None:
                    q["$filter"] = flt.replace(" ", "+")
                if "$select" in params:
                    q["$select"] = params["$select"]
                q["$top"] = top
                if s:
                    q["$skip"] = s
                q["ver"] = VER
                return f"{LOCAL}{uri}?" + urlencode(q, safe="+$").replace("%24", "$")
            body.update({"currentItemCount": len(page), "items": page, "itemsPerPage": top, "pageIndex": skip // top + 1,
                         "startIndex": skip + 1, "totalItems": total, "totalPages": max(1, -(-total // top)), "selfLink": link(skip)})
            if skip + top < total:
                body["nextLink"] = link(skip + top)
            if skip > 0:
                body["previousLink"] = link(max(0, skip - top))
            return self.reply(200, body)
        if items:
            body["items"] = items
        self.reply(200, body)

    def object(self, uri, kind, builder, noun, rest, params):
        obj, _, tail = rest.partition("/")
        if not obj.startswith("~") and uri not in ("/mgmt/tm/net/interface", "/mgmt/tm/auth/partition"):
            obj = "~Common~" + obj  # unqualified names resolve in Common
        full = obj.replace("~", "/") if obj.startswith("~") else obj
        items = builder(True)
        item = next((i for i in items if i["fullPath"] == full), None)
        if item is None:
            return self.error(404, f"01020036:3: The requested {noun} ({full}) was not found.", api_error=3)
        if not tail:
            if "expandSubcollections" not in params or params["expandSubcollections"] != "true":
                item = builder(False)[items.index(item)]
            if "$select" in params:
                keep = [s.strip() for s in params["$select"].split(",")]
                item = {k: v for k, v in item.items() if k in keep}
            return self.reply(200, item)
        if tail == "stats" and uri in STATS:
            k, names, b = STATS[uri]
            return self.reply(200, stats_collection(uri, k, names, b, single=full))
        if uri == "/mgmt/tm/ltm/pool" and tail.startswith("members"):
            sub = tail[len("members"):].strip("/")
            members = member_items(full)
            if not sub:
                return self.collection(f"{uri}/{obj}/members", "tm:ltm:pool:members:members", None, params, items=members)
            if sub == "stats":
                return self.reply(200, members_stats_response(full))
            member, _, mtail = sub.partition("/")
            mfull = member.replace("~", "/")
            if not any(m["fullPath"] == mfull for m in members):
                return self.error(404, f"01020036:3: The requested Pool Member ({mfull} {full}) was not found.", api_error=3)
            if mtail == "stats":
                return self.reply(200, members_stats_response(full, mfull))
            if not mtail:
                return self.reply(200, next(m for m in members if m["fullPath"] == mfull))
        self.error(404, "Public URI path not registered: " + uri + "/" + rest, stack=False)


def main():
    p = argparse.ArgumentParser(description="Synthetic F5 BIG-IP iControl REST API (TMOS 17.1.1 shapes) for local verification.")
    p.add_argument("--port", type=int, default=18093, help="loopback TCP port (default 18093)")
    p.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1; keep it on loopback)")
    p.add_argument("--token-timeout", type=int, default=1200, help="seconds before an issued X-F5-Auth-Token expires (default 1200, like BIG-IP)")
    a = p.parse_args()
    if not os.environ.get("F5_USERNAME") or not os.environ.get("F5_PASSWORD"):
        p.error("Set F5_USERNAME and F5_PASSWORD locally (throwaway values; never customer credentials).")
    STATE["token_timeout"] = a.token_timeout
    server = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"Synthetic BIG-IP iControl REST listening on http://{a.host}:{server.server_address[1]}/mgmt (token timeout {a.token_timeout}s)", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
