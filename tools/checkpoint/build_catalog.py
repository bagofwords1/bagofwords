#!/usr/bin/env python3
"""Regenerate backend/app/data_sources/clients/checkpoint/catalog.json.

The catalog is the frozen allow-list of Management API ``show-*`` commands the
connector may issue, with the reply fields it flattens into columns. Field
names and types come from the official Management API reference data
(data/v1.9.1/dynamic/apis.json, see tools/checkpoint/SOURCES.md). Edit this
file, run it, and review the diff; the client never expands the surface at
runtime.
"""
import hashlib
import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "backend/app/data_sources/clients/checkpoint/catalog.json"

S, INT, N, B, J = "string", "integer", "number", "boolean", "json"


def f(**fields):
    return {k: {"type": t, "description": d} for k, (t, d) in fields.items()}


DATE = lambda prefix, what: f(**{  # noqa: E731
    f"{prefix}.iso-8601": (S, f"{what} in ISO 8601 (management server timezone)."),
    f"{prefix}.posix": (INT, f"{what} as milliseconds since the Unix epoch."),
})

META = {
    **f(**{
        "domain.uid": (S, "UID of the domain holding the object."),
        "domain.name": (S, "Name of the domain holding the object (SMC User on a single-domain server)."),
        "domain.domain-type": (S, "domain or data domain (Check Point predefined data)."),
        "comments": (S, "Object comments."),
        "color": (S, "Object color."),
        "icon": (S, "Object icon path."),
        "tags": (J, "Tag objects (uid/name/type) attached to the object."),
        "read-only": (B, "True when the object cannot be edited by the current session."),
        "meta-info.lock": (S, "unlocked, locked by current session or locked by other session."),
        "meta-info.validation-state": (S, "Validation state (ok, warning, error)."),
        "meta-info.last-modifier": (S, "Administrator who last modified the object."),
        "meta-info.creator": (S, "Administrator who created the object."),
    }),
    **DATE("meta-info.last-modify-time", "Last modification time"),
    **DATE("meta-info.creation-time", "Creation time"),
}

IDENT = f(**{
    "uid": (S, "Object unique identifier."),
    "name": (S, "Object name, unique in the domain."),
    "type": (S, "Object type as reported by the API."),
})

NAT = f(**{
    "nat-settings.auto-rule": (B, "Automatic NAT rule generation enabled."),
    "nat-settings.method": (S, "NAT method (hide or static) when auto-rule is on."),
    "nat-settings.hide-behind": (S, "gateway or ip-address for hide NAT."),
    "nat-settings.ipv4-address": (S, "Translated IPv4 address for automatic NAT."),
    "nat-settings.install-on": (S, "Gateway that applies the automatic NAT."),
})

GROUPS = f(**{"groups": (J, "Groups this object is a member of (requires show-membership; may be empty).")})

OBJECT_CONTROLS = ["search", "order_by"]

SERVICE_COMMON = f(**{
    "port": (S, "Destination port or port range, for example 443 or 1024-65535."),
    "source-port": (S, "Source port restriction, empty when any."),
    "protocol": (S, "Protocol signature associated with the service."),
    "session-timeout": (INT, "Virtual session timeout in seconds."),
    "use-default-session-timeout": (B, "Whether the global default timeout applies."),
    "match-for-any": (B, "Service participates in rules whose service column is Any."),
    "match-by-protocol-signature": (B, "Match by protocol signature instead of port only."),
    "keep-connections-open-after-policy-installation": (B, "Keep established connections after policy install."),
    "sync-connections-on-cluster": (B, "Synchronize connections between cluster members."),
    "aggressive-aging.enable": (B, "Aggressive aging enabled."),
    "aggressive-aging.timeout": (INT, "Aggressive aging timeout in seconds."),
    "aggressive-aging.use-default-timeout": (B, "Aggressive aging uses the default timeout."),
    "aggressive-aging.default-timeout": (INT, "Aggressive aging default timeout in seconds."),
})

