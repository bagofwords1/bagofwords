# SAP Connector — Comprehensive Analysis

**Status:** Research / analysis only — no implementation.
**Goal:** Let users connect their SAP systems, index all semantic objects, build agents
on top of those objects, and run natural-language queries — with **per-user
authorization** so each user only sees data they are entitled to. Must cover **both
on-prem and cloud**, modeled on how SAP Datasphere and Power BI connect to SAP.

---

## 0. Bottom line up front

1. **This is not greenfield.** The platform already ships a working SAP baseline and,
   crucially, a **generic per-user delegated-auth framework** that is exactly the
   mechanism this request needs. Two SAP clients already exist:
   - `SapDatasphereClient` (`backend/app/data_sources/clients/sap_datasphere_client.py`,
     added 2026-07-20) — cloud semantic layer over the **OData Consumption API**, with
     catalog discovery, measure/dimension parsing, server-side aggregation, and
     **dual-mode auth** (technical user + per-user delegated token).
   - `SapHanaClient` (`backend/app/data_sources/clients/sap_hana_client.py`,
     added 2026-07-15) — plain SQL over the HANA SQL port via `hdbcli`, covering
     **HANA Cloud, on-prem HANA, and Datasphere's Open-SQL schema**.

2. **"SAP business subjects" ≈ "SAP semantic objects."** There is no single API that
   spans all SAP semantic layers. The queryable "objects" live in six products, each
   with its own catalog API, query protocol, and row-level security model. Most likely
   the user means **SAP BusinessObjects (universes)** and/or the general SAP semantic
   layer. **BusinessObjects, BW, S/4 CDS, and SAC connectors do not exist yet.**

3. **The per-user requirement is the whole game.** Server-side row-level security in SAP
   (Datasphere Data Access Controls, HANA analytic privileges, BW analysis
   authorizations, S/4 DCLs) is *only* enforced when the query runs **under the end
   user's identity**. A single shared technical user collapses all of it to one identity
   and silently defeats the requirement. The existing framework already supports this via
   `auth_policy = "user_required"` + delegated OAuth + on-behalf-of tokens.

4. **Recommended shape:** treat "SAP" as a *family* of connectors sharing one
   normalization + auth substrate, prioritized by protocol cleanliness:
   **SQL/JDBC (HANA, Datasphere Open-SQL) → OData (Datasphere, S/4 CDS, BW-OData) →
   InA (BW/S4 analytical richness) → REST (BusinessObjects `/biprws`) → RFC (last
   resort, licensing-blocked).**

---

## 1. What "SAP business subjects" actually means — the product landscape

| Product | Deployment | The "objects" you catalog & query | Native query protocol(s) | Row-level security model |
|---|---|---|---|---|
| **SAP Datasphere** (ex Data Warehouse Cloud) | Cloud | Spaces → Views, **Analytic Models**, Fact/Dimension entities | Analytical/Relational **OData v4**; **SQL** via Open-SQL schema (HANA Cloud) | **Data Access Controls (DACs)** |
| **SAP HANA** / **HANA Cloud** | Both | **Calculation views**, tables, SQL views | **SQL** (JDBC/ODBC/`hdbcli`); calc views also OData/InA | **Analytic privileges** |
| **SAP BW / BW/4HANA** | On-prem (BW/4 also HANA Cloud) | **BEx queries**, InfoProviders, CompositeProviders | **InA** (native), **OData/EasyQuery**, MDX/BICS/XMLA, generated HANA calc view | **Analysis authorizations** (RSECADMIN) |
| **SAP BusinessObjects (BOBJ)** | On-prem (mostly) | **Universes (.unx)**, Web Intelligence documents | **REST `/biprws`** (Semantic Layer + Raylight), legacy RaaS/SOAP | CMS security + universe restrictions |
| **SAP S/4HANA** (embedded analytics) | Both | **CDS views** (`@Analytics.query`), OData services | **OData v2/v4**, InA, BICS | PFCG roles + **DCL access controls** |
| **SAP Analytics Cloud (SAC)** | Cloud | Models, Stories | **Data Export Service** (REST/OData) | Model data-access (but export is a consumption layer) |

