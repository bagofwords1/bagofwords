# OpenText Documentum Connector — Research & Recommendation

**Status:** Research / analysis only — no implementation.
**Goal:** Let users connect an OpenText Documentum repository to BOW so the agent can
browse folders, find documents, read their content and metadata, and analyze the
spreadsheets/PDFs/Office files stored in it.
**Researched:** 2026-09-08. Brand page: https://www.opentext.com/about/brands/documentum.
Every claim below carries a source; §10 lists what could *not* be verified (OpenText's
developer portal and the official guides sit behind a JavaScript shell / My Support
login, so several details are second-hand via OpenText staff forum posts, OpenText's own
public javadocs, and reference client source code).

---

## 0. Bottom line up front

1. **There is no Documentum MCP server — official or community — to point a preset at.**
   The official MCP registry returns zero results for `documentum`, `opentext` and `dctm`
   (probed live). OpenText has said (May 2026) that Content Cloud "will support" MCP and
   A2A and ships MCP servers in its *DevOps* portfolio, but nothing for Documentum. The
   only community "Documentum MCP" repos are two hobby projects with 0 stars, one
   archived. So "add Documentum MCP" cannot mean "add an `McpPreset`."

2. **What does exist is a mature, well-structured REST API: Documentum REST Services**
   (`https://{host}/dctm-rest`). It ships with every Documentum Server / Documentum
   Content Management release from 7.x through CE 26.2 (June 2026), is hypermedia-driven
   (HAL-style `links[]`), speaks `application/vnd.emc.documentum+json`, and exposes
   repositories → cabinets → folders → documents, content download, object types, a
   **read-only DQL endpoint**, xPlore full-text search with facets, users/groups/ACLs and
   audit trails. This is the surface every modern integrator uses (Elastic's connector,
   One Fox's Power Automate connector, community RAG builds).

3. **Recommendation: build a native `documentum` connector with `data_shape="files"`,
   modeled on `sharepoint_onprem` + `network_dir`.** Documentum is a document repository;
   it maps onto BOW's file-source contract (`list_files` / `read_file` / `search_files` /
   `read_raw_bytes`) almost one-to-one (§6). That gives the agent real files it can read,
   grep, attach and analyze — not a bag of opaque JSON tools. No new driver dependency:
   plain `requests`.

4. **Do not route this through `CustomApiPreset` or a third-party bridge.** Documentum is
   per-customer on-prem (or OpenText-hosted per tenant), so the fixed `base_url` /
   `server_url` of either preset mechanism cannot express it — the same gap that blocks a
   Priority ERP preset. And the agent would lose file semantics.

5. **Auth: Basic first, OTDS OAuth2 second, Kerberos as a service-account option.**
   REST Services defaults to HTTP Basic against repository users; since Documentum 23.4
   OpenText Directory Services (OTDS) is mandatory and 24.4 made it the license gate, so
   customers on current releases increasingly front REST with OTDS OAuth2 bearer tokens
   (`oauth2` / `otds_token` modes). Per-user credentials work the same way "User-required
   NTLM" works for SharePoint Server today.

6. **The blocker is not the API, it is the lab.** There is no developer edition, trial,
   public sandbox or public Docker image. Container images (`dctm-server`, `dctm-rest`,
   …) come from `registry.opentext.com` / My Support and require a customer or partner
   entitlement. Integration testing needs a customer sandbox or partner access; unit
   tests can mock the HTTP boundary with the documented response shapes (Elastic's
   open-source connector is a ready-made fixture reference).

---

## 1. What "Documentum" is in 2026 (landscape)

| Product name (brand page) | What it is | API relevance |
|---|---|---|
| **OpenText Documentum Content Management** (formerly "Documentum Platform"; Content Server + REST + xPlore + DA) | The repository itself. Release train "Cloud Editions" twice a year: 24.2 → 24.4 → 25.2 → 25.4 → 26.1 → **26.2 (June 8, 2026)**. On-prem installers still ship (26.2: PostgreSQL 18, JDK 21). | **Primary target.** Documentum REST Services (`/dctm-rest`) is its API. |
| **Documentum D2** | Configurable business-user client on top of Content Server. | Has its own REST extension, **D2-REST** (`/d2fs-rest`, renamed `client-rest` in 26.2), layered on Documentum REST and adding D2 config/security semantics. Only matters if a customer wants D2-specific behavior. |
| **Documentum xCP** | Case-management application platform. | Per-application REST; not relevant. |
| Documentum CM for Life Sciences / Engineering | Vertical solutions on the same platform. | Same REST surface; heavier custom object types. |
| Deployment | On-prem, private cloud, public cloud (AWS/GCP/Azure), OpenText-managed, "sovereign cloud". Licensing consolidated into "Documentum X-Plans" since 23.4. | REST endpoint is per-customer in all cases. |

Not to be confused with (different products, different APIs):
- **OpenText Content Management** (formerly Extended ECM / Content Server, "OTCS") — has its own REST v2 API, an official Python library (`pyxecm`, actively released on PyPI as of Sept 2026), and several community MCP servers. None of that works against Documentum.
- **OpenText Core Content Management** — multi-tenant SaaS on `*.api.opentext.com`. The OT2 endpoint `https://na-1-dev.api.opentext.com/dctm-rest/repositories` returns 404; there is no public Documentum trial instance (forum threads 309883, 313575).

**AI story.** Content Aviator for Documentum (since CE 24.3) is OpenText's own assistant; 25.4 added a "Direct LLM Access API" and off-cloud LLM option; 26.2 added "chat-based APIs (DFC and REST)" exposing Aviator. Aviator Studio (agent builder) advertises "open protocols such as MCP and A2A" with no dates or product list. None of this gives a third-party assistant an MCP endpoint into Documentum; the integration substrate remains REST. Sources: https://blogs.opentext.com/whats-new-in-opentext-documentum-content-management/ , https://blogs.opentext.com/modernize-your-content-foundation-for-the-era-of-agentic-ai/ , https://www.opentext.com/aviator-ai/aviator-studio .