RULE_META = {
    **f(**{
        "layer": (S, "Access layer name queried (explicit parent)."),
        "layer.uid": (S, "UID of the queried layer."),
        "section": (S, "Section name the rule belongs to; empty for rules under the global section."),
        "section.uid": (S, "Section UID."),
        "rule-number": (INT, "Position of the rule in the layer (1-based)."),
        "uid": (S, "Rule UID."),
        "name": (S, "Rule name (may be empty)."),
        "type": (S, "access-rule."),
        "enabled": (B, "False for disabled rules; disabled rules never match traffic."),
        "action": (S, "Accept, Drop, Reject, Ask, Inform, Apply Layer, ... resolved from the objects dictionary."),
        "action.uid": (S, "Action object UID."),
        "source": (J, "Source object names resolved through the objects dictionary."),
        "source.uids": (J, "Source object UIDs."),
        "source.types": (J, "Source object types (host, network, group, CpmiAnyObject, ...)."),
        "source-negate": (B, "Source column negated."),
        "destination": (J, "Destination object names."),
        "destination.uids": (J, "Destination object UIDs."),
        "destination.types": (J, "Destination object types."),
        "destination-negate": (B, "Destination column negated."),
        "service": (J, "Service & application object names."),
        "service.uids": (J, "Service object UIDs."),
        "service.types": (J, "Service object types."),
        "service-negate": (B, "Service column negated."),
        "vpn": (J, "VPN column object names."),
        "content": (J, "Content (data) column object names."),
        "content-negate": (B, "Content column negated."),
        "content-direction": (S, "Direction the content inspection applies to."),
        "time": (J, "Time object names."),
        "track": (S, "Track setting name (None, Log, Detailed Log, Extended Log, Alert, ...)."),
        "track.uid": (S, "Track object UID."),
        "track-alert": (S, "Alert type when track is an alert."),
        "install-on": (J, "Install-on target names (Policy Targets means every package target)."),
        "inline-layer": (S, "Name of the inline layer applied by this rule, if any."),
        "inline-layer.uid": (S, "Inline layer UID."),
        "comments": (S, "Rule comments."),
        "custom-fields.field-1": (S, "Custom field 1."),
        "custom-fields.field-2": (S, "Custom field 2."),
        "custom-fields.field-3": (S, "Custom field 3."),
        "hits.value": (INT, "Hit count (only when hits are returned by the server)."),
        "hits.level": (S, "Hit level (zero, low, medium, high, very high)."),
        "hits.percentage": (S, "Share of hits relative to the layer."),
    }),
    **DATE("hits.first-date", "First hit"),
    **DATE("hits.last-date", "Last hit"),
    **f(**{
        "domain.name": (S, "Domain holding the rule."),
        "meta-info.lock": (S, "Lock state."),
        "meta-info.last-modifier": (S, "Last modifier."),
        "meta-info.creator": (S, "Creator."),
    }),
    **DATE("meta-info.last-modify-time", "Last modification time"),
    **DATE("meta-info.creation-time", "Creation time"),
}

TABLES = {}


def table(name, command, description, fields, *, kind="objects", container="objects", details_level="full",
          controls=None, parents=None, order_fields=("name",), extra_body=None, notes=None, **more):
    TABLES[name] = {
        "command": command,
        "kind": kind,
        "container": container,
        "details_level": details_level,
        "description": description,
        "fields": fields,
        "controls": list(controls or []),
        "parents": list(parents or []),
        "order_fields": list(order_fields),
        "extra_body": extra_body or {},
        "notes": notes or "",
        **more,
    }


table("hosts", "show-hosts", "Host objects (single IP addresses) in the domain.",
      {**IDENT, **f(**{"ipv4-address": (S, "IPv4 address."), "ipv6-address": (S, "IPv6 address.")}), **NAT,
       **f(**{"interfaces": (J, "Host interfaces (name, subnet4, mask-length4)."),
              "host-servers.dns-server": (B, "Host marked as DNS server."),
              "host-servers.mail-server": (B, "Host marked as mail server."),
              "host-servers.web-server": (B, "Host marked as web server.")}), **GROUPS, **META},
      controls=OBJECT_CONTROLS, extra_body={"show-membership": True})