**Unifying fact:** almost everything ultimately runs on HANA and is reachable via
**(a) SQL to HANA, (b) OData, or (c) InA**. The connector's job on the security side is
narrow but essential: **propagate the end-user identity** so SAP's own controls fire.

---

## 2. What already exists in the codebase (the baseline)

### 2.1 Connector contract
Every connector subclasses `DataSourceClient`
(`backend/app/data_sources/clients/base.py:67`). Required surface:
`description` (prompt doc), `test_connection`, `get_schemas` → `List[Table]`,
`get_schema`, `prompt_schema`, `execute_query` (+ `query` alias). Async wrappers
(`aget_schemas`, `aexecute_query`, …) offload blocking I/O. A `Capability` enum
(`base.py:10`) declares `QUERY` / file / mail capabilities; SAP clients are `QUERY`.

### 2.2 Registry & instantiation
Central static registry `REGISTRY` in
`backend/app/schemas/data_source_registry.py:359`. Each entry declares a
`config_schema`, a `credentials_auth` (`AuthOptions` → `AuthVariant`s with
`scopes: ["system","user"]`), and a `client_path`. `resolve_client_class()`
(`:1782`) imports the client. Existing SAP entries: `sap_hana` (`:397`),
`sap_datasphere` (`:407`).

### 2.3 Indexing pipeline ("index all the objects")
Two-tier storage:
- **`ConnectionTable`** (`backend/app/models/connection_table.py`) — the physical
  schema discovered from a connection (columns, pks, fks, topology metrics).
- **`DataSourceTable`** (`backend/app/models/datasource_table.py`) — the per-agent
  ("domain") **selection/activation** of those objects (`is_active`, metadata
  overrides), referencing a `ConnectionTable`.

Flow: `ConnectionIndexingService`
(`backend/app/services/connection_indexing_service.py`) runs a job → `ConnectionService.refresh_schema`
(`connection_service.py:960`) calls `client.aget_schemas(progress_callback, prior_catalog)`
(supports **incremental** indexing) → upserts `ConnectionTable` rows →
`_sync_linked_data_sources` fans out to `DataSourceTable` rows. This is exactly the
"index everything, then build an agent on a curated subset" model the user described.

### 2.4 Agents build on the indexed objects & execute queries
- `DataSource.get_schemas()` / `prompt_schema()` (`models/data_source.py`) surface
  `DataSourceTable` → `Table` with **usage-weighted top-k** selection, rendered into the
  LLM prompt by `ai/context/sections/tables_schema_section.py`.
- NL → query is done by **generating sandboxed Python** (not raw SQL emission): the
  planner (`ai/agents/planner/`) → coder (`ai/agents/coder/coder.py`) emits code that
  calls `ds_clients["<key>"].execute_query(...)`; `StreamingCodeExecutor`
  (`ai/code_execution/code_execution.py`) AST-validates (read-only) and runs it, wrapping
  each client to capture queries and enforce timeouts. `agent_v2.py` builds the
  per-connection `ds_clients` dict, **honoring per-user credential 403s**.

### 2.5 Per-user / delegated auth framework (already built — the key asset)
- **`Connection.auth_policy`** (`models/connection.py:32`): `system_only` (one
  service-account credential decrypted for everyone) vs **`user_required`** (per-user).
- **`UserDataSourceCredentials` / `UserConnectionCredentials`** — Fernet-encrypted
  per-(user, connection) credentials/tokens with `auth_mode`, `expires_at`.
- **`ConnectionService.resolve_credentials`** is the single source of truth for
  delegated/OBO tokens, OAuth refresh, the admin query-identity toggle, and the
  owner/admin system fallback. `DataSourceService.resolve_credentials`
  (`data_source_service.py:1876`) selects per-user creds for `user_required` sources.
