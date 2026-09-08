# Getting a Documentum Lab — How to Obtain a Test Environment

**Status:** Research / analysis only — no implementation.
**Companion to:** [`documentum-connector-analysis.md`](documentum-connector-analysis.md) (§7 there flagged the
missing lab as the main schedule risk; this document answers "so how do we get one?").
**Researched:** 2026-09-08. Every claim carries a source; §8 lists what could not be verified.
Forum quotes were extracted from the JSON embedded in forums.opentext.com pages; PDFs were read directly.

---

## 0. Bottom line up front

1. **There is no self-service way to get Documentum.** No developer edition (discontinued
   2012, last VMs pulled 2017), no trial download ("discontinued a long time ago" — OpenText
   consultant, 2023), no pay-as-you-go marketplace listing (only private offers), no public
   demo endpoint, no public container image. The Content Aviator "free trial" is a chat
   workspace with no API. The developer-portal 90-day trial covers OpenText's SaaS APIs, and
   `/dctm-rest` on it returns 404.

2. **Every real route is a way to obtain an *entitlement*, not a download link.** Binaries
   and images (`dctm-server`, `dctm-rest`, `dctm-admin`, xPlore) come only from My Support /
   `registry.opentext.com` (probed: 401 "jwt missing"), and since 24.4 the server refuses to
   start without an OTDS-issued licence. "You need to have a customer account on
   support.opentext.com linked to a valid contract that allows you to download the software."
   (forum, Nov 2025).

3. **Recommended plan — run three tracks in parallel:**
   - **Track 1 (days): build against a high-fidelity mock.** Elastic's open-source Documentum
     connector ships a Flask mock of the seven `/dctm-rest` routes we need plus unit-test
     fixtures; combine with recorded responses and, once obtained, the real OpenAPI spec.
     This unblocks development now. It is blind to OTDS, ACS content links and xPlore, so it
     cannot be the only validation — Elastic built mock-only and then pulled the connector
     from `main` pending "test with the real instance".
   - **Track 2 (weeks): a design-partner customer's non-production repository.** Every
     Documentum shop has DEV/TEST docbases with REST already deployed (D2 depends on it).
     Zero licence cost, cleanest legal footing, highest fidelity. Israel candidates: NessPRO
     (OpenText's strategic partner in Israel, runs a local data centre), ParaDocs (Documentum-
     only ISV), Ministry of Education / State Archives, Bar-Ilan University, BDO Israel.
   - **Track 3 (months): join the OpenText Partner Program, Technology track**, and request
     internal-enablement software under the partner agreement's *Demonstrator* module. This
     is the only durable fix: our own lab, current versions, indefinitely. ISVs pay an
     undisclosed annual fee. The Documentum product team actively courts partner-built
     integrations (partners-only "Documentum Dragons' Den SDK Challenge").

4. **Also viable as accelerants:** a time-boxed **PoC licence via an Account Executive**
   ("Full functionality, limited timeframe" — needs a sales opportunity, ideally our joint
   prospect); an **engagement with a consultancy that holds an entitled lab** (they run our
   tests in *their* environment: ParaDocs, NessPRO, fme, dbi services, Flatirons, Forefront);
   **OpenText Learning Services labs** (USD 4,500 per course, 10–20 lab hours over 30 days) for
   a developer to learn on a real repository — not for CI.

5. **Rejected: unofficial Docker Hub images.** Several accounts have re-pushed what are
   almost certainly OpenText's private-registry images (3.6–8.7 GB `transport-documentumcs-24.2`,
   `-d2-24.4`, `t3-dctm` 16.4, `dctm-rest` WAR images). OpenText's EULA §5.1 forbids
   redistribution and third-party use; a downstream user holds no licence at all. For a
   vendor whose pitch is being an OpenText-ecosystem integration, that is a legal and
   commercial risk far larger than a lab costs. Do not pull them.

---

## 1. Things that do NOT exist (stop looking)