table("networks", "show-networks", "Network objects (subnets) in the domain.",
      {**IDENT, **f(**{"subnet4": (S, "IPv4 network address."), "subnet-mask": (S, "IPv4 network mask."),
                       "mask-length4": (INT, "IPv4 prefix length."), "subnet6": (S, "IPv6 network address."),
                       "mask-length6": (INT, "IPv6 prefix length."), "broadcast": (S, "allow or disallow broadcast address inclusion.")}),
       **NAT, **GROUPS, **META}, controls=OBJECT_CONTROLS, extra_body={"show-membership": True})

table("address_ranges", "show-address-ranges", "Address range objects (first-last IP) in the domain.",
      {**IDENT, **f(**{"ipv4-address-first": (S, "First IPv4 address."), "ipv4-address-last": (S, "Last IPv4 address."),
                       "ipv6-address-first": (S, "First IPv6 address."), "ipv6-address-last": (S, "Last IPv6 address.")}),
       **NAT, **GROUPS, **META}, controls=OBJECT_CONTROLS, extra_body={"show-membership": True})

table("groups", "show-groups", "Network object groups with their direct members (nested groups stay as group members).",
      {**IDENT, **f(**{"members": (J, "Direct members (uid, name, type, ipv4-address/subnet4 when the member carries one)."),
                       "member-count": (INT, "Number of direct members (derived).")}), **GROUPS, **META},
      controls=OBJECT_CONTROLS, extra_body={"dereference-group-members": True, "show-membership": True})

table("group_members", "show-groups", "One row per (group, direct member): flattened membership for joins with hosts/networks/rules.",
      f(**{"group.uid": (S, "Group UID."), "group.name": (S, "Group name."), "member.uid": (S, "Member UID."),
           "member.name": (S, "Member name."), "member.type": (S, "Member type (host, network, group, address-range, ...)."),
           "member.ipv4-address": (S, "Member IPv4 address when it is a host."),
           "member.subnet4": (S, "Member subnet when it is a network."),
           "member.mask-length4": (INT, "Member prefix length when it is a network."),
           "member.ipv4-address-first": (S, "First address when the member is an address range."),
           "member.ipv4-address-last": (S, "Last address when the member is an address range.")}),
      kind="group_members", controls=OBJECT_CONTROLS, extra_body={"dereference-group-members": True})

table("services_tcp", "show-services-tcp", "TCP service objects.", {**IDENT, **SERVICE_COMMON, **GROUPS, **META},
      controls=OBJECT_CONTROLS, extra_body={"show-membership": True})
table("services_udp", "show-services-udp", "UDP service objects.",
      {**IDENT, **SERVICE_COMMON, **f(**{"accept-replies": (B, "Accept UDP replies for the connection.")}), **GROUPS, **META},
      controls=OBJECT_CONTROLS, extra_body={"show-membership": True})
table("service_groups", "show-service-groups", "Service groups with their direct members.",
      {**IDENT, **f(**{"members": (J, "Direct member services (uid, name, type, port)."),
                       "member-count": (INT, "Number of direct members (derived).")}), **GROUPS, **META},
      controls=OBJECT_CONTROLS, extra_body={"dereference-group-members": True, "show-membership": True})

table("packages", "show-packages", "Policy packages: which layers they contain and where they are installed.",
      {**IDENT, **f(**{"access": (B, "Access control policy enabled."), "access-layers": (J, "Access layers (uid, name) in order."),
                       "threat-prevention": (B, "Threat prevention policy enabled."), "threat-layers": (J, "Threat layers (uid, name)."),
                       "nat-policy": (B, "NAT policy enabled."), "qos": (B, "QoS policy enabled."),
                       "desktop-security": (B, "Desktop security policy enabled."),
                       "https-inspection-policy": (B, "HTTPS inspection policy enabled."),
                       "vpn-traditional-mode": (B, "VPN traditional mode."),
                       "installation-targets": (J, "Installation targets: the string all or a list of gateway objects."),
                       "installation-targets-revision": (J, "Per target: target-name, target-uid and the revision installed.")}), **META},
      container="packages", controls=OBJECT_CONTROLS)