---

## 2. Does Documentum have an MCP server? — the evidence

| Source | Finding |
|---|---|
| `registry.modelcontextprotocol.io/v0/servers?search=documentum` / `=opentext` / `=dctm` | `{"servers":[],"metadata":{"count":0}}` for all three (probed 2026-09-08). |
| github.com/opentext (104 repos) | No MCP, no Documentum REST client. `pyxecm` is for Content Server, not Documentum. |
| github.com/Enterprise-Content-Management (the official "OpenText Documentum" org) | REST reference clients (Java/.NET/Python/Ruby/Swift/ObjC), samples, extensibility SDK — last meaningful updates 2018–2022. No MCP. |
| PulseMCP, Smithery, Glama, mcp.so, npm, PyPI (`documentum-mcp`, `dctm-mcp`) | Nothing. |
| GitHub community | `maitreya-ai/AIAdoption-GovernanceEngine` (`packages/mcp-servers/documentum`: TypeScript, Basic auth, tools `search_documents` (DQL), `get_document`, `get_document_content`, `list_repositories`, `list_cabinets`; **archived, 0 stars, 1 commit**). `ancyonio/DQLOptimizer` (`dctm-dql-mcp`, Python, DQL tuning, "non-production", created 2026-08-13, 0 stars). |
| OpenText statements | Blog 2026-05-13: Content Cloud "will support emerging interoperability standards such as MCP and A2A". Forrester (Nov 2025): "There's an MCP server for Performance Engineering" (DevOps). No Documentum MCP announced. |

**Conclusion: nothing to point a preset at.** If a customer wants MCP, the only path is a
self-built server over REST — which is strictly less useful to BOW than a native file
connector (no catalog, no file reads/attach, fixed tool list).

---

## 3. Documentum REST Services — the surface we would build on

### 3a. Deployment and discovery
- Delivered as `dctm-rest.war` (Spring Boot) on Tomcat; configured by
  `WEB-INF/classes/dfc.properties` (docbroker, global registry) and
  `rest-api-runtime.properties` (auth mode, paging, CORS, CSRF). Official Docker image since
  REST 7.3; in the Kubernetes edition it is the `dctm-rest` container.
- Home document: `GET /dctm-rest/services` (`application/home+json`) → links to
  `repositories` and `about`.
- `GET /dctm-rest/repositories` → feed of repositories (docbases).
  `GET /dctm-rest/repositories/{repo}` → repository resource whose `links[]` are the entry
  points: `cabinets`, `folders`, `users`, `groups`, `types`, `formats`, `dql`, `search`,
  `saved-searches`, `acls`, `audit-trails`, `batch-capabilities`, …
- Versions: REST 7.0 (2013, context root then `/documentum-rest`), 7.1–7.3, 16.4, 16.7,
  20.x, 21.x, 23.2, 23.4, 24.2, 24.4, 25.2, 25.4, 26.2. OpenText publishes the REST
  extensibility javadocs for 23.4 → 26.2 publicly in https://github.com/opentext/d2sv-sdk
  (`<ver>/dctm-rest/`), which is where the constant names below come from. 23.4+ is
  Spring 6 / JDK 17 and Tomcat 10; build URLs **without trailing slashes**
  (`rest.requestmapping.trailing.slash.match` is being phased out).

### 3b. Media types and representation
Constants from `SupportedMediaTypes` (REST 25.4 javadoc):
`application/vnd.emc.documentum+json` (canonical), `application/vnd.emc.documentum+xml`,
`application/hal+json`, `application/home+json`, `application/atom+xml` (XML feeds),
`application/json` / `application/xml` accepted "for compatible viewing", `application/zip`,
`multipart/related`. Content negotiation also works via `.json` / `.xml` suffixes.

Feed shape (actual response, dbi-services walkthrough):
```json
{
  "id": "http://HOST/dctm-rest/repositories/REPO/cabinets",
  "title": "Cabinets", "author": [{"name": "EMC Documentum"}],
  "updated": "2018-09-28T14:41:29.686+00:00",
  "page": 1, "items-per-page": 100, "total": 12,
  "links": [{"rel": "self", "href": "..."}, {"rel": "next", "href": "..."}],
  "entries": [
    {"id": "...", "title": "Contracts", "summary": "...", "updated": "...",
     "links": [{"rel": "self", "href": "..."}, {"rel": "edit", "href": "..."}],
     "content": {"properties": {"r_object_id": "0c0180aa80001107", "object_name": "Contracts"}}}
  ]
}
```
`content.properties` is populated only with `?inline=true`; otherwise entries carry just
id/title/links and you follow `self`. Repeating attributes are arrays.

Link relations: standard (`self`, `edit`, `contents`, `content`, `enclosure`, `next`,
`previous`, `first`, `last`, `version-history`, `predecessor-version`) plus
Documentum-specific rels prefixed `http://identifiers.emc.com/linkrel/`: `cabinets`,
`folders`, `documents`, `objects`, `parent-links`, `child-links`, `primary-content`,
`content-media`, `type`, `types`, `acl`, `permissions`, `permission-set`, `dql`, `search`,
`saved-searches`, `audit-trails`, `relations`, `virtual-document-nodes`, `lightweight-objects`,
`checkout`/`checkin-*`, `lifecycle`, … (full catalogue: `LinkRelation.java` in the official
Java reference client). D2-REST rels use `http://identifiers.opentext.com/linkrel/`.

