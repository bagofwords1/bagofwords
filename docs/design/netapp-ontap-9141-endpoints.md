# ONTAP 9.14.1 complete endpoint inventory

Source: [NetApp Swagger UI](https://docs.netapp.com/us-en/ontap-restapi-9141/swagger-ui/index.html). Retrieved 2026-09-13.

This is an exhaustive path/method inventory. The original MVP/phase-2/deferred labels below are historical prioritization, superseded by the [revised RCA connector plan](netapp-ontap-9141-connector-plan.md). They are not coverage limits, runtime authorization, or tested support. Implementation must assess all 504 GET operations for the broader diagnostic catalog, including detail-only evidence. All POST/PATCH/DELETE operations remain excluded. The companion JSON dispositions retain the same historical status; its Swagger facts are unchanged.

Counts: **531 path entries; 529 callable paths; 1009 explicit operations; 504 GET; 178 POST; 168 PATCH; 159 DELETE; 1,062 definitions.**

`/cluster/ntp` and `/storage/quota` are documentation-only path entries. HEAD and OPTIONS are described globally but not individually declared; implicit collection mutation variants are also outside these counts.

Canonical parsed-spec SHA-256: `15e7812a4d2d16255855f68f74ba7213c049b837c6b18f33742372f55ef734be`.

The companion JSON preserves each operation ID, introduced-version marker, parameter metadata, response schema reference, and review flags. Neither file contains device data or credentials.

| API family | Callable paths | GET | POST | PATCH | DELETE |
|---|---:|---:|---:|---:|---:|
| application | 17 | 15 | 7 | 3 | 5 |
| cloud | 2 | 2 | 1 | 1 | 1 |
| cluster | 51 | 50 | 13 | 12 | 9 |
| name-services | 31 | 31 | 9 | 14 | 10 |
| network | 42 | 42 | 12 | 11 | 12 |
| protocols | 162 | 159 | 53 | 50 | 56 |
| resource-tags | 4 | 4 | 1 | 0 | 1 |
| security | 80 | 65 | 39 | 30 | 27 |
| snapmirror | 6 | 6 | 3 | 3 | 2 |
| storage | 89 | 86 | 27 | 27 | 24 |
| support | 30 | 29 | 9 | 12 | 8 |
| svm | 15 | 15 | 4 | 5 | 4 |

## application

Paths below include the `/api` base path.

| Path | Declared methods | GET disposition | Review notes |
|---|---|---|---|
| `/api/application/applications` | GET, POST | Deferred: no initial analytics use case | explicit expensive fields |
| `/api/application/applications/{application.uuid}/components` | GET | Deferred: no initial analytics use case | — |
| `/api/application/applications/{application.uuid}/components/{component.uuid}/snapshots` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/application/applications/{application.uuid}/components/{component.uuid}/snapshots/{uuid}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/application/applications/{application.uuid}/components/{component.uuid}/snapshots/{uuid}/restore` | POST | Excluded: mutation | — |
| `/api/application/applications/{application.uuid}/components/{uuid}` | GET | Deferred: no initial analytics use case | — |
| `/api/application/applications/{application.uuid}/snapshots` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/application/applications/{application.uuid}/snapshots/{uuid}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/application/applications/{application.uuid}/snapshots/{uuid}/restore` | POST | Excluded: mutation | — |
| `/api/application/applications/{uuid}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | explicit expensive fields |
| `/api/application/consistency-groups` | GET, POST | Phase 2: feature-specific read candidate | explicit expensive fields |
| `/api/application/consistency-groups/{consistency_group.uuid}/metrics` | GET | Phase 2: feature-specific read candidate | history / sample quality |
| `/api/application/consistency-groups/{consistency_group.uuid}/snapshots` | GET, POST | Phase 2: feature-specific read candidate | explicit expensive fields |
| `/api/application/consistency-groups/{consistency_group.uuid}/snapshots/{uuid}` | GET, PATCH, DELETE | Phase 2: feature-specific read candidate | explicit expensive fields |
| `/api/application/consistency-groups/{uuid}` | GET, PATCH, DELETE | Phase 2: feature-specific read candidate | explicit expensive fields |
| `/api/application/templates` | GET | Deferred: no initial analytics use case | — |
| `/api/application/templates/{name}` | GET | Deferred: no initial analytics use case | — |

## cloud

Paths below include the `/api` base path.

| Path | Declared methods | GET disposition | Review notes |
|---|---|---|---|
| `/api/cloud/targets` | GET, POST | Phase 2: feature-specific read candidate | — |
| `/api/cloud/targets/{uuid}` | GET, PATCH, DELETE | Phase 2: feature-specific read candidate | — |

## cluster

Paths below include the `/api` base path.

| Path | Declared methods | GET disposition | Review notes |
|---|---|---|---|
| `/api/cluster` | GET, POST, PATCH | MVP: inventory / relationships / events | — |
| `/api/cluster/chassis` | GET | Phase 2: feature-specific read candidate | — |
| `/api/cluster/chassis/{id}` | GET | Phase 2: feature-specific read candidate | — |
| `/api/cluster/counter/tables` | GET | Later: bounded diagnostics | raw counters / denominator semantics |
| `/api/cluster/counter/tables/{counter_table.name}/rows` | GET | Later: bounded diagnostics | raw counters / denominator semantics |
| `/api/cluster/counter/tables/{counter_table.name}/rows/{id}` | GET | Later: bounded diagnostics | raw counters / denominator semantics |
| `/api/cluster/counter/tables/{name}` | GET | Later: bounded diagnostics | raw counters / denominator semantics |
| `/api/cluster/firmware/history` | GET | Deferred: no initial analytics use case | — |
| `/api/cluster/jobs` | GET | Deferred: no initial analytics use case | — |
| `/api/cluster/jobs/{uuid}` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/cluster/licensing/capacity-pools` | GET | Deferred: no initial analytics use case | — |
| `/api/cluster/licensing/capacity-pools/{serial_number}` | GET | Deferred: no initial analytics use case | — |
| `/api/cluster/licensing/license-managers` | GET | Deferred: no initial analytics use case | — |
| `/api/cluster/licensing/license-managers/{uuid}` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/cluster/licensing/licenses` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/cluster/licensing/licenses/{name}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/cluster/mediators` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/cluster/mediators/{uuid}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/cluster/metrics` | GET | MVP: scoped historical metrics | history / sample quality |
| `/api/cluster/metrocluster` | GET, POST, PATCH | Phase 2: feature-specific read candidate | — |
| `/api/cluster/metrocluster/diagnostics` | GET, POST | Phase 2: feature-specific read candidate | — |
| `/api/cluster/metrocluster/dr-groups` | GET, POST | Phase 2: feature-specific read candidate | — |
| `/api/cluster/metrocluster/dr-groups/{id}` | GET, DELETE | Phase 2: feature-specific read candidate | — |
| `/api/cluster/metrocluster/interconnects` | GET | Phase 2: feature-specific read candidate | — |
| `/api/cluster/metrocluster/interconnects/{node.uuid}/{partner_type}/{adapter}` | GET, PATCH | Phase 2: feature-specific read candidate | — |
| `/api/cluster/metrocluster/nodes` | GET | Phase 2: feature-specific read candidate | — |
| `/api/cluster/metrocluster/nodes/{node.uuid}` | GET | Phase 2: feature-specific read candidate | — |
| `/api/cluster/metrocluster/operations` | GET | Phase 2: feature-specific read candidate | — |
| `/api/cluster/metrocluster/operations/{uuid}` | GET | Phase 2: feature-specific read candidate | — |
| `/api/cluster/metrocluster/svms` | GET | Phase 2: feature-specific read candidate | — |
| `/api/cluster/metrocluster/svms/{cluster.uuid}/{svm.uuid}` | GET | Phase 2: feature-specific read candidate | — |
| `/api/cluster/nodes` | GET, POST | MVP: inventory / relationships / events | explicit expensive fields |
| `/api/cluster/nodes/{uuid}` | GET, PATCH, DELETE | MVP: optional detail lookup | — |
| `/api/cluster/nodes/{uuid}/metrics` | GET | MVP: scoped historical metrics | history / sample quality |
| `/api/cluster/ntp` | — | Documentation only | — |
| `/api/cluster/ntp/keys` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/cluster/ntp/keys/{id}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/cluster/ntp/servers` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/cluster/ntp/servers/{server}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/cluster/peers` | GET, POST | Phase 2: feature-specific read candidate | — |
| `/api/cluster/peers/{uuid}` | GET, PATCH, DELETE | Phase 2: feature-specific read candidate | — |
| `/api/cluster/schedules` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/cluster/schedules/{uuid}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/cluster/sensors` | GET | Phase 2: feature-specific read candidate | — |
| `/api/cluster/sensors/{node.uuid}/{index}` | GET | Phase 2: feature-specific read candidate | — |
| `/api/cluster/software` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/cluster/software/download` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/cluster/software/history` | GET | Deferred: no initial analytics use case | — |
| `/api/cluster/software/packages` | GET | Deferred: no initial analytics use case | — |
| `/api/cluster/software/packages/{version}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/cluster/software/upload` | POST | Excluded: mutation | — |
| `/api/cluster/web` | GET, PATCH | Deferred: no initial analytics use case | — |

## name-services

Paths below include the `/api` base path.

| Path | Declared methods | GET disposition | Review notes |
|---|---|---|---|
| `/api/name-services/cache/group-membership/settings` | GET | Deferred: no initial analytics use case | — |
| `/api/name-services/cache/group-membership/settings/{svm.uuid}` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/name-services/cache/host/settings` | GET | Deferred: no initial analytics use case | — |
| `/api/name-services/cache/host/settings/{uuid}` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/name-services/cache/netgroup/settings` | GET | Deferred: no initial analytics use case | — |
| `/api/name-services/cache/netgroup/settings/{svm.uuid}` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/name-services/cache/setting` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/name-services/cache/unix-group/settings` | GET | Deferred: no initial analytics use case | — |
| `/api/name-services/cache/unix-group/settings/{svm.uuid}` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/name-services/cache/unix-user/settings` | GET | Deferred: no initial analytics use case | — |
| `/api/name-services/cache/unix-user/settings/{svm.uuid}` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/name-services/dns` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/name-services/dns/{uuid}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/name-services/host-record/{svm.uuid}/{host}` | GET | Deferred: no initial analytics use case | — |
| `/api/name-services/ldap` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/name-services/ldap-schemas` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/name-services/ldap-schemas/{owner.uuid}/{name}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/name-services/ldap/{svm.uuid}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/name-services/local-hosts` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/name-services/local-hosts/{owner.uuid}/{address}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/name-services/name-mappings` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/name-services/name-mappings/{svm.uuid}/{direction}/{index}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/name-services/netgroup-files/{svm.uuid}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/name-services/nis` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/name-services/nis/{svm.uuid}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/name-services/unix-groups` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/name-services/unix-groups/{svm.uuid}/{name}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/name-services/unix-groups/{svm.uuid}/{unix_group.name}/users` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/name-services/unix-groups/{svm.uuid}/{unix_group.name}/users/{name}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/name-services/unix-users` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/name-services/unix-users/{svm.uuid}/{name}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |

## network

Paths below include the `/api` base path.

| Path | Declared methods | GET disposition | Review notes |
|---|---|---|---|
| `/api/network/ethernet/broadcast-domains` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/network/ethernet/broadcast-domains/{uuid}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/network/ethernet/ports` | GET, POST | MVP: inventory / relationships / events | — |
| `/api/network/ethernet/ports/{uuid}` | GET, PATCH, DELETE | MVP: optional detail lookup | — |
| `/api/network/ethernet/ports/{uuid}/metrics` | GET | MVP: scoped historical metrics | history / sample quality |
| `/api/network/ethernet/switch/ports` | GET | Deferred: no initial analytics use case | — |
| `/api/network/ethernet/switch/ports/{switch}/{identity.name}/{identity.index}` | GET | Deferred: no initial analytics use case | — |
| `/api/network/ethernet/switches` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/network/ethernet/switches/{name}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/network/fc/fabrics` | GET | Phase 2: feature-specific read candidate | explicit expensive fields; cached topology / cache age |
| `/api/network/fc/fabrics/{fabric.name}/switches` | GET | Phase 2: feature-specific read candidate | explicit expensive fields; cached topology / cache age |
| `/api/network/fc/fabrics/{fabric.name}/switches/{wwn}` | GET | Phase 2: feature-specific read candidate | explicit expensive fields; cached topology / cache age |
| `/api/network/fc/fabrics/{fabric.name}/zones` | GET | Phase 2: feature-specific read candidate | explicit expensive fields; cached topology / cache age |
| `/api/network/fc/fabrics/{fabric.name}/zones/{name}` | GET | Phase 2: feature-specific read candidate | explicit expensive fields; cached topology / cache age |
| `/api/network/fc/fabrics/{name}` | GET | Phase 2: feature-specific read candidate | explicit expensive fields; cached topology / cache age |
| `/api/network/fc/interfaces` | GET, POST | MVP: inventory / relationships / events | — |
| `/api/network/fc/interfaces/{fc_interface.uuid}/metrics` | GET | MVP: scoped historical metrics | history / sample quality |
| `/api/network/fc/interfaces/{fc_interface.uuid}/metrics/{timestamp}` | GET | MVP: optional detail lookup | history / sample quality |
| `/api/network/fc/interfaces/{uuid}` | GET, PATCH, DELETE | MVP: optional detail lookup | explicit expensive fields |
| `/api/network/fc/logins` | GET | MVP: inventory / relationships / events | — |
| `/api/network/fc/logins/{interface.uuid}/{initiator.wwpn}` | GET | Deferred: no initial analytics use case | — |
| `/api/network/fc/ports` | GET | MVP: inventory / relationships / events | explicit expensive fields |
| `/api/network/fc/ports/{fc_port.uuid}/metrics` | GET | MVP: scoped historical metrics | history / sample quality |
| `/api/network/fc/ports/{fc_port.uuid}/metrics/{timestamp}` | GET | MVP: optional detail lookup | history / sample quality |
| `/api/network/fc/ports/{uuid}` | GET, PATCH | MVP: optional detail lookup | explicit expensive fields |
| `/api/network/fc/wwpn-aliases` | GET, POST | Phase 2: feature-specific read candidate | — |
| `/api/network/fc/wwpn-aliases/{svm.uuid}/{alias}` | GET, DELETE | Phase 2: feature-specific read candidate | — |
| `/api/network/http-proxy` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/network/http-proxy/{uuid}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/network/ip/bgp/peer-groups` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/network/ip/bgp/peer-groups/{uuid}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/network/ip/interfaces` | GET, POST | MVP: inventory / relationships / events | — |
| `/api/network/ip/interfaces/{uuid}` | GET, PATCH, DELETE | MVP: optional detail lookup | — |
| `/api/network/ip/interfaces/{uuid}/metrics` | GET | MVP: scoped historical metrics | history / sample quality |
| `/api/network/ip/routes` | GET, POST | Deferred: no initial analytics use case | explicit expensive fields |
| `/api/network/ip/routes/{uuid}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/network/ip/service-policies` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/network/ip/service-policies/{uuid}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/network/ip/subnets` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/network/ip/subnets/{uuid}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/network/ipspaces` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/network/ipspaces/{uuid}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |

## protocols

Paths below include the `/api` base path.

| Path | Declared methods | GET disposition | Review notes |
|---|---|---|---|
| `/api/protocols/active-directory` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/protocols/active-directory/{svm.uuid}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | GET side-effect parameter: reset_discovered_servers |
| `/api/protocols/active-directory/{svm.uuid}/preferred-domain-controllers` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/protocols/active-directory/{svm.uuid}/preferred-domain-controllers/{fqdn}/{server_ip}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/protocols/audit` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/protocols/audit/{svm.uuid}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/protocols/audit/{svm.uuid}/object-store` | GET, POST, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/connections` | GET | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/domains` | GET | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/domains/{svm.uuid}` | GET, PATCH | Deferred: no initial analytics use case | GET side-effect parameter: rediscover_trusts; GET side-effect parameter: reset_discovered_servers |
| `/api/protocols/cifs/domains/{svm.uuid}/preferred-domain-controllers` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/domains/{svm.uuid}/preferred-domain-controllers/{fqdn}/{server_ip}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/group-policies` | GET | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/group-policies/{svm.uuid}` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/group-policies/{svm.uuid}/central-access-policies` | GET | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/group-policies/{svm.uuid}/central-access-policies/{name}` | GET | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/group-policies/{svm.uuid}/central-access-rules` | GET | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/group-policies/{svm.uuid}/central-access-rules/{name}` | GET | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/group-policies/{svm.uuid}/objects` | GET | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/group-policies/{svm.uuid}/objects/{index}` | GET | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/group-policies/{svm.uuid}/restricted-groups` | GET | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/group-policies/{svm.uuid}/restricted-groups/{policy_index}/{group_name}` | GET | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/home-directory/search-paths` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/home-directory/search-paths/{svm.uuid}/{index}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/local-groups` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/local-groups/{svm.uuid}/{local_cifs_group.sid}/members` | GET, POST, DELETE | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/local-groups/{svm.uuid}/{local_cifs_group.sid}/members/{name}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/local-groups/{svm.uuid}/{sid}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/local-users` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/local-users/{svm.uuid}/{sid}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/netbios` | GET | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/services` | GET, POST | Phase 2: feature-specific read candidate | explicit expensive fields |
| `/api/protocols/cifs/services/{svm.uuid}` | GET, PATCH, DELETE | Phase 2: feature-specific read candidate | — |
| `/api/protocols/cifs/services/{svm.uuid}/metrics` | GET | Phase 2: feature-specific read candidate | history / sample quality |
| `/api/protocols/cifs/session/files` | GET | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/session/files/{node.uuid}/{svm.uuid}/{identifier}/{connection.identifier}/{session.identifier}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/sessions` | GET | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/sessions/{node.uuid}/{svm.uuid}/{identifier}/{connection_id}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/shadow-copies` | GET | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/shadow-copies/{client_uuid}` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/shadowcopy-sets` | GET | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/shadowcopy-sets/{uuid}` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/shares` | GET, POST | Phase 2: feature-specific read candidate | — |
| `/api/protocols/cifs/shares/{svm.uuid}/{name}` | GET, PATCH, DELETE | Phase 2: feature-specific read candidate | — |
| `/api/protocols/cifs/shares/{svm.uuid}/{share}/acls` | GET, POST | Phase 2: feature-specific read candidate | — |
| `/api/protocols/cifs/shares/{svm.uuid}/{share}/acls/{user_or_group}/{type}` | GET, PATCH, DELETE | Phase 2: feature-specific read candidate | — |
| `/api/protocols/cifs/unix-symlink-mapping` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/unix-symlink-mapping/{svm.uuid}/{unix_path}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/users-and-groups/bulk-import/{svm.uuid}` | GET, POST, PATCH | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/users-and-groups/privileges` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/protocols/cifs/users-and-groups/privileges/{svm.uuid}/{name}` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/protocols/file-security/effective-permissions/{svm.uuid}/{path}` | GET | Deferred: no initial analytics use case | — |
| `/api/protocols/file-security/permissions/{svm.uuid}/{path}` | GET, POST, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/protocols/file-security/permissions/{svm.uuid}/{path}/acl` | POST | Excluded: mutation | — |
| `/api/protocols/file-security/permissions/{svm.uuid}/{path}/acl/{user}` | PATCH, DELETE | Excluded: mutation | — |
| `/api/protocols/fpolicy` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/protocols/fpolicy/{svm.uuid}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/protocols/fpolicy/{svm.uuid}/connections` | GET | Deferred: no initial analytics use case | — |
| `/api/protocols/fpolicy/{svm.uuid}/connections/{node.uuid}/{policy.name}/{server}` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/protocols/fpolicy/{svm.uuid}/engines` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/protocols/fpolicy/{svm.uuid}/engines/{name}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/protocols/fpolicy/{svm.uuid}/events` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/protocols/fpolicy/{svm.uuid}/events/{name}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/protocols/fpolicy/{svm.uuid}/persistent-stores` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/protocols/fpolicy/{svm.uuid}/persistent-stores/{name}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/protocols/fpolicy/{svm.uuid}/policies` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/protocols/fpolicy/{svm.uuid}/policies/{name}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/protocols/locks` | GET | Deferred: no initial analytics use case | — |
| `/api/protocols/locks/{uuid}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/protocols/ndmp` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/protocols/ndmp/nodes` | GET | Deferred: no initial analytics use case | — |
| `/api/protocols/ndmp/nodes/{node.uuid}` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/protocols/ndmp/sessions` | GET | Deferred: no initial analytics use case | — |
| `/api/protocols/ndmp/sessions/{owner.uuid}/{session.id}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/protocols/ndmp/svms` | GET | Deferred: no initial analytics use case | — |
| `/api/protocols/ndmp/svms/{svm.uuid}` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/protocols/ndmp/svms/{svm.uuid}/passwords/{user}` | GET | Excluded: credentials, content, or operational scope | GET generates password |
| `/api/protocols/nfs/connected-client-maps` | GET | Deferred: no initial analytics use case | — |
| `/api/protocols/nfs/connected-client-settings` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/protocols/nfs/connected-clients` | GET | Deferred: no initial analytics use case | explicit expensive fields |
| `/api/protocols/nfs/export-policies` | GET, POST | Phase 2: feature-specific read candidate | — |
| `/api/protocols/nfs/export-policies/{id}` | GET, PATCH, DELETE | Phase 2: feature-specific read candidate | — |
| `/api/protocols/nfs/export-policies/{policy.id}/rules` | GET, POST | Phase 2: feature-specific read candidate | — |
| `/api/protocols/nfs/export-policies/{policy.id}/rules/{index}` | GET, PATCH, DELETE | Phase 2: feature-specific read candidate | — |
| `/api/protocols/nfs/export-policies/{policy.id}/rules/{index}/clients` | GET, POST | Phase 2: feature-specific read candidate | — |
| `/api/protocols/nfs/export-policies/{policy.id}/rules/{index}/clients/{match}` | DELETE | Excluded: mutation | — |
| `/api/protocols/nfs/kerberos/interfaces` | GET | Deferred: no initial analytics use case | — |
| `/api/protocols/nfs/kerberos/interfaces/{interface.uuid}` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/protocols/nfs/kerberos/realms` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/protocols/nfs/kerberos/realms/{svm.uuid}/{name}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/protocols/nfs/services` | GET, POST | Phase 2: feature-specific read candidate | explicit expensive fields |
| `/api/protocols/nfs/services/{svm.uuid}` | GET, PATCH, DELETE | Phase 2: feature-specific read candidate | — |
| `/api/protocols/nfs/services/{svm.uuid}/metrics` | GET | Phase 2: feature-specific read candidate | history / sample quality |
| `/api/protocols/nvme/interfaces` | GET | Phase 2: feature-specific read candidate | — |
| `/api/protocols/nvme/interfaces/{uuid}` | GET | Phase 2: feature-specific read candidate | — |
| `/api/protocols/nvme/services` | GET, POST | Phase 2: feature-specific read candidate | explicit expensive fields |
| `/api/protocols/nvme/services/{svm.uuid}` | GET, PATCH, DELETE | Phase 2: feature-specific read candidate | — |
| `/api/protocols/nvme/services/{svm.uuid}/metrics` | GET | Phase 2: feature-specific read candidate | history / sample quality |
| `/api/protocols/nvme/services/{svm.uuid}/metrics/{timestamp}` | GET | Phase 2: feature-specific read candidate | history / sample quality |
| `/api/protocols/nvme/subsystem-controllers` | GET | Phase 2: feature-specific read candidate | — |
| `/api/protocols/nvme/subsystem-controllers/{subsystem.uuid}/{id}` | GET | Phase 2: feature-specific read candidate | — |
| `/api/protocols/nvme/subsystem-maps` | GET, POST | Phase 2: feature-specific read candidate | explicit expensive fields |
| `/api/protocols/nvme/subsystem-maps/{subsystem.uuid}/{namespace.uuid}` | GET, DELETE | Phase 2: feature-specific read candidate | explicit expensive fields |
| `/api/protocols/nvme/subsystems` | GET, POST | Phase 2: feature-specific read candidate | — |
| `/api/protocols/nvme/subsystems/{subsystem.uuid}/hosts` | GET, POST | Phase 2: feature-specific read candidate | explicit expensive fields |
| `/api/protocols/nvme/subsystems/{subsystem.uuid}/hosts/{nqn}` | GET, DELETE | Phase 2: feature-specific read candidate | — |
| `/api/protocols/nvme/subsystems/{uuid}` | GET, PATCH, DELETE | Phase 2: feature-specific read candidate | explicit expensive fields |
| `/api/protocols/s3/buckets` | GET, POST | Phase 2: feature-specific read candidate | — |
| `/api/protocols/s3/buckets/{svm.uuid}/{uuid}` | GET, PATCH, DELETE | Phase 2: feature-specific read candidate | — |
| `/api/protocols/s3/services` | GET, POST | Phase 2: feature-specific read candidate | explicit expensive fields |
| `/api/protocols/s3/services/{svm.uuid}` | GET, PATCH, DELETE | Phase 2: feature-specific read candidate | — |
| `/api/protocols/s3/services/{svm.uuid}/buckets` | GET, POST | Phase 2: feature-specific read candidate | — |
| `/api/protocols/s3/services/{svm.uuid}/buckets/{s3_bucket.uuid}/rules` | GET, POST | Phase 2: feature-specific read candidate | — |
| `/api/protocols/s3/services/{svm.uuid}/buckets/{s3_bucket.uuid}/rules/{name}` | GET, PATCH, DELETE | Phase 2: feature-specific read candidate | — |
| `/api/protocols/s3/services/{svm.uuid}/buckets/{uuid}` | GET, PATCH, DELETE | Phase 2: feature-specific read candidate | — |
| `/api/protocols/s3/services/{svm.uuid}/groups` | GET, POST | Phase 2: feature-specific read candidate | — |
| `/api/protocols/s3/services/{svm.uuid}/groups/{id}` | GET, PATCH, DELETE | Phase 2: feature-specific read candidate | — |
| `/api/protocols/s3/services/{svm.uuid}/metrics` | GET | Phase 2: feature-specific read candidate | history / sample quality |
| `/api/protocols/s3/services/{svm.uuid}/policies` | GET, POST | Phase 2: feature-specific read candidate | — |
| `/api/protocols/s3/services/{svm.uuid}/policies/{name}` | GET, PATCH, DELETE | Phase 2: feature-specific read candidate | — |
| `/api/protocols/s3/services/{svm.uuid}/users` | GET, POST | Excluded: account scope | — |
| `/api/protocols/s3/services/{svm.uuid}/users/{name}` | GET, PATCH, DELETE | Excluded: account scope | — |
| `/api/protocols/san/fcp/services` | GET, POST | Phase 2: feature-specific read candidate | explicit expensive fields |
| `/api/protocols/san/fcp/services/{svm.uuid}` | GET, PATCH, DELETE | Phase 2: feature-specific read candidate | — |
| `/api/protocols/san/fcp/services/{svm.uuid}/metrics` | GET | Phase 2: feature-specific read candidate | history / sample quality |
| `/api/protocols/san/fcp/services/{svm.uuid}/metrics/{timestamp}` | GET | Phase 2: feature-specific read candidate | history / sample quality |
| `/api/protocols/san/igroups` | GET, POST | MVP: inventory / relationships / events | explicit expensive fields |
| `/api/protocols/san/igroups/{igroup.uuid}/igroups` | GET, POST | MVP: inventory / relationships / events | direct vs nested membership |
| `/api/protocols/san/igroups/{igroup.uuid}/igroups/{uuid}` | GET, DELETE | MVP: optional detail lookup | direct vs nested membership |
| `/api/protocols/san/igroups/{igroup.uuid}/initiators` | GET, POST | MVP: inventory / relationships / events | explicit expensive fields; direct vs nested membership |
| `/api/protocols/san/igroups/{igroup.uuid}/initiators/{name}` | GET, PATCH, DELETE | MVP: optional detail lookup | explicit expensive fields; direct vs nested membership |
| `/api/protocols/san/igroups/{uuid}` | GET, PATCH, DELETE | MVP: optional detail lookup | explicit expensive fields; direct vs nested membership |
| `/api/protocols/san/initiators` | GET | MVP: inventory / relationships / events | — |
| `/api/protocols/san/initiators/{svm.uuid}/{name}` | GET | Deferred: no initial analytics use case | — |
| `/api/protocols/san/iscsi/credentials` | GET, POST | Excluded: credentials, content, or operational scope | — |
| `/api/protocols/san/iscsi/credentials/{svm.uuid}/{initiator}` | GET, PATCH, DELETE | Excluded: credentials, content, or operational scope | — |
| `/api/protocols/san/iscsi/services` | GET, POST | Phase 2: feature-specific read candidate | explicit expensive fields |
| `/api/protocols/san/iscsi/services/{svm.uuid}` | GET, PATCH, DELETE | Phase 2: feature-specific read candidate | — |
| `/api/protocols/san/iscsi/services/{svm.uuid}/metrics` | GET | Phase 2: feature-specific read candidate | history / sample quality |
| `/api/protocols/san/iscsi/services/{svm.uuid}/metrics/{timestamp}` | GET | Phase 2: feature-specific read candidate | history / sample quality |
| `/api/protocols/san/iscsi/sessions` | GET | Phase 2: feature-specific read candidate | — |
| `/api/protocols/san/iscsi/sessions/{svm.uuid}/{tpgroup}/{tsih}` | GET | Phase 2: feature-specific read candidate | — |
| `/api/protocols/san/lun-maps` | GET, POST | MVP: inventory / relationships / events | — |
| `/api/protocols/san/lun-maps/{lun.uuid}/{igroup.uuid}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/protocols/san/lun-maps/{lun.uuid}/{igroup.uuid}/reporting-nodes` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/protocols/san/lun-maps/{lun.uuid}/{igroup.uuid}/reporting-nodes/{uuid}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/protocols/san/portsets` | GET, POST | Phase 2: feature-specific read candidate | — |
| `/api/protocols/san/portsets/{portset.uuid}/interfaces` | GET, POST | Phase 2: feature-specific read candidate | — |
| `/api/protocols/san/portsets/{portset.uuid}/interfaces/{uuid}` | GET, DELETE | Phase 2: feature-specific read candidate | — |
| `/api/protocols/san/portsets/{uuid}` | GET, DELETE | Phase 2: feature-specific read candidate | — |
| `/api/protocols/san/vvol-bindings` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/protocols/san/vvol-bindings/{protocol_endpoint.uuid}/{vvol.uuid}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/protocols/vscan` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/protocols/vscan/server-status` | GET | Deferred: no initial analytics use case | — |
| `/api/protocols/vscan/{svm.uuid}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/protocols/vscan/{svm.uuid}/events` | GET | Deferred: no initial analytics use case | — |
| `/api/protocols/vscan/{svm.uuid}/on-access-policies` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/protocols/vscan/{svm.uuid}/on-access-policies/{name}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/protocols/vscan/{svm.uuid}/on-demand-policies` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/protocols/vscan/{svm.uuid}/on-demand-policies/{name}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/protocols/vscan/{svm.uuid}/scanner-pools` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/protocols/vscan/{svm.uuid}/scanner-pools/{name}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |

## resource-tags

Paths below include the `/api` base path.

| Path | Declared methods | GET disposition | Review notes |
|---|---|---|---|
| `/api/resource-tags` | GET | Phase 2: feature-specific read candidate | — |
| `/api/resource-tags/{resource_tag.value}/resources` | GET, POST | Phase 2: feature-specific read candidate | — |
| `/api/resource-tags/{resource_tag.value}/resources/{href}` | GET, DELETE | Phase 2: feature-specific read candidate | — |
| `/api/resource-tags/{value}` | GET | Phase 2: feature-specific read candidate | — |

## security

Paths below include the `/api` base path.

| Path | Declared methods | GET disposition | Review notes |
|---|---|---|---|
| `/api/security` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/security/accounts` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/security/accounts/{owner.uuid}/{name}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/security/anti-ransomware/suspects` | GET | Later: bounded diagnostics | — |
| `/api/security/anti-ransomware/suspects/{volume.uuid}` | DELETE | Excluded: mutation | — |
| `/api/security/audit` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/security/audit/destinations` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/security/audit/destinations/{address}/{port}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/security/audit/messages` | GET | Later: bounded diagnostics | — |
| `/api/security/authentication/cluster/ad-proxy` | GET, POST, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/security/authentication/cluster/ldap` | GET, POST, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/security/authentication/cluster/nis` | GET, POST, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/security/authentication/cluster/oauth2` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/security/authentication/cluster/oauth2/clients` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/security/authentication/cluster/oauth2/clients/{name}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/security/authentication/cluster/saml-sp` | GET, POST, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/security/authentication/duo/groups` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/security/authentication/duo/groups/{owner.uuid}/{name}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/security/authentication/duo/profiles` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/security/authentication/duo/profiles/{owner.uuid}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/security/authentication/password` | POST | Excluded: mutation | — |
| `/api/security/authentication/publickeys` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/security/authentication/publickeys/{owner.uuid}/{account.name}/{index}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/security/aws-kms` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/security/aws-kms/{aws_kms.uuid}/rekey-external` | POST | Excluded: mutation | — |
| `/api/security/aws-kms/{aws_kms.uuid}/rekey-internal` | POST | Excluded: mutation | — |
| `/api/security/aws-kms/{aws_kms.uuid}/restore` | POST | Excluded: mutation | — |
| `/api/security/aws-kms/{uuid}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/security/azure-key-vaults` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/security/azure-key-vaults/{azure_key_vault.uuid}/rekey-external` | POST | Excluded: mutation | — |
| `/api/security/azure-key-vaults/{uuid}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/security/azure-key-vaults/{uuid}/rekey-internal` | POST | Excluded: mutation | — |
| `/api/security/azure-key-vaults/{uuid}/restore` | POST | Excluded: mutation | — |
| `/api/security/certificate-signing-request` | POST | Excluded: mutation | — |
| `/api/security/certificates` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/security/certificates/{ca.uuid}/sign` | POST | Excluded: mutation | — |
| `/api/security/certificates/{uuid}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/security/gcp-kms` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/security/gcp-kms/{gcp_kms.uuid}/rekey-external` | POST | Excluded: mutation | — |
| `/api/security/gcp-kms/{uuid}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/security/gcp-kms/{uuid}/rekey-internal` | POST | Excluded: mutation | — |
| `/api/security/gcp-kms/{uuid}/restore` | POST | Excluded: mutation | — |
| `/api/security/ipsec` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/security/ipsec/ca-certificates` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/security/ipsec/ca-certificates/{certificate.uuid}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/security/ipsec/policies` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/security/ipsec/policies/{uuid}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/security/ipsec/security-associations` | GET | Deferred: no initial analytics use case | — |
| `/api/security/ipsec/security-associations/{uuid}` | GET | Deferred: no initial analytics use case | — |
| `/api/security/key-manager-configs` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/security/key-managers` | GET, POST | Excluded: credentials, content, or operational scope | explicit expensive fields |
| `/api/security/key-managers/{security_key_manager.uuid}/auth-keys` | GET, POST | Excluded: credentials, content, or operational scope | — |
| `/api/security/key-managers/{security_key_manager.uuid}/auth-keys/{key_id}` | GET, DELETE | Excluded: credentials, content, or operational scope | — |
| `/api/security/key-managers/{security_key_manager.uuid}/keys/{node.uuid}/key-ids` | GET | Excluded: credentials, content, or operational scope | — |
| `/api/security/key-managers/{security_key_manager.uuid}/keys/{node.uuid}/key-ids/{key_id}` | GET | Excluded: credentials, content, or operational scope | — |
| `/api/security/key-managers/{security_key_manager.uuid}/restore` | POST | Excluded: mutation | — |
| `/api/security/key-managers/{source.uuid}/migrate` | POST | Excluded: mutation | — |
| `/api/security/key-managers/{uuid}` | GET, PATCH, DELETE | Excluded: credentials, content, or operational scope | explicit expensive fields |
| `/api/security/key-managers/{uuid}/key-servers` | GET, POST | Excluded: credentials, content, or operational scope | explicit expensive fields |
| `/api/security/key-managers/{uuid}/key-servers/{server}` | GET, PATCH, DELETE | Excluded: credentials, content, or operational scope | explicit expensive fields |
| `/api/security/key-stores` | GET | Excluded: credentials, content, or operational scope | — |
| `/api/security/key-stores/{uuid}` | GET, PATCH, DELETE | Excluded: credentials, content, or operational scope | — |
| `/api/security/login/messages` | GET | Deferred: no initial analytics use case | — |
| `/api/security/login/messages/{uuid}` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/security/login/totps` | GET, POST | Excluded: credentials, content, or operational scope | — |
| `/api/security/login/totps/{owner.uuid}/{account.name}` | GET, PATCH, DELETE | Excluded: credentials, content, or operational scope | — |
| `/api/security/multi-admin-verify` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/security/multi-admin-verify/approval-groups` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/security/multi-admin-verify/approval-groups/{owner.uuid}/{name}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/security/multi-admin-verify/requests` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/security/multi-admin-verify/requests/{index}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/security/multi-admin-verify/rules` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/security/multi-admin-verify/rules/{owner.uuid}/{operation}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/security/roles` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/security/roles/{owner.uuid}/{name}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/security/roles/{owner.uuid}/{name}/privileges` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/security/roles/{owner.uuid}/{name}/privileges/{path}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/security/ssh` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/security/ssh/svms` | GET | Deferred: no initial analytics use case | — |
| `/api/security/ssh/svms/{svm.uuid}` | GET, PATCH | Deferred: no initial analytics use case | — |

## snapmirror

Paths below include the `/api` base path.

| Path | Declared methods | GET disposition | Review notes |
|---|---|---|---|
| `/api/snapmirror/policies` | GET, POST | MVP: inventory / relationships / events | — |
| `/api/snapmirror/policies/{uuid}` | GET, PATCH, DELETE | MVP: optional detail lookup | — |
| `/api/snapmirror/relationships` | GET, POST | MVP: inventory / relationships / events | explicit expensive fields; destination vs source view |
| `/api/snapmirror/relationships/{relationship.uuid}/transfers` | GET, POST | MVP: inventory / relationships / events | destination vs source view |
| `/api/snapmirror/relationships/{relationship.uuid}/transfers/{uuid}` | GET, PATCH | MVP: optional detail lookup | destination vs source view |
| `/api/snapmirror/relationships/{uuid}` | GET, PATCH, DELETE | MVP: optional detail lookup | explicit expensive fields; destination vs source view |

## storage

Paths below include the `/api` base path.

| Path | Declared methods | GET disposition | Review notes |
|---|---|---|---|
| `/api/storage/aggregates` | GET, POST | MVP: inventory / relationships / events | explicit expensive fields |
| `/api/storage/aggregates/{aggregate.uuid}/cloud-stores` | GET, POST | Phase 2: feature-specific read candidate | — |
| `/api/storage/aggregates/{aggregate.uuid}/cloud-stores/{target.uuid}` | GET, PATCH, DELETE | Phase 2: feature-specific read candidate | — |
| `/api/storage/aggregates/{aggregate.uuid}/plexes` | GET | Deferred: no initial analytics use case | — |
| `/api/storage/aggregates/{aggregate.uuid}/plexes/{name}` | GET | Deferred: no initial analytics use case | — |
| `/api/storage/aggregates/{uuid}` | GET, PATCH, DELETE | MVP: optional detail lookup | explicit expensive fields |
| `/api/storage/aggregates/{uuid}/metrics` | GET | MVP: scoped historical metrics | history / sample quality |
| `/api/storage/bridges` | GET | Deferred: no initial analytics use case | — |
| `/api/storage/bridges/{wwn}` | GET | Deferred: no initial analytics use case | — |
| `/api/storage/cluster` | GET | MVP: inventory / relationships / events | — |
| `/api/storage/disks` | GET, PATCH | MVP: inventory / relationships / events | — |
| `/api/storage/disks/{name}` | GET | MVP: optional detail lookup | — |
| `/api/storage/file/clone` | POST | Excluded: mutation | — |
| `/api/storage/file/clone/split-loads` | GET | Excluded: credentials, content, or operational scope | — |
| `/api/storage/file/clone/split-loads/{node.uuid}` | GET, PATCH | Excluded: credentials, content, or operational scope | — |
| `/api/storage/file/clone/split-status` | GET | Excluded: credentials, content, or operational scope | — |
| `/api/storage/file/clone/split-status/{volume.uuid}` | GET | Excluded: credentials, content, or operational scope | — |
| `/api/storage/file/clone/tokens` | GET, POST | Excluded: credentials, content, or operational scope | — |
| `/api/storage/file/clone/tokens/{node.uuid}/{uuid}` | GET, PATCH, DELETE | Excluded: credentials, content, or operational scope | — |
| `/api/storage/file/copy` | POST | Excluded: mutation | — |
| `/api/storage/file/moves` | GET, POST | Excluded: credentials, content, or operational scope | — |
| `/api/storage/file/moves/{node.uuid}/{uuid}/{index}` | GET | Excluded: credentials, content, or operational scope | — |
| `/api/storage/flexcache/flexcaches` | GET, POST | Phase 2: feature-specific read candidate | explicit expensive fields |
| `/api/storage/flexcache/flexcaches/{uuid}` | GET, PATCH, DELETE | Phase 2: feature-specific read candidate | explicit expensive fields |
| `/api/storage/flexcache/origins` | GET | Phase 2: feature-specific read candidate | explicit expensive fields |
| `/api/storage/flexcache/origins/{uuid}` | GET, PATCH | Phase 2: feature-specific read candidate | explicit expensive fields |
| `/api/storage/luns` | GET, POST | MVP: inventory / relationships / events | explicit expensive fields |
| `/api/storage/luns/{lun.uuid}/attributes` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/storage/luns/{lun.uuid}/attributes/{name}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/storage/luns/{lun.uuid}/metrics` | GET | MVP: scoped historical metrics | history / sample quality |
| `/api/storage/luns/{lun.uuid}/metrics/{timestamp}` | GET | MVP: optional detail lookup | history / sample quality |
| `/api/storage/luns/{uuid}` | GET, PATCH, DELETE | MVP: optional detail lookup | explicit expensive fields; JSON metadata only; block multipart and data.* |
| `/api/storage/namespaces` | GET, POST | Phase 2: feature-specific read candidate | explicit expensive fields |
| `/api/storage/namespaces/{nvme_namespace.uuid}/metrics` | GET | Phase 2: feature-specific read candidate | history / sample quality |
| `/api/storage/namespaces/{nvme_namespace.uuid}/metrics/{timestamp}` | GET | Phase 2: feature-specific read candidate | history / sample quality |
| `/api/storage/namespaces/{uuid}` | GET, PATCH, DELETE | Phase 2: feature-specific read candidate | explicit expensive fields |
| `/api/storage/pools` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/storage/pools/{uuid}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/storage/ports` | GET | Deferred: no initial analytics use case | — |
| `/api/storage/ports/{node.uuid}/{name}` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/storage/qos/policies` | GET, POST | MVP: inventory / relationships / events | — |
| `/api/storage/qos/policies/{uuid}` | GET, PATCH, DELETE | MVP: optional detail lookup | — |
| `/api/storage/qos/qos-options` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/storage/qos/workloads` | GET | Deferred: no initial analytics use case | — |
| `/api/storage/qos/workloads/{uuid}` | GET | Deferred: no initial analytics use case | — |
| `/api/storage/qtrees` | GET, POST | Phase 2: feature-specific read candidate | explicit expensive fields |
| `/api/storage/qtrees/{volume.uuid}/{id}` | GET, PATCH, DELETE | Phase 2: feature-specific read candidate | explicit expensive fields |
| `/api/storage/quota` | — | Documentation only | — |
| `/api/storage/quota/reports` | GET | Phase 2: feature-specific read candidate | — |
| `/api/storage/quota/reports/{volume.uuid}/{index}` | GET | Phase 2: feature-specific read candidate | — |
| `/api/storage/quota/rules` | GET, POST | Phase 2: feature-specific read candidate | — |
| `/api/storage/quota/rules/{uuid}` | GET, PATCH, DELETE | Phase 2: feature-specific read candidate | — |
| `/api/storage/shelves` | GET | MVP: inventory / relationships / events | — |
| `/api/storage/shelves/{uid}` | GET, PATCH | MVP: optional detail lookup | — |
| `/api/storage/snaplock/audit-logs` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/storage/snaplock/audit-logs/{svm.uuid}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/storage/snaplock/compliance-clocks` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/storage/snaplock/compliance-clocks/{node.uuid}` | GET | Deferred: no initial analytics use case | — |
| `/api/storage/snaplock/event-retention/operations` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/storage/snaplock/event-retention/operations/{id}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/storage/snaplock/event-retention/policies` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/storage/snaplock/event-retention/policies/{policy.name}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/storage/snaplock/file-fingerprints` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/storage/snaplock/file-fingerprints/{id}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/storage/snaplock/file/{volume.uuid}/{path}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/storage/snaplock/litigations` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/storage/snaplock/litigations/{id}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/storage/snaplock/litigations/{litigation.id}/files` | GET | Deferred: no initial analytics use case | — |
| `/api/storage/snaplock/litigations/{litigation.id}/operations` | POST | Excluded: mutation | — |
| `/api/storage/snaplock/litigations/{litigation.id}/operations/{id}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/storage/snapshot-policies` | GET, POST | MVP: inventory / relationships / events | — |
| `/api/storage/snapshot-policies/{snapshot_policy.uuid}/schedules` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/storage/snapshot-policies/{snapshot_policy.uuid}/schedules/{schedule.uuid}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/storage/snapshot-policies/{uuid}` | GET, PATCH, DELETE | MVP: optional detail lookup | — |
| `/api/storage/switches` | GET | Deferred: no initial analytics use case | — |
| `/api/storage/switches/{name}` | GET | Deferred: no initial analytics use case | — |
| `/api/storage/tape-devices` | GET | Deferred: no initial analytics use case | — |
| `/api/storage/tape-devices/{node.uuid}/{device_id}` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/storage/volume-efficiency-policies` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/storage/volume-efficiency-policies/{uuid}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/storage/volumes` | GET, POST | MVP: inventory / relationships / events | explicit expensive fields; separate FlexGroup constituents |
| `/api/storage/volumes/{uuid}` | GET, PATCH, DELETE | MVP: optional detail lookup | explicit expensive fields |
| `/api/storage/volumes/{volume.uuid}/files/{path}` | GET, POST, PATCH, DELETE | Excluded: credentials, content, or operational scope | explicit expensive fields |
| `/api/storage/volumes/{volume.uuid}/metrics` | GET | MVP: scoped historical metrics | history / sample quality |
| `/api/storage/volumes/{volume.uuid}/snapshots` | GET, POST | MVP: inventory / relationships / events | explicit expensive fields |
| `/api/storage/volumes/{volume.uuid}/snapshots/{uuid}` | GET, PATCH, DELETE | MVP: optional detail lookup | — |
| `/api/storage/volumes/{volume.uuid}/top-metrics/clients` | GET | Later: bounded diagnostics | feature-dependent / high cardinality |
| `/api/storage/volumes/{volume.uuid}/top-metrics/directories` | GET | Later: bounded diagnostics | feature-dependent / high cardinality |
| `/api/storage/volumes/{volume.uuid}/top-metrics/files` | GET | Later: bounded diagnostics | feature-dependent / high cardinality |
| `/api/storage/volumes/{volume.uuid}/top-metrics/users` | GET | Later: bounded diagnostics | feature-dependent / high cardinality |

## support

Paths below include the `/api` base path.

| Path | Declared methods | GET disposition | Review notes |
|---|---|---|---|
| `/api/support/auto-update` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/support/auto-update/configurations` | GET | Deferred: no initial analytics use case | — |
| `/api/support/auto-update/configurations/{uuid}` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/support/auto-update/updates` | GET | Deferred: no initial analytics use case | — |
| `/api/support/auto-update/updates/{uuid}` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/support/autosupport` | GET, PATCH | Deferred: no initial analytics use case | explicit expensive fields |
| `/api/support/autosupport/messages` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/support/autosupport/messages/{node.uuid}/{index}/{destination}` | GET | Deferred: no initial analytics use case | — |
| `/api/support/configuration-backup` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/support/configuration-backup/backups` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/support/configuration-backup/backups/{node.uuid}/{name}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/support/coredump/coredumps` | GET | Deferred: no initial analytics use case | — |
| `/api/support/coredump/coredumps/{node.uuid}/{name}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/support/ems` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/support/ems/application-logs` | POST | Excluded: mutation | — |
| `/api/support/ems/destinations` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/support/ems/destinations/{name}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | explicit expensive fields |
| `/api/support/ems/events` | GET | MVP: inventory / relationships / events | — |
| `/api/support/ems/filters` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/support/ems/filters/{name}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/support/ems/filters/{name}/rules` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/support/ems/filters/{name}/rules/{index}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/support/ems/messages` | GET | Deferred: no initial analytics use case | — |
| `/api/support/ems/role-configs` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/support/ems/role-configs/{access_control_role.name}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/support/snmp` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/support/snmp/traphosts` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/support/snmp/traphosts/{host}` | GET, DELETE | Deferred: no initial analytics use case | — |
| `/api/support/snmp/users` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/support/snmp/users/{engine_id}/{name}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |

## svm

Paths below include the `/api` base path.

| Path | Declared methods | GET disposition | Review notes |
|---|---|---|---|
| `/api/svm/migrations` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/svm/migrations/{svm_migration.uuid}/volumes` | GET | Deferred: no initial analytics use case | — |
| `/api/svm/migrations/{svm_migration.uuid}/volumes/{volume.uuid}` | GET | Deferred: no initial analytics use case | — |
| `/api/svm/migrations/{uuid}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/svm/peer-permissions` | GET, POST | Deferred: no initial analytics use case | — |
| `/api/svm/peer-permissions/{cluster_peer.uuid}/{svm.uuid}` | GET, PATCH, DELETE | Deferred: no initial analytics use case | — |
| `/api/svm/peers` | GET, POST | Phase 2: feature-specific read candidate | — |
| `/api/svm/peers/{uuid}` | GET, PATCH, DELETE | Phase 2: feature-specific read candidate | — |
| `/api/svm/svms` | GET, POST | MVP: inventory / relationships / events | explicit expensive fields |
| `/api/svm/svms/{svm.uuid}/top-metrics/clients` | GET | Later: bounded diagnostics | feature-dependent / high cardinality |
| `/api/svm/svms/{svm.uuid}/top-metrics/directories` | GET | Later: bounded diagnostics | feature-dependent / high cardinality |
| `/api/svm/svms/{svm.uuid}/top-metrics/files` | GET | Later: bounded diagnostics | feature-dependent / high cardinality |
| `/api/svm/svms/{svm.uuid}/top-metrics/users` | GET | Later: bounded diagnostics | feature-dependent / high cardinality |
| `/api/svm/svms/{svm.uuid}/web` | GET, PATCH | Deferred: no initial analytics use case | — |
| `/api/svm/svms/{uuid}` | GET, PATCH, DELETE | MVP: optional detail lookup | explicit expensive fields |
