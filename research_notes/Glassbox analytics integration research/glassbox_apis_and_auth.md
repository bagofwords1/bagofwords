# Glassbox (glassbox.com) programmatic read APIs and authentication

Research date: 2026-11-17. Scope: Glassbox the digital experience analytics / session replay vendor (formerly Clarisite).

**Headline result:** No public technical API documentation for Glassbox could be found. No endpoint paths, base URLs, auth scheme, request/response shapes, rate limits, OpenAPI spec or Postman collection are published on the open web. Everything concrete below is marketing-level, contractual, or third-party; every endpoint-level detail a connector needs is in the Gaps lists and must be obtained from Glassbox (customer portal or account team) by a licensed customer.

Evidence-quality note: about 25 searches/fetches were run. Several candidate sources could not be read (listed per section). Items marked "(search snippet only)" were seen in search-result summaries and not verified against the full page.

## 1. What REST/HTTP APIs does Glassbox expose (endpoints, base URLs, methods, parameters, pagination, formats)?

### Takeaway
Glassbox publicly claims an "open API" and "data export" capability but publishes no endpoint reference; not a single endpoint path, hostname pattern, HTTP method or response schema was found in any public source. The only programmatic surface with a dated, official public announcement is the Glassbox MCP Server (July 2026), and it too has no public technical spec.