table("access_layers", "show-access-layers", "Access control layers (ordered rulebases referenced by packages).",
      {**IDENT, **f(**{"firewall": (B, "Firewall blade enabled on the layer."),
                       "applications-and-url-filtering": (B, "Application & URL filtering enabled."),
                       "content-awareness": (B, "Content awareness enabled."), "mobile-access": (B, "Mobile access enabled."),
                       "shared": (B, "Layer shared between packages."), "parent-layer": (S, "Parent layer for inline layers."),
                       "implicit-cleanup-action": (S, "drop or accept for traffic matching no rule."),
                       "detect-using-x-forward-for": (B, "Use X-Forwarded-For header.")}), **META},
      container="access-layers", controls=OBJECT_CONTROLS)

table("access_rules", "show-access-rulebase",
      "Flattened access rulebase of one layer: sections, rule numbers and names resolved through the objects dictionary.",
      RULE_META, kind="rulebase", container="rulebase", details_level="standard",
      controls=["search", "package", "show_hits"], parents=["layer"],
      extra_body={"use-object-dictionary": True, "show-as-ranges": False},
      notes="Rules are returned in rulebase order (rule-number ascending). Rule numbers restart inside inline layers; "
            "query an inline layer by its own name. search maps to the server-side rulebase filter.")

table("nat_rules", "show-nat-rulebase", "Flattened NAT rulebase of one package (manual and automatically generated rules).",
      {**f(**{"package": (S, "Policy package queried (explicit parent)."), "rulebase.uid": (S, "NAT layer UID."),
              "section": (S, "Section name (Manual Lower Rules, Automatic Generated Rules : ...); empty for global rules."),
              "section.uid": (S, "Section UID."), "rule-number": (INT, "Position in the NAT rulebase."),
              "uid": (S, "Rule UID."), "name": (S, "Rule name."), "type": (S, "nat-rule / rule."),
              "enabled": (B, "Rule enabled."), "auto-generated": (B, "Generated from object NAT settings."),
              "method": (S, "static, hide or nat64/nat46."),
              "original-source": (S, "Original source name."), "original-source.uid": (S, "Original source UID."),
              "original-destination": (S, "Original destination name."), "original-destination.uid": (S, "Original destination UID."),
              "original-service": (S, "Original service name."), "original-service.uid": (S, "Original service UID."),
              "translated-source": (S, "Translated source name (Original = unchanged)."), "translated-source.uid": (S, "Translated source UID."),
              "translated-destination": (S, "Translated destination name."), "translated-destination.uid": (S, "Translated destination UID."),
              "translated-service": (S, "Translated service name."), "translated-service.uid": (S, "Translated service UID."),
              "install-on": (J, "Install-on target names."), "comments": (S, "Rule comments."),
              "domain.name": (S, "Domain holding the rule."), "meta-info.lock": (S, "Lock state."),
              "meta-info.last-modifier": (S, "Last modifier."), "meta-info.creator": (S, "Creator.")}),
       **DATE("meta-info.last-modify-time", "Last modification time"), **DATE("meta-info.creation-time", "Creation time")},
      kind="rulebase", container="rulebase", details_level="standard", controls=["search"], parents=["package"],
      extra_body={"use-object-dictionary": True})

