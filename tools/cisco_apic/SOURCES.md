# Evidence for the Cisco APIC connector

Catalog reference: **Cisco APIC 6.x management information model**
(`model-doc-latest` jsonmeta, fetched 2026-11-10). No physical APIC or ACI
simulator VM was reachable from the build environment, so the wire contract
comes from Cisco documentation, Cisco's machine-readable model, and the
request/response handling of Cisco's and third parties' maintained clients.

Grades: **verified** = read on a primary source or in client source code;
**partial** = secondary source or only part confirmed; **inferred** = our
reasoning, to confirm on a real fabric.

## Protocol

| Fact | Grade | Source |
|---|---|---|
| Password login `POST /api/aaaLogin.json` with `{"aaaUser":{"attributes":{"name","pwd"}}}`; response `imdata[0].aaaLogin.attributes.{token, refreshTimeoutSeconds, version}`; token sent as cookie `APIC-cookie` | verified | Cisco APIC REST API Configuration Guide 4.2(x) and later, "Using the REST API" (https://www.cisco.com/c/en/us/td/docs/dcn/aci/apic/all/apic-rest-api-configuration-guide/cisco-apic-rest-api-configuration-guide-42x-and-later/m_using_the_rest_api.html); Cobra `cobra/mit/session.py` |
| `POST /api/aaaLogout.json`, `GET /api/aaaRefresh.json` rotates the token | verified | REST API Configuration Guide |
| Remote users log in as `apic:<domain>\\<user>` | verified | REST API Configuration Guide |
| Every login/refresh/logout creates an `aaaSessionLR` record | verified | Model metadata `aaa/SessionLR.json` |
| Signature auth: RSA PKCS#1 v1.5 / SHA-256 over `METHOD + path?query + body`, no separators; cookies `APIC-Certificate-Algorithm=v1.0`, `APIC-Certificate-DN=uni/userext/user-<u>/usercert-<cert>`, `APIC-Certificate-Fingerprint=fingerprint`, `APIC-Request-Signature=<base64>`; no login | verified | Ansible `cisco.aci` `plugins/module_utils/aci.py` (`cert_auth`, including the note that the documentation wrongly shows a space), Cobra docs "certificate based sessions" |
| Signature auth is available to local users only | inferred | the certificate is attached under `uni/userext/user-<u>`, which holds local users |
| Reads are `GET /api/class/<cls>.json`, `/api/node/class/topology/pod-<p>/node-<n>/<cls>.json`; writes are POST | verified | REST API Configuration Guide; DataDog `integrations-core/cisco_aci/datadog_checks/cisco_aci/api.py` (node-scoped class queries) |
| `subscription=yes` opens a websocket subscription | verified | REST API Configuration Guide ("Subscribing to Query Results") |
| `query-target-filter` operators `eq ne lt gt le ge bw not and or xor anybit allbits wcard`; at most 20 expressions | verified | REST API Configuration Guide; Cobra `cobra/mit/request.py` |
| `wcard` operand is a regular expression; a backslash cannot be passed inside the quoted value | partial | guide examples and community reports; the connector avoids escaping by sending `.` for metacharacters and re-checking the exact substring client-side |
| Paging `page` (zero-based) / `page-size`; `totalCount` is a string with the total across pages; 2000 objects per page cap since 5.1(1) (lifted for page 0 only in 5.2(3)) | verified | REST API Configuration Guide |
| `order-by=<cls>.<prop>|asc|desc` | verified | REST API Configuration Guide, Cobra |
| Error envelope `{"totalCount":"1","imdata":[{"error":{"attributes":{"code","text"}}}]}` with HTTP 400/403/503 | verified | Cobra and Ansible error parsing; DataDog fixtures |
| Classes with >100k objects return 503 "result dataset is too big"; retry and filter | verified | REST API Configuration Guide |
| Timestamps `2025-03-12T14:50:49.955+00:00` (ms + controller offset); numbers as strings | verified | DataDog recorded fixture `cisco_aci/tests/fixtures/faults/5d56882ce9d312af184fedfe77bc08df.txt` |
| Unset dates are `1970-01-01T00:00:00.000+00:00` | partial | widely seen in APIC output; the client maps the epoch to null |
| Time comparisons in `query-target-filter` use the same ISO format (`gt(faultInst.lastTransition,"...+00:00")`) | verified | DataDog `api.py` |
| A class whose read access is admin-only returns empty `imdata` (not 403) to a `read-all` user | inferred | model `readAccess` lists `admin` only for `faultInst`, `faultRecord`, `eventRecord`, `aaaModLR`, stats; community reports of read-only users missing data. The connector labels these tables and warns at connection test |
| `/api` throttling (`comm-policy` → https → throttle) answers 429/503 | partial | REST API Configuration Guide; the client retries 429/502/503/504 three times with backoff |
| Every APIC answers the full API (no VIP); clients keep one session per controller | partial | DataDog keeps a session per URL; the connector fails over between configured controllers |

## Classes and properties

Every table's class, DN formats, read access and property list (name, base
type, valid enum values, `secure` flag) come from
`https://pubhub.devnetcloud.com/media/model-doc-latest/docs/doc/jsonmeta/<pkg>/<Class>.json`
(**verified** for names and types). `build_catalog.py` regenerates the catalog
from those files; properties flagged `secure` (for example `bgpPeer.password`,
which is why `bgp_peers` reads `bgpPeerEntry`) and per-object bookkeeping
(`childAction`, `status`, `rn`, `uid`, `userdom`, `extMngdBy`, `annotation`,
`lcOwn`, `monPolDn`) are excluded.

Type mapping (**inferred** from the base types and the recorded fixture):
`scalar:Date` → UTC datetime; unsigned/signed integers without enum values →
`Int64` (left as text if a value does not parse); `Double`/`Float` → `Float64`;
enums, bitmasks, `yes`/`no` booleans, DNs, addresses, encapsulations and
durations (`scalar:Time`, e.g. `systemUpTime`) → text, exactly as the APIC
returns them.

Simulator values (fault codes such as F1394 interface down, F0299 BGP peer
not established, F0413 PSU, F0546 port down; event codes; `operStQual`
reasons; DN shapes such as `topology/pod-1/node-303/sys/phys-[eth1/50]/phys`
and `subj-[<dn>]/mod-<id>`) follow the model's DN formats and enum lists;
individual fault/event code-to-text pairings are **inferred** and only shape
the synthetic story.

## Other sources

- Cisco ACI software lifecycle brief (6.1 supported to 2029-02-28): https://www.cisco.com/c/en/us/solutions/collateral/data-center-virtualization/application-centric-infrastructure/softwarep-lifecycle-support-aci-pb.pdf
- Cobra SDK: https://github.com/datacenter/cobra, https://cobra.readthedocs.io/en/stable/
- Ansible cisco.aci: https://github.com/CiscoDevNet/ansible-aci
- DataDog Cisco ACI integration: https://github.com/DataDog/integrations-core/tree/master/cisco_aci
- Signature auth walkthrough: https://community.cisco.com/t5/data-center-documents/cisco-apic-signature-based-transactions-rest-api-using-postman/m-p/3388216
- Read access for non-admin users: https://community.cisco.com/t5/application-centric-infrastructure/cisco-aci-read-access-to-rest-apis-using-non-admin-user/m-p/4034818
- Research write-up: `docs/design/cisco-apic-connector-research.md`