- **Delegated OAuth engine** `backend/app/services/connection_oauth_service.py`:
  per-provider authorize/token URL builders (Microsoft, Google, ServiceNow,
  **SAP Datasphere at `:251`**), `exchange_code_for_tokens`, `refresh_access_token`,
  and **`exchange_obo_token`** (on-behalf-of), PKCE S256.
- **Identity-aware indexing**: `_refresh_shared_user_overlay`
  (`data_source_service.py:3862`) re-runs discovery with the *caller's* token to produce
  a **user-scoped overlay** of visible objects — so even the catalog respects per-user
  visibility.
- **Client construction** (`construct_client`, `:1911`) merges config + resolved creds,
  strips `oauth_*`/meta keys, and narrows to the constructor signature — so a per-user
  `access_token` flows straight into the client for the `user` scope.

### 2.6 How the existing SAP Datasphere client already uses all of this
`SapDatasphereClient.__init__` accepts either `client_id/client_secret` (technical user,
`client_credentials`) **or** a pre-obtained `access_token` (per-user). When
`access_token` is set, every request rides on it and no client-credentials grant is done
(`sap_datasphere_client.py:111-156`). The registry `sap_datasphere` entry (`:407`)
already exposes two auth variants: `technical_user` (scope `system`) and
`oauth` — "Sign in with SAP (per-user)" (scope `user`, `OAuthDelegatedCredentials`).
Discovery crawls the **catalog `assets`** collection with OData paging (`_list_assets`,
`:183`), parses each asset's `$metadata` into `role=measure`/`role=dimension`
(`_parse_metadata_columns`, `:268`), and queries the analytical OData endpoint with
server-side aggregation (`execute_query`, `:339`).

**Dependency note:** `hdbcli>=2.29.25` is the only SAP dep in `pyproject.toml`. The
Datasphere client uses `requests` + `defusedxml` only — no `pyodata`, no SOAP, no RFC.

---

## 3. Requirements → coverage matrix