table("threat_rules", "show-threat-rulebase", "Flattened threat prevention rulebase of one layer.",
      {**f(**{"layer": (S, "Threat layer name queried (explicit parent)."), "layer.uid": (S, "Layer UID."),
              "section": (S, "Section name."), "section.uid": (S, "Section UID."), "rule-number": (INT, "Position in the layer."),
              "uid": (S, "Rule UID."), "name": (S, "Rule name."), "type": (S, "threat-rule / rule."), "enabled": (B, "Rule enabled."),
              "action": (S, "Threat profile name applied."), "action.uid": (S, "Profile UID."),
              "protected-scope": (J, "Protected scope object names."), "protected-scope-negate": (B, "Protected scope negated."),
              "source": (J, "Source object names."), "source-negate": (B, "Source negated."),
              "destination": (J, "Destination object names."), "destination-negate": (B, "Destination negated."),
              "service": (J, "Service object names."), "service-negate": (B, "Service negated."),
              "track": (S, "Track setting name."), "track-settings.packet-capture": (B, "Packet capture enabled."),
              "install-on": (J, "Install-on target names."), "exceptions": (J, "Exception rules (uid, name) attached."),
              "comments": (S, "Rule comments."), "domain.name": (S, "Domain."), "meta-info.last-modifier": (S, "Last modifier.")}),
       **DATE("meta-info.last-modify-time", "Last modification time")},
      kind="rulebase", container="rulebase", details_level="standard", controls=["search", "package"], parents=["layer"],
      extra_body={"use-object-dictionary": True})

BLADES = ["firewall", "ips", "anti-bot", "anti-virus", "application-control", "url-filtering", "identity-awareness",
          "content-awareness", "threat-emulation", "threat-extraction", "site-to-site-vpn", "mobile-access", "monitoring",
          "qos", "data-loss-prevention", "anti-spam", "zero-phishing"]
MGMT_BLADES = ["network-policy-management", "logging-and-status", "smart-event-server", "smart-event-correlation",
               "identity-logging", "compliance", "secondary", "monitoring", "endpoint-policy", "identity-awareness"]

table("gateways_and_servers", "show-gateways-and-servers",
      "Every Check Point gateway, cluster, cluster member and management/log server with version, blades and installed policy.",
      {**IDENT, **f(**{"ipv4-address": (S, "Main IPv4 address."), "ipv6-address": (S, "Main IPv6 address."),
                       "version": (S, "Software version (for example R81.20)."), "operating-system": (S, "Operating system (Gaia)."),
                       "hardware": (S, "Appliance model or Open server."), "sic-status": (S, "communicating, uninitialized, ..."),
                       "cluster-member-names": (J, "Member names for clusters."),
                       "policy.access-policy-installed": (B, "An access policy is installed."),
                       "policy.access-policy-name": (S, "Installed access policy package."),
                       "policy.threat-policy-installed": (B, "A threat policy is installed."),
                       "policy.threat-policy-name": (S, "Installed threat policy package."),
                       "policy.access-policy-revision": (J, "Revision object of the installed access policy."),
                       "interfaces": (J, "Interfaces (interface-name, ipv4-address, ipv4-mask-length, topology)."),
                       "vpn-encryption-domain": (S, "VPN domain mode.")}),
       **DATE("policy.access-policy-installation-date", "Access policy installation time"),
       **DATE("policy.threat-policy-installation-date", "Threat policy installation time"),
       **{f"network-security-blades.{b}": {"type": B, "description": f"{b} blade enabled."} for b in BLADES},
       **{f"management-blades.{b}": {"type": B, "description": f"Management blade {b} enabled."} for b in MGMT_BLADES},
       **GROUPS, **META},
      controls=["order_by"], notes="Blade flags are absent (null) when a blade is off; treat null as false only for Check Point gateways.")

table("simple_gateways", "show-simple-gateways", "Simple (single) gateway objects with blade flags and logging settings.",
      {**IDENT, **f(**{"ipv4-address": (S, "Main IPv4 address."), "ipv6-address": (S, "Main IPv6 address."),
                       "version": (S, "Gateway software version."), "os-name": (S, "Operating system."), "hardware": (S, "Hardware model."),
                       "sic-name": (S, "SIC DN."), "sic-state": (S, "SIC trust state."), "dynamic-ip": (B, "Dynamic IP address."),
                       "firewall": (B, "Firewall blade."), "vpn": (B, "IPsec VPN blade."), "ips": (B, "IPS blade."),
                       "anti-bot": (B, "Anti-Bot blade."), "anti-virus": (B, "Anti-Virus blade."),
                       "application-control": (B, "Application Control blade."), "url-filtering": (B, "URL Filtering blade."),
                       "threat-emulation": (B, "Threat Emulation blade."), "threat-extraction": (B, "Threat Extraction blade."),
                       "identity-awareness": (B, "Identity Awareness blade."), "content-awareness": (B, "Content Awareness blade."),
                       "monitoring": (B, "Monitoring blade."), "enable-https-inspection": (B, "HTTPS inspection enabled."),
                       "hit-count": (B, "Hit count tracking."), "save-logs-locally": (B, "Logs saved locally."),
                       "send-logs-to-server": (J, "Log servers."), "send-logs-to-backup-server": (J, "Backup log servers."),
                       "interfaces": (J, "Interfaces (name, ipv4-address, ipv4-mask-length, topology).")}), **GROUPS, **META},
      controls=OBJECT_CONTROLS)