### 3c. Common query parameters (`QueryParamNames`, REST 25.4)

| Param | Purpose |
|---|---|
| `inline=true` | Embed full object properties in feed entries. |
| `links=false` | Omit `links[]` (smaller payloads). |
| `view=object_name,r_modify_date` / `view=:all` | Attribute projection. |
| `filter=contains(object_name,"COFFEE") and r_modify_date >= date("2026-01-01")` | XPath-like predicate; operators `= < > <= >= !=`, `and/or/not()`, `date()`, `starts-with()`, `contains()`. |
| `sort=r_modify_date desc` | Sort. |
| `page=N&items-per-page=M` | 1-based paging; default `rest.paging.default.size`, capped by `rest.paging.max.size` (server-configured — do not assume; observed 100 and 1000). Pagination links `next`/`previous`/`first`/`last`. |
| `include-total=true` | Return `total` (costly; use sparingly). |
| `media-url-policy=local|all`, `network-location=` | Whether content links point at REST itself or at ACS/BOCS content servers. **Use `local`** so the binary is streamed by the REST WAR under the same auth. |
| `recursive=true&parent-type=dm_document` | Type hierarchy listing. |
| `object-type=<type>` | Filter a folder's `documents` collection by type. |

### 3d. Resources a read-only connector needs (relative to `/dctm-rest`)

| Purpose | Request |
|---|---|
| Test connection / current user | `GET /repositories/{repo}` ; `GET /repositories/{repo}/currentuser` |
| Cabinets (top-level folders) | `GET /repositories/{repo}/cabinets?page=1&items-per-page=100` |
| Child folders / documents | `GET /repositories/{repo}/folders/{id}/folders` ; `GET /repositories/{repo}/folders/{id}/documents?inline=true` ; `…/objects` for everything |
| Object metadata | `GET /repositories/{repo}/objects/{r_object_id}` (or `/documents/{id}`) |
| Content download | Follow rel `primary-content` (`GET /repositories/{repo}/objects/{id}/contents/content?media-url-policy=local`) → content resource → follow `enclosure` → bytes. Renditions: `GET …/objects/{id}/contents?inline=true`. Shortcut rel `content-media` streams the primary rendition directly (query params seen in the wild: `format=pdf`, unverified). |
| Object types | `GET /repositories/{repo}/types?recursive=true&parent-type=dm_document` ; `GET /repositories/{repo}/types/{name}?inherited=false` |
| **DQL (SELECT only)** | `GET /repositories/{repo}?dql=<urlencoded SELECT>&items-per-page=200&page=1` → feed; columns in `entries[].content.properties`; paginate via `next`. |
| Simple full-text search | `GET /repositories/{repo}/search?q=<terms>&inline=true&items-per-page=50` |
| Structured search (AQL, facets, path scoping) | `POST /repositories/{repo}/search` with JSON body: `types`, `columns`, `sorts`, `locations` (`pathLocation("/Cabinet/Path", descend)`), `expression-set` (`fulltext`, `property` expressions), facet definitions. |
| Versions | rel `version-history` on an object |
| Users / groups / ACLs | `GET /repositories/{repo}/users`, `/groups`, `/acls`; rels `permissions`, `permission-set` |
| Audit trail | rel `audit-trails` (`filter`, `sort`, `view`, paging) |
| Batches (7.2+) | rel `batches` — multiple operations per request; not needed read-only |

### 3e. DQL essentials for the connector
- Types: `dm_sysobject` (root), `dm_document`, `dm_folder` (`dm_cabinet` is a subtype),
  `dm_user`, `dm_group`. Customers subtype `dm_document` heavily; `FROM dm_document`
  includes subtypes.
- Identity: `r_object_id` = one version; `i_chronicle_id` = the version tree.
  Default queries return CURRENT versions only; `FROM dm_document (ALL)` returns all.
- Scoping: `WHERE FOLDER('/Cabinet/Path', DESCEND)`; `r_folder_path` lives on `dm_folder`;
  `i_folder_id` links objects to folders (objects can be linked into **several** folders —
  paths are not unique ids).
- Useful columns: `object_name`, `title`, `subject`, `a_content_type` (format name, e.g.
  `pdf`, `msw12`, `excel12book`), `r_full_content_size`, `r_content_size`, `r_modify_date`,
  `r_creation_date`, `r_modifier`, `owner_name`, `r_version_label` (repeating), `acl_name`,
  `acl_domain`, `keywords` (repeating), `r_object_type`.
- Full text: `SELECT … FROM dm_document SEARCH DOCUMENT CONTAINS 'budget forecast'`
  (xPlore must be installed and indexed); REST `/search` (AQL) is the cleaner route.
- Paging: DQL has no OFFSET/LIMIT. `ENABLE (RETURN_TOP n)` for top-N;
  `ENABLE (RETURN_RANGE start end 'attr ASC')` for pages (1-based, sort clause mandatory).
  Via REST, `page`/`items-per-page` on the DQL resource is simpler and server-cursored.
- Gotchas: `select * from dm_job` breaks the REST view (alias `r_object_id as method_id`);
  aliasing to system-attribute names such as `r_creation_date` trips REST date formatting.
  Always select explicit columns.
- Registered tables: `SELECT … FROM dm_dbo.<table>` works through DQL; no dedicated resource.

### 3f. Other APIs — and why they don't change the plan