| Rumoured route | Reality | Evidence |
|---|---|---|
| Documentum Developer Edition | Free editions existed 2009–2015: 6.5 SP2 and 6.6 (Windows, EDN), then 7.1 (2014) and 7.2 (2015) CentOS VMs **with REST Services bundled**, "for non-production use", no expiry. EMC (Jerry Silver, 2012-12-14): "We have regretfully decided to discontinue the free Developer Edition download." 7.2 downloads were already dead by 2017-01 ("This file is not currently available for download"). Community moderator, 2020-08: "there is no longer a developer edition". No 6.7 DE ever shipped. | forums.opentext.com threads 144221 (p1/p2), 171481, 300696; edoc-systems.com 7.1 DE page |
| Trial download | "the Documentum trial versions were discontinued a long time ago" — Pedro Maia, OpenText Senior Consultant, 2023-03-08. | forum 311293 |
| Developer-portal free plan | The 90-day trial covers Information Management Services / Core SaaS APIs. A user tried `https://na-1-dev.api.opentext.com/dctm-rest/repositories` → 404. Answer: "you cannot download anything with a trial account. You need to purchase a license." | forums 308092, 302483, 309883 |
| Content Aviator for Documentum "free trial" | Provisions an "AI-enabled business workspace" to upload documents and chat (30 days). The page never mentions a repository, REST, D2 or OTDS; OpenText's blog describes it in Extended ECM vocabulary. No API. | opentext.com/resources/content-aviator-documentum-free-trial ; blogs.opentext.com/whats-new-in-opentext-content-aviator |
| Hands-on labs (hol3.eimdemo.com) | Guided HTML *simulations*, not live systems. | hol.eimdemo.com/p/about-hands-on-labs |
| Public REST endpoint | A 2020 Azure demo (`td-ecd.eastus.cloudapp.azure.com:7072/dctm-rest`, Documentum 16.7) once answered Basic-auth GETs; it is dead (timed out 2026-09-08). Nothing else has ever been public. | forum 301032; live probe |
| Public container image | `registry.opentext.com/v2/` → 401 "jwt missing" (My Support credentials). No `emcecd` namespace on Docker Hub. | live probe |
| Marketplace pay-as-you-go | AWS/Azure/GCP carry Documentum CM only as **private offers** negotiated with OpenText sales; the Azure listing (`opentextglobal.opentext_documentum`) is a fully managed SaaS. No trial badge, no PAYG. | opentext.com/partners/opentext-on-{amazon-web-services,microsoft-azure,google-cloud}; marketplace.microsoft.com |

---

## 2. Official OpenText routes

### 2a. Route A — Partner Program, Technology track (the durable route)
- OpenText lists six partner models; the relevant one is **Technology**: "Partners who either
  build connectors from their own IP to OpenText products, build add-ons/modules that augment
  OpenText products … or embed OpenText products." The application form adds: "Technology
  Integration — Enabled to access OpenText tools and resources (SDK, APIs) to aid in building
  onto their own IP … **ISVs pay an annual membership fee to OpenText for access to tools.**"
  Solution Extension (resold by OpenText) comes later: "Most Solution Extension Partners begin
  their journey with OpenText through membership in the Technology Program."
  (opentext.com/partners/become-a-partner, /partners/become-a-partner-form, /partners/solution-extension-partners)
- The **Consolidated Partner Agreement v2.0 (Nov 2025)** has a *Demonstrator Partner Type*
  module: partners "may use the OpenText Products for (a) internal enablement … (b)
  demonstration purposes", only "on Partner-controlled environments and on OpenText supported
  configurations". Caveats in the text: no Support Services; quantity/capacity limits; "may be
  made available subject to payment of fees"; "granted entirely at OpenText's discretion and
  may be terminated at any time, with or without notice"; and: "If the relevant OpenText
  Product … is or contains a development tool, Partner shall not develop any product with the
  development tool". Our use is integration *testing against the REST API*, not building on
  the SDK, but get that distinction in writing with the Partner Account Manager.
  (opentext.com/media/agreement/opentext-consolidated-partner-agreement-en.pdf)