table("sessions", "show-sessions", "Management sessions (open, published, expired) with locks and pending changes.",
      {**IDENT, **f(**{"user-name": (S, "Administrator who owns the session."), "state": (S, "open, published, discarded, ..."),
                       "application": (S, "SmartConsole, WEB_API, ..."), "changes": (INT, "Number of unpublished changes."),
                       "locks": (INT, "Number of objects locked by the session."), "ip-address": (S, "Client IP address."),
                       "in-work": (B, "Session currently connected."), "connection-mode": (S, "read write or read only."),
                       "read-only": (B, "Session opened read-only."), "session-timeout": (INT, "Idle timeout in seconds."),
                       "expired-session": (B, "Session expired."), "description": (S, "Session description."),
                       "email": (S, "Administrator email.")}),
       **DATE("last-login-time", "Last login"), **DATE("last-logout-time", "Last logout"), **DATE("publish-time", "Publish time"),
       **f(**{"domain.name": (S, "Domain."), "comments": (S, "Session comments.")})},
      controls=["search", "order_by", "view_published_sessions"],
      notes="Stale locks: state open, in-work false, locks > 0 and an old last-login-time.")

table("unused_objects", "show-unused-objects", "Objects not referenced by any rule or other object.",
      {**IDENT, **f(**{"ipv4-address": (S, "IPv4 address for hosts."), "subnet4": (S, "Subnet for networks."),
                       "mask-length4": (INT, "Prefix length for networks."), "port": (S, "Port for services.")}), **META},
      controls=OBJECT_CONTROLS)

table("tasks", "show-tasks", "Management tasks (policy installation, publish, show-changes, ...) with progress and status.",
      {**IDENT, **f(**{"task-id": (S, "Task UID used by show-task."), "task-name": (S, "Task name."),
                       "status": (S, "succeeded, failed, in progress, partially succeeded."),
                       "progress-percentage": (INT, "Progress 0-100."), "progress-description": (S, "Progress text."),
                       "suppressed": (B, "Task hidden from the UI."), "task-details": (J, "Task specific details.")}),
       **DATE("start-time", "Start time"), **DATE("last-update-time", "Last update"),
       **f(**{"domain.name": (S, "Domain.")})},
      container="tasks", details_level="full", controls=["status", "initiator", "lookback", "start_time", "end_time", "order_by"],
      order_fields=("name",))