| Requirement | Status today | Where / what's missing |
|---|---|---|
| **Connect to SAP** | ✅ Datasphere (OData) + HANA/Datasphere Open-SQL (SQL) | No BusinessObjects / BW / S/4 CDS / SAC |
| **Index all objects** | ✅ Datasphere catalog crawl + HANA `SYS.*`; stored in `ConnectionTable`/`DataSourceTable`; incremental + user-overlay | Per-product discovery adapters needed for the missing products |
| **Build agents on objects** | ✅ Generic — `DataSourceTable` activation + top-k prompt context | Nothing SAP-specific missing; works the moment objects are indexed |
| **Execute queries (NL→query)** | ✅ Generic codegen → `execute_query`; Datasphere (OData) & HANA (SQL) implemented | Query methods for the missing products |
| **Per-user auth (each user sees only their data)** | ✅ Framework: `user_required` + delegated OAuth + OBO + user-overlay indexing. ✅ Datasphere per-user OAuth variant wired | See §4 — SAML-bearer & Cloud-Connector propagation not yet implemented; on-prem HANA/BW SSO not implemented |
| **Cloud SAP** | ✅ Datasphere + HANA Cloud (443/TLS) | — |
| **On-prem SAP** | ⚠️ On-prem **HANA only** (direct SQL, port `3xx15`) | No Cloud Connector / principal propagation / Kerberos-SNC; no on-prem BW/BObj path |
| **Model on Datasphere/Power BI** | ✅ Architecture already mirrors Datasphere's "ingest → semantic layer → DAC" and Power BI's OData/ODBC choice | Kerberos-SSO-style on-prem propagation (Power BI's model) not present |

**Reading of the matrix:** the *platform mechanics* (indexing, agents, execution,
delegated-auth plumbing) are essentially done and generic. The remaining work is
**(1) more per-product SAP adapters** and **(2) the identity-propagation flows for true
per-user security, especially on-prem.**

---

## 4. The hard part — per-user authorization (deep dive)

Server-side row-level security in SAP fires **only under the real user identity**. Mapping
each SAP identity flow onto the existing framework:

| SAP identity flow | When to use | Fits existing framework as | Build effort |
|---|---|---|---|
| **OAuth2 authorization_code (per-user)** | Datasphere/BTP cloud, interactive users | Already implemented — `oauth` variant, `access_token` into client | ✅ Done for Datasphere |
| **OAuth2 SAML Bearer Assertion** (`grant_type=…saml2-bearer`) | Cloud SAP where your platform brokers identity (no interactive login per query) | New grant in `connection_oauth_service.py` + XSUAA broker; token → same `access_token` path | Medium |
| **XSUAA JWT / JWT→SAML exchange** | Anything on BTP; broker to downstream | New provider logic; reuses OBO plumbing (`exchange_obo_token`) | Medium |
| **Cloud Connector + Principal Propagation** (JWT/SAML → short-lived X.509, CN→user via CERTRULE) | **On-prem S/4, BW** behind firewall | New connectivity layer (tunnel/mTLS) + config; identity still resolves per user | High (infra) |
| **Kerberos Constrained Delegation / SNC** (CommonCryptoLib) | Direct on-prem **HANA/BW SSO**, the Power BI model | Extends existing Kerberos-delegated support already used for MSSQL (S4U2Self/S4U2Proxy) | Medium–High |
| **Technical user** (`client_credentials`) | Indexing, shared/non-sensitive models only | Already implemented (`system` scope) | ✅ — but must **never** back user-facing queries on RLS-protected objects |

**Design rule to enforce:** for any SAP object protected by DACs / analytic privileges /
analysis authorizations / DCLs, the connection's `auth_policy` **must** be `user_required`
and the query **must** run on a per-user token/identity. Use the technical user strictly
for catalog discovery of non-sensitive metadata and explicitly-shared models. The
existing `_refresh_shared_user_overlay` already gives per-user catalog visibility, so this
is a policy/wiring task, not a re-architecture.

**A subtlety worth flagging:** the Datasphere **Open-SQL / HANA SQL** path uses a
**database user**, which is a weaker per-user story than the OData consumption API with a
per-user OAuth token. If row-level DACs matter, prefer the **OData consumption** path
(per-user OAuth) over the SQL path for those models — mirror this tradeoff in the UI
guidance (it's the same ODBC-vs-OData tradeoff Power BI users face).

---

## 5. On-prem vs cloud connectivity

| Scenario | Mechanism | Status |
|---|---|---|
| Cloud SAP (Datasphere, HANA Cloud, SAC, S/4 Cloud) | Direct HTTPS/OData + SQL on 443 (TLS); OAuth/SAML bearer for identity | ✅ (Datasphere/HANA Cloud) |
| On-prem HANA | Direct JDBC/ODBC on `3xx15` inside network; Kerberos/SNC for SSO | ⚠️ SQL yes; SSO no |
| On-prem BW / S/4 | **SAP Cloud Connector** (reverse-invoke tunnel, no inbound ports) + **principal propagation**; or in-LAN RFC/OData | ❌ Not built |
| On-prem behind firewall from a SaaS | Cloud Connector is SAP's blessed pattern (outbound TLS tunnel; whitelisted resources) | ❌ Not built |

If the platform runs as SaaS, **on-prem SAP cannot be reached without Cloud Connector or
an in-network deployment/agent** — this is a hard infrastructure prerequisite, not a code
detail, and should be decided early.

---

## 6. Per-product connector recommendations

For each missing product, the discovery API, query protocol, per-user auth, and a
difficulty read:

### 6.1 SAP BusinessObjects (BOBJ) — most likely the literal ask
- **Discovery:** `/biprws/sl/v1/universes` (Semantic Layer REST), `/biprws/raylight/v1/documents` (Web Intelligence).
- **Query:** REST `/biprws` — logon `POST /biprws/logon/long` returns `X-SAP-LogonToken`; build/run ad-hoc queries against a universe; JSON or XML.
- **Per-user:** each end user logs on (Enterprise/LDAP/AD/SAML) → LogonToken carries their identity → universe + DB restrictions apply per user. Fits `user_required` with a token cached in `UserConnectionCredentials`.
- **Constraints:** **no official Python SDK** — plain `requests` over `/biprws`. On-prem-bound. **Highest business value if "business subjects" = BusinessObjects.**
- **Difficulty:** Medium (REST client + token lifecycle), no new heavy deps.

### 6.2 SAP S/4HANA embedded analytics (CDS views)
- **Discovery:** OData catalog `/sap/opu/odata/IWFND/CATALOGSERVICE;v=2/` (or V4); each service `$metadata`; analytical CDS carry `@Analytics.query: true`.
- **Query:** OData v2/v4 (`$select/$filter/$orderby/$top`). `pyodata` (Apache-2.0) or `requests`.
- **Per-user:** OAuth/SAML bearer to the gateway (cloud) or Cloud Connector principal propagation (on-prem); DCL access controls enforce rows.
- **Difficulty:** Medium. Cleanest deps (OData, open-source).

### 6.3 SAP BW / BW/4HANA (BEx queries)
- **Discovery:** list externally-released queries; InfoProviders/CompositeProviders.
- **Query, in order of preference:** **InA** (native JSON analytical — hierarchies, variables, richest) → **OData/EasyQuery** (simple but **only one structure** — a real functional limit) → generated **HANA calc view** via plain SQL (loses hierarchies/variables) → MDX/XMLA/BICS (BICS not licensed for external use).
- **Per-user:** analysis authorizations, enforced only under the named user → principal propagation or Kerberos/SNC.
- **Difficulty:** High (InA is semi-public; OData is limited; on-prem propagation needed).

### 6.4 SAP HANA calc views (deepen existing HANA client)
- Already have SQL. Add first-class handling of `_SYS_BIC` calculation views and analytic privileges; consider per-user HANA identity (Kerberos/SNC) instead of one technical DB user for RLS-protected views.
- **Difficulty:** Low–Medium (extends `SapHanaClient`).

### 6.5 SAP Analytics Cloud (SAC)
- **Discovery/Query:** Data Export Service (`/api/v1/dataexport/...`), OData-ish, `$select/$filter/...`.
- **Per-user:** OAuth; SAP sample wrapper is client-credentials only (technical user) — 3-legged per-user is possible but more work.
- **Recommendation:** **lowest priority** — SAC is a presentation layer; query the underlying Datasphere/BW/HANA source directly instead.

---

## 7. Recommended architecture & phased roadmap

**Principle:** don't build "a SAP connector" — build a **SAP connector family** sharing the
existing normalization (`Table`/`TableColumn`), indexing (`ConnectionTable`/`DataSourceTable`),
and delegated-auth substrate, differing only in discovery + query protocol + identity flow.

- **Phase 0 — Harden what exists (days).** Confirm Datasphere per-user OAuth end-to-end
  (authorize/token/refresh, DAC enforcement with a real user), tighten the technical-user
  vs per-user policy so RLS-protected models can't be queried on the shared identity,
  document the OData-vs-Open-SQL RLS tradeoff in the connect UI.
- **Phase 1 — Identify the real target (hours, needs user input).** Confirm whether
  "business subjects" = **BusinessObjects universes** (most likely), Datasphere, BW, or
  S/4 CDS. This single answer reorders everything below.
- **Phase 2 — Add the highest-value missing adapter.** If BusinessObjects: a `/biprws`
  REST client (`requests`) with LogonToken per-user auth and universe/Webi discovery. If
  S/4: a `pyodata`/OData CDS client. Both reuse indexing + agents unchanged.
- **Phase 3 — Per-user auth breadth.** Add the **SAML Bearer / XSUAA** grant to
  `connection_oauth_service.py` for cloud brokered identity; extend Kerberos-SNC (already
  present for MSSQL) to on-prem HANA/BW.
- **Phase 4 — On-prem reachability.** Design the **Cloud Connector / principal
  propagation** story (or an in-network agent). Infra decision, gated by deployment model.
- **Phase 5 — BW (InA) and SAC** as demand warrants.

---

## 8. Risks & licensing flags

- **Per-user security is the compliance make-or-break.** A shared technical user silently
  bypasses DACs / analytic privileges / analysis authorizations. Any user-facing query on
  RLS-protected data **must** run under the end-user identity. SAP explicitly warns against
  re-implementing authorization in a middle tier.
- **On-prem needs Cloud Connector** (or in-network deployment) — not reachable from SaaS
  otherwise. Decide the deployment model before promising on-prem.
- **Driver / library licensing:**
  - `hdbcli` — official HANA client; `pip`-installable but governed by the **SAP HANA
    Client license**; the underlying client is **not redistributable**. Already a dep.
  - `pyodata` — **Apache-2.0, fully open**, no SAP driver. Cleanest choice for OData
    (S/4, BW-OData, Datasphere OData).
  - `requests` — safe/open; the right tool for BusinessObjects `/biprws` and SAC DES.
  - **`pyrfc` + SAP NW RFC SDK — avoid.** Proprietary, license-gated, **non-redistributable**,
    and the wrapper is **decommissioned/unsupported**. Design around RFC using OData/SQL/InA.
- **Functional limits to design around:** BW-OData exposes **only one structure** per query;
  BObj has **no Python SDK** (REST only); Datasphere path prefix changed in 2025.19
  (`/api/v1/dwc/...` deprecated, supported until **March 2027** — the client already makes
  these paths configurable).
- **Verification caveat:** several SAP Help/Community pages block automated fetching (403);
  exact `/biprws` sub-paths and a few endpoint specifics should be spot-checked against the
  SAP Help Portal before implementation.

---

## 9. Open questions for the user (blocking prioritization)

1. **Which SAP product(s)?** Confirm "business subjects" — BusinessObjects universes,
   Datasphere, BW/BEx, S/4 CDS, or a mix. (Most likely BusinessObjects + Datasphere.)
2. **On-prem, cloud, or both — and is the platform SaaS or customer-hosted?** This decides
   whether Cloud Connector / in-network deployment is needed at all.
3. **Per-user auth mechanism available in the customer's landscape:** interactive OAuth,
   SAML IdP/XSUAA brokering, or Kerberos/AD SSO? Different customers will have different
   answers; the framework can support several.
4. **Acceptable to use the technical user for indexing only**, with per-user tokens for all
   RLS-protected queries? (Recommended.)
5. **Priority order** across products, given the effort reads in §6.

---

## 10. Key files (anchors)

- Contract: `backend/app/data_sources/clients/base.py:67`
- Registry: `backend/app/schemas/data_source_registry.py:359` (SAP: `:397`, `:407`)
- Existing SAP clients: `backend/app/data_sources/clients/sap_datasphere_client.py`,
  `backend/app/data_sources/clients/sap_hana_client.py`
- Config/creds schemas: `backend/app/schemas/data_sources/configs.py:46` (HANA), `:98` (Datasphere)
- Indexing: `backend/app/services/connection_indexing_service.py`,
  `connection_service.py:960`; storage `models/connection_table.py`, `models/datasource_table.py`
- Per-user auth: `models/connection.py:32` (`auth_policy`),
  `services/connection_oauth_service.py` (SAP Datasphere `:251`, `exchange_obo_token`),
  `services/data_source_service.py:1876` (`resolve_credentials`), `:3862` (user overlay)
- NL→query: `ai/agents/coder/coder.py`, `ai/code_execution/code_execution.py`, `ai/agent_v2.py`

---

## 11. On-prem BO + BW connectivity design (verified)

**Scenario:** the product is deployed **self-hosted inside the customer's LAN**, and must
reach **SAP BusinessObjects (universes/Webi)** and **SAP BW/BW4HANA** running on-prem in
the same network.

**Topology — Cloud Connector drops out entirely.** SAP Cloud Connector / principal
propagation exist only to cross a *cloud→on-prem* boundary. An in-network deployment talks
to SAP **directly over the LAN**. Open only the needed ports: BO `/biprws` on the web tier
(Tomcat `8080` / WACS `6405`, TLS), BW ABAP HTTP/ICF (`8000`/`44300`) for InA/OData/Gateway,
and — if BW-on-HANA — the HANA SQL port `3<inst>15`. The two real design problems are
**protocol choice** and **running each query under the end-user identity**.

### 11.1 BusinessObjects — clean, supported, no proprietary binaries

- **Logon:** `POST /biprws/logon/long` with `{"userName","password","auth"}`. Response
  carries the token in the **`X-SAP-LogonToken`** header; resend it on every call
  **wrapped in double quotes** (the token contains header-illegal chars). Logoff
  `POST /biprws/logoff`.
- **Auth plugins (`auth`):** `secEnterprise` | `secLDAP` | `secWinAD` | `secSAPR3`. Each
  resolves to a **named CMS user**, which drives all security. `secSAPR3` aliases the BO
  user to an SAP/BW account — useful for passing SAP identity down to a BW OLAP connection
  behind a universe.
- **Trusted authentication = per-user impersonation WITHOUT the user's password** (the key
  capability for a trusted in-LAN app):
  - **Shared-secret:** enable in *CMC → Authentication → Enterprise → Trusted
    Authentication*, download `TrustedPrincipal.conf`; the app logs a named user on via the
    trusted logon endpoint sending header **`X-SAP-TRUSTED-USER: <username>`** + the shared
    secret (KBA 2866781).
  - **X.509 client cert** (BI 4.2 SP04+) — SAP's recommended, more secure variant.
  - Pattern: authenticate the end user once at our platform, then mint a **per-user BO
    token** via trusted auth. No passwords collected.
- **Discovery / query:** Semantic Layer base `/biprws/sl/v1` — enumerate universes
  `GET /biprws/sl/v1/universes` (+ `/{id}` for metadata); Raylight (Web Intelligence) base
  `/biprws/raylight/v1` — `POST /biprws/raylight/v1/documents` to create a doc from a
  universe, add a data provider, refresh, read dataprovider results. (Exact SL query JSON
  is version-specific — validate against the 4.3 Webi RESTful WS Dev Guide during build.)
- **Row/object security:** universe security profiles + CMC object rights enforce
  **automatically** against the logged-on named identity — trusted auth produces a genuine
  named-user session, so nothing is bypassable.
- **Deps:** pure HTTPS/JSON via `requests`. **This is the recommended primary path.**

### 11.2 BW / BW4HANA — pick the protocol (ranked)

1. **OData via SAP Gateway (supported, redistributable).** Release the BEx query "By OData"
   (`RSZELTPROP-ODATASUPPORT`; EasyQuery lineage via `EQMANAGER`), activate in
   `/IWFND/MAINT_SERVICE`, consume at `/sap/opu/odata/sap/<SERVICE>/…/Results`. Enforces
   analysis authorizations under the named user. **Hard limit: "only one structure" per
   query** — a two-structure grid (e.g. key figures × Actual/Plan/PY columns) is not
   expressible and can return empty. Best *supported* option where query shapes allow.
2. **Generated external HANA view + plain SQL** (reuses `SapHanaClient`). BW generates a
   calc view `_SYS_BIC."system-local.bw.bw2hana.query.<PROVIDER>/<QUERY>"`, queryable with
   HANA SQL — fast, no OLAP runtime. **⚠️ Security nuance (important):** the SQL path
   enforces **HANA analytic privileges only**, *not* the live BW analysis-authorization
   engine. BW auth must be **converted** to HANA analytic privileges by
   `RS2HANA_ADMIN`/`RS2HANA_CHECK` (table `RS2HANA_AUTH_FIL`). Constructs that don't
   translate (some hierarchy/complex auths) are **silently not enforced** → data-exposure
   risk. Only use when RS2HANA coverage is validated per model, and still requires per-user
   identity propagated to HANA.
3. **InA (`/sap/bw/ina/GetCatalog|GetResponse|GetServerInfo|ValueHelp`, activated in
   SICF).** Richest (hierarchies/variables/drilldown; what SAC live & Analysis for Office
   use), no RFC SDK. **⚠️ Proprietary & undocumented for third parties** — SAP states there
   is no public spec. Building against it is reverse-engineering an unsupported interface →
   flag as risk.
4. **MDX/XMLA & OLAP BAPIs (`RSR_OLAP*`, `BAPI_MDDATASET*`) via RFC — avoid.** Needs the
   proprietary, non-redistributable NW RFC SDK / JCo.

### 11.3 Per-user identity to BW (no Cloud Connector) — ranked

- **A) SAP Assertion / Logon Tickets (`MYSAPSSO2`) — cleanest password-less OBO for HTTP.**
  A trusted in-LAN issuer mints a ticket for a named user for InA/OData/Gateway. Issuer:
  `login/create_sso2_ticket=3` (assertion-only); BW: `login/accept_sso2_ticket=1` + import
  the issuer signing cert into the ticket ACL via `STRUSTSSO2`; users must be dialog users.
  Ticket carried as `MYSAPSSO2` cookie/header.
