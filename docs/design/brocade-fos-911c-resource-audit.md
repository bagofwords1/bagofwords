# Brocade FOS resource coverage inventory

This inventory enumerates the public **9.1.0b baseline**, not verified 9.1.1c endpoints. It contains 177 direct list/container candidates across 35 resource-bearing modules, plus 32 RPC declarations across the 80 YANG files. Nested list views will change the eventual table count. All candidate paths need contract verification; module-version has a PyFOS-confirmed special path.

Do not use this document as an executable allowlist. Read the companion design for field exclusions, version and permission checks, and response semantics.

| Module | Resource candidate | Declared key | Expanded leaf paths | Source |
|---|---|---|---:|---|
| brocade-access-gateway | `port-group` | port-group-id | 6 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-access-gateway.yang#L88) |
| brocade-access-gateway | `n-port-map` | n-port | 16 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-access-gateway.yang#L181) |
| brocade-access-gateway | `f-port-list` | f-port | 5 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-access-gateway.yang#L377) |
| brocade-access-gateway | `policy` | None declared | 2 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-access-gateway.yang#L438) |
| brocade-access-gateway | `n-port-settings` | None declared | 1 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-access-gateway.yang#L475) |
| brocade-access-gateway | `device-list` | wwn | 4 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-access-gateway.yang#L494) |
| brocade-application-server | `application-server-device` | None declared | 7 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-application-server.yang#L174) |
| brocade-chassis | `chassis` | None declared | 27 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-chassis.yang#L229) |
| brocade-chassis | `ha-status` | None declared | 9 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-chassis.yang#L544) |
| brocade-chassis | `management-interface-configuration` | None declared | 6 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-chassis.yang#L654) |
| brocade-chassis | `management-ethernet-interface` | cp-name interface-name | 34 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-chassis.yang#L729) |
| brocade-chassis | `management-port-track-configuration` | None declared | 2 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-chassis.yang#L1153) |
| brocade-chassis | `management-port-connection-statistics` | None declared | 13 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-chassis.yang#L1180) |
| brocade-chassis | `credit-recovery` | None declared | 5 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-chassis.yang#L1197) |
| brocade-extension-ip-route | `extension-ip-route` | name dp-id ip-address ip-prefix-length | 6 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-extension-ip-route.yang#L74) |
| brocade-extension-ipsec-policy | `extension-ipsec-policy` | policy-name | 5 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-extension-ipsec-policy.yang#L111) |
| brocade-extension-tunnel | `extension-tunnel` | name | 45 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-extension-tunnel.yang#L99) |
| brocade-extension-tunnel | `extension-tunnel-statistics` | name | 12 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-extension-tunnel.yang#L800) |
| brocade-extension-tunnel | `extension-circuit` | name circuit-id | 32 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-extension-tunnel.yang#L895) |
| brocade-extension-tunnel | `extension-circuit-statistics` | name circuit-id | 12 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-extension-tunnel.yang#L1299) |
| brocade-extension-tunnel | `circuit-qos-statistics` | ve-port circuit-id ha-type priority | 22 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-extension-tunnel.yang#L1389) |
| brocade-extension-tunnel | `wan-statistics` | ve-port circuit-id connection-id | 48 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-extension-tunnel.yang#L1525) |
| brocade-extension | `dp-hcl-status` | slot dp-id | 9 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-extension.yang#L89) |
| brocade-extension | `traffic-control-list` | traffic-control-list-name | 23 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-extension.yang#L160) |
| brocade-extension | `lan-flow-statistics` | flow-index slot dp-id | 48 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-extension.yang#L465) |
| brocade-extension | `global-lan-statistics` | slot dp-id | 80 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-extension.yang#L877) |
| brocade-fabric-traffic-controller | `fabric-traffic-controller-device` | n-port-id | 39 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fabric-traffic-controller.yang#L508) |
| brocade-fabric-traffic-controller | `fabric-traffic-controller-ag-f-port` | port-index | 28 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fabric-traffic-controller.yang#L537) |
| brocade-fabric-traffic-controller | `fabric-traffic-controller-ag-n-port` | port-index | 4 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fabric-traffic-controller.yang#L580) |
| brocade-fabric | `fabric-switch` | name | 16 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fabric.yang#L99) |
| brocade-fabric | `access-gateway` | switch-wwn | 5 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fabric.yang#L229) |
| brocade-fdmi | `hba` | hba-id | 21 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fdmi.yang#L86) |
| brocade-fdmi | `port` | port-name | 35 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fdmi.yang#L367) |
| brocade-fibrechannel-configuration | `switch-configuration` | None declared | 5 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fibrechannel-configuration.yang#L85) |
| brocade-fibrechannel-configuration | `f-port-login-settings` | None declared | 7 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fibrechannel-configuration.yang#L185) |
| brocade-fibrechannel-configuration | `port-configuration` | None declared | 4 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fibrechannel-configuration.yang#L296) |
| brocade-fibrechannel-configuration | `zone-configuration` | None declared | 2 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fibrechannel-configuration.yang#L370) |
| brocade-fibrechannel-configuration | `fabric` | None declared | 4 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fibrechannel-configuration.yang#L399) |
| brocade-fibrechannel-configuration | `chassis-config-settings` | None declared | 9 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fibrechannel-configuration.yang#L474) |
| brocade-fibrechannel-diagnostics | `fibrechannel-diagnostics` | name | 47 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fibrechannel-diagnostics.yang#L316) |
| brocade-fibrechannel-logical-switch | `fibrechannel-logical-switch` | fabric-id | 9 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fibrechannel-logical-switch.yang#L73) |
| brocade-fibrechannel-routing | `routing-configuration` | None declared | 7 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fibrechannel-routing.yang#L148) |
| brocade-fibrechannel-routing | `lsan-zone` | None declared | 3 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fibrechannel-routing.yang#L273) |
| brocade-fibrechannel-routing | `lsan-device` | None declared | 6 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fibrechannel-routing.yang#L310) |
| brocade-fibrechannel-routing | `edge-fabric-alias` | edge-fabric-id | 2 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fibrechannel-routing.yang#L374) |
| brocade-fibrechannel-routing | `fibrechannel-router` | None declared | 5 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fibrechannel-routing.yang#L399) |
| brocade-fibrechannel-routing | `router-statistics` | None declared | 7 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fibrechannel-routing.yang#L460) |
| brocade-fibrechannel-routing | `proxy-config` | imported-fabric-id device-wwn | 3 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fibrechannel-routing.yang#L537) |
| brocade-fibrechannel-routing | `translate-domain-config` | imported-fabric-id exported-fabric-id | 6 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fibrechannel-routing.yang#L573) |
| brocade-fibrechannel-routing | `stale-translate-domain` | imported-fabric-id stale-translate-domain-id | 5 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fibrechannel-routing.yang#L646) |
| brocade-fibrechannel-switch | `fibrechannel-switch` | name | 32 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fibrechannel-switch.yang#L138) |
| brocade-fibrechannel-switch | `topology-domain` | domain-id | 5 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fibrechannel-switch.yang#L594) |
| brocade-fibrechannel-switch | `topology-route` | None declared | 4 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fibrechannel-switch.yang#L656) |
| brocade-fibrechannel-switch | `topology-error` | None declared | 2 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fibrechannel-switch.yang#L707) |
| brocade-fibrechannel-trunk | `trunk` | group source-port | 9 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fibrechannel-trunk.yang#L72) |
| brocade-fibrechannel-trunk | `performance` | group | 10 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fibrechannel-trunk.yang#L161) |
| brocade-fibrechannel-trunk | `trunk-area` | trunk-index | 4 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fibrechannel-trunk.yang#L244) |
| brocade-ficon | `cup` | None declared | 9 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-ficon.yang#L84) |
| brocade-ficon | `logical-path` | link-address channel-image-id | 4 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-ficon.yang#L218) |
| brocade-ficon | `rnid` | link-address | 13 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-ficon.yang#L280) |
| brocade-ficon | `switch-rnid` | switch-address | 10 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-ficon.yang#L401) |
| brocade-ficon | `lirr` | link-address | 6 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-ficon.yang#L495) |
| brocade-ficon | `rlir` | None declared | 29 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-ficon.yang#L561) |
| brocade-firmware | `firmware-history` | None declared | 6 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-firmware.yang#L63) |
| brocade-firmware | `firmware-config` | None declared | 1 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-firmware.yang#L137) |
| brocade-fru | `blade` | slot-number | 35 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fru.yang#L207) |
| brocade-fru | `fan` | unit-number | 16 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fru.yang#L613) |
| brocade-fru | `power-supply` | unit-number | 20 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fru.yang#L708) |
| brocade-fru | `history-log` | None declared | 6 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fru.yang#L843) |
| brocade-fru | `sensor` | id | 6 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fru.yang#L883) |
| brocade-fru | `wwn` | unit-number | 17 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fru.yang#L945) |
| brocade-interface | `fibrechannel` | name | 104 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-interface.yang#L414) |
| brocade-interface | `fibrechannel-statistics` | name | 74 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-interface.yang#L1912) |
| brocade-interface | `fibrechannel-router-statistics` | port-index | 3 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-interface.yang#L2576) |
| brocade-interface | `extension-ip-interface` | name dp-id ip-address | 7 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-interface.yang#L2611) |
| brocade-interface | `gigabitethernet` | name | 14 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-interface.yang#L2701) |
| brocade-interface | `gigabitethernet-statistics` | name | 20 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-interface.yang#L2882) |
| brocade-interface | `logical-e-port` | port-index | 6 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-interface.yang#L3023) |
| brocade-interface | `portchannel` | name | 11 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-interface.yang#L3152) |
| brocade-interface | `portchannel-statistics` | name | 18 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-interface.yang#L3287) |
| brocade-interface | `fibrechannel-lag` | name | 7 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-interface.yang#L3425) |
| brocade-license | `license` | name | 10 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-license.yang#L75) |
| brocade-license | `end-user-license-agreement` | None declared | 1 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-license.yang#L211) |
| brocade-license | `ports-on-demand-license-info` | None declared | 39 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-license.yang#L229) |
| brocade-lldp | `lldp-global` | None declared | 7 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-lldp.yang#L226) |
| brocade-lldp | `lldp-profile` | name | 4 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-lldp.yang#L303) |
| brocade-lldp | `lldp-neighbor` | slot-port | 6 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-lldp.yang#L355) |
| brocade-lldp | `lldp-statistics` | slot-port | 8 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-lldp.yang#L370) |
| brocade-logging | `audit` | None declared | 3 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-logging.yang#L96) |
| brocade-logging | `syslog-server` | server | 3 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-logging.yang#L138) |
| brocade-logging | `raslog` | message-id | 7 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-logging.yang#L178) |
| brocade-logging | `raslog-module` | module-id | 2 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-logging.yang#L260) |
| brocade-logging | `log-quiet-control` | log-type | 5 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-logging.yang#L287) |
| brocade-logging | `log-setting` | None declared | 3 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-logging.yang#L373) |
| brocade-logging | `supportftp` | None declared | 8 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-logging.yang#L433) |
| brocade-logging | `audit-log` | None declared | 15 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-logging.yang#L534) |
| brocade-logging | `error-log` | None declared | 10 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-logging.yang#L689) |
| brocade-logging | `management-session-login-information` | None declared | 9 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-logging.yang#L792) |
| brocade-management-ip-interface | `management-ip-interface` | None declared | 11 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-management-ip-interface.yang#L68) |
| brocade-management-ip-interface | `management-interface-lldp-neighbor` | cp-name physical-interface | 7 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-management-ip-interface.yang#L243) |
| brocade-management-ip-interface | `management-interface-lldp-statistics` | cp-name physical-interface | 9 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-management-ip-interface.yang#L269) |
| brocade-maps | `switch-status-policy-report` | None declared | 18 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-maps.yang#L131) |
| brocade-maps | `system-resources` | None declared | 4 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-maps.yang#L288) |
| brocade-maps | `paused-cfg` | group-type | 2 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-maps.yang#L338) |
| brocade-maps | `group` | name | 7 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-maps.yang#L418) |
| brocade-maps | `maps-config` | None declared | 11 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-maps.yang#L519) |
| brocade-maps | `dashboard-rule` | None declared | 6 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-maps.yang#L670) |
| brocade-maps | `dashboard-misc` | None declared | 2 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-maps.yang#L754) |
| brocade-maps | `credit-stall-dashboard` | None declared | 9 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-maps.yang#L783) |
| brocade-maps | `oversubscription-dashboard` | None declared | 4 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-maps.yang#L887) |
| brocade-maps | `rule` | name | 15 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-maps.yang#L937) |
| brocade-maps | `maps-policy` | name | 6 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-maps.yang#L1107) |
| brocade-maps | `monitoring-system-matrix` | monitoring-system group-type | 17 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-maps.yang#L1200) |
| brocade-maps | `fpi-profile` | name | 8 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-maps.yang#L1388) |
| brocade-maps | `dashboard-history` | None declared | 15 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-maps.yang#L1510) |
| brocade-media | `media-rdp` | name | 73 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-media.yang#L283) |
| brocade-module-version | `module` | None declared | 4 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-module-version.yang#L66) |
| brocade-name-server | `fibrechannel-name-server` | port-id | 27 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-name-server.yang#L114) |
| brocade-security | `ipfilter-policy` | name | 6 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L111) |
| brocade-security | `ipfilter-rule` | policy-name index | 9 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L193) |
| brocade-security | `user-specific-password-cfg` | user-name | 6 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L307) |
| brocade-security | `password-cfg` | None declared | 23 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L386) |
| brocade-security | `user-config` | name | 14 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L663) |
| brocade-security | `role-config` | name | 4 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L832) |
| brocade-security | `auth-spec` | None declared | 3 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L909) |
| brocade-security | `radius-server` | server | 7 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L1000) |
| brocade-security | `tacacs-server` | server | 7 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L1090) |
| brocade-security | `ldap-server` | server | 6 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L1174) |
| brocade-security | `ldap-role-map` | ldap-role | 4 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L1238) |
| brocade-security | `sec-crypto-cfg-template-action` | None declared | 7 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L1288) |
| brocade-security | `sec-crypto-cfg-template` | name | 2 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L1324) |
| brocade-security | `sec-crypto-cfg` | None declared | 16 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L1346) |
| brocade-security | `sshutil` | None declared | 2 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L1492) |
| brocade-security | `sshutil-known-host` | remote-host-name | 4 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L1520) |
| brocade-security | `sshutil-key` | algorithm-type key-type | 5 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L1569) |
| brocade-security | `sshutil-public-key` | user-name | 2 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L1625) |
| brocade-security | `sshutil-public-key-action` | None declared | 8 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L1653) |
| brocade-security | `password` | None declared | 3 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L1703) |
| brocade-security | `security-certificate-generate` | None declared | 16 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L1735) |
| brocade-security | `security-certificate-action` | None declared | 11 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L1820) |
| brocade-security | `security-certificate` | certificate-entity certificate-type | 5 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L1888) |
| brocade-security | `security-certificate-extension` | None declared | 7 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L1951) |
| brocade-security | `management-rbac-map` | None declared | 3 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L2031) |
| brocade-security | `acl-policy` | None declared | 2 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L2063) |
| brocade-security | `defined-fcs-policy-member-list` | None declared | 5 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L2103) |
| brocade-security | `defined-scc-policy-member-list` | None declared | 3 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L2140) |
| brocade-security | `defined-dcc-policy-member-list` | None declared | 6 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L2163) |
| brocade-security | `active-fcs-policy-member-list` | None declared | 5 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L2199) |
| brocade-security | `active-scc-policy-member-list` | None declared | 3 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L2229) |
| brocade-security | `active-dcc-policy-member-list` | None declared | 6 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L2241) |
| brocade-security | `policy-distribution-config` | None declared | 7 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L2263) |
| brocade-security | `security-policy-size` | None declared | 3 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L2334) |
| brocade-security | `security-violation-statistics` | domain-id | 9 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L2370) |
| brocade-security | `dh-chap-authentication-secret` | None declared | 4 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L2446) |
| brocade-security | `authentication-configuration` | None declared | 5 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L2488) |
| brocade-security | `rbac-class` | None declared | 5 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-security.yang#L2530) |
| brocade-snmp | `system` | None declared | 12 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-snmp.yang#L89) |
| brocade-snmp | `mib-capability` | mib-name | 2 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-snmp.yang#L213) |
| brocade-snmp | `trap-capability` | trap-name | 3 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-snmp.yang#L234) |
| brocade-snmp | `v1-account` | index | 3 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-snmp.yang#L269) |
| brocade-snmp | `v1-trap` | index | 4 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-snmp.yang#L302) |
| brocade-snmp | `v3-account` | index | 8 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-snmp.yang#L338) |
| brocade-snmp | `v3-trap` | trap-index | 6 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-snmp.yang#L430) |
| brocade-snmp | `access-control` | index | 3 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-snmp.yang#L485) |
| brocade-supportlink | `supportlink-profile` | None declared | 22 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-supportlink.yang#L83) |
| brocade-supportlink | `supportlink-history` | None declared | 6 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-supportlink.yang#L385) |
| brocade-time | `time-zone` | None declared | 3 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-time.yang#L61) |
| brocade-time | `clock-server` | None declared | 4 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-time.yang#L97) |
| brocade-time | `ntp-clock-server` | server | 2 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-time.yang#L133) |
| brocade-time | `ntp-clock-server-key` | index | 3 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-time.yang#L143) |
| brocade-traffic-optimizer | `performance-group-profile` | name | 7 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-traffic-optimizer.yang#L76) |
| brocade-traffic-optimizer | `performance-group` | identifier stats-duration io-category | 11 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-traffic-optimizer.yang#L155) |
| brocade-traffic-optimizer | `performance-group-flows` | None declared | 4 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-traffic-optimizer.yang#L256) |
| brocade-usb | `usb-file` | None declared | 4 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-usb.yang#L63) |
| brocade-zone | `defined-configuration` | None declared | 9 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-zone.yang#L169) |
| brocade-zone | `effective-configuration` | None declared | 15 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-zone.yang#L299) |
| brocade-zone | `fabric-lock` | None declared | 9 | [YANG](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-zone.yang#L517) |

RPC declarations are inventoried separately in the JSON. Login belongs to the internal authentication lifecycle; operational RPCs are outside the initial diagnostic query surface.

The JSON preserves all source leaf names for review, including leaves that must be excluded from the eventual connector. It intentionally is not a production schema.