LOG_FIELDS = f(**{
    "time": (S, "Log time in ISO 8601 (UTC)."), "id": (S, "Log record id."), "log_uid": (S, "Log UID."),
    "type": (S, "Log, Correlated, ..."), "blade": (S, "Blade (Firewall, IPS, VPN, Application Control, ...)."),
    "product": (S, "Product name."), "product_family": (S, "Network, Threat, Access, ..."),
    "action": (S, "Accept, Drop, Reject, Prevent, Detect, Key Install, ..."), "severity": (S, "Informational, Low, Medium, High, Critical."),
    "origin": (S, "Gateway or server that produced the log."), "orig": (S, "Origin IP."),
    "src": (S, "Source IP."), "dst": (S, "Destination IP."), "service": (S, "Destination port."), "s_port": (S, "Source port."),
    "proto": (S, "IP protocol number."), "service_id": (S, "Service object name."),
    "rule": (S, "Matched rule number."), "rule_name": (S, "Matched rule name."), "rule_uid": (S, "Matched rule UID."),
    "layer_name": (S, "Matched layer name."), "layer_uuid": (S, "Matched layer UID."),
    "policy_name": (S, "Policy package."), "policy": (S, "Policy name."), "policy_date": (S, "Policy installation date."),
    "i_f_name": (S, "Interface name."), "i_f_dir": (S, "inbound or outbound."), "conn_direction": (S, "Connection direction."),
    "src_user_name": (S, "Source user."), "dst_user_name": (S, "Destination user."), "user": (S, "User."),
    "bytes": (S, "Total bytes."), "sent_bytes": (S, "Bytes sent."), "received_bytes": (S, "Bytes received."),
    "description": (S, "Log description."), "calc_desc": (S, "Human readable summary."),
    "protection_name": (S, "Protection name (threat blades)."), "protection_type": (S, "Protection type."),
    "confidence_level": (S, "Threat confidence."), "verdict": (S, "Threat verdict."),
    "index_time": (S, "Index time."), "orig_log_server": (S, "Log server IP."), "sequencenum": (S, "Sequence number."),
    "marker": (S, "Log marker."),
})
AUDIT_FIELDS = f(**{
    "time": (S, "Audit time in ISO 8601 (UTC)."), "id": (S, "Record id."), "log_uid": (S, "Log UID."), "type": (S, "Log."),
    "blade": (S, "Blade (SmartConsole, WEB_API, Mgmt)."), "product": (S, "Product."), "action": (S, "Audit action."),
    "severity": (S, "Severity."), "origin": (S, "Management server."), "orig": (S, "Origin IP."),
    "administrator": (S, "Administrator name."), "user": (S, "User."), "client_ip": (S, "Client IP address."), "machine": (S, "Client machine."),
    "operation": (S, "Operation (Log In, Log Out, Publish, Modify Object, Install Policy, ...)."),
    "operation_number": (S, "Operation number."), "subject": (S, "Subject (Object Manipulation, Session Management, ...)."),
    "objectname": (S, "Object name."), "objecttype": (S, "Object type."), "objecttable": (S, "Object table."),
    "uid": (S, "Object UID."), "session_name": (S, "Session name."), "session_uid": (S, "Session UID."),
    "audit_status": (S, "Success or Failure."), "additional_info": (S, "Additional information."),
    "fieldschanges": (S, "Changed fields."), "application": (S, "Application."), "index_time": (S, "Index time."),
    "sequencenum": (S, "Sequence number."), "marker": (S, "Log marker."),
})
LOG_CONTROLS = ["search", "lookback", "start_time", "end_time", "time_frame", "log_servers"]
table("logs", "show-logs", "Traffic and threat logs from the log server (SmartView query language in search).", LOG_FIELDS,
      kind="logs", container="logs", log_type="logs", controls=LOG_CONTROLS, dynamic_fields=True,
      notes="Log records carry arbitrary extra keys; request them by name in fields. Values are strings as returned by the log server.")
table("audit_logs", "show-logs", "Management audit trail (logins, publishes, object changes, policy installs).", AUDIT_FIELDS,
      kind="logs", container="logs", log_type="audit", controls=LOG_CONTROLS, dynamic_fields=True)

table("api_versions", "show-api-versions", "API versions supported by the management server (one row per version).",
      f(**{"version": (S, "API version string."), "current": (B, "True for the server's latest version.")}),
      kind="api_versions", container="supported-versions", controls=[])

table("session", "show-session", "The connector's own read-only session as seen by the server (identity check).",
      {**IDENT, **f(**{"user-name": (S, "Logged in administrator."), "state": (S, "Session state."), "application": (S, "WEB_API."),
                       "ip-address": (S, "Client IP seen by the server."), "read-only": (B, "Must be true."),
                       "connection-mode": (S, "read only."), "session-timeout": (INT, "Idle timeout."), "changes": (INT, "Pending changes (0)."),
                       "locks": (INT, "Locks (0)."), "in-work": (B, "Connected."), "description": (S, "Session description."),
                       "domain.name": (S, "Domain."), "api-server-version": (S, "api-server-version from login."),
                       "login.url": (S, "Web API URL reported by login."), "login.standby": (B, "Management server is standby.")}),
       **DATE("last-login-time", "Last login")},
      kind="singleton", container="", controls=[])