- The public **NFR guides cover only the cybersecurity line** (Carbonite/Webroot: Availability,
  Endpoint Backup, DNS Protection …; 12-month NFR subscriptions; requested via Partner Account
  Manager). Documentum NFR / internal-use software is not promised publicly at any tier; it is
  a Demonstrator-module request.
  (opentext.com/en/media/guide/opentext-accelerate-not-for-resale-and-benefits-guide-emea-south-en.pdf)
- **Fee:** not published for the Technology track (a comparable OpenText MSP programme lists
  USD 5,000/year). Apply via the form or partners@opentext.com; it asks for company profile,
  product brochures, rate cards.
- **Precedent that the Documentum team wants partner integrations:** the partners-only
  "Documentum Dragons' Den: Partner SDK Challenge" (2023; winner AmeXio, finalists BTM Software
  and Reva Solutions, fme also competed): "Partners were invited to participate… The
  collaboration with OpenText engineering provided training, assistance." The Documentum D2
  Principal PM (Kyle Pettit) posts on the developer forum and shared an "unofficial and
  unsupported" Kubernetes deployment tutorial there in 2023 — those are the people to ask.
- **What you get:** the right to run Documentum in our own lab (images from
  `registry.opentext.com`, OTDS licence file) for enablement and demos, for as long as we are
  a partner; partner training; directory listing. **Time:** weeks to a few months.

### 2b. Route B — a customer's DEV/TEST repository (the fastest route)
Every Documentum shop has DEV/TEST docbases, and since D2 depends on REST, `dctm-rest.war` is
almost always already deployed. Zero licence cost. The customer's licence covers internal
development/test use; running a vendor's tests inside the customer's non-production
environment, under the customer's control, is the cleanest legal footing short of OpenText
consent. Ask for exactly what established connectors publicly require:

1. HTTPS exposure of `/dctm-rest` on a **non-production** repository (VPN, IP allow-list or
   reverse proxy on 443). One Fox's documented failure mode: "502 Bad Gateway … the Documentum
   environment … is not available from external webservices".
2. A dedicated service account with **Browse/Read** on a designated test cabinet (ACL-scoped),
   plus two end-user accounts with different ACLs to prove security trimming; optionally one
   account with Write for round-trip tests.
3. Auth mode: Basic on DEV, or an **OTDS OAuth client** (client_id/secret, redirect URIs) on
   23.4+. ServiceNow's connector prerequisites read: "OTDS REST API, DCTM REST API …
   publicly accessible. DCTM REST API … configured with OTDS."
4. Whether xPlore is installed (full-text `/search` vs database search).
5. Versions (Server/REST/D2 CE), whether D2FS-REST is present, page-size settings, CORS.
6. Pre-empt objections: non-production data only, time-boxed access, credential storage,
   data egress to a SaaS/LLM, permission trimming, logging/review.

Candidates: any prospect already asking for the connector; in Israel, NessPRO (see §3),
ParaDocs, Ministry of Education / State Archives, Bar-Ilan University, BDO Israel (OpenText
customer stories / 2018 NessPRO-OpenText Documentum event speakers). **Time:** days to weeks
after their security review.

### 2c. Route C — sales PoC licence
Forum reply (2024-09-30) to "is there a trial?": "Have you talked to your AE about getting a
**PoC-Version of DCTM? Full functionality, limited timeframe**." There is no public evaluation
programme; PoC licences are tied to a sales opportunity. An ISV alone has little leverage; an
ISV **with a mutual prospect** has plenty. Licence usually free for the window; we host it.
**Time:** weeks.

### 2d. Route D — Learning Services labs (learning, not CI)
- Learning Subscription: USD 5,000/yr Standard, 7,500/yr Premium per named user; four 10-hour
  hands-on lab sessions, each open 30 days; open to "customers, Partners, and practitioners".
  Lab terms: "self-paced training only; no production use permitted; no data export from lab
  environments allowed".