| API | Status | Verdict |
|---|---|---|
| **DFC** (Java library; what REST itself uses) | Current; 26.2 Aviator chat API is "DFC and REST". | Java only — would need a JVM sidecar. Used by full-corpus crawlers (Aspire, IDOL, Mindbreeze, Coveo, migration tools). Not for a Python backend. |
| **DFS** (SOAP) | Still ships (23.4 release notes found); community expects REST to supersede it. No EOL notice found. | Skip. |
| **CMIS** (`dctm-cmis.war`; AtomPub + Browser binding) | Still ships and was extended in 24.x/25.x (SAP S/4HANA). Separate WAR, often not deployed; known slow multi-repo auth. | Viable fallback from Python (`cmislib`) if a customer refuses to expose `/dctm-rest`, but loses DQL, renditions detail, ACL richness. Not the primary path. |
| **D2-REST** (`/d2fs-rest`, `client-rest` in 26.2) | Current; superset of Documentum REST with `-d2` rels. | Support as an alternate `rest_url` only; base resources are the same. |
| xCP REST, Smart View SDK, Documentum Reporting Services | App-specific / UI / legacy. | Irrelevant. |
| Content Aviator "chat-based APIs" (26.2) | Announced; no public endpoint spec found. | Watch; not a connector substrate. |

---

## 4. Authentication

Governed by `rest.security.auth.mode` in `rest-api-runtime.properties`; the javadoc
default is `basic` (`@Value("${rest.security.auth.mode:basic}")`, `DefaultSecurityRuntime`,
REST 25.4). One mode string is active; compound modes are hyphenated; a fallback mode can
be set via `rest.security.sso.fallback.auth.mode`.

| Mode | Client sends | Notes |
|---|---|---|
| `basic` (default) | `Authorization: Basic <repo user:password>` | Credentials are **repository users** (or OTDS-synchronized users). Works everywhere; what Elastic's and One Fox's connectors use. |
| `basic-ct` / `ct-*` | Basic once; server sets cookie `DOCUMENTUM-CLIENT-TOKEN` (timeout `rest.security.client.token.timeout`); replay cookie | Session-style; CSRF header `DOCUMENTUM-CSRF-TOKEN` required for POST/PUT/DELETE only (`rest.security.csrf.enabled=true` default). Irrelevant for a GET-only connector. |
| `oauth2` / `ct-oauth2` | OTDS OAuth2 access token | OTDS token endpoint `{otds}/otdsws/oauth2/token` (client credentials) or `/otdsws/oauth2/auth?response_type=token&client_id=…` (implicit) / authorization-code for per-user. Community walkthrough (Documentum 21.2) passes the token as `?access_token=<TOKEN>`; `Authorization: Bearer` acceptance by `dctm-rest` in every mode is **not verified** (§10). OTDS "impersonation" must be enabled on the resource/client for delegated access. |
| `otds_token`, `otds_ticket-otds_token`, `otds_password` (+ `ct-` variants) | OTDS token / ticket / OTDS-validated password | Listed in the D2FS REST 24.2 guide as the supported set alongside `basic`, `basic-ct`, `oauth2`, `ct-oauth2`. From 23.4, D2-REST "will support only full authentication for the oauth2 and ct-oauth2 modes" and "removed the source code of the deprecated authentication mechanisms" — so treat Kerberos/CAS/ClearTrust as legacy on 23.4+ (whether Documentum REST itself removed them is unverified). |
| Kerberos / SPNEGO | 401 + `WWW-Authenticate: Negotiate`, then `Authorization: Negotiate <token>`; SPN `HTTP/<host>` | Documented for REST 7.3; header table in the D2FS guide still lists it. Our existing `requests-gssapi` + mounted-keytab story from `docs/sharepoint-server.md` applies verbatim. Legacy on new releases. |
| Pre-authenticated ("trusted") | Upstream proxy asserts the user identity in a header; REST logs the user in via a superuser principal | OpenText staff answer (forum 312650): "Documentum expects requests to be already authenticated upstream … It then uses a superuser account to perform DFC Principal authentication to login the user." This is the only documented *password-less per-user* path when the customer cannot hand out OTDS tokens. Requires customer-side config. |
| CAS, RSA ClearTrust, SAML | Proprietary SSO chains | Legacy; not worth supporting. |