- **B) Kerberos/SPNego** to the ABAP ICF stack (add SPNego to the SICF logon procedure;
  needs Common Crypto Lib) + **SNC** for any RFC. AD→SAP mapping on the SU01 SNC tab.
  Extends the Kerberos-constrained-delegation already used for MSSQL.
- **C) X.509 client-cert mapping** via `CERTRULE` / `USREXTID` (`VUSREXTID`), per user in
  SU01.
- **D) Basic auth with each user's own SAP credentials — simplest fallback.** Runs under
  the named SAP user, so `RSECADMIN` analysis authorizations are **fully enforced** by the
  OLAP runtime. Cost: platform stores/relays per-user SAP creds
  (`UserConnectionCredentials`, already Fernet-encrypted with `expires_at`).

**Make-or-break constraints:** (1) BW analysis authorizations enforce **only** when the
query runs under the end user's SAP identity — so §11.3 is mandatory, not optional; (2) the
OData "one structure" limit constrains query shapes; (3) the generated-HANA-view path can
under-enforce complex BW auths.

### 11.4 Mapping onto this codebase

- **New `businessobjects` client** (`requests` + LogonToken lifecycle) — discovery via
  `/biprws/sl/v1/universes`; `execute_query` drives a universe query/Webi data provider.
  Register in `data_source_registry.py` with `auth_policy="user_required"`; auth variants
  for `secLDAP`/`secWinAD`/`secEnterprise` **and a `trusted` variant** (shared secret /
  X.509) for password-less per-user impersonation.
- **New `bw` client** — default to **OData** (`pyodata`/`requests`); optionally reuse
  `SapHanaClient` against generated HANA views for BW/4HANA where RS2HANA coverage is
  validated. Auth variants: `basic` (per-user creds), `sso2_ticket`, `kerberos`.
- **Reused unchanged:** per-user credential storage/refresh (`UserConnectionCredentials`,
  `ConnectionService.resolve_credentials`), catalog storage + **user-scoped overlay**
  (`ConnectionTable`/`DataSourceTable`, `_refresh_shared_user_overlay`), agent context, and
  NL→query codegen/execution. The connectors add *discovery + query protocol + identity
  flow* only.

**Recommendation:** ship **BusinessObjects first** (supported REST + trusted auth = real
per-user security, no proprietary deps). For **BW**, baseline on **OData + per-user
identity (assertion ticket, or basic auth to start)**; treat the generated-HANA-view path
as a performance option gated on validated RS2HANA auth coverage; treat InA as a
richer-but-unsupported future option.