table("last_published_session", "show-last-published-session", "The most recently published session (configuration revision).",
      {**IDENT, **f(**{"user-name": (S, "Publisher."), "state": (S, "published."), "application": (S, "Application."),
                       "description": (S, "Session description."), "changes": (INT, "Published changes."), "domain.name": (S, "Domain.")}),
       **DATE("publish-time", "Publish time"), **DATE("last-login-time", "Last login")},
      kind="singleton", container="", controls=[])

table("domains", "show-domains", "Domains (one on a Security Management Server, many on a Multi-Domain Server).",
      {**IDENT, **f(**{"domain-type": (S, "domain, global domain, ..."), "servers": (J, "Domain servers (name, ipv4-address, type, active)."),
                       "global-domain-assignments": (J, "Global domain assignments.")}), **META},
      controls=OBJECT_CONTROLS)

table("vpn_communities", "show-vpn-communities-meshed", "Meshed and star VPN communities with encryption settings and member gateways.",
      {**IDENT, **f(**{"community-type": (S, "meshed or star (derived from the command)."),
                       "encryption-method": (S, "IKE method."), "encryption-suite": (S, "Suite name or custom."),
                       "use-shared-secret": (B, "Pre-shared secret in use (value never returned)."),
                       "ike-phase-1.encryption-algorithm": (S, "Phase 1 cipher."), "ike-phase-1.data-integrity": (S, "Phase 1 integrity."),
                       "ike-phase-1.diffie-hellman-group": (S, "Phase 1 DH group."),
                       "ike-phase-2.encryption-algorithm": (S, "Phase 2 cipher."), "ike-phase-2.data-integrity": (S, "Phase 2 integrity."),
                       "gateways": (J, "Meshed member gateways."), "center-gateways": (J, "Star center gateways."),
                       "satellite-gateways": (J, "Star satellite gateways."), "mesh-center-gateways": (B, "Star centers meshed.")}),
       **META},
      kind="vpn_communities", controls=OBJECT_CONTROLS, commands=["show-vpn-communities-meshed", "show-vpn-communities-star"])

table("changes", "show-changes", "Published changes between sessions or dates: one row per added, modified or deleted object.",
      {**f(**{"session.session-uid": (S, "Publishing session UID."), "session.user-name": (S, "Publisher."),
              "session.domain-info.name": (S, "Domain."), "operation": (S, "added, modified or deleted."),
              "object.uid": (S, "Object UID."), "object.name": (S, "Object name (new name for modified)."),
              "object.type": (S, "Object type."), "old-object.name": (S, "Previous name for modified objects."),
              "task-id": (S, "show-changes task UID.")}), **DATE("session.publish-time", "Publish time")},
      kind="changes", container="tasks", controls=["lookback", "start_time", "end_time", "from_session", "to_session", "limit_sessions"],
      notes="show-changes runs as a task; the connector polls show-task until it completes (bounded by the query deadline).")

SESSION_COMMANDS = ["login", "logout", "keepalive", "show-session", "show-task"]
commands = sorted({c for t in TABLES.values() for c in t.get("commands", [t["command"]])} | set(SESSION_COMMANDS))
assert all(c.startswith("show-") or c in {"login", "logout", "keepalive"} for c in commands)

catalog = {
    "contract": "1.9.1",
    "source": "Check Point Management API reference (R81.20 JHF Take 43 = v1.9.1; verified against v2 / R82 data) — see tools/checkpoint/SOURCES.md",
    "allowed_commands": commands,
    "tables": TABLES,
}
body = json.dumps(catalog, indent=1, sort_keys=True)
catalog["sha256"] = hashlib.sha256(body.encode()).hexdigest()
OUT.write_text(json.dumps(catalog, indent=1, sort_keys=True) + "\n")
print(f"wrote {OUT} tables={len(TABLES)} commands={len(commands)} sha256={catalog['sha256'][:12]}")