- Documentum On-Demand courses at USD 4,500 each: 3-8010 *Technical Fundamentals* (40 h,
  ~20 h lab over four weeks; DQL, types, virtual documents), 3-8011 *System Administration
  Fundamentals*, 2-8701 *Client (D2/Smart View) Configuration*. None covers REST Services;
  course 4-0144 "REST API and Content Web Service Fundamentals" is for OpenText Content
  Management (Content Server), **not** Documentum.
- Verdict: legitimate hands-on time with a real repository for a developer to learn DQL and
  administration; the lab VM is a browser-hosted desktop with no promise of network access
  from our backend and cannot be automated.

### 2e. Route E — buy the smallest real entitlement
Documentum CM is sold as X-Plans: Express (X1), Premium (X2), Ultimate (X3, private cloud
only); off-cloud plans are X1/X2. Pricing-guide footnote: "Developer extensibility is included
in off-cloud deployments. For OpenText Cloud offerings, this capability requires an additional
**Cloud Sandbox license**." Prices are not public; third-party summaries put mid-size
deployments at USD 200k–800k licence. Not a dev-sandbox route on our own — but a customer on
OpenText Cloud can buy a Cloud Sandbox and grant us access (Route B variant), and Documentum
CM 26.2 added a "cloud-native development sandbox for customer-built extensions and automated
testing" for cloud customers. The one public hosted price point found: SynApps (UK G-Cloud)
£20/user/month including "an API sandbox or test environment" — UK public-sector procurement
only. Forefront Technologies (US) markets "Documentum as a Service"; Armedia hosts Documentum
for US federal — worth asking whether either sells a 1–5-user dev tenant (unverified).

### 2f. Route F — OEM programme
"All OpenText solutions are available under the OEM program"; Documentum CM is listed
explicitly (opentext.com/products/oem-marketplace). Meant for embedding OpenText inside a
product with royalties; overkill for a read-only connector, but another door to a development
entitlement if the Technology track stalls.

---

## 3. Consultancies and community (who holds entitled labs)

Nobody sells a public "Documentum sandbox". Consultancies obtain binaries through their own
partner or customer entitlements, and EULA §5.1(b)/(d) bars them from letting "third parties
access, use, and/or exploit the Software" or charging "a fee to any party for access". The
legitimate shape of an engagement is therefore: **their engineers run our connector/test suite
inside their lab and report**, or they co-develop, or OpenText consents in writing.

| Firm | Relevance | Notes |
|---|---|---|
| **NessPRO (Ness), Israel** | OpenText's strategic partner in Israel since 2018: resells and supports "all OpenText solutions", runs "a dedicated data center in Israel" for cloud-based OpenText, delivered Documentum for the State Archive; co-hosted the 2018 Herzliya "Documentum is growing with OpenText" event with OpenText Documentum PM. | First call locally: entitlement holder, likely has DEV environments, and a channel to OpenText Israel sales for a PoC licence. |
| **ParaDocs Software, Israel** | Documentum-only ISV/consultancy (12+ years), won the KKL-JNF Documentum tender (2018), hiring Documentum developers in 2025. | Small, approachable, benefits from a modern connector for its customers. |
| **Malam Team, Israel** | OpenText Gold partner (10+ years) — positioned on Content Server/xECM rather than Documentum (unverified). | Secondary. |
| **fme (DE/US/Life Sciences)** | 25-year Documentum partner; sells container packages "Basic Workshop → Scoping → Proof of Concept (test infrastructure) → Production"; authors of the REST field report; dqMan. | Strongest technical partner in Europe; ask for a PoC-infrastructure engagement. |
| **dbi services (CH)** | Prolific Documentum bloggers (REST, paging, OTDS, K8s migrations); managed services. | Good for expert validation of the auth/OTDS story. |
| **Flatirons Digital Innovations (US)**, **Forefront Technologies (US)**, **Armedia (US)** | OpenText Gold partner (20 years Documentum) / "Documentum as a Service" hosting / FedRAMP-hosted Documentum. | Hosting-capable; ask about a small dev tenant. |
| **One Fox (NL)** | Built the Microsoft Power Automate Documentum connector "in close collaboration with OpenText"; architecture is a SaaS relay to the customer's own REST endpoint — they do not host Documentum. | The model ISV to emulate: partner membership + co-marketing, not a sandbox. |
| TSG (acquired by Alfresco 2020), Blue Fish (merged into ArgonDigital 2021), Generis (dropped Documentum dependency) | No longer Documentum lab sources. | — |

