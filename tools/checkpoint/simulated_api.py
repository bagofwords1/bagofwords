#!/usr/bin/env python3
"""Synthetic Check Point Management Web API for repeatable local verification. NOT Check Point.

Models the R81.20 Management API (v1.9.1) wire contract: ``POST /web_api[/v<ver>]/<command>``
with a JSON body, ``login`` -> ``sid`` -> ``X-chkp-sid`` header, ``logout``/``keepalive``,
``show-*`` collections with ``limit``/``offset``/``details-level``/``order``/``filter`` and
the ``from``/``to``/``total`` envelope, ``show-access-rulebase``/``show-nat-rulebase`` with
sections and ``objects-dictionary``, ``show-logs`` with ``new-query``/``query-id`` paging,
and the documented error bodies ``{"code": ..., "message": ...}``.

It refuses every mutating command (publish, set-*, add-*, delete-*, install-policy,
run-script, discard, ...) with HTTP 403 and answers unknown commands with HTTP 404
``generic_err_command_not_found``. Binds loopback only. Credentials come from
CHECKPOINT_USERNAME/CHECKPOINT_PASSWORD and/or CHECKPOINT_API_KEY.
"""

import argparse
import ipaddress
import json
import os
import random
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API_VERSION = "1.9.1"
SUPPORTED_VERSIONS = ["1", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.6.1", "1.7", "1.7.1", "1.8", "1.8.1", "1.9", "1.9.1"]
SESSION_TIMEOUT = 600
NS = uuid.UUID("7d3b4b7e-5a1c-4d8e-9c7a-1f2e3d4c5b6a")
DATA_DOMAIN = {"uid": "a0bbbc99-adef-4ef8-bb6d-defdefdefdef", "name": "Check Point Data", "domain-type": "data domain"}
ANY_UID = "97aeb369-9aea-11d5-bd16-0090272ccb30"
ORIGINAL_UID = "85c0f50f-6d8a-4528-88ab-5fb11d8fe16c"
POLICY_TARGETS_UID = "6c488338-8eec-4103-ad21-cd461ac2c476"
ACTIONS = {"Accept": "6c488338-8eec-4103-ad21-cd461ac2c472", "Drop": "6c488338-8eec-4103-ad21-cd461ac2c473",
           "Reject": "6c488338-8eec-4103-ad21-cd461ac2c474"}
TRACKS = {"None": "29e53e3d-23bf-48fe-b6b1-d59bd88036f9", "Log": "598ead32-aa42-4615-90ed-f51a5928d41d",
          "Detailed Log": "5a2c6b5e-5e0e-4b3f-9a8f-0d8f1a1b2c3d"}
# Commands a Security Management Server accepts but a read-only connector must never issue.
MUTATING_PREFIXES = ("add-", "set-", "delete-", "install-", "uninstall-", "run-", "publish", "discard", "approve-",
                     "reject-", "submit-", "assign-", "take-over-", "switch-", "continue-", "verify-", "export",
                     "import", "reset-", "disconnect", "purge", "where-used", "unlock-", "lock-", "abort-", "restore",
                     "backup", "update-", "process-", "put-file", "migrate", "move-", "clone-", "generate", "install")


def uid_for(kind, name):
    return str(uuid.uuid5(NS, f"{kind}:{name}"))


def api_date(dt):
    return {"posix": int(dt.timestamp() * 1000), "iso-8601": dt.strftime("%Y-%m-%dT%H:%M%z")}


def error(status, code, message, **extra):
    return status, {"code": code, "message": message, **extra}


class Estate:
    """Deterministic but coherent estate: a single-domain SMC with policy, gateways, sessions and logs."""

    def __init__(self, domain_name="SMC User", now=None):
        self.now = now or datetime.now(timezone.utc)
        self.rng = random.Random(20250407)
        self.domain = {"uid": "41e821a0-3720-11e3-aa6e-0800200c9fde", "name": domain_name, "domain-type": "domain"}
        self.objects = {}  # uid -> full object
        self.build()

    # -------------------------------------------------------------- helpers
    def base(self, kind, name, icon, color="black", comments="", creator="admin", days_ago=120, **fields):
        created = self.now - timedelta(days=days_ago)
        modified = created + timedelta(days=self.rng.randint(0, max(1, days_ago - 1)))
        obj = {
            "uid": uid_for(kind, name), "name": name, "type": kind, "domain": self.domain,
            "meta-info": {
                "lock": "unlocked", "validation-state": "ok",
                "last-modify-time": api_date(modified), "last-modifier": creator,
                "creation-time": api_date(created), "creator": creator,
            },
            "tags": [], "read-only": False, "comments": comments, "color": color, "icon": icon, "groups": [],
        }
        obj.update(fields)
        self.objects[obj["uid"]] = obj
        return obj

    def predefined(self, uid, name, kind):
        obj = {"uid": uid, "name": name, "type": kind, "domain": DATA_DOMAIN, "icon": "General/globalsAny",
               "color": "black", "comments": "", "tags": [], "read-only": True, "groups": [],
               "meta-info": {"lock": "unlocked", "validation-state": "ok", "last-modifier": "System", "creator": "System",
                             "last-modify-time": api_date(self.now - timedelta(days=900)),
                             "creation-time": api_date(self.now - timedelta(days=900))}}
        self.objects[uid] = obj
        return obj

    def ref(self, uid):
        o = self.objects[uid]
        return {"uid": o["uid"], "name": o["name"], "type": o["type"], "domain": o["domain"]}

    # -------------------------------------------------------------- estate
    def build(self):
        self.any = self.predefined(ANY_UID, "Any", "CpmiAnyObject")
        self.predefined(ORIGINAL_UID, "Original", "Global")
        self.predefined(POLICY_TARGETS_UID, "Policy Targets", "Global")
        for name, uid in ACTIONS.items():
            self.predefined(uid, name, "RulebaseAction")
        for name, uid in TRACKS.items():
            self.predefined(uid, name, "Track")
        self.internet = self.predefined("5e8a4c1e-9d0f-4a7b-8c3d-2f1e0d9c8b7a", "Internet", "Internet")

        hosts = [
            ("web-01", "10.1.10.11", "Public web frontend"), ("web-02", "10.1.10.12", "Public web frontend"),
            ("app-01", "10.1.20.21", "Application tier"), ("app-02", "10.1.20.22", "Application tier"),
            ("app-03", "10.1.20.23", "Application tier (added last week, NOT in grp-app-servers)"),
            ("db-01", "10.1.30.31", "PostgreSQL primary"), ("db-02", "10.1.30.32", "PostgreSQL replica"),
            ("dns-01", "10.1.5.53", "Internal DNS"), ("ntp-01", "10.1.5.123", "Internal NTP"),
            ("ad-dc-01", "10.1.5.10", "Active Directory DC"), ("jump-01", "10.1.99.5", "Admin jump host"),
            ("siem-01", "10.1.40.41", "SIEM collector"), ("mail-01", "10.1.50.25", "Mail relay"),
            ("legacy-ftp-01", "10.1.60.21", "Legacy FTP server (decommissioned)"), ("vpn-peer-01", "203.0.113.77", "Partner VPN peer"),
        ]
        self.hosts = []
        for i, (name, ip, comment) in enumerate(hosts):
            h = self.base("host", name, "Objects/host", "blue" if "db" in name else "black", comment, days_ago=200 - i * 9,
                          **{"ipv4-address": ip, "ipv6-address": "", "interfaces": [],
                             "nat-settings": {"auto-rule": name.startswith("web"), "method": "static" if name.startswith("web") else None,
                                              "hide-behind": None, "install-on": "All" if name.startswith("web") else None,
                                              "ipv4-address": f"198.51.100.{11 if name == 'web-01' else 12}" if name.startswith("web") else None},
                             "host-servers": {"dns-server": name == "dns-01", "mail-server": name == "mail-01", "web-server": name.startswith("web")}})
            h["nat-settings"] = {k: v for k, v in h["nat-settings"].items() if v is not None}
            self.hosts.append(h)
        self.hosts[4]["meta-info"]["creation-time"] = api_date(self.now - timedelta(days=6))
        self.hosts[4]["meta-info"]["last-modify-time"] = api_date(self.now - timedelta(days=6))
        self.hosts[4]["meta-info"]["creator"] = self.hosts[4]["meta-info"]["last-modifier"] = "jsmith"

        nets = [
            ("net-web", "10.1.10.0", 24, "Web tier"), ("net-app", "10.1.20.0", 24, "Application tier"),
            ("net-db", "10.1.30.0", 24, "Database tier"), ("net-infra", "10.1.5.0", 24, "Infrastructure services"),
            ("net-mgmt", "10.1.99.0", 24, "Management network"), ("net-users", "10.2.0.0", 16, "User LAN"),
            ("net-dmz", "172.16.10.0", 24, "DMZ"), ("net-guest-wifi", "10.3.0.0", 16, "Guest Wi-Fi (untrusted)"),
        ]
        self.networks = []
        for name, subnet, mask, comment in nets:
            net = ipaddress.ip_network(f"{subnet}/{mask}")
            self.networks.append(self.base("network", name, "NetworkObjects/network", "black", comment, days_ago=300,
                                           **{"subnet4": subnet, "mask-length4": mask, "subnet-mask": str(net.netmask),
                                              "subnet6": "", "mask-length6": None, "broadcast": "allow",
                                              "nat-settings": {"auto-rule": False}}))
        self.ranges = [
            self.base("address-range", "rng-dhcp-users", "NetworkObjects/addressRange", comments="DHCP pool",
                      **{"ipv4-address-first": "10.2.100.1", "ipv4-address-last": "10.2.199.254", "ipv6-address-first": "",
                         "ipv6-address-last": "", "nat-settings": {"auto-rule": False}}),
            self.base("address-range", "rng-partner-b2b", "NetworkObjects/addressRange", comments="Partner B2B range",
                      **{"ipv4-address-first": "203.0.113.64", "ipv4-address-last": "203.0.113.95", "ipv6-address-first": "",
                         "ipv6-address-last": "", "nat-settings": {"auto-rule": False}}),
        ]
        by_name = {o["name"]: o for o in self.hosts + self.networks + self.ranges}

        def mk_group(name, members, comment):
            g = self.base("group", name, "General/group", "orange", comment, days_ago=250)
            g["members"] = [self.ref(by_name[m]["uid"]) for m in members]
            for m in members:
                by_name[m]["groups"].append({"uid": g["uid"], "name": g["name"], "type": "group", "domain": self.domain})
            by_name[name] = g
            return g

        self.groups = [
            mk_group("grp-web-servers", ["web-01", "web-02"], "Public web servers"),
            mk_group("grp-app-servers", ["app-01", "app-02"], "App servers allowed to reach the database tier"),
            mk_group("grp-db-servers", ["db-01", "db-02"], "Database servers"),
            mk_group("grp-infra-services", ["dns-01", "ntp-01", "ad-dc-01"], "Core infrastructure"),
            mk_group("grp-internal-nets", ["net-web", "net-app", "net-db", "net-infra", "net-mgmt", "net-users"], "All internal networks"),
        ]
        for g in self.groups:
            for m in g["members"]:
                # The member reference carries the identity fields of the member (dereference-group-members).
                full = self.objects[m["uid"]]
                for k in ("ipv4-address", "subnet4", "mask-length4", "subnet-mask", "ipv4-address-first", "ipv4-address-last"):
                    if k in full:
                        m[k] = full[k]

        svc = [("https", "443", "tcp"), ("http", "80", "tcp"), ("ssh", "22", "tcp"), ("postgresql", "5432", "tcp"),
               ("smtp", "25", "tcp"), ("ftp", "21", "tcp"), ("tcp-8443-app", "8443", "tcp"),
               ("domain-udp", "53", "udp"), ("ntp-udp", "123", "udp"), ("syslog-udp", "514", "udp")]
        self.services = []
        for name, port, proto in svc:
            kind = f"service-{proto}"
            extra = {"port": port, "source-port": "", "protocol": {"https": "HTTPS", "http": "HTTP", "ssh": "SSH", "smtp": "SMTP",
                                                                    "ftp": "FTP", "domain-udp": "DNS_UDP"}.get(name, ""),
                     "session-timeout": 3600 if proto == "tcp" else 40, "use-default-session-timeout": True,
                     "match-for-any": True, "match-by-protocol-signature": False,
                     "keep-connections-open-after-policy-installation": False, "sync-connections-on-cluster": True,
                     "aggressive-aging": {"enable": True, "timeout": 600, "use-default-timeout": True, "default-timeout": 600}}
            if proto == "udp":
                extra["accept-replies"] = True
            self.services.append(self.base(kind, name, f"Services/{proto.upper()}Service", "black", "", days_ago=400, **extra))
            by_name[name] = self.services[-1]
        self.service_groups = []
        for name, members, comment in [("sg-web", ["http", "https"], "Web traffic"), ("sg-infra", ["domain-udp", "ntp-udp"], "Infra services"),
                                       ("sg-admin", ["ssh", "tcp-8443-app"], "Administrative access")]:
            g = self.base("service-group", name, "General/group", "orange", comment, days_ago=350)
            g["members"] = [{**self.ref(by_name[m]["uid"]), "port": by_name[m]["port"]} for m in members]
            by_name[name] = g
            self.service_groups.append(g)
        by_name["Any"], by_name["Internet"], by_name["Original"] = self.any, self.internet, self.objects[ORIGINAL_UID]
        self.by_name = by_name

        # Layers, packages, rulebases
        self.layers = {}
        for name, shared in [("Standard Network", False), ("DMZ Network", False)]:
            self.layers[name] = self.base("access-layer", name, "ApplicationFirewall/rulebase", comments="", days_ago=500,
                                          **{"firewall": True, "applications-and-url-filtering": name == "Standard Network",
                                             "content-awareness": False, "mobile-access": False, "shared": shared,
                                             "parent-layer": "", "implicit-cleanup-action": "drop", "detect-using-x-forward-for": False})
        self.threat_layer = self.base("threat-layer", "Standard Threat Prevention", "ApplicationFirewall/rulebase", days_ago=500)
        self.packages = {}
        for name, layer in [("Standard", "Standard Network"), ("DMZ", "DMZ Network")]:
            self.packages[name] = self.base("package", name, "Blades/Access", comments=f"{name} policy package", days_ago=500,
                                            **{"access": True, "access-layers": [self.ref(self.layers[layer]["uid"])],
                                               "threat-prevention": name == "Standard",
                                               "threat-layers": [self.ref(self.threat_layer["uid"])] if name == "Standard" else [],
                                               "nat-policy": True, "qos": False, "desktop-security": False,
                                               "https-inspection-policy": False, "vpn-traditional-mode": False,
                                               "installation-targets": "all", "installation-targets-revision": []})
        self.build_gateways()
        self.rulebases = {"Standard Network": self.build_standard_rules(), "DMZ Network": self.build_dmz_rules()}
        self.nat_rulebases = {"Standard": self.build_nat_rules(), "DMZ": []}
        self.threat_rulebases = {"Standard Threat Prevention": self.build_threat_rules()}
        self.build_sessions()
        self.build_tasks()
        self.build_vpn()
        self.logs = self.build_logs()
        self.audit_logs = self.build_audit_logs()
        self.changes = self.build_changes()

    def rule(self, number, name, src, dst, svc, action, track="Log", enabled=True, comments="", install_on=None, days_ago=90,
             modifier="admin", layer_type="access-rule"):
        uid = uid_for("rule", f"{name}:{number}")
        created = self.now - timedelta(days=days_ago)
        rule = {
            "uid": uid, "name": name, "type": layer_type, "domain": self.domain, "enabled": enabled, "comments": comments,
            "meta-info": {"lock": "unlocked", "validation-state": "ok", "read-only": False,
                          "last-modify-time": api_date(created + timedelta(hours=3)), "last-modifier": modifier,
                          "creation-time": api_date(created), "creator": modifier},
            "install-on": install_on or [POLICY_TARGETS_UID],
            "source": [self.by_name[s]["uid"] if s in self.by_name else s for s in src], "source-negate": False,
            "destination": [self.by_name[d]["uid"] if d in self.by_name else d for d in dst], "destination-negate": False,
            "service": [self.by_name[s]["uid"] if s in self.by_name else s for s in svc], "service-negate": False,
            "vpn": [ANY_UID], "action": ACTIONS[action], "action-settings": {"enable-identity-captive-portal": False},
            "content": [ANY_UID], "content-negate": False, "content-direction": "any",
            "track": {"type": TRACKS[track], "alert": "none", "accounting": False, "per-session": False, "per-connection": track != "None",
                      "enable-firewall-session": False}, "track-alert": "none", "time": [ANY_UID],
            "custom-fields": {"field-1": "", "field-2": "", "field-3": ""}, "rule-number": number,
            "hits": {"value": self.rng.randint(0, 50000) if enabled else 0, "level": "medium" if enabled else "zero", "percentage": "0%",
                     "first-date": api_date(created), "last-date": api_date(self.now - timedelta(hours=self.rng.randint(1, 72)))},
        }
        if not enabled or action == "Drop" and "Cleanup" not in name:
            rule["hits"]["value"] = 0 if not enabled else rule["hits"]["value"]
        self.objects[uid] = rule
        return rule

    def section(self, name, rules):
        return {"uid": uid_for("section", name), "name": name, "type": "access-section", "rulebase": rules,
                "from": rules[0]["rule-number"], "to": rules[-1]["rule-number"]}

    def build_standard_rules(self):
        gw = [g["uid"] for g in self.gateways if g["type"] == "simple-gateway"]
        r = self.rule
        s1 = [
            r(1, "Stealth - protect gateways", ["Any"], gw, ["Any"], "Drop", comments="Drop direct access to firewalls except from mgmt", days_ago=500),
            r(2, "Mgmt to gateways", ["net-mgmt", "jump-01"], gw, ["ssh", "https"], "Accept", days_ago=500),
            r(3, "Admin SSH from jump host", ["jump-01"], ["grp-internal-nets"], ["ssh"], "Accept", "Detailed Log", days_ago=400),
            r(4, "Legacy FTP admin (disabled)", ["jump-01"], ["legacy-ftp-01"], ["ftp"], "Accept", enabled=False,
              comments="Disabled 2024-02 - FTP server decommissioned", days_ago=380),
            r(5, "SIEM log collection", ["grp-internal-nets"], ["siem-01"], ["syslog-udp"], "Accept", "None", days_ago=300),
        ]
        s2 = [
            r(6, "Infra services", ["grp-internal-nets"], ["grp-infra-services"], ["sg-infra"], "Accept", "None", days_ago=300),
            r(7, "Users to web", ["net-users", "rng-dhcp-users"], ["grp-web-servers"], ["sg-web"], "Accept", days_ago=300),
            r(8, "Web to app", ["grp-web-servers"], ["grp-app-servers"], ["tcp-8443-app"], "Accept", days_ago=280),
            r(9, "App to DB", ["grp-app-servers"], ["grp-db-servers"], ["postgresql"], "Accept", "Detailed Log",
              comments="app-03 was never added to grp-app-servers -> its DB traffic hits the cleanup rule", days_ago=280),
            r(10, "DB replication", ["db-01"], ["db-02"], ["postgresql"], "Accept", days_ago=270),
            r(11, "Mail relay out", ["mail-01"], ["Internet"], ["smtp"], "Accept", days_ago=250),
            r(12, "TEMP troubleshooting - Any Any Accept", ["Any"], ["Any"], ["Any"], "Accept", "Log", enabled=True,
              comments="TEMP added during incident INC-4471, REMOVE AFTER - shadows every rule below it", days_ago=12, modifier="jsmith"),
            r(13, "Block guest wifi to internal", ["net-guest-wifi"], ["grp-internal-nets"], ["Any"], "Drop", comments="Shadowed by rule 12", days_ago=250),
            r(14, "Block P2P (disabled)", ["net-users"], ["Internet"], ["Any"], "Drop", enabled=False, comments="Disabled pending app-control rollout", days_ago=200),
        ]
        s3 = [
            r(15, "Users to Internet web", ["net-users", "net-guest-wifi"], ["Internet"], ["sg-web"], "Accept", days_ago=250),
            r(16, "Partner VPN to app", ["rng-partner-b2b", "vpn-peer-01"], ["grp-app-servers"], ["tcp-8443-app"], "Accept", "Detailed Log", days_ago=150),
            r(17, "Web to Internet (updates)", ["grp-web-servers"], ["Internet"], ["sg-web"], "Accept", days_ago=150),
            r(18, "Block DMZ to internal", ["net-dmz"], ["grp-internal-nets"], ["Any"], "Reject", days_ago=150),
            r(19, "Mgmt from users (disabled)", ["net-users"], ["net-mgmt"], ["ssh"], "Accept", enabled=False, comments="Disabled after audit finding", days_ago=100),
            r(20, "Cleanup rule", ["Any"], ["Any"], ["Any"], "Drop", "Log", comments="Default deny", days_ago=500),
        ]
        return [self.section("Management Access", s1), self.section("Internal Services", s2), self.section("Internet Access & Cleanup", s3)]

    def build_dmz_rules(self):
        r = self.rule
        rules = [
            r(1, "Internet to DMZ web", ["Any"], ["net-dmz"], ["sg-web"], "Accept", days_ago=200),
            r(2, "DMZ to DB (should not exist)", ["net-dmz"], ["grp-db-servers"], ["postgresql"], "Accept", "Detailed Log",
              comments="Flagged in last audit", days_ago=60, modifier="jsmith"),
            r(3, "DMZ outbound DNS", ["net-dmz"], ["dns-01"], ["domain-udp"], "Accept", "None", days_ago=200),
            r(4, "DMZ cleanup", ["Any"], ["Any"], ["Any"], "Drop", days_ago=200),
        ]
        return [self.section("DMZ", rules)]

    def nat_rule(self, number, name, osrc, odst, osvc, tsrc, tdst, tsvc, method, auto=False, enabled=True, comments=""):
        uid = uid_for("nat-rule", f"{name}:{number}")
        ref = lambda n: self.by_name[n]["uid"] if n in self.by_name else n  # noqa: E731
        created = self.now - timedelta(days=200)
        rule = {"uid": uid, "name": name, "type": "nat-rule", "domain": self.domain, "enabled": enabled, "comments": comments,
                "meta-info": {"lock": "unlocked", "validation-state": "ok", "read-only": auto, "last-modify-time": api_date(created),
                              "last-modifier": "System" if auto else "admin", "creation-time": api_date(created),
                              "creator": "System" if auto else "admin"},
                "install-on": [POLICY_TARGETS_UID], "auto-generated": auto, "original-source": ref(osrc),
                "original-destination": ref(odst), "original-service": ref(osvc), "translated-source": ref(tsrc),
                "translated-destination": ref(tdst), "translated-service": ref(tsvc), "method": method, "rule-number": number}
        self.objects[uid] = rule
        return rule

    def build_nat_rules(self):
        pub = self.base("host", "pub-web-vip", "Objects/host", comments="Public VIP for web", **{"ipv4-address": "198.51.100.10"})
        self.hosts.append(pub)
        self.by_name["pub-web-vip"] = pub
        n = self.nat_rule
        manual_upper = [n(1, "Publish web VIP", "Any", "pub-web-vip", "https", "Original", "web-01", "Original", "static",
                          comments="Inbound static NAT for the web VIP"),
                        n(2, "No NAT inside", "grp-internal-nets", "grp-internal-nets", "Any", "Original", "Original", "Original", "static")]
        auto = [n(3, "web-01 static NAT", "web-01", "Any", "Any", "Original", "Original", "Original", "static", auto=True),
                n(4, "web-02 static NAT", "web-02", "Any", "Any", "Original", "Original", "Original", "static", auto=True)]
        hide = [n(5, "Hide internal behind gateway", "grp-internal-nets", "Internet", "Any", "gw-hq-fw01", "Original", "Original", "hide",
                  comments="Outbound hide NAT")]
        return manual_upper + [
            {"uid": uid_for("nat-section", "Automatic Generated Rules : Machine Static NAT"), "name": "Automatic Generated Rules : Machine Static NAT",
             "type": "nat-section", "rulebase": auto, "from": 3, "to": 4},
            {"uid": uid_for("nat-section", "Automatic Generated Rules : Machine Hide NAT"), "name": "Automatic Generated Rules : Machine Hide NAT",
             "type": "nat-section", "rulebase": []},
            {"uid": uid_for("nat-section", "Manual Lower Rules"), "name": "Manual Lower Rules", "type": "nat-section", "rulebase": hide, "from": 5, "to": 5},
        ]

    def build_threat_rules(self):
        profile = self.predefined("44b83c65-e73c-4d63-aa55-4cdcea4a6d4b", "Optimized", "ThreatProfile")
        rules = []
        for number, name, scope, comments in [(1, "Protect DMZ", ["net-dmz"], ""), (2, "Protect internal", ["grp-internal-nets"], "Default profile")]:
            uid = uid_for("threat-rule", name)
            created = self.now - timedelta(days=300)
            rules.append({"uid": uid, "name": name, "type": "threat-rule", "domain": self.domain, "enabled": True, "comments": comments,
                          "meta-info": {"lock": "unlocked", "validation-state": "ok", "read-only": False, "last-modify-time": api_date(created),
                                        "last-modifier": "admin", "creation-time": api_date(created), "creator": "admin"},
                          "install-on": [POLICY_TARGETS_UID], "source": [ANY_UID], "source-negate": False, "destination": [ANY_UID],
                          "destination-negate": False, "service": [ANY_UID], "service-negate": False,
                          "protected-scope": [self.by_name[s]["uid"] for s in scope], "protected-scope-negate": False,
                          "track": TRACKS["Log"], "track-settings": {"packet-capture": True}, "action": profile["uid"],
                          "exceptions": [], "rule-number": number})
            self.objects[uid] = rules[-1]
        return [{"uid": uid_for("threat-section", "Threat Prevention"), "name": "Threat Prevention", "type": "threat-section",
                 "rulebase": rules, "from": 1, "to": 2}]

    def build_gateways(self):
        blades_full = {"firewall": True, "ips": True, "anti-bot": True, "anti-virus": True, "application-control": True,
                       "url-filtering": True, "identity-awareness": False, "threat-emulation": False, "site-to-site-vpn": True}
        specs = [
            ("gw-hq-fw01", "10.1.99.1", "R81.20", "Check Point 6600", "communicating", True, 2, blades_full, "HQ perimeter gateway"),
            ("gw-branch-fw02", "10.9.1.1", "R81.10", "Check Point 3800", "communicating", True, 97, {"firewall": True, "site-to-site-vpn": True},
             "Branch gateway - still on R81.10, policy not pushed since Q1 (installation 97 days old)"),
            ("gw-dmz-fw03", "172.16.10.1", "R81.20", "Open server", "not communicating", False, None, {"firewall": True, "ips": True},
             "DMZ gateway - SIC trust lost, no policy installed, no logs received for 3 days"),
        ]
        self.gateways = []
        for name, ip, version, hw, sic, installed, days, blades, comment in specs:
            policy = {}
            if installed:
                policy = {"access-policy-installed": True, "access-policy-name": "Standard",
                          "access-policy-installation-date": api_date(self.now - timedelta(days=days, hours=3)),
                          "access-policy-revision": {"uid": uid_for("revision", f"{name}:{days}"), "name": f"Standard rev {days}", "type": "session"},
                          "threat-policy-installed": name == "gw-hq-fw01", "threat-policy-name": "Standard" if name == "gw-hq-fw01" else "",
                          "threat-policy-installation-date": api_date(self.now - timedelta(days=days, hours=3))}
            else:
                policy = {"access-policy-installed": False, "threat-policy-installed": False}
            g = self.base("simple-gateway", name, "NetworkObjects/CheckPoint/GWs/xGW_CP", "red" if sic != "communicating" else "black",
                          comment, days_ago=600,
                          **{"ipv4-address": ip, "ipv6-address": "", "version": version, "operating-system": "Gaia", "os-name": "Gaia",
                             "hardware": hw, "sic-status": sic, "sic-state": sic, "sic-name": f"CN={name},O=mgmt-smc01..a1b2c3",
                             "policy": policy, "network-security-blades": blades,
                             "management-blades": {}, "vpn-encryption-domain": "addresses_behind_gw", "dynamic-ip": False,
                             "interfaces": [{"interface-name": "eth0", "name": "eth0", "ipv4-address": ip, "ipv4-network-mask": "255.255.255.0",
                                             "ipv4-mask-length": 24, "topology": {"leads-to-internet": False}, "dynamic-ip": False},
                                            {"interface-name": "eth1", "name": "eth1", "ipv4-address": "198.51.100." + ip.split(".")[1],
                                             "ipv4-network-mask": "255.255.255.0", "ipv4-mask-length": 24,
                                             "topology": {"leads-to-internet": True}, "dynamic-ip": False}],
                             "send-logs-to-server": ["mgmt-smc01"], "send-logs-to-backup-server": [], "save-logs-locally": False,
                             "hit-count": True, "enable-https-inspection": False, "cluster-member-names": [],
                             **{k: v for k, v in blades.items()}, "vpn": blades.get("site-to-site-vpn", False)})
            self.gateways.append(g)
            self.by_name[name] = g
        self.mgmt = self.base("checkpoint-host", "mgmt-smc01", "NetworkObjects/CheckPoint/Hosts/xHost_CP", comments="Security Management Server",
                              days_ago=700, **{"ipv4-address": "10.1.99.10", "ipv6-address": "", "version": "R81.20", "operating-system": "Gaia",
                                               "hardware": "Open server", "sic-status": "communicating", "policy": {},
                                               "network-security-blades": {},
                                               "management-blades": {"network-policy-management": True, "logging-and-status": True},
                                               "interfaces": [{"interface-name": "eth0", "ipv4-address": "10.1.99.10", "ipv4-network-mask": "255.255.255.0",
                                                               "ipv4-mask-length": 24, "topology": {"leads-to-internet": False}, "dynamic-ip": False}]})
        self.mgmt["type"] = "CpmiHostCkp"
        self.mgmt["read-only"] = True
        self.by_name["mgmt-smc01"] = self.mgmt

    def session_obj(self, name, user, app, state, in_work, changes, locks, ip, login_days, publish_days=None, desc="", read_only=False):
        uid = uid_for("session", name)
        login = self.now - timedelta(days=login_days)
        obj = {"uid": uid, "name": name, "type": "session", "domain": self.domain, "user-name": user, "state": state,
               "application": app, "changes": changes, "locks": locks, "ip-address": ip, "in-work": in_work,
               "connection-mode": "read only" if read_only else "read write", "read-only": read_only,
               "session-timeout": SESSION_TIMEOUT, "expired-session": False, "description": desc, "email": f"{user}@example.test",
               "last-login-time": api_date(login), "comments": "", "color": "black", "icon": "General/session", "tags": [],
               "meta-info": {"lock": "unlocked", "validation-state": "ok", "last-modify-time": api_date(login), "last-modifier": user,
                             "creation-time": api_date(login), "creator": user}}
        if publish_days is not None:
            obj["publish-time"] = api_date(self.now - timedelta(days=publish_days))
            obj["last-logout-time"] = api_date(self.now - timedelta(days=publish_days))
        self.objects[uid] = obj
        return obj

    def build_sessions(self):
        self.sessions = [
            self.session_obj("admin@SmartConsole", "admin", "SmartConsole", "open", True, 3, 2, "10.1.99.5", 0, desc="Working on INC-4471 cleanup"),
            self.session_obj("svc-automation-nightly", "svc-automation", "WEB_API", "open", False, 0, 1, "10.1.40.41", 5,
                             desc="Stale: disconnected 5 days ago, still holds 1 lock on grp-app-servers"),
            self.session_obj("jsmith-temp-rule", "jsmith", "SmartConsole", "published", False, 2, 0, "10.2.10.44", 12, 12,
                             desc="Added TEMP troubleshooting rule"),
            self.session_obj("jsmith-app-03", "jsmith", "SmartConsole", "published", False, 1, 0, "10.2.10.44", 6, 6, desc="Added host app-03"),
            self.session_obj("admin-q1-policy", "admin", "SmartConsole", "published", False, 9, 0, "10.1.99.5", 97, 97, desc="Q1 policy refresh"),
        ]
        self.groups[1]["meta-info"]["lock"] = "locked by other session"
        self.groups[1]["meta-info"]["locking-admin"] = "svc-automation"
        self.groups[1]["meta-info"]["locking-session-id"] = self.sessions[1]["uid"]
        self.last_published = self.sessions[3]

    def build_tasks(self):
        self.tasks = []
        specs = [("Install policy Standard on gw-hq-fw01", "succeeded", 100, 2, "admin"),
                 ("Install policy Standard on gw-dmz-fw03", "failed", 100, 3, "admin"),
                 ("Publish session jsmith-app-03", "succeeded", 100, 6, "jsmith"),
                 ("Publish session jsmith-temp-rule", "succeeded", 100, 12, "jsmith"),
                 ("Install policy Standard on gw-branch-fw02", "failed", 100, 40, "svc-automation"),
                 ("Install policy Standard on gw-branch-fw02", "succeeded", 100, 97, "admin"),
                 ("Verify policy Standard", "in progress", 60, 0, "admin")]
        for i, (name, status, pct, days, initiator) in enumerate(specs):
            start = self.now - timedelta(days=days, minutes=30)
            detail = {"initiator": initiator}
            if "Install" in name:
                target = name.split(" on ")[1]
                detail["stagesInfo"] = [{"stageName": "Verification", "status": status}]
                detail["target"] = target
                if status == "failed":
                    detail["errors"] = [{"message": f"Policy installation failed on {target}: SIC not established" if "dmz" in target
                                         else f"Gateway {target} did not respond (timeout)"}]
            self.tasks.append({"uid": uid_for("task", f"{name}:{i}"), "task-id": uid_for("task", f"{name}:{i}"), "name": name,
                               "task-name": name, "type": "task", "domain": self.domain, "status": status,
                               "progress-percentage": pct, "progress-description": "Operation Complete" if pct == 100 else "Verifying policy...",
                               "suppressed": False, "start-time": api_date(start), "last-update-time": api_date(start + timedelta(minutes=4)),
                               "task-details": [detail], "color": "black", "icon": "General/task", "tags": [], "comments": "",
                               "meta-info": {"lock": "unlocked", "validation-state": "ok", "last-modify-time": api_date(start),
                                             "last-modifier": initiator, "creation-time": api_date(start), "creator": initiator}})

    def build_vpn(self):
        members = [self.ref(self.by_name[n]["uid"]) for n in ("gw-hq-fw01", "gw-branch-fw02")]
        self.vpn_meshed = [self.base("vpn-community-meshed", "corp-mesh", "VPNCommunities/Meshed", comments="HQ <-> branch", days_ago=400,
                                     **{"use-shared-secret": False, "encryption-method": "ikev2 only", "encryption-suite": "custom",
                                        "ike-phase-1": {"encryption-algorithm": "aes-256", "diffie-hellman-group": "group 19", "data-integrity": "sha256"},
                                        "ike-phase-2": {"encryption-algorithm": "aes-gcm-128", "data-integrity": "aes-xcbc"}, "gateways": members})]
        self.vpn_star = [self.base("vpn-community-star", "partner-star", "VPNCommunities/Star", comments="Partner B2B", days_ago=200,
                                   **{"use-shared-secret": True, "shared-secrets": [{"external-gateway": "vpn-peer-01", "shared-secret": "s3cr3t-must-never-leak"}],
                                      "encryption-method": "ikev1 for ipv4 and ikev2 for ipv6 only", "encryption-suite": "suite-b-gcm-128",
                                      "ike-phase-1": {"encryption-algorithm": "aes-128", "diffie-hellman-group": "group 2", "data-integrity": "sha1"},
                                      "ike-phase-2": {"encryption-algorithm": "aes-128", "data-integrity": "sha1"},
                                      "center-gateways": members[:1], "satellite-gateways": [], "mesh-center-gateways": False})]

    def rule_by_number(self, layer, number):
        for item in self.rulebases[layer]:
            for r in item["rulebase"]:
                if r["rule-number"] == number:
                    return r

    def build_logs(self):
        rng = random.Random(7)
        layer = self.layers["Standard Network"]
        gws = {"gw-hq-fw01": "10.1.99.1", "gw-branch-fw02": "10.9.1.1"}
        logs = []
        scenarios = [  # (weight, rule number, action, src pool, dst pool, service, proto, blade)
            (30, 7, "Accept", ["10.2.10.44", "10.2.11.7", "10.2.100.23"], ["10.1.10.11", "10.1.10.12"], "443", "6", "Firewall"),
            (20, 9, "Accept", ["10.1.20.21", "10.1.20.22"], ["10.1.30.31"], "5432", "6", "Firewall"),
            (25, 20, "Drop", ["10.1.20.23"], ["10.1.30.31"], "5432", "6", "Firewall"),  # app-03 not in group -> cleanup
            (10, 12, "Accept", ["10.3.4.5", "10.3.9.12"], ["10.1.30.31", "10.1.5.10"], "22", "6", "Firewall"),  # guest wifi passes via TEMP rule
            (8, 15, "Accept", ["10.2.10.44"], ["93.184.216.34"], "443", "6", "Application Control"),
            (4, 20, "Drop", ["172.16.10.9"], ["10.1.30.32"], "5432", "6", "Firewall"),
            (3, 16, "Accept", ["203.0.113.77"], ["10.1.20.21"], "8443", "6", "VPN"),
            (3, 2, "Accept", ["10.1.99.5"], ["10.1.99.1"], "22", "6", "Firewall"),
        ]
        pool = [s for s in scenarios for _ in range(s[0])]
        for i in range(200):
            s = rng.choice(pool)
            minutes_ago = rng.randint(1, 48 * 60)
            if s[1] == 20 and s[3][0] == "10.1.20.23":
                minutes_ago = rng.randint(1, 6 * 24 * 60) if rng.random() < 0.2 else rng.randint(1, 30 * 60)
                minutes_ago = min(minutes_ago, 48 * 60)
            t = self.now - timedelta(minutes=minutes_ago)
            rule = self.rule_by_number("Standard Network", s[1])
            gw = "gw-branch-fw02" if s[1] == 15 and rng.random() < 0.3 else "gw-hq-fw01"
            severity = {"Drop": "Medium", "Accept": "Informational", "Reject": "Medium"}[s[2]]
            sent, recv = rng.randint(200, 90000), rng.randint(100, 400000)
            log = {
                "time": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "id": f"{uid_for('log', str(i))[:8]}-{i:04x}-0000-5e80-{i:012x}",
                "log_uid": uid_for("logrec", str(i)).upper(), "type": "Log", "blade": s[7], "product": s[7] if s[7] != "Firewall" else "VPN-1 & FireWall-1",
                "product_family": "Access" if s[7] in ("Firewall", "Application Control") else "Network", "action": s[2], "severity": severity,
                "origin": gw, "orig": gws[gw], "src": rng.choice(s[3]), "dst": rng.choice(s[4]), "service": s[5], "s_port": str(rng.randint(1025, 65000)),
                "proto": s[6], "service_id": {"443": "https", "5432": "postgresql", "22": "ssh", "8443": "tcp-8443-app"}[s[5]],
                "rule": str(s[1]), "rule_name": rule["name"], "rule_uid": rule["uid"], "layer_name": layer["name"], "layer_uuid": layer["uid"],
                "policy_name": "Standard", "policy": "Standard", "policy_date": (self.now - timedelta(days=2, hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "i_f_name": "eth0", "i_f_dir": "inbound", "conn_direction": "Internal" if not s[4][0].startswith("93.") else "Outgoing",
                "bytes": str(sent + recv), "sent_bytes": str(sent), "received_bytes": str(recv),
                "index_time": (t + timedelta(seconds=3)).strftime("%Y-%m-%dT%H:%M:%SZ"), "orig_log_server": "10.1.99.10",
                "sequencenum": str(i), "marker": f"@A@@B@{int(t.timestamp())}@C@{i}",
                "calc_desc": f"{s[2]} {rng.choice(s[3])} -> {rng.choice(s[4])}:{s[5]} by rule {s[1]} ({rule['name']})",
            }
            if s[2] == "Drop":
                log["description"] = "Dropped by the cleanup rule" if s[1] == 20 else "Dropped"
            if s[7] == "Application Control":
                log["appi_name"] = "Web Browsing"
                log["app_category"] = "Web Services Provider"
            if s[7] == "VPN":
                log["vpn_feature_name"] = "VPN"
                log["scheme"] = "IKE"
            logs.append(log)
        logs.sort(key=lambda r: r["time"], reverse=True)
        return logs

    def build_audit_logs(self):
        rng = random.Random(11)
        entries = []
        ops = [("admin", "10.1.99.5", "SmartConsole", "Log In", "Session Management", "", "Success"),
               ("admin", "10.1.99.5", "SmartConsole", "Log Out", "Session Management", "", "Success"),
               ("jsmith", "10.2.10.44", "SmartConsole", "Modify Object", "Object Manipulation", "grp-app-servers", "Success"),
               ("jsmith", "10.2.10.44", "SmartConsole", "Create Object", "Object Manipulation", "app-03", "Success"),
               ("jsmith", "10.2.10.44", "SmartConsole", "Publish", "Session Management", "", "Success"),
               ("admin", "10.1.99.5", "SmartConsole", "Install Policy", "Policy Installation", "Standard", "Success"),
               ("admin", "10.1.99.5", "SmartConsole", "Install Policy", "Policy Installation", "Standard", "Failure"),
               ("svc-automation", "10.1.40.41", "WEB_API", "Log In", "Session Management", "", "Success"),
               ("svc-automation", "10.1.40.41", "WEB_API", "Log In", "Session Management", "", "Failure"),
               ("bow-readonly", "127.0.0.1", "WEB_API", "Log In", "Session Management", "", "Success")]
        for i in range(50):
            op = rng.choice(ops)
            t = self.now - timedelta(minutes=rng.randint(1, 48 * 60))
            if op[3] == "Install Policy" and op[6] == "Failure":
                t = self.now - timedelta(days=3, hours=rng.randint(0, 3))
            entries.append({"time": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "id": f"audit-{i:04d}", "log_uid": uid_for("audit", str(i)).upper(),
                            "type": "Log", "blade": op[2], "product": "SmartConsole" if op[2] == "SmartConsole" else "Management API",
                            "action": "Audit", "severity": "Informational" if op[6] == "Success" else "Medium", "origin": "mgmt-smc01",
                            "orig": "10.1.99.10", "administrator": op[0], "user": op[0], "client_ip": op[1], "machine": op[1],
                            "operation": op[3], "operation_number": str(rng.randint(1, 200)), "subject": op[4], "objectname": op[5],
                            "objecttype": "group" if op[5].startswith("grp") else "host" if op[5] == "app-03" else "package" if op[5] == "Standard" else "",
                            "objecttable": "network_objects" if op[5] and op[5] != "Standard" else "", "uid": self.by_name[op[5]]["uid"] if op[5] in self.by_name else "",
                            "session_name": f"{op[0]}@{op[2]}", "session_uid": uid_for("session", f"{op[0]}@{op[2]}"), "audit_status": op[6],
                            "additional_info": "Policy installation failed on gw-dmz-fw03: SIC not established" if op[6] == "Failure" and op[3] == "Install Policy"
                            else "Authentication failed" if op[6] == "Failure" else "",
                            "fieldschanges": "members: added app-02" if op[3] == "Modify Object" else "", "application": op[2],
                            "index_time": (t + timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ"), "sequencenum": str(i),
                            "marker": f"@A@@B@{int(t.timestamp())}@C@{i}"})
        entries.sort(key=lambda r: r["time"], reverse=True)
        return entries

    def build_changes(self):
        def sess(s):
            return {"session-uid": s["uid"], "user-name": s["user-name"], "publish-time": s["publish-time"],
                    "domain-info": self.domain}
        rule12 = self.rule_by_number("Standard Network", 12)
        return [
            {"session": sess(self.sessions[3]), "operations": {"added-objects": [self.ref(self.hosts[4]["uid"])],
                                                                "modified-objects": [], "deleted-objects": []}},
            {"session": sess(self.sessions[2]), "operations": {"added-objects": [{"uid": rule12["uid"], "name": rule12["name"], "type": "access-rule", "domain": self.domain}],
                                                                "modified-objects": [{"old-object": {**self.ref(self.layers["Standard Network"]["uid"])},
                                                                                      "new-object": {**self.ref(self.layers["Standard Network"]["uid"])}}],
                                                                "deleted-objects": []}},
            {"session": sess(self.sessions[4]), "operations": {"added-objects": [], "modified-objects": [
                {"old-object": {**self.ref(self.groups[1]["uid"]), "name": "grp-app-servers"}, "new-object": self.ref(self.groups[1]["uid"])}],
                "deleted-objects": [{"uid": uid_for("host", "legacy-app-00"), "name": "legacy-app-00", "type": "host", "domain": self.domain}]}},
        ]

    # -------------------------------------------------------------- views
    STANDARD_KEYS = ("uid", "name", "type", "domain", "icon", "color", "ipv4-address", "ipv6-address", "subnet4", "subnet-mask",
                     "mask-length4", "subnet6", "mask-length6", "ipv4-address-first", "ipv4-address-last", "port", "members",
                     "access-layers", "threat-layers", "version", "user-name", "state", "domain-type", "task-id", "task-name", "status",
                     "progress-percentage", "progress-description", "suppressed", "last-update-time", "start-time", "community-type")

    def view(self, obj, level):
        if level == "uid":
            return obj["uid"]
        if level == "standard":
            return {k: obj[k] for k in self.STANDARD_KEYS if k in obj}
        return obj

    def collection(self, name):
        hosts = self.hosts
        return {
            "show-hosts": hosts, "show-networks": self.networks, "show-address-ranges": self.ranges, "show-groups": self.groups,
            "show-services-tcp": [s for s in self.services if s["type"] == "service-tcp"],
            "show-services-udp": [s for s in self.services if s["type"] == "service-udp"],
            "show-service-groups": self.service_groups, "show-packages": list(self.packages.values()),
            "show-access-layers": list(self.layers.values()), "show-simple-gateways": self.gateways,
            "show-gateways-and-servers": self.gateways + [self.mgmt], "show-sessions": self.sessions, "show-tasks": self.tasks,
            "show-domains": [self.domain_object()], "show-vpn-communities-meshed": self.vpn_meshed, "show-vpn-communities-star": self.vpn_star,
            "show-unused-objects": self.unused_objects(),
            "show-objects": hosts + self.networks + self.ranges + self.groups + self.services + self.service_groups + self.gateways,
        }[name]

    def domain_object(self):
        return {**self.domain, "type": "domain", "servers": [{"name": "mgmt-smc01", "ipv4-address": "10.1.99.10", "type": "management server", "active": True}],
                "global-domain-assignments": [], "tags": [], "color": "black", "icon": "Objects/domain", "comments": "", "read-only": True,
                "meta-info": self.mgmt["meta-info"]}

    def unused_objects(self):
        used = set()
        for layer in self.rulebases.values():
            for item in layer:
                for r in item["rulebase"]:
                    for k in ("source", "destination", "service", "install-on"):
                        used.update(r[k])
        for item in self.nat_rulebases["Standard"]:
            rules = item["rulebase"] if "rulebase" in item else [item]
            for r in rules:
                for k in ("original-source", "original-destination", "original-service", "translated-source", "translated-destination", "translated-service"):
                    used.add(r[k])
        for g in self.groups + self.service_groups:
            used.update(m["uid"] for m in g["members"])
        for o in self.hosts + self.networks + self.ranges + self.groups + self.services + self.service_groups:
            for g in o.get("groups", []):
                if o["uid"] in used:
                    used.add(g["uid"])
        return [o for o in self.hosts + self.networks + self.ranges + self.services + self.service_groups if o["uid"] not in used]


# ------------------------------------------------------------------ filters
def text_match(obj, expression):
    """SmartConsole-style object search: AND/OR of case-insensitive terms over name, comments, tags and IP fields."""
    hay = " ".join(str(v) for k, v in obj.items() if k in ("name", "comments", "ipv4-address", "subnet4", "ipv4-address-first",
                                                           "ipv4-address-last", "port", "user-name", "type", "uid")).lower()
    hay += " " + " ".join(t.get("name", "") for t in obj.get("tags", []) if isinstance(t, dict)).lower()
    for disjunct in re.split(r"\s+OR\s+", expression.strip()):
        terms = [t for t in re.split(r"\s+AND\s+|\s+", disjunct) if t]
        if terms and all(t.lower() in hay for t in terms):
            return True
    return False


LOG_ALIASES = {"source": "src", "destination": "dst", "orig": "origin", "rule": "rule_name", "user": "user", "admin": "administrator",
               "operation": "operation", "severity": "severity", "action": "action", "blade": "blade", "product": "product", "service": "service"}


class LogQuery:
    """Small Check Point log query language: field:value, quoted values, (a OR b), AND/OR/NOT, wildcards, free text."""

    TOKEN = re.compile(r'\s*(\(|\)|[^\s():"]+:"[^"]*"|"[^"]*"|[^\s()]+)')

    def __init__(self, text):
        self.tokens = [t for t in self.TOKEN.findall(text or "")]
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self):
        tok = self.peek()
        self.pos += 1
        return tok

    def parse(self):
        if not self.tokens:
            return lambda log: True
        node = self.parse_or()
        if self.peek() is not None:
            raise ValueError("unbalanced expression")
        return node

    def parse_or(self):
        left = self.parse_and()
        while self.peek() and self.peek().upper() == "OR":
            self.take()
            right = self.parse_and()
            left = (lambda a, b: lambda log: a(log) or b(log))(left, right)
        return left

    def parse_and(self):
        left = self.parse_not()
        while self.peek() and self.peek().upper() not in ("OR", ")") and self.peek() is not None:
            if self.peek().upper() == "AND":
                self.take()
            right = self.parse_not()
            left = (lambda a, b: lambda log: a(log) and b(log))(left, right)
        return left

    def parse_not(self):
        if self.peek() and self.peek().upper() == "NOT":
            self.take()
            inner = self.parse_not()
            return lambda log: not inner(log)
        return self.parse_atom()

    def parse_atom(self):
        tok = self.take()
        if tok is None:
            raise ValueError("unexpected end")
        if tok == "(":
            node = self.parse_or()
            if self.take() != ")":
                raise ValueError("missing )")
            return node
        if ":" in tok and not tok.startswith('"'):
            field, _, value = tok.partition(":")
            value = value.strip('"')
            if value == "(":
                raise ValueError("unsupported")
            if value == "":
                nxt = self.peek()
                if nxt == "(":
                    self.take()
                    values = []
                    while self.peek() not in (")", None):
                        v = self.take()
                        if v.upper() != "OR":
                            values.append(v.strip('"'))
                    self.take()
                    return lambda log: any(self.field_match(log, field, v) for v in values)
                value = self.take() or ""
            value = value.strip('"')
            return lambda log: self.field_match(log, field, value)
        text = tok.strip('"').lower()
        return lambda log: any(text in str(v).lower() for v in log.values())

    @staticmethod
    def field_match(log, field, value):
        key = LOG_ALIASES.get(field.lower(), field.lower())
        candidates = [key] + (["src"] if key == "source" else []) + (["dst"] if key == "destination" else [])
        if key == "rule_name":
            candidates += ["rule", "rule_name"]
        pattern = "^" + ".*".join(re.escape(p) for p in value.lower().split("*")).replace(r"\?", ".") + "$"
        for c in candidates:
            if c in log and re.fullmatch(pattern, str(log[c]).lower()):
                return True
        return False


# ------------------------------------------------------------------ server
class Server:
    """Command dispatcher shared by the HTTP handler and in-process tests."""

    def __init__(self, env=None, domain_name="SMC User"):
        env = os.environ if env is None else env
        self.username = env.get("CHECKPOINT_USERNAME", "")
        self.password = env.get("CHECKPOINT_PASSWORD", "")
        self.api_key = env.get("CHECKPOINT_API_KEY", "")
        if not (self.username and self.password) and not self.api_key:
            raise SystemExit("Set CHECKPOINT_USERNAME and CHECKPOINT_PASSWORD, or CHECKPOINT_API_KEY, locally.")
        self.estate = Estate(domain_name)
        self.sessions = {}
        self.queries = {}
        self.lock = threading.Lock()
        self.last_login = {}

    # ---- helpers
    @staticmethod
    def check_params(body, allowed):
        for key in body:
            if key not in allowed:
                return error(400, "generic_err_invalid_parameter_name", f"Unrecognized parameter [{key}]")
        return None

    @staticmethod
    def paging(body):
        limit, offset = body.get("limit", 50), body.get("offset", 0)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 500:
            return None, error(400, "generic_err_invalid_parameter", "Invalid parameter for [limit]. Expected an integer from 0 to 500")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            return None, error(400, "generic_err_invalid_parameter", "Invalid parameter for [offset]. Expected a non-negative integer")
        level = body.get("details-level", "standard")
        if level not in ("uid", "standard", "full"):
            return None, error(400, "generic_err_invalid_parameter", "Invalid parameter for [details-level]. Expected one of: uid, standard, full")
        return (limit, offset, level), None

    @staticmethod
    def order(items, body):
        order = body.get("order")
        if order is None:
            return items
        if not isinstance(order, list) or not all(isinstance(o, dict) and len(o) == 1 and list(o)[0] in ("ASC", "DESC") for o in order):
            return None
        for term in reversed(order):
            direction, field = next(iter(term.items()))
            if field not in ("name",):
                return None
            items = sorted(items, key=lambda o: str(o.get(field, "")).lower(), reverse=direction == "DESC")
        return items

    def envelope(self, items, limit, offset, level, container="objects"):
        page = items[offset:offset + limit]
        return 200, {"from": offset + 1 if page else 0, "to": offset + len(page) if page else 0, "total": len(items),
                     container: [self.estate.view(o, level) for o in page]}

    # ---- dispatch
    def handle(self, version, command, headers, body):
        if version is not None and version not in SUPPORTED_VERSIONS:
            return error(400, "err_unknown_api_version", f"Unknown API version [{version}]")
        if command == "login":
            return self.login(body, headers)
        if command.startswith(MUTATING_PREFIXES) or command in ("publish", "discard", "install-policy", "run-script", "logout-all"):
            # Only after validating the session, like the real server.
            sid_error = self.require_session(headers)
            if sid_error:
                return sid_error
            return error(403, "generic_err_no_permissions",
                         f"Read-only session: command [{command}] is not permitted. This simulator refuses every mutating command.")
        handler = getattr(self, "cmd_" + command.replace("-", "_"), None)
        if handler is None:
            return error(404, "generic_err_command_not_found", f"Command [{command}] not found")
        sid_error = self.require_session(headers)
        if sid_error:
            return sid_error
        return handler(body, headers)

    def require_session(self, headers):
        sid = headers.get("X-chkp-sid")
        if not sid:
            return error(401, "generic_err_missing_session_id", "Missing header: [X-chkp-sid]")
        with self.lock:
            session = self.sessions.get(sid)
            if session is None:
                return error(401, "generic_err_wrong_session_id", f"Wrong session id [{sid[:8]}...]. Session may be expired. Please check session id or login again.")
            if time.monotonic() - session["last"] > SESSION_TIMEOUT:
                del self.sessions[sid]
                return error(401, "generic_err_session_expired", "Session expired. Login again.")
            session["last"] = time.monotonic()
        return None

    def login(self, body, headers):
        err = self.check_params(body, {"user", "password", "api-key", "domain", "read-only", "continue-last-session", "session-name",
                                       "session-description", "session-comments", "session-timeout", "enter-last-published-session"})
        if err:
            return err
        ok = False
        user = "api-key-admin"
        if "api-key" in body:
            if "user" in body or "password" in body:
                return error(400, "generic_err_invalid_parameter", "Use either api-key or user and password")
            ok = bool(self.api_key) and secrets.compare_digest(str(body["api-key"]), self.api_key)
        else:
            user = str(body.get("user", ""))
            ok = bool(self.username) and secrets.compare_digest(user, self.username) and secrets.compare_digest(str(body.get("password", "")), self.password)
        domain = body.get("domain")
        if domain is not None and domain not in (self.estate.domain["name"], self.estate.domain["uid"]):
            ok = False
        if not ok:
            return error(400, "err_login_failed", "Authentication to server failed.")
        read_only = body.get("read-only", False)
        if not isinstance(read_only, bool):
            return error(400, "generic_err_invalid_parameter", "Invalid parameter for [read-only]. Expected a boolean")
        sid = secrets.token_urlsafe(32)
        uid = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        with self.lock:
            self.sessions[sid] = {"uid": uid, "user": user, "read-only": read_only, "last": time.monotonic(), "login": now,
                                  "ip": headers.get("_client_ip", "127.0.0.1"), "description": body.get("session-description", "")}
            last = self.last_login.get(user)
            self.last_login[user] = now
        reply = {"sid": sid, "url": headers.get("_url", "https://127.0.0.1:443/web_api"), "uid": uid, "session-timeout": SESSION_TIMEOUT,
                 "api-server-version": API_VERSION, "read-only": read_only, "standby": False}
        if last:
            reply["last-login-was-at"] = api_date(last)
        return 200, reply

    def cmd_logout(self, body, headers):
        with self.lock:
            self.sessions.pop(headers.get("X-chkp-sid"), None)
        return 200, {"message": "OK"}

    def cmd_keepalive(self, body, headers):
        return 200, {"message": "OK"}

    def cmd_show_api_versions(self, body, headers):
        return 200, {"current-version": API_VERSION, "supported-versions": list(SUPPORTED_VERSIONS)}

    def current_session_object(self, headers):
        s = self.sessions[headers["X-chkp-sid"]]
        return {"uid": s["uid"], "name": s.get("description") or "", "type": "session", "domain": self.estate.domain,
                "user-name": s["user"], "state": "open", "application": "WEB_API", "changes": 0, "locks": 0, "ip-address": s["ip"],
                "in-work": True, "connection-mode": "read only" if s["read-only"] else "read write", "read-only": s["read-only"],
                "session-timeout": SESSION_TIMEOUT, "expired-session": False, "description": s.get("description", ""),
                "email": "", "last-login-time": api_date(s["login"]), "color": "black", "icon": "General/session", "tags": [], "comments": "",
                "meta-info": {"lock": "unlocked", "validation-state": "ok", "last-modify-time": api_date(s["login"]), "last-modifier": s["user"],
                              "creation-time": api_date(s["login"]), "creator": s["user"]}}

    def cmd_show_session(self, body, headers):
        err = self.check_params(body, {"uid", "detailed-admin-info"})
        if err:
            return err
        if "uid" in body:
            for s in self.estate.sessions:
                if s["uid"] == body["uid"]:
                    return 200, s
            return error(404, "generic_err_object_not_found", f"Requested object [{body['uid']}] not found")
        return 200, self.current_session_object(headers)

    def cmd_show_last_published_session(self, body, headers):
        return 200, self.estate.last_published

    def generic_collection(self, command, body, headers, extra_params=(), container="objects"):
        err = self.check_params(body, {"filter", "limit", "offset", "order", "details-level", "domains-to-process", "show-membership",
                                       "dereference-group-members", "show-as-ranges", "ignore-warnings", "show-only-local-domain", *extra_params})
        if err:
            return err
        paging, err = self.paging(body)
        if err:
            return err
        limit, offset, level = paging
        items = list(self.estate.collection(command))
        if command == "show-sessions" and not body.get("view-published-sessions"):
            items = [s for s in items if s["state"] == "open"]
            items = [self.current_session_object(headers)] + items
        elif command == "show-sessions":
            items = items + [self.current_session_object(headers)]
        if command == "show-objects" and body.get("type", "object") != "object":
            items = [o for o in items if o["type"] == body["type"]]
        if command == "show-tasks":
            status = body.get("status", "all")
            if status not in ("successful", "failed", "in-progress", "all"):
                return error(400, "generic_err_invalid_parameter", "Invalid parameter for [status]")
            want = {"successful": "succeeded", "failed": "failed", "in-progress": "in progress"}.get(status)
            if want:
                items = [t for t in items if t["status"] == want]
            if body.get("initiator"):
                items = [t for t in items if t["task-details"][0].get("initiator") == body["initiator"]]
            for key, after in (("from-date", True), ("to-date", False)):
                if key in body:
                    try:
                        bound = datetime.fromisoformat(str(body[key]).replace("Z", "+00:00"))
                    except ValueError:
                        return error(400, "generic_err_invalid_parameter", f"Invalid parameter for [{key}]. Expected ISO 8601")
                    if bound.tzinfo is None:
                        bound = bound.replace(tzinfo=timezone.utc)
                    items = [t for t in items if (t["last-update-time"]["posix"] / 1000 >= bound.timestamp()) == after or
                             t["last-update-time"]["posix"] / 1000 == bound.timestamp()]
            items.sort(key=lambda t: t["last-update-time"]["posix"], reverse=True)
        if "filter" in body:
            if not isinstance(body["filter"], str):
                return error(400, "generic_err_invalid_parameter", "Invalid parameter for [filter]. Expected a string")
            items = [o for o in items if text_match(o, body["filter"])]
        if command != "show-tasks":
            items = sorted(items, key=lambda o: str(o.get("name", "")).lower())
        items = self.order(items, body)
        if items is None:
            return error(400, "generic_err_invalid_parameter", "Invalid parameter for [order]")
        if command in ("show-groups", "show-service-groups") and not body.get("dereference-group-members", False) and level != "full":
            items = [{**o, "members": [m["uid"] for m in o["members"]]} for o in items]
        if command in ("show-groups",) and body.get("show-as-ranges"):
            items = [{**{k: v for k, v in o.items() if k != "members"}, "ranges": self.ranges_for(o["uid"])} for o in items]
        return self.envelope(items, limit, offset, level, container)

    def ranges_for(self, uid):
        ipv4, others = [], []
        for start, end, other in self.expand(uid):
            if other:
                others.append(self.estate.ref(other))
            else:
                ipv4.append({"start": start, "end": end})
        return {"ipv4": ipv4, "ipv6": [], "others": others, "excluded-others": []}

    def expand(self, uid, depth=0):
        o = self.estate.objects.get(uid)
        if o is None or depth > 5:
            return []
        if o["type"] == "host":
            return [(o["ipv4-address"], o["ipv4-address"], None)]
        if o["type"] == "network":
            net = ipaddress.ip_network(f"{o['subnet4']}/{o['mask-length4']}")
            return [(str(net.network_address), str(net.broadcast_address), None)]
        if o["type"] == "address-range":
            return [(o["ipv4-address-first"], o["ipv4-address-last"], None)]
        if o["type"] == "group":
            return [r for m in o["members"] for r in self.expand(m["uid"], depth + 1)]
        if o["type"] == "CpmiAnyObject":
            return [("0.0.0.0", "255.255.255.255", None)]
        return [(None, None, uid)]

    def cmd_show_hosts(self, body, headers):
        return self.generic_collection("show-hosts", body, headers)

    def cmd_show_networks(self, body, headers):
        return self.generic_collection("show-networks", body, headers)

    def cmd_show_address_ranges(self, body, headers):
        return self.generic_collection("show-address-ranges", body, headers)

    def cmd_show_groups(self, body, headers):
        return self.generic_collection("show-groups", body, headers, ("async-response",))

    def cmd_show_services_tcp(self, body, headers):
        return self.generic_collection("show-services-tcp", body, headers)

    def cmd_show_services_udp(self, body, headers):
        return self.generic_collection("show-services-udp", body, headers)

    def cmd_show_service_groups(self, body, headers):
        return self.generic_collection("show-service-groups", body, headers)

    def cmd_show_packages(self, body, headers):
        return self.generic_collection("show-packages", body, headers, ("show-installation-targets",), container="packages")

    def cmd_show_access_layers(self, body, headers):
        return self.generic_collection("show-access-layers", body, headers, container="access-layers")

    def cmd_show_simple_gateways(self, body, headers):
        return self.generic_collection("show-simple-gateways", body, headers)

    def cmd_show_gateways_and_servers(self, body, headers):
        return self.generic_collection("show-gateways-and-servers", body, headers)

    def cmd_show_sessions(self, body, headers):
        return self.generic_collection("show-sessions", body, headers, ("view-published-sessions",))

    def cmd_show_unused_objects(self, body, headers):
        return self.generic_collection("show-unused-objects", body, headers)

    def cmd_show_domains(self, body, headers):
        return self.generic_collection("show-domains", body, headers)

    def cmd_show_vpn_communities_meshed(self, body, headers):
        return self.generic_collection("show-vpn-communities-meshed", body, headers)

    def cmd_show_vpn_communities_star(self, body, headers):
        return self.generic_collection("show-vpn-communities-star", body, headers)

    def cmd_show_objects(self, body, headers):
        return self.generic_collection("show-objects", body, headers, ("type", "uids", "ip-only", "in", "not"))

    def cmd_show_tasks(self, body, headers):
        return self.generic_collection("show-tasks", body, headers, ("initiator", "status", "from-date", "to-date"), container="tasks")

    def cmd_show_task(self, body, headers):
        err = self.check_params(body, {"task-id", "details-level"})
        if err:
            return err
        ids = body.get("task-id")
        ids = ids if isinstance(ids, list) else [ids]
        tasks = [t for t in self.estate.tasks if t["task-id"] in ids]
        if not tasks:
            return error(404, "generic_err_object_not_found", f"Requested object [{ids[0]}] not found")
        return 200, {"tasks": tasks}

    def cmd_show_changes(self, body, headers):
        err = self.check_params(body, {"from-date", "from-session", "limit", "offset", "to-date", "to-session", "dereference-group-members",
                                       "show-membership", "dereference-max-depth", "details-level"})
        if err:
            return err
        changes = list(self.estate.changes)
        for key, after in (("from-date", True), ("to-date", False)):
            if key in body:
                try:
                    bound = datetime.fromisoformat(str(body[key]).replace("Z", "+00:00"))
                except ValueError:
                    return error(400, "generic_err_invalid_parameter", f"Invalid parameter for [{key}]. Expected ISO 8601")
                if bound.tzinfo is None:
                    bound = bound.replace(tzinfo=timezone.utc)
                changes = [c for c in changes if (c["session"]["publish-time"]["posix"] / 1000 >= bound.timestamp()) == after]
        if "to-session" in body:
            uids = [c["session"]["session-uid"] for c in changes]
            if body["to-session"] not in uids:
                return error(404, "generic_err_object_not_found", f"Requested object [{body['to-session']}] not found")
            changes = changes[uids.index(body["to-session"]):]
        limit, offset = body.get("limit", 10), body.get("offset", 0)
        changes = changes[offset:offset + limit]
        task_id = str(uuid.uuid5(NS, "show-changes:" + json.dumps(body, sort_keys=True)))
        return 200, {"tasks": [{"task-name": "Show Changes", "task-id": task_id, "status": "succeeded", "progress-percentage": 100,
                                "progress-description": "Operation Complete", "suppressed": False,
                                "task-details": [{"changes": changes, "limit": limit, "offset": offset}]}]}

    # ---- rulebases
    def rulebase_reply(self, body, headers, layers, layer_key, rule_types, extra_params):
        err = self.check_params(body, {"name", "uid", "package", "filter", "filter-settings", "limit", "offset", "order", "details-level",
                                       "use-object-dictionary", "show-as-ranges", "show-hits", "hits-settings", "dereference-group-members",
                                       "show-membership", "show-expiration-settings", *extra_params})
        if err:
            return err
        paging, err = self.paging(body)
        if err:
            return err
        limit, offset, level = paging
        key = body.get(layer_key) if layer_key == "package" else body.get("name") or body.get("uid")
        if key is None:
            return error(400, "generic_err_missing_required_parameters", f"Missing required parameters: [{layer_key if layer_key == 'package' else 'name or uid'}]")
        layer = layers.get(key) if key in layers else next((v for k, v in layers.items() if (self.estate.layers.get(k) or self.estate.packages.get(k) or {}).get("uid") == key), None)
        if layer is None:
            return error(404, "generic_err_object_not_found", f"Requested object [{key}] not found")
        if "package" in body and layer_key != "package" and body["package"] not in self.estate.packages:
            return error(404, "generic_err_object_not_found", f"Requested object [{body['package']}] not found")
        use_dict = body.get("use-object-dictionary", True)
        show_hits = body.get("show-hits", False)
        text = body.get("filter")
        if text is not None and not isinstance(text, str):
            return error(400, "generic_err_invalid_parameter", "Invalid parameter for [filter]")
        flat = []  # (section or None, rule)
        for item in layer:
            if "rulebase" in item:
                for r in item["rulebase"]:
                    flat.append((item, r))
            else:
                flat.append((None, item))
        if text:
            def matches(rule):
                hay = [rule.get("name", ""), rule.get("comments", ""), rule["uid"]]
                for k in ("source", "destination", "service", "original-source", "original-destination", "original-service", "protected-scope"):
                    for ref in rule.get(k, []) if isinstance(rule.get(k), list) else [rule.get(k)] if rule.get(k) else []:
                        o = self.estate.objects.get(ref, {})
                        hay += [o.get("name", ""), o.get("ipv4-address", ""), o.get("subnet4", "")]
                        if o.get("type") == "group":
                            hay += [m.get("name", "") + " " + m.get("ipv4-address", "") for m in o.get("members", [])]
                        if o.get("type") == "CpmiAnyObject":
                            hay.append("any")
                return text_match({"name": " ".join(hay)}, text)
            flat = [(s, r) for s, r in flat if matches(r)]
        total = len(flat)
        page = flat[offset:offset + limit]
        dictionary = {}

        def render_ref(uid):
            o = self.estate.objects.get(uid)
            if o is None:
                return uid
            dictionary[uid] = self.estate.ref(o["uid"])
            return uid if use_dict else self.estate.ref(o["uid"])

        def render_rule(rule):
            out = {}
            for k, v in rule.items():
                if k in ("source", "destination", "service", "vpn", "content", "time", "install-on", "protected-scope", "exceptions"):
                    out[k] = [render_ref(x) for x in v]
                elif k in ("action", "original-source", "original-destination", "original-service", "translated-source",
                           "translated-destination", "translated-service"):
                    out[k] = render_ref(v)
                elif k == "track" and isinstance(v, dict):
                    out[k] = {**v, "type": render_ref(v["type"])}
                elif k == "track":
                    out[k] = render_ref(v)
                elif k == "hits":
                    if show_hits:
                        out[k] = v
                else:
                    out[k] = v
            if level == "uid":
                return rule["uid"]
            if body.get("show-as-ranges") and "source" in rule:
                for col in ("source", "destination"):
                    ipv4, others = [], []
                    for uid in rule[col]:
                        for start, end, other in self.expand(uid):
                            (others.append(render_ref(other)) if other else ipv4.append({"start": start, "end": end}))
                    out[col + "-ranges"] = {"ipv4": ipv4, "ipv6": [], "others": others, "excluded-others": []}
                    out.pop(col, None)
                    out.pop(col + "-negate", None)
            return out

        rendered, current_section, rows = [], None, None
        for section, rule in page:
            if section is None:
                rendered.append(render_rule(rule))
                current_section = None
                continue
            if current_section is None or current_section["uid"] != section["uid"]:
                rows = []
                current_section = {"uid": section["uid"], "name": section["name"], "type": section["type"], "from": rule["rule-number"],
                                   "to": rule["rule-number"], "rulebase": rows}
                rendered.append(current_section)
            rows.append(render_rule(rule))
            current_section["to"] = rule["rule-number"]
        layer_obj = self.estate.layers.get(key) or self.estate.packages.get(key) or self.estate.threat_layer
        reply = {"from": offset + 1 if page else 0, "to": offset + len(page) if page else 0, "total": total,
                 "name": layer_obj["name"], "uid": layer_obj["uid"] if layer_key != "package" else uid_for("nat-layer", key), "rulebase": rendered}
        if use_dict:
            reply["objects-dictionary"] = sorted(dictionary.values(), key=lambda o: o["name"].lower())
        return 200, reply

    def cmd_show_access_rulebase(self, body, headers):
        return self.rulebase_reply(body, headers, self.estate.rulebases, "name", ("access-rule",), ())

    def cmd_show_nat_rulebase(self, body, headers):
        return self.rulebase_reply(body, headers, self.estate.nat_rulebases, "package", ("nat-rule",), ())

    def cmd_show_threat_rulebase(self, body, headers):
        return self.rulebase_reply(body, headers, self.estate.threat_rulebases, "name", ("threat-rule",), ())

    # ---- logs
    def cmd_show_logs(self, body, headers):
        err = self.check_params(body, {"new-query", "query-id", "ignore-warnings"})
        if err:
            return err
        if "query-id" in body:
            with self.lock:
                q = self.queries.get(body["query-id"])
            if q is None:
                return error(400, "generic_err_invalid_parameter", f"Query id [{body['query-id']}] not found or expired")
            return self.log_page(body["query-id"], q)
        nq = body.get("new-query")
        if not isinstance(nq, dict):
            return error(400, "generic_err_missing_required_parameters", "Missing required parameters: [new-query or query-id]")
        err = self.check_params(nq, {"filter", "time-frame", "custom-start", "custom-end", "max-logs-per-request", "top", "type", "log-servers"})
        if err:
            return err
        frame = nq.get("time-frame", "last-7-days")
        frames = {"last-hour": timedelta(hours=1), "today": None, "last-24-hours": timedelta(hours=24), "yesterday": None,
                  "this-week": timedelta(days=7), "this-month": timedelta(days=31), "last-7-days": timedelta(days=7),
                  "last-30-days": timedelta(days=30), "all-time": timedelta(days=36500), "custom": None}
        if frame not in frames:
            return error(400, "generic_err_invalid_parameter", "Invalid parameter for [time-frame]")
        now = datetime.now(timezone.utc)
        if frame == "custom":
            try:
                start = datetime.fromisoformat(str(nq.get("custom-start", "")).replace("Z", "+00:00"))
                end = datetime.fromisoformat(str(nq.get("custom-end", "")).replace("Z", "+00:00"))
            except ValueError:
                return error(400, "generic_err_invalid_parameter", "Invalid parameter for [custom-start/custom-end]. Expected ISO 8601")
            start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
            end = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
        elif frame == "today":
            start, end = now.replace(hour=0, minute=0, second=0, microsecond=0), now
        elif frame == "yesterday":
            end = now.replace(hour=0, minute=0, second=0, microsecond=0)
            start = end - timedelta(days=1)
        else:
            start, end = now - frames[frame], now
        kind = nq.get("type", "logs")
        if kind not in ("logs", "audit"):
            return error(400, "generic_err_invalid_parameter", "Invalid parameter for [type]. Expected one of: logs, audit")
        size = nq.get("max-logs-per-request", 100)
        if isinstance(size, str) and size.isdigit():
            size = int(size)
        if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= 100:
            return error(400, "generic_err_invalid_parameter", "Invalid parameter for [max-logs-per-request]. Expected an integer from 1 to 100")
        try:
            predicate = LogQuery(nq.get("filter", "")).parse()
        except ValueError:
            return error(400, "err_validation_failed", "Validation failed with 1 error",
                         errors=[{"message": "Invalid log query filter syntax"}])
        source = self.estate.audit_logs if kind == "audit" else self.estate.logs
        selected = [r for r in source if start <= datetime.strptime(r["time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) <= end and predicate(r)]
        if "top" in nq:
            field = (nq["top"] or {}).get("field", "blades")
            key = {"sources": "src", "destinations": "dst", "services": "service", "actions": "action", "blades": "blade",
                   "origins": "origin", "users": "user", "applications": "appi_name"}.get(field)
            if key is None:
                return error(400, "generic_err_invalid_parameter", "Invalid parameter for [top.field]")
            counts = {}
            for r in selected:
                counts[r.get(key, "")] = counts.get(r.get(key, ""), 0) + 1
            top = sorted(counts.items(), key=lambda kv: -kv[1])[: int((nq["top"] or {}).get("count", 10))]
            return 200, {"tops": [{k: str(v)} for k, v in top], "query-id": "WEB_API_" + str(uuid.uuid4()), "tops-count": str(len(selected))}
        qid = "WEB_API_" + str(uuid.uuid4())
        with self.lock:
            self.queries[qid] = {"rows": selected, "offset": 0, "size": size}
        return self.log_page(qid, self.queries[qid])

    def log_page(self, qid, q):
        with self.lock:
            page = q["rows"][q["offset"]:q["offset"] + q["size"]]
            q["offset"] += len(page)
            done = q["offset"] >= len(q["rows"])
        return 200, {"logs": page, "logs-count": str(len(page)), "query-id": qid, "query-status": "done" if done else "in progress"}


class Handler(BaseHTTPRequestHandler):
    server_version = "CheckPointSimulator/1.9.1"
    api: Server = None

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
        self.reply(405, {"code": "generic_err_invalid_syntax", "message": "Request method 'GET' not supported; the Management API accepts POST only"})

    def do_POST(self):
        match = re.fullmatch(r"/web_api(?:/v([0-9.]+))?/([A-Za-z0-9_-]+)/?", self.path.split("?")[0])
        if not match:
            return self.reply(404, {"code": "generic_err_command_not_found", "message": f"Command [{self.path}] not found"})
        version, command = match.group(1), match.group(2)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if raw and ctype and ctype != "application/json":
            return self.reply(400, {"code": "generic_err_invalid_header", "message": "Invalid header [Content-Type]: expected application/json"})
        try:
            body = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return self.reply(400, {"code": "generic_err_invalid_syntax", "message": "Payload is not a valid JSON"})
        if not isinstance(body, dict):
            return self.reply(400, {"code": "generic_err_invalid_syntax", "message": "Payload must be a JSON object"})
        headers = {"X-chkp-sid": self.headers.get("X-chkp-sid"), "_client_ip": self.client_address[0],
                   "_url": f"http://{self.headers.get('Host', '127.0.0.1')}/web_api" + (f"/v{version}" if version else "")}
        status, reply = self.api.handle(version, command, headers, body)
        self.reply(status, reply)


def make_server(port=0, env=None, domain_name="SMC User"):
    api = Server(env, domain_name)
    handler = type("BoundHandler", (Handler,), {"api": api})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    return httpd, api


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=18092, help="loopback TCP port (default 18092)")
    p.add_argument("--domain", default="SMC User", help="name of the single domain the simulator serves (default 'SMC User')")
    a = p.parse_args(argv)
    httpd, _ = make_server(a.port, os.environ, a.domain)
    print(f"Synthetic Check Point Management API listening on http://127.0.0.1:{httpd.server_address[1]}/web_api (API {API_VERSION})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