### Cited Findings
- Glassbox's integrations page states: "Integrate most non-native solutions in just two clicks with our open API." No endpoints, docs link or developer portal appear on the page; the only support link is the Customer Portal `https://support.glassbox.com/`. — [Glassbox integrations](https://glassbox.com/platform/integrations)
- Native/named integrations listed there: Adobe Analytics, Google Analytics (GA4), New Relic, Dynatrace, Google Tag Manager, Adobe Launch, Tealium, Ensighten, SurveyMonkey, Qualtrics, Medallia, Jira, Slack, Firebase, AB Tasty, Adobe Target, Optimizely, Monetate, Sitespect, VWO, Dynamic Yield, Kameleoon. — [Glassbox integrations](https://glassbox.com/platform/integrations)
- Most third-party integrations are session-link or client-side integrations, not server-side reads of Glassbox data:
  - Survicate: authorised via a "Connect" button in the Survicate UI; result is a "View Glassbox Session" link per response and a "Glassbox URL" column in exports. No URL format or API detail is documented. — [Survicate help](https://help.survicate.com/en/articles/3942551-glassbox-integration)
  - Kameleoon: "This method sends custom events directly from Kameleoon to Glassbox when a Kameleoon campaign exposes a visitor" (a write into Glassbox from the browser, not a read). No event schema or JS call is documented. — [Kameleoon docs](https://docs.kameleoon.com/user-manual/integrations/as-a-destination/analytics/glassbox.md)
  - Blue Triangle (search snippet only; page returned HTTP 403): shows a Glassbox session link in its RUM detail view; setup needs a custom variable and a per-customer Glassbox domain. — [Aternity/Blue Triangle help](https://help-uji.aternity.com/hc/en-us/articles/29836438605459-Blue-Triangle-and-Glassbox)
  - Medallia Digital (search snippet only; page blocked by a browser check): pairs surveys with Glassbox recordings; requires contacting Medallia support and supplying a URL pattern from the customer. — [Medallia docs](https://docs.medallia.com/medallia-digital/medallia-digital-web/analytics-solution/session-recording-software/glassbox)
  - Qualtrics (search snippet only): a link in Qualtrics opens the matching Glassbox replay; Glassbox, not Qualtrics, supports the integration. — [Glassbox blog](https://www.glassbox.com/blog/glassbox-qualtrics-scaling-customer-feedback), [Qualtrics marketplace](https://www.qualtrics.com/marketplace/glassbox-digital)
- Export claims (all marketing/review level, search snippets only, some pages several years old):
  - Compliance blog: data can be exported to combine with other sources, and "can be exported in real time if required". — [Glassbox compliance blog](https://www.glassbox.com/blog/digital-conduct-compliance-solution/)
  - Interaction Maps page lists a "Data Export" feature for exporting data for further analysis. — [Glassbox Interaction Maps blog](https://www.glassbox.com/blog/glassbox-interaction-maps/)
  - A Glassbox video is titled "Transform Your Data: Reduce Issues, Fraud, and Revenue Loss With Glassbox Data Export" (content not reviewed). — [YouTube](https://www.youtube.com/watch?v=Ofl9gy1WUP4)
  - A TrustRadius reviewer describes exporting "all interaction data in real-time" and wants filtering to manage export volume. — [TrustRadius](https://www.trustradius.com/products/glassbox/reviews)
  - AWS Marketplace listing: no API documented; one reviewer mentions "open APIs" and "pre-built connectors"; the page's AI-generated overview mentions automated data pipelines to "chosen tools or data warehouses". — [AWS Marketplace](https://aws.amazon.com/marketplace/pp/B09ZVL2TQB)
- Counter-evidence (search snippet only; thread returned HTTP 403, date not confirmed, URL id suggests roughly 2023): a Splunk Community user reported there was no option to export Glassbox session events alone, that events were only visible in the in-session "expert view", and that they could not find documentation for extracting events. — [Splunk Community](https://community.splunk.com/t5/Monitoring-Splunk/integration-of-splunk-with-glassbox/m-p/660054)
- API Tracker has a Glassbox profile with APIs/Specs/Explorer sections, but the fetched pages contained no API entries, base URLs, auth types or spec links; it lists the vendor site as `https://glassboxdigital.com`. — [API Tracker](https://apitracker.io/a/glassbox/apis)

### Inferences
- The "open API" is most likely a customer-gated REST surface plus a separately licensed data-export/streaming product, documented only inside the customer portal. This is inference from the absence of public docs combined with the marketing claims.
- Hostnames are probably per-tenant: Blue Triangle's setup asks for a per-customer Glassbox domain and AWS Marketplace lists hybrid, single-tenant and multi-tenant deployments. A connector should therefore take the base URL as a user-supplied config field rather than hard-coding one.
- Because hybrid/on-premises-style deployments exist, API availability and version may differ per customer.

### Gaps
- Not found anywhere public: endpoint paths, base URL pattern, regions, HTTP methods, query parameters, pagination scheme, response formats, API versioning, session search/export API, reports/funnels/dashboards API, events API, struggle/errors API, voice-of-customer API.
- The exact session replay deep-link URL format was not found (the two pages most likely to show it, Blue Triangle and Medallia, could not be fetched).
- Export destinations and formats (Kafka, S3, Snowflake, BigQuery, Splunk, etc.) are not confirmed by any source found.
- The Splunk thread conflicts with the marketing export claims; it may reflect an older version or an unlicensed add-on. Unresolved.

## 2. How does authentication work, and how are credentials issued?

### Takeaway
Nothing public describes API authentication for Glassbox. No source found mentions API keys, OAuth2, tokens, IP allow-listing or a console screen for issuing credentials.

### Cited Findings
- The only authorisation flow observed is in a third-party UI: Survicate users click "Connect" to authorise the Glassbox integration; the page gives no detail of what credential is exchanged. — [Survicate help](https://help.survicate.com/en/articles/3942551-glassbox-integration)
- The MCP Server announcement describes "governed access" but states no authentication method. — [Glassbox news, 2026-07-29](https://www.glassbox.com/news/glassbox-pulse-mcp-server-operational-intelligence/)
- Glassbox operates a Trust Center at `https://trust.glassbox.com/`; the fetch returned only the page title (JavaScript-rendered), so no SSO, API-security or allow-listing statements could be extracted. — [Glassbox Trust Center](https://trust.glassbox.com/)

### Inferences
- The connector design should keep auth pluggable (bearer token/API key, basic, and OAuth2 client credentials) until a customer supplies the real scheme. This is a design hedge, not a finding.

### Gaps
- Auth scheme, token lifetime, scopes/roles, service-account support, SSO interplay, IP allow-listing, and where in the Glassbox console credentials are created: all unknown.

## 3. Is there a query language, SQL-like interface, GIA API or MCP server?

### Takeaway
No public evidence of a SQL or query-language interface. Glassbox has two AI surfaces: GIA (in-product assistant, January 2024) and the Glassbox MCP Server with Glassbox Pulse (announced 29 July 2026); the MCP Server is the most relevant route for an AI analyst, but its tools, transport, endpoint and auth are undocumented publicly.

### Cited Findings
- Glassbox MCP Server (announced 2026-07-29): it "provides enterprise AI systems with governed access to Glassbox digital experience intelligence"; via the Model Context Protocol, "AI applications and agents can access relevant Glassbox context and evidence". The release says it avoids streaming raw data to AI. No tool names, schemas, endpoint, auth, supported clients, GA/beta status or pricing are given. — [Glassbox news](https://www.glassbox.com/news/glassbox-pulse-mcp-server-operational-intelligence/)
- Glassbox Pulse (same release): "an AI-powered operational intelligence engine"; "Pulse applies machine learning first to identify meaningful changes"; users "can ask operational questions in natural language and receive answers tied to underlying evidence", covering "customer behavior, journey impact, errors, network logs and server-side failures". — [Glassbox news](https://www.glassbox.com/news/glassbox-pulse-mcp-server-operational-intelligence/)
- The release says Glassbox plans to keep expanding Pulse and the MCP Server (forward-looking statement, not a delivered capability). — [Glassbox news](https://www.glassbox.com/news/glassbox-pulse-mcp-server-operational-intelligence/)
- GIA (Glassbox Insights Assistant) launched January 2024, powered by Microsoft Azure OpenAI Service; it answers questions over reports and produces session summaries and mobile report summaries (search snippets only). No programmatic GIA API is described. — [destinationCRM](https://www.destinationcrm.com/Articles/CRM-News/CRM-Across-the-Wire/Glassbox-Launches-GIA-(Glassbox-Insights-Assistant)-162107.aspx), [Glassbox GIA page](https://glassbox.com/platform/glassbox-insights-assistant)
- The same release states the platform holds ISO 42001 certification for AI management. — [Glassbox news](https://www.glassbox.com/news/glassbox-pulse-mcp-server-operational-intelligence/)

### Inferences
- The MCP Server appears designed to return curated/aggregated "context and evidence" rather than raw event rows, so it may not support arbitrary analytical queries.
- For an AI-analyst product, wrapping the vendor's MCP Server may be a more durable route than reverse-building a REST connector, provided the customer is licensed for it.

### Gaps
- No evidence found for or against a SQL/ClickHouse-style interface; the storage engine is not publicly documented in anything found.
- Whether GIA is callable by API, and whether GIA and the MCP Server share a backend, is unconfirmed.
- MCP transport (stdio vs. remote HTTP), URL, auth and tool list are unknown.

## 4. Rate limits, quotas, retention, latency, licensing

### Takeaway
No API rate limits or quotas are published. Licensing is session-based (and, contractually, "API Calls"-based), retention is customer-specific and reviewers call it short, and Glassbox claims near-real-time capture.

### Cited Findings
- Contract terms (General Terms & Conditions effective 15 July 2022, marked superseded 2 August 2022; old): Glassbox bills on monthly Sessions and/or "API Calls" with overage ("Licensed Monthly API Calls"). Here "API Calls" are defined as "the medium of interaction between different applications" and capturing them "allows for the monitoring of all client-server side communications" — i.e. the captured API traffic of the customer's own application, not calls to a Glassbox API. — [Glassbox T&Cs 2022](https://www.glassbox.com/glassbox-general-terms-conditions-effective-july-15-2022/)
- Session definition (same document): a session starts when an end-user accesses the customer's platform and ends on abandonment — browser/app close, or "a period of 20 minutes" with no activity. — [Glassbox T&Cs 2022](https://www.glassbox.com/glassbox-general-terms-conditions-effective-july-15-2022/)
- Same document: use is "limited to Customer's internal business use"; Customer Data and derived information "belong exclusively to Customer"; no retention period is stated. — [Glassbox T&Cs 2022](https://www.glassbox.com/glassbox-general-terms-conditions-effective-july-15-2022/)
- AWS Marketplace 12-month tiers: Basic up to 50K sessions/month and 10 seats ($50,000); Standard up to 100K sessions/month and 15 seats ($75,000); Enterprise from 100K sessions via private offer ($100,000 listed). Delivery is SaaS with hybrid, single-tenant or multi-tenant options. API access is not mentioned as included or as an add-on. — [AWS Marketplace](https://aws.amazon.com/marketplace/pp/B09ZVL2TQB)
- Reviewer-reported limits (not vendor statements): dashboard limits on widgets, days of data and table rows; short data retention hindering long-term analysis (search snippet only for G2). — [AWS Marketplace reviews](https://aws.amazon.com/marketplace/pp/B09ZVL2TQB), [G2](https://www.g2.com/products/glassbox/reviews)
- Latency (vendor marketing claim, search snippet only): sessions are recorded as they happen and delivered "with a 2-second or less delay". — [Glassbox session recording page](https://www.glassbox.com/cp/session-recording-tool/)

### Inferences
- Since entitlement is counted in sessions and seats, programmatic access is probably governed by contract/add-on rather than a published per-key quota; a connector should assume conservative throttling and be configurable.

### Gaps
- API rate limits, concurrency limits, export size caps, retention periods per data type, aggregate-report freshness, and whether API/data export/MCP are paid add-ons: all unknown.
- Current (post-2022) terms were not reviewed.

## 5. Where do the docs live, and which are public vs. gated?

### Takeaway
Technical documentation appears to be entirely customer-gated behind the Glassbox Customer Portal at `https://support.glassbox.com/`. No public developer portal, Swagger/OpenAPI spec or Postman collection was found.

### Cited Findings
- `https://support.glassbox.com/` is labelled "Customer Portal" in the Glassbox site footer and is the support address on the AWS Marketplace listing; fetching it returned an empty body, so its platform and login requirement could not be confirmed directly. — [Glassbox integrations](https://glassbox.com/platform/integrations), [AWS Marketplace](https://aws.amazon.com/marketplace/pp/B09ZVL2TQB)
- `https://docs.glassbox.com/` did not resolve (DNS ENOTFOUND) from the research environment. This is ambiguous: the host may not exist, or it may be blocked by the environment's network allow-list. — attempted fetch of [docs.glassbox.com](https://docs.glassbox.com/)
- Public Glassbox pages that mention APIs contain marketing text only: [integrations](https://glassbox.com/platform/integrations), [GIA](https://glassbox.com/platform/glassbox-insights-assistant), [MCP/Pulse release](https://www.glassbox.com/news/glassbox-pulse-mcp-server-operational-intelligence/), [Trust Center](https://trust.glassbox.com/), [SLA](https://www.glassbox.com/sla).
- Contacts: partners@glassbox.com for private offers (AWS listing); support@glassbox.com for the Qualtrics integration (search snippet only). — [AWS Marketplace](https://aws.amazon.com/marketplace/pp/B09ZVL2TQB), [Qualtrics marketplace](https://www.qualtrics.com/marketplace/glassbox-digital)
- A third-party article "Glassbox Developer Documentation: A Practical Guide" exists on webeyez.com; the fetch returned empty content, and the snippet says only that developer docs are the reference for the API, SDKs and event schemas without linking to them. Treat as low-value (a competitor's SEO page). — [Webeyez](https://webeyez.com/insights/guides/glassbox-developer-documentation-guide)
- Name collisions to ignore: PyPI `glassbox-py` (uses `GLASSBOX_API_URL`/`GLASSBOX_API_TOKEN`, an unrelated product), `glassbox-rag`, GitHub `skysoart/glassbox-ai`, `DRMacIver/glassbox`, and Glassboxx (ebook app with a Bearer-token API). — [PyPI glassbox-py](https://pypi.org/project/glassbox-py/), [Glassboxx API](https://glassboxx.com/help-centre/using-the-api/)

### Inferences
- An engineer building the connector will need a customer login or NDA-level access to obtain the API reference; public research cannot substitute for it.

### Gaps
- web.archive.org was not checked (tool budget exhausted); archived copies of support.glassbox.com or old Clarisite docs might exist.
- Adobe Exchange, Azure Marketplace, Google Cloud Marketplace and Salesforce AppExchange listings were not checked.
- Pages that blocked fetching and may hold useful detail if opened in a browser: the Blue Triangle article, the Medallia Glassbox page, the Splunk Community thread, the Trust Center, and the Qualtrics marketplace page.

## 6. Glassbox data model and exposed fields/identifiers

### Takeaway
Only a conceptual data model can be assembled from public sources: sessions, captured events, client-server API calls, struggle scores, journeys/funnels, segments, errors, heatmap/interaction data and custom events. No field-level schema or identifier names are published.

### Cited Findings
- Session: begins when an end-user accesses the customer's platform; ends on browser/app close or 20 minutes of inactivity (2022 terms). — [Glassbox T&Cs 2022](https://www.glassbox.com/glassbox-general-terms-conditions-effective-july-15-2022/)
- Events: Glassbox says it captures and classifies more than 1,000 events in an average session (search snippet only). — [Glassbox session replay](https://glassbox.com/platform/session-replay)
- Client-server traffic: captured "API Calls" cover "all client-server side communications" (2022 terms); Pulse's evidence spans "errors, network logs and server-side failures". — [Glassbox T&Cs 2022](https://www.glassbox.com/glassbox-general-terms-conditions-effective-july-15-2022/), [Glassbox news](https://www.glassbox.com/news/glassbox-pulse-mcp-server-operational-intelligence/)
- Custom events exist and can be sent by third parties from the browser (Kameleoon campaign exposure). — [Kameleoon docs](https://docs.kameleoon.com/user-manual/integrations/as-a-destination/analytics/glassbox.md)
- Reviewer-described features (AWS Marketplace page; reviewer claims, not vendor spec): session search by session or correlation ID; heatmaps; journey mapping; custom segments; a struggle score aggregating behavioural and technical triggers such as rage clicks and Ajax failures; dashboards; threshold-based alerts; real-time session viewing; manual page classification. — [AWS Marketplace](https://aws.amazon.com/marketplace/pp/B09ZVL2TQB)
- Capture: vendor claims tagless capture of web and mobile interactions with automatic masking of PII/PCI/PHI/NPI; a reviewer describes iOS and Android SDKs with React Native and Flutter wrappers. — [AWS Marketplace](https://aws.amazon.com/marketplace/pp/B09ZVL2TQB)
- Funnels/flows and Interaction Maps are product objects; Interaction Maps elements can be added to flows and funnels (search snippet only). — [Glassbox Interaction Maps blog](https://www.glassbox.com/blog/glassbox-interaction-maps/)
- Each session has a shareable replay URL, as surfaced by Survicate's "Glassbox URL" column. — [Survicate help](https://help.survicate.com/en/articles/3942551-glassbox-integration)

### Inferences
- Masking happens at capture, so sensitive field values will likely be absent or redacted in any export; an AI analyst should not expect raw PII.
- A session identifier plus a customer-defined correlation ID are the likely join keys to other systems.

### Gaps
- Field names, types, identifier formats (session ID/GUID, user ID, device ID, page/screen ID), event taxonomy, struggle-score scale and components, error object shape, DOM-event and mobile-event schemas, user-attribute model, and funnel/segment definitions as API objects: none are public.
- Client-side JS/SDK API names for web and mobile could not be confirmed from any source.