Independent bloggers (Alvaro de Andrés/aldago, AppWorks-tips, dbi) all obtain software through
entitled My Support accounts and even they hit walls ("Can't download cmis (not entitled)").
Scott Roth's blog is gone (HTTP 410); Andrey Panfilov's domain no longer resolves. Freelance
marketplaces list Documentum developers, but a freelancer offering "environment access" is by
construction using someone else's licence; hire them to run tests in an environment they are
entitled to instead.

---

## 4. Unofficial images — why they are rejected

Docker Hub (probed 2026-09-08) carries re-pushed images that by name and size are OpenText's
private-registry images: `sgupta4924/transport-documentumcs-24.2:NYCDOB` (8.7 GB, tag names a
specific customer), `…/transport-documentumd2-24.4` (2.85 GB), `…-documentumda/search/cts-24.4`,
`…-otds-24.1/25.2`, `atomicunitsammp/transport-documentumcs-24.2` (6.4 GB),
`ngupta634/transport-documentumcs-23.4` (3.6 GB), `izarral/t3-dctm:base16.4` (8.4 GB, 2018,
818 pulls), `vashadow/{webtop,da,mtr}`, `nippanisk/dctm-rest` (Tomcat + probable `dctm-rest.war`,
717 pulls). None has a description or licence.

OpenText EULA §5.1: the licensee "will not and will not permit any other party to: (a) assign,
transfer, give, distribute, reproduce, transmit, sell, lease, license, sublicense, publicly
display or perform, redistribute or encumber the Software by any means to any party; (b) … in
any other way allow third parties to access, use, and/or exploit the Software; … (d) charge a
fee to any party for access to or use of the Software" (opentext.com/assets/documents/en-US/pdf/opentext-eula-usa-en.pdf).
Whoever pushed these breached their licence; a downstream user has none, and §13.2 notes the
software "may contain devices or functionality to monitor Licensee's compliance". No takedown
history was found, which is not tolerance.

The GitHub recipes are different and fine to read: `vkbdev83/dctmdevdocker` (20.2),
`jcwhall/Documentum-23.4-Docker` (23.4 + DA + xPlore + Postgres compose; images "from a private
registry using credentials from an OpenText support account"), `amit17051980/documentum-16.4-docker`,
`koeppj/docker-documentum` (22.2 incl. REST), `jppop/dctm-docker`, `bkoz/documentum`
(OpenShift), and OpenText's own "unofficial and unsupported" Kubernetes tutorial (forum
311559). All are bring-your-own-binaries; they are the playbook for the day we hold an
entitlement.

---

## 5. Simulate now — the mock strategy and its limits

### 5a. Assets that exist
- **Elastic `elastic/connectors` branch 8.14** — `tests/sources/fixtures/opentext_documentum/fixture.py`:
  a Flask mock of `/dctm-rest/repositories`, `/repositories/{name}`, `…/cabinets`, `…/folders`,
  `…/folders/{id}/folders`, `…/folders/{id}/documents`, `…/nodes/{id}/content`; generates
  15–35 repositories × 20–30 cabinets × 20–30 folders × 2 files (small/medium/large), pages
  via `items-per-page`/`page`, serves fake bytes, no auth. `tests/sources/test_opentext_documentum.py`
  mocks `aiohttp` and covers 429/404/500. The connector source (`connectors/sources/opentext_documentum.py`)
  shows the Basic-auth session, `content-type: application/vnd.emc.documentum+json`, 100-per-page
  loop and `Retry-After` handling. Caveat: the shapes were "developed and tested with the fake
  API responses using mocked service", not recorded from a server (PR 2318), and the connector
  was removed from `main` (PR 2670) to be tested "with the real instance". A scaffold, not truth.