**OTDS is the key environmental fact.** OTDS has been mandatory since Documentum 23.4
and since 24.4 also gates licensing ("you won't be able to use any client without license,
not even with dmadmin"). Server-side, the `OTDSAuthentication` web app / process bridges
Content Server to OTDS (`otdsauth.properties`: `otds_rest_oauth2_url=${otds}/otdsws/oauth2/token`,
`client_id`, `client_secret`, `<repo>_resource_id`, …). REST-side, admins set e.g.
`rest.security.auth.mode=ct-otds_token-basic` and `rest.security.otds.login.url=http://otds:8181/otdsws/login`.
In practice this means: on 23.4+ even Basic credentials are validated through OTDS, and
the customer's IdP (Entra ID etc.) can sit behind OTDS via OIDC. Sources:
https://blog.aldago.es/2025/02/16/otds-faq-for-documentum/ ,
https://blog.aldago.es/2025/05/19/documentum-24-4-otds-licensing-configuration/ ,
https://appworks-tips.com/2021/10/01/113_bring_life_in_the_documentum_connector/ .

**Content Server login tickets** (`DM_TICKET…`, IAPI `getlogin`, default 30-min TTL) can
replace a password in DFC; using one as the Basic password against REST is plausible but
unverified. Not needed for v1.

**Per-user delegated auth, ranked for BOW:**
1. Per-user Basic (repository or OTDS-synced username/password) — `scopes=["system","user"]`, exactly today's SharePoint Server "User-required NTLM" UX. Security trimming is then done by Content Server ACLs on every call, for free.
2. OTDS OAuth2 authorization-code per user via the existing `OAuthDelegatedCredentials` flow — only when the customer has OTDS OAuth clients configured for REST (`oauth2` mode). Needs a lab to validate token transport (`Authorization: Bearer` vs `access_token` param).
3. Shared service account (Basic or Kerberos keytab) — simplest, but every BOW user sees whatever the service account sees. Flag in the UI exactly as `sharepoint_onprem` does for Kerberos service mode.

---

## 5. SDKs, client libraries, reference code

| Library | Lang | Targets | Auth | Status | Use for us |
|---|---|---|---|---|---|
| `Enterprise-Content-Management/documentum-rest-client-java` | Java (Spring RestTemplate) | REST 7.1–7.3 | Basic, Basic+client token, CSRF | Apache-2.0; last update Dec 2022; 47★ | **Best reference** for link rels (`LinkRelation.java`), DQL/search/content samples (`DQLQuerySample`, `SearchSample`, `ContentManagementSample`). |
| `…/documentum-rest-client-python` | Python **2.7–3.5**, `requests` | REST 7.2 | Basic | Apache-2.0; last content change Feb 2018; not on PyPI | Reference only: `RestClient.py`/`RestDemo.py` show `dql()`, `simple_search()`, `aql_search()`, `next_page()`, `items-per-page`/`page`/`inline` params. **Do not depend on it.** |
| `…/documentum-rest-client-dotnet`, `-ruby`, `-swift`, `-objective-c`, `-sample-html5`, `-sample-filemanager`, `-extensibility-samples` (+ a Postman collection `REST-BOF-Tutorial.collection.json`) | various | REST 7.2/7.3 | Basic (+Kerberos in .NET) | 2017–2018 | Postman collection is a handy request catalogue. |
| `elastic/connectors` — `connectors/sources/opentext_documentum.py` (branch 8.14; **removed from `main`**) | Python 3, `aiohttp` | REST (`/dctm-rest`) | Basic; header `content-type: application/vnd.emc.documentum+json` | **Elastic License 2.0** (source-available, not OSI open source — read for reference, do not vendor); "example connector, as-is"; no permission sync; full sync only; 10 MB extraction cap | **Closest thing to our connector in Python** — 499 lines; URL templates for repositories/cabinets/folders/recursive folders/documents with `page`/`items-per-page`, walks `entries[]`, uses `id`, `title`, `updated`, `size`; handles 429 `Retry-After`. Good fixture/shape reference. |
| `dctmpy` (PyPI 0.3.3, 2017; ZPL-2.1; Andrey Panfilov) | Python | Native Content Server client protocol (no DFC/REST — protocol not confirmed) | repo credentials | unmaintained | No. |
| `opentext/pyxecm` (PyPI, Sept 2026, Apache-2.0) | Python | **Content Server (OTCS) REST** | OTDS | active, official | Not Documentum — but a good model for how OpenText expects OTDS OAuth to be driven from Python. |
| `backbridge/ootd` (TS, 2026), `mmohen/dctm-rest-samples` (Spring Boot over REST 7.3 + CMIS), `aldago/DctmRestJava11`, `jppop/dctm-docker` (sample `rest-api-runtime.properties`) | misc | REST | — | small / stale | Config-file and request examples. |
| OpenAPI/Swagger | — | Bundled in the `dctm-rest` WAR and REST SDK since ~21.2 ("experimental" in 23.2); examples known to be wrong in places (folder-link body). Exact served path unverified. No OpenText-published public spec file. | | | Ask the customer to export it from their instance; do not build on it blind. |

**Driver decision: none.** Plain `requests` with `Accept: application/vnd.emc.documentum+json`
and HTTP Basic (or Bearer) is the whole client.

---

## 6. How Documentum maps onto BOW's existing connector patterns

BOW has three ways to add a source. Only one fits Documentum.

| Mechanism | What it needs | Why it does / doesn't fit |
|---|---|---|
| `McpPreset` (a tile pointing at a vendor MCP server, `type="mcp"`) | A fixed `server_url` and OAuth/bearer | **No.** There is no OpenText or community Documentum MCP server to point at (§2), and Documentum is per-customer — `server_url` is a fixed string. |
| `CustomApiPreset` (curated REST endpoints, `type="custom_api"`) | A fixed `base_url` + a hand-written endpoint list | **No.** Same fixed-URL problem, and the agent would get opaque JSON tools, not files it can read, grep, attach and analyze. Documentum's linked JSON also needs link-following, which the custom-API tool does not do. |
| **Native `files`-shaped connector** (`data_shape="files"`, `category="files"`, `is_document_based=True`) | A client implementing the file-source contract | **Yes.** Documentum is a document repository: cabinets/folders/documents with content. This is exactly what `sharepoint_onprem`, `network_dir`, `s3`, `google_drive` and `graph_drive` are. |

### 6a. The file-source contract the client must implement

Read from `backend/app/data_sources/clients/network_dir_client.py` and
`sharepoint_onprem_client.py` (the two closest references), consumed by the tools in
`backend/app/ai/tools/implementations/{list_files,read_file,search_files,grep_files,attach_file}.py`:

| Method | Documentum REST call behind it |
|---|---|
| `test_connection()` | `GET /repositories/{repo}` (and `/currentuser`) with credentials; confirm the repository resolves and the scope folder exists (a `FOLDER()` DQL or folder lookup). |
| `list_files(folder_id, recursive, limit, progress_callback)` | Folder walk via `…/folders/{id}/folders` + `…/folders/{id}/documents?inline=true` (`items-per-page`, `page`), **or** one DQL per page: `SELECT r_object_id, object_name, r_full_content_size, r_modify_date, a_content_type, r_object_type FROM dm_document WHERE FOLDER('/Cabinet/Path', DESCEND)`. DQL is one round-trip per page and is the pragmatic choice for indexing; the folder walk is what Elastic does. |
| `read_file(file_id, sheet, max_bytes, page_range)` | `primary-content` → `enclosure` (`media-url-policy=local`) download, then the shared extraction pipeline (`_document_text.py`, `_office_convert.py`) — same as the other file connectors. Enforce `max_file_mb` from `r_full_content_size` **before** downloading. |
| `read_raw_bytes(file_id)` | Same download, unparsed, for `attach_file`. Return `NamedBytes(data, name, mime)` — Documentum ids are opaque 16-hex `r_object_id`s with no extension, exactly the case `NamedBytes` was added for (see the docstring in `_file_source_common.py`). Map `a_content_type` (`pdf`, `msw12`, `excel12book`, …) to a MIME/extension via `GET /repositories/{repo}/formats` (cache it) or a small static table. |
| `search_files(query, limit)` | `GET /repositories/{repo}/search?q=…` (xPlore full-text when installed; otherwise database search) **plus** a live DQL `object_name`/`title` `LIKE` match, mirroring how `sharepoint_onprem` supplements indexed content search with live filename matches. Re-authorize and scope-check every hit (path under `root_path`, globs). |
| `get_schemas(progress_callback)` | Catalog rows per file according to `index_mode` (`none` / `metadata` / `content`), same tiers as `network_dir`. |
| `description` / `prompt_schema()` | Human text: "Documentum repository `{repo}` under `/Cabinet/Path` (read-only)". |

The `file_id` the agent sees should be the **`r_object_id`** (stable across renames and
moves; unique per version), with the display path carried alongside — not the path,
because objects can be linked into multiple folders and paths are not unique. Note that
`r_object_id` changes when a new version is checked in; index by `i_chronicle_id` if
"same document, newer version" identity matters to the catalog.

### 6b. Config and credential schema sketch (for `configs.py`)

`DocumentumConfig`
- `rest_url` — REST Services base, e.g. `https://dctm.example.com/dctm-rest` (per customer; may also be a D2-REST root).
- `repository` — repository (docbase) name; one connection = one repository.
- `root_path` — cabinet/folder scope, e.g. `/Contracts/2026`. Everything outside is denied at the resolve chokepoint (same access-boundary model as `include_globs`).
- `include_globs`, `recursive`, `index_mode`, `max_file_mb`, `max_catalog_objects` — copy the `network_dir` / `sharepoint_onprem` fields verbatim so the file connectors stay aligned.
- `object_types` (optional) — comma list of `dm_document` subtypes to expose; default `dm_document` (subtypes included via DQL type inheritance).
- `current_versions_only` (default true) — DQL default; `(ALL)` off unless asked.
- `allow_http` — lab-only escape hatch, as on `sharepoint_onprem`.

Credential variants (`AuthOptions`):
- `basic` — repository username/password → HTTP Basic. `scopes=["system","user"]`, so per-user credentials work as "User-required NTLM" does for SharePoint Server.
- `otds_oauth` — OTDS OAuth2 (client id/secret + token endpoint for a service identity; or per-user authorization-code via the existing `OAuthDelegatedCredentials` flow). Gate behind lab verification of token transport.
- `kerberos` — service-account keytab, `scopes=["system"]`, reusing the `KRB5_CLIENT_KTNAME` deployment story from `docs/sharepoint-server.md`. Legacy on 23.4+; include only if a customer asks.

Registry entry: `category="files"`, `data_shape="files"`, `catalog_ownership="shared"`,
`requires_license="enterprise"` (matches `sharepoint_onprem`), explicit `client_path`,
`dev_only=True` while incubating.

### 6c. Driver dependency

None beyond `requests` (already present). The official Python sample client is Python
2.7–3.5 era and is a **reference for link-rel names and media types only**. `dctmpy` is
not needed.

### 6d. Behavioural details to get right
- Send `Accept: application/vnd.emc.documentum+json`; always request `inline=true` on
  listings so one call yields properties; pass `links=false` on large listings.
- Never assume the page size: read `items-per-page` back from the feed and stop when
  `entries` is empty (Elastic's loop) or `next` is absent.
- Handle 429 with `Retry-After` (Elastic does), 401 vs 403 distinctly (403 = ACL-trimmed:
  hide, don't error), and 404 on `content` for content-less objects (folders, virtual
  document roots, records with no rendition).
- CSRF: not applicable to GET; if we ever write, honour `DOCUMENTUM-CSRF-HEADER-NAME`.
- Content size: `r_full_content_size` may be 0 for objects whose content lives in an
  external store; fall back to the content resource's `content-size`.
- Formats: `a_content_type` is a Documentum format name, not a MIME type.

### 6e. What we cannot do in-house today
- **No Documentum instance to test against.** Every other file connector was validated in
  a lab. Documentum images are distributed only through OpenText's entitled download
  portal / container registry (§7), so integration tests need a customer sandbox or a
  partner entitlement. Unit tests can mock the HTTP boundary using the documented
  response shapes (Elastic's connector tests are a fixture source).

---

## 7. Dev / test environment availability

| Question | Finding | Source |
|---|---|---|
| Developer edition / free download? | **No.** The last free one was Documentum 6.6 Developer Edition (~2010). Forum answer: "There is no longer a developer edition… if your company has a valid license, you can download docker containers." | forums.opentext.com 144221, 171481 |
| Official container images | Yes: `registry.opentext.com` (`docker login` with My Support credentials; probed: `/v2/` → 401 "jwt missing") and tarballs on My Support (e.g. `documentum_server_23.4_docker_compose_scripts.tar`, `dctm_server_20.2_centos.tar`). Images: `dctm-server`, `dctm-tomcat`, `dctm-admin`, **`dctm-rest`**, xPlore. | blog.aldago.es docker category; github.com/jcwhall/Documentum-23.4-Docker |
| Entitlement | Customer or partner My Support account with Documentum download rights. Partner-network membership is the usual ISV route (inference). | same |
| Docker Hub | Only unofficial/leaked-looking pushes (`sgupta4924/transport-documentumcs-24.2`, `…-documentumd2-24.4`, `izarral/t3-dctm` 16.4, `vashadow/webtop`…). **Do not use** — licensing. | hub.docker.com search |
| Trials | "Content Aviator for Documentum" 30-day trial is a business-user workspace; no API access. | opentext.com/resources/content-aviator-documentum-free-trial |
| Public demo REST endpoint | None. Samples assume `http://localhost:8080/dctm-rest`. | forum 309883, 313575 |
| Mocks | No standalone `dctm-rest` mock. `erangilboa/ECM-Developer-Tools` has an in-memory Documentum mock (subset DQL, cabinets/folders, ACLs). Elastic's connector unit tests contain REST response fixtures. | GitHub |
| Community install recipes | 23.4 compose (jcwhall), 20.2 (vkbdev83/dctmdevdocker, aldago), 16.7.1 K8s, 25.4/26.2 WSL2 guides — all assume you already hold the OpenText binaries. Hardware: ~4 GB RAM / 25 GB disk minimum for CS + Postgres. | GitHub, blog.aldago.es |

**Practical plan:** ask the first interested customer for a non-production repository
with REST Services exposed (they almost always have one: REST ships with the server and
D2 depends on it), or obtain partner access. Until then, build against recorded fixtures.
The routes, costs, legal constraints and a week-by-week plan are worked out in
[`documentum-lab-access.md`](documentum-lab-access.md).

---

## 8. How other products connect to Documentum (the idiomatic path)

| Product | API used | Auth / security | Note |
|---|---|---|---|
| Elastic Enterprise Search connector (8.14; example, later removed) | **REST** `/dctm-rest` | Basic; no permission sync | Python, closest analogue to ours. |
| One Fox "OpenText Documentum" connector for Power Automate / Copilot Studio / Logic Apps | **REST** ("Documentum with its REST service available externally") via One Fox SaaS proxy | API key + Documentum username/password; 100 calls/min | Get/Update document, content, properties; write actions overwrite newest version. |
| Community RAG build (June 2026) | **REST** + Tika + pgvector | — | Captures ACLs and folder paths for permission filtering; built explicitly to avoid Aviator cost. |
| Accenture Aspire (Microsoft 365 Copilot, Google Cloud Search), OpenText IDOL/Knowledge Discovery, Mindbreeze, Coveo (legacy), fme migration-center, BA Insight | **DFC / DQL** (Java, superuser, `dfc.properties`) | Document-level ACL trimming at index time | Full-corpus crawlers with security trimming. |
| OpenText AppWorks, SAP S/4HANA extensions | **CMIS** | — | CMIS remains for standards-based integrations. |
| Glean, Lucidworks, Onna, Purview (as a source) | No Documentum connector found | — | Absence not proven. |

**Pattern:** crawlers use DFC/DQL in Java; integration platforms, low-code, AI/RAG and
Python code use REST with Basic (or OTDS OAuth). BOW is squarely in the second group.

---

## 9. Risks and open questions

1. **Token transport for OTDS OAuth2** — `Authorization: Bearer` vs `?access_token=` on
   `dctm-rest`; which modes accept which. Needs a 23.4+/OTDS lab. Ship Basic first.
2. **Full-text search availability** — `/search` returns database results when xPlore is
   absent, and xPlore indexing is asynchronous (same caveat we document for SharePoint
   Server crawls). Always supplement with live name matching and say so in the UI.
3. **Page-size defaults are server-configured** (`rest.paging.default.size` / `max.size`);
   read them back rather than assume.
4. **Content links may point at ACS/BOCS** hosts unreachable from the backend; force
   `media-url-policy=local`.
5. **Multi-filing and versions** — an object can appear under several paths; `r_object_id`
   changes per version. Decide catalog identity (`r_object_id` vs `i_chronicle_id`) up front.
6. **Heavily subtyped repositories** — Life Sciences / Engineering editions have hundreds
   of `dm_document` subtypes with custom attributes; `object_types` filter + `view=`
   projection keeps listings sane. A later "objects"-shaped or DQL-query mode could expose
   metadata as tables (Documentum customers *do* ask "how many SOPs are in Approved
   state" — that is a DQL question, not a file question). Out of scope for v1 but the
   DQL endpoint makes it cheap to add.
7. **Legacy servers (7.x/16.x)** — REST is present but older; the community holds that a
   newer REST WAR can front an older server. Target 20.x+ for support statements.
8. **No lab** (§7) — the single biggest schedule risk; the API itself is low-risk.

---

## 10. Confidence and unverified items

**Verified directly (live probes or primary source read in this session):** empty MCP
registry results; Elastic connector source (URL templates, media type, Basic auth,
pagination loop, 429 handling) and its docs page (no permission sync, 10 MB cap, full
sync only); One Fox connector page (REST prerequisite, actions, throttling); OpenText
staff forum answer on pre-authenticated mode (thread 312650); `registry.opentext.com`
requiring a JWT; Docker Hub unofficial images; PyPI (`dctmpy` 2017, no maintained
Documentum package); the official Python sample client's parameter names; the D2FS REST
24.2 guide's auth-mode list, header table (Basic + Negotiate) and paging-property names.

**From OpenText-authored public artefacts (javadocs in `opentext/d2sv-sdk`, reference
clients):** media-type and query-parameter constants, link-relation catalogue,
`rest.security.auth.mode` default `basic`, CSRF/client-token property names, REST
javadocs existing for 23.4 → 26.2.

**Second-hand / could not verify:**
1. Official REST Development / Reference / Administrator guides (developer.opentext.com is
   JavaScript-only for fetchers; Scribd mirrors 403). Chapter contents not confirmed.
2. Exact served path of the bundled OpenAPI/Swagger UI; `content-media` query parameters.
3. Whether `Authorization: Bearer <OTDS token>` is accepted in every mode; OTDS
   `ticketforuser` impersonation applicability to Documentum; `DM_TICKET` as Basic password.
4. Which legacy auth schemes (Kerberos/CAS/ClearTrust/SAML) Documentum REST itself removed
   in 23.4+ (confirmed only for D2-REST).
5. Which resources emit `ETag` / 412 semantics.
6. Deprecation status of DFS and CMIS (both still ship; no EOL notice found).
7. Backward compatibility of a newer REST WAR with older Content Servers (community claim).
8. Partner-network membership as the ISV route to entitled downloads (inference).
9. Forum-snippet-only claims: "no developer edition… docker containers with valid license";
   Performance Engineering MCP server details; Azure Marketplace listing being
   OpenText-managed.

---

## 11. Sources

Official / OpenText
- Brand & product: https://www.opentext.com/about/brands/documentum ; https://www.opentext.com/products/documentum-content-management ; https://blogs.opentext.com/whats-new-in-opentext-documentum-content-management/ ; https://blogs.opentext.com/whats-new-in-opentext-content-aviator/ ; https://blogs.opentext.com/modernize-your-content-foundation-for-the-era-of-agentic-ai/ ; https://www.opentext.com/aviator-ai/aviator-studio
- Developer portal (SPA): https://developer.opentext.com/ce/products/documentum ; https://developer.opentext.com/ce/products/documentum/documentation/documentum-rest-services-development-guide ; https://developer.opentext.com/ce/products/documentum/tutorials/how-to-install-documentum-rest-services/1
- REST javadocs 23.4–26.2 and D2FS REST guides: https://github.com/opentext/d2sv-sdk ; https://opentext.github.io/d2sv-sdk/ ; https://opentext.github.io/d2sv-sdk/24.2.0/bundle/pdf/OpenText%20Documentum%20D2FS%20REST%20Services%20Development%20Guide.pdf
- Reference clients: https://enterprise-content-management.github.io/rest/ ; https://github.com/Enterprise-Content-Management/documentum-rest-client-java ; https://github.com/Enterprise-Content-Management/documentum-rest-client-python ; https://github.com/Enterprise-Content-Management/documentum-rest-client-dotnet ; https://github.com/Enterprise-Content-Management/documentum-rest-extensibility-samples
- Forums (OpenText staff answers): https://forums.opentext.com/forums/developer/discussion/312650/dctm-rest-23-4-with-otds-without-password ; …/172553/download-file-content-using-documentum-rest-services ; …/172264/documentum-rest-kerberos-sso ; …/311299/documentum-rest-extension-adding-openapi-documentation ; …/172152/documentum-rest-7-3-using-or-operator-in-aql ; …/309883/how-to-run-documentum-rest-apis ; …/313575/how-to-test-documentum-rest-api ; …/302772/documentum-develop-environment-with-docker
- MCP registry: https://registry.modelcontextprotocol.io/v0/servers?search=documentum

Third-party integrations
- Elastic: https://www.elastic.co/docs/reference/search-connectors/es-connectors-opentext ; https://github.com/elastic/connectors/blob/8.14/connectors/sources/opentext_documentum.py
- One Fox / Microsoft: https://learn.microsoft.com/en-us/connectors/opentextdocumentum/
- Aspire (DFC): https://contentanalytics.digital.accenture.com/display/aspire40/Documentum+DQL+How+To+Configure ; IDOL: https://www.microfocus.com/documentation/idol/IDOL_24_2/DocumentumConnector_24.2_Documentation/Help/Content/Install_Requirements.htm ; Mindbreeze: https://help.mindbreeze.com/en/index.php?topic=doc/Configuration---Documentum-Connector/index.htm

Community / practitioner
- fme field report: https://www.fme-lifesciences.com/blog/opentext-documentum-rest-api-field-report-from-developers-point-of-view/
- dbi-services: https://www.dbi-services.com/blog/documentum-rest-api/ ; https://www.dbi-services.com/blog/paginating-through-a-documentums-result-set/ ; https://www.dbi-services.com/blog/dctm-managing-licenses-through-otds/
- Alvaro de Andrés (aldago): https://blog.aldago.es/category/rest/ ; https://blog.aldago.es/2025/02/16/otds-faq-for-documentum/ ; https://blog.aldago.es/2025/05/19/documentum-24-4-otds-licensing-configuration/ ; https://blog.aldago.es/2024/01/08/documentum-rest-documentation/ ; https://blog.aldago.es/2021/11/01/documentum-rest-dm_job-object-on-dql-queries/ ; https://blog.aldago.es/2026/06/15/rag-integration-with-documentum/ ; https://blog.aldago.es/2026/07/26/documentum-26-2-postgresql-18-on-rocky-linux-9-6-wsl2-install-guide/
- AppWorks-tips (OTDS + REST walkthrough): https://appworks-tips.com/2021/10/01/113_bring_life_in_the_documentum_connector/
- Docker recipes: https://github.com/jcwhall/Documentum-23.4-Docker ; https://github.com/vkbdev83/dctmdevdocker ; https://blog.aldago.es/2020/04/09/opentext-documentum-20-2-docker-with-postgresql-install-guide/
- Community MCP attempts: https://github.com/maitreya-ai/AIAdoption-GovernanceEngine ; https://github.com/ancyonio/DQLOptimizer
- Tag Salad REST 7.0 tutorial (filters, home document): https://tagsalad.wordpress.com/2013/06/04/emc-documentum-platform-rest-services-tutorial/