- **Real recorded responses:** dbi services' 2019 walkthrough (repositories feed, object
  creation, Basic header); forum 301032's genuine 16.7 `repositories/corp` response including the
  `search` link `hreftemplate` with `{?collections,facet,include-total,inline,items-per-page,locations,object-type,page,q,sort,timezone,view}`;
  the official Java client samples (`DQLQuerySample`, `SearchSample`, `ContentManagementSample`)
  and the Postman collection in `documentum-rest-extensibility-samples` for request shapes.
- **OpenAPI spec:** bundled in the `dctm-rest` WAR and in the REST SDK since ~21.2
  ("experimental" in 23.2); OpenText staff said the developer portal would carry it per release.
  Known to contain wrong examples (folder-link body). Nobody has republished it; obtain it from
  a partner/customer's SDK download (My Support). Serve it with Prism for contract checks.
- **`erangilboa/ECM-Developer-Tools`** — in-memory "FakeDocbase" (cabinets, folders, subset
  DQL/IAPI, ACLs, users); routes not enumerated; no licence shown. Low priority.
- **CMIS stand-ins** (Apache Chemistry OpenCMIS InMemory, Alfresco) only matter if we ship a
  CMIS binding; Documentum customers expose `/dctm-rest`.

### 5b. What a mock can and cannot prove

| Area | Fidelity | Why |
|---|---|---|
| Resource shapes, `links`/`hreftemplate`, `entries` paging (`items-per-page`, `page`, `include-total`), `inline=true` | High with the real spec + recorded samples | Documented, stable |
| Basic auth | High | Trivial |
| **OTDS / OAuth2 / Kerberos / pre-auth modes** | Low | Server-config dependent; token transport unverified |
| **ACS/BOCS content links** (signed, expiring redirect URLs vs `media-url-policy=local`) | Low–medium | Server-generated |
| **xPlore full-text search**, facets, indexing lag | Low | Needs real xPlore |
| DQL semantics, custom subtypes, renditions, formats | Medium–low | Customer-specific type models |
| Error semantics (429, DFC session limits, `E_*` codes) | Medium | Some samples exist |
| Version drift 16.7 → 24.x (`objects/{id}/contents` vs `nodes/{id}/content`, RADL → OpenAPI) | Low without multiple real specs | Elastic and dbi differ |

Use the mock to build and unit-test the client, the catalog tiers and the tool layer. Reserve
auth, content download, search and scale behaviour for Track 2/3 validation — and say so in
the connector's verification-boundary note, as `docs/sharepoint-server.md` does for Kerberos.

---

## 6. Recommended plan

| When | Action | Owner-ish |
|---|---|---|
| Week 0 | Stand up the Elastic fixture in `backend/tests/integrations` style (docker-compose), build the client against it, write unit tests at the HTTP boundary. | Eng |
| Week 0 | Email NessPRO and ParaDocs: introduce the connector, ask for (a) a non-production `/dctm-rest` to test against under their control, (b) an intro to OpenText Israel sales for a PoC licence, (c) whether they would co-develop. Same email to fme for a PoC-infrastructure quote. | Founders |
| Week 0 | Identify one prospect that runs Documentum and wants the connector; frame the ask with the §2b checklist. | Sales |
| Week 1 | Submit the OpenText Partner Program application (Technology track) via opentext.com/partners/become-a-partner-form; in the description state "read-only analytics/AI connector to Documentum REST Services; need internal-enablement environment for integration testing". Ask the Partner Account Manager explicitly about the Demonstrator module, the development-tool clause, and `registry.opentext.com` access. | Founders |
| Week 1 | Ask the partner/customer contact for the REST SDK's OpenAPI spec (23.4+) to drive Prism contract tests. | Eng |
| Weeks 2–6 | First real run against a customer/partner DEV repository: verify auth mode(s), page-size defaults, content-link policy, xPlore presence, ACL trimming. Record anonymised responses as fixtures. | Eng |
| Optional | One developer takes course 3-8010 (USD 4,500) for hands-on DQL/administration time if no partner lab materialises within a month. | Eng |
| Months 2–4 | Partner agreement signed → pull `dctm-server`, `dctm-rest`, `dctm-admin`, xPlore images with My Support credentials, run the community compose/K8s recipes, add a `CONTAINER_REGISTRY`-style integration target. | Eng |

---

## 7. Sources

Official OpenText
- Partner program: https://www.opentext.com/partners/become-a-partner ; https://www.opentext.com/partners/become-a-partner-form ; https://www.opentext.com/partners/solution-extension-partners ; https://www.opentext.com/partners/grow-as-a-partner ; https://www.opentext.com/products/oem-marketplace
- Consolidated Partner Agreement v2.0: https://www.opentext.com/media/agreement/opentext-consolidated-partner-agreement-en.pdf
- Accelerate NFR guide (cybersecurity line only): https://www.opentext.com/en/media/guide/opentext-accelerate-not-for-resale-and-benefits-guide-emea-south-en.pdf ; Accelerate program guide: https://www-cdn.webroot.com/3517/4239/7782/OpenText_Accelerate_Partner_Program_Guide_Rebranded_05102024.pdf
- EULA (USA v4.1): https://www.opentext.com/assets/documents/en-US/pdf/opentext-eula-usa-en.pdf
- Documentum X-Plans pricing guide (Cloud Sandbox footnote): https://www.opentext.com/media/guide/simple-purpose-built-pricing-plans-for-opentext-documentum-content-management-guide-en.pdf
- Marketplaces: https://www.opentext.com/partners/opentext-on-amazon-web-services ; https://www.opentext.com/partners/opentext-on-microsoft-azure ; https://www.opentext.com/partners/opentext-on-google-cloud ; https://marketplace.microsoft.com/en-us/product/saas/opentextglobal.opentext_documentum
- Content Aviator trial: https://www.opentext.com/resources/content-aviator-documentum-free-trial ; https://blogs.opentext.com/whats-new-in-opentext-content-aviator/
- Developer trial scope: https://forums.opentext.com/forums/developer/discussion/308092/trial-learn-more-about-signing-up-for-our-developer-trial ; https://forums.opentext.com/forums/developer/discussion/302483/trial-information-management-services
- Learning Services: https://www.opentext.com/learning-services/subscriptions ; https://www.opentext.com/TrainingRegistry/course/details/2916 (3-8010) ; https://www.opentext.com/TrainingRegistry/course/details/2917 (3-8011) ; https://www.opentext.com/TrainingRegistry/course/details/2914 (2-8701) ; https://www.opentext.com/TrainingRegistry/course/details/2392 (subscription terms)
- SDK Challenge: https://www.amexiogroup.com/2023/11/09/amexio-wins-documentum-sdk-challenge-with-myinsight/ ; https://videos.opentext.com/watch/xSsxcF8UBx8oayvbqQUuGz
- Documentum CM 26.2 sandbox: https://blogs.opentext.com/whats-new-in-opentext-documentum-content-management/

Forum threads (forums.opentext.com/forums/developer/discussion/…)
- 144221 (6.6 DE, discontinuation), 171481 (7.2 DE dead links, "no longer a developer edition"), 300696 (no trial; "if you are working for a client … with active support contract"), 62294 / 311293 (trials discontinued; "PoC-Version … full functionality, limited timeframe"), 302773 (docker images need a support contract), 309883 (developer-cloud trial ≠ Documentum), 313575 ("customer account … linked to a valid contract"), 311545 / 311559 (OpenText's unofficial K8s tutorial), 301032 (dead Azure demo endpoint, real 16.7 response), 311299 (OpenAPI spec in WAR/SDK).

Community / third parties
- Elastic connector: https://github.com/elastic/connectors/tree/8.14/tests/sources/fixtures/opentext_documentum ; https://github.com/elastic/connectors/pull/2318 ; https://github.com/elastic/connectors/pull/2670 ; https://www.elastic.co/docs/reference/search-connectors/es-connectors-opentext
- One Fox / Microsoft: https://learn.microsoft.com/en-us/connectors/opentextdocumentum/ ; https://www.onefox.com/product/opentext-documentum-power-automate-by-one-fox/
- ServiceNow Documentum connector prerequisites: https://store.servicenow.com/store/app/afbb27ea1b246a50a85b16db234bcb5c
- NessPRO: https://www.opentext.com/about/press-releases/nesspro-selected-as-opentext-strategic-partner-in-israel ; https://documentum-opentext.events.co.il/home ; ParaDocs: https://www.linkedin.com/company/paradocs-software-ltd- ; Israeli OpenText customers: https://www.opentext.com/customers/ministry-of-education-israel ; https://www.opentext.com/customers/bdo-israel
- fme: https://en.fme.de/services/technology-services/enterprise-content-management/opentext-documentum-services/container-technology-meets-opentext-documentum/ ; dbi services: https://www.dbi-services.com/technologies/opentext-documentum/ ; Flatirons: https://fdiinc.com/solutions/opentext-documentum/ ; Forefront: https://www.fftechnologies.com/documentum/services ; Armedia: https://armedia.com/news/opentext-documentum-is-fedramp-authorized-within-the-armedia-content-cloud-acc/ ; SynApps G-Cloud: https://www.applytosupply.digitalmarketplace.service.gov.uk/g-cloud/services/587710396989923
- Developer-edition history: https://www.edoc-systems.com/documentum-7-1-developers-edition-download/ ; https://www.cmswire.com/cms/enterprise-cms/emc-releases-free-documentum-developer-edition-enterprise-cms-004660.php ; https://blog.aldago.es/2016/11/10/documentum-7-3-available-to-download/ ; https://blog.aldago.es/2024/05/26/documentum-24-2-released/
- Docker recipes (bring-your-own binaries): https://github.com/jcwhall/Documentum-23.4-Docker ; https://github.com/vkbdev83/dctmdevdocker ; https://github.com/koeppj/docker-documentum ; https://github.com/jppop/dctm-docker ; https://github.com/amit17051980/documentum-16.4-docker ; https://github.com/bkoz/documentum

---

## 8. Unverified / low-confidence items

1. The Technology-track annual fee and whether Documentum internal-use software is granted at
   entry tier — only the cybersecurity NFR guide is public; everything else is via a Partner
   Account Manager.
2. Azure Marketplace listing plan types (Contact-me vs transactable) and any trial badge — JS-only
   page; only the canonical URL and "fully managed service" text confirmed.
3. Whether a public (non-private-offer) Documentum listing exists on AWS or GCP Marketplace.
4. Whether the Content Aviator trial tenant is Extended ECM (strongly implied) or Documentum,
   and its eligibility rules.
5. Whether Learning Services lab VMs include a deployed `dctm-rest` and allow inbound/outbound
   network access ("no data export" suggests not).
6. Whether the developer portal serves the raw Documentum REST OpenAPI JSON without login.
7. Whether the Docker Hub `transport-documentum*` / `t3-dctm` / `dctm-rest` images actually
   contain OpenText binaries — inferred from names and 3.6–8.7 GB sizes; layers not inspected.
   Irrelevant to the decision: they are rejected either way.
8. Whether Forefront, Armedia or a UK G-Cloud supplier would sell a small dev tenant to a
   private Israeli ISV.
9. Malam Team's Documentum (vs Content Server) capability; any Documentum practice at Matrix,
   Taldor, Elad, One1, Bynet, or the global SIs — no evidence found either way.
10. Forum poster roles (OpenText staff vs community) are taken from signatures and LinkedIn,
    not forum badges. REST 7.0 compatibility with Content Server 6.7 SP2 rests on a search
    snippet of a now-deleted blog.
