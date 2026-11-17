# Glassbox (glassbox.com) — Bulk Data Export, Data Streaming, and Data-Warehouse Integration Options

Research date: 2026-11-17. Scope: Glassbox the digital experience analytics / session replay vendor (formerly Clarisite).

Confidence legend used below:
- **[Fetched]** = I opened the page and the claim is on it.
- **[Snippet]** = seen only in a search-result summary; page not opened or not fully readable. Treat as lead, not fact.
- All fetched pages were read through an automated page summariser, so "verbatim" quotes are as relayed by that tool and may be truncated.

Headline for the report writer: **I found no public documentation of a named warehouse/lake export product (no "Data Export", "Data Hub", "Data Feed" SKU, no Snowflake/BigQuery/Redshift/Databricks/S3/ADLS/GCS/Kafka/Kinesis/Pub/Sub destination list, no schema, no file format, no cadence).** Glassbox's technical documentation appears to be gated behind customer login; no public developer portal or API reference surfaced in any search. What is public is (a) marketing claims of an "open API", "unlimited API access", an "event streaming pipeline" and "automated data pipelines" to "chosen tools or data warehouses", (b) a list of named point integrations, and (c) a July 2026 MCP Server launch.

---

## 1. Does Glassbox offer a data export / streaming / "Data Hub" / "Connect" / "Data Feed" product, and what destinations are supported?

### Takeaway
Publicly, Glassbox advertises an "open API", "Unlimited API access" on all plans, and an "event streaming pipeline" with "fully automated data pipelines", but names no warehouse, lake, or message-bus destination anywhere I could find. The only product literally named "Glassbox Connect" is "Glassbox Connect for Adobe Analytics" (an Adobe Exchange app), not a general export product.

### Cited Findings
- **[Fetched]** Glassbox's integrations page says it offers "native integrations and open API" to "integrate with almost any third-party solution in 2-clicks or less" and "Integrate most non-native solutions in just two clicks with our open API." The page does not mention data export, data streaming, data warehouses, webhooks, BI tools, or CDPs. — [Glassbox Integrations](https://www.glassbox.com/platform/integrations/)
- **[Fetched]** The pricing page lists "Unlimited API access" as included in all three plan columns ("Production Operation", "Marketing/Business", "Product"). Data export and data streaming are not listed as line items. — [Glassbox Plans & Pricing](https://www.glassbox.com/platform/plans-pricing/)
- **[Fetched]** The Glassbox-authored AWS Marketplace listing (undated vendor text) states: "Event streaming pipeline built on Autocapture architecture that delivers instant access to high-quality, trusted data..." and "Fully automated data pipelines that enable users to act on insights through streamlined workflows...", under a feature heading "Automated Data Integration". No destination is named. — [AWS Marketplace: Glassbox Enterprise Customer Intelligence and Session Replay](https://aws.amazon.com/marketplace/pp/prodview-waapo3vlxej2o)
- **[Fetched]** The other AWS Marketplace listing was summarised as mentioning "integration with chosen tools or data warehouses" through automated pipelines, with no specific warehouse or export format named. — [AWS Marketplace listing B09ZVL2TQB](https://aws.amazon.com/marketplace/pp/B09ZVL2TQB)
- **[Fetched]** An AWS Marketplace reviewer (Ranu Singhal, Jul 29 2026) wrote that "Glassbox integrates by sharing session data and connecting with tools like Dynatrace and other platforms through pre-built connectors and open APIs". — [AWS Marketplace listing](https://aws.amazon.com/marketplace/pp/prodview-waapo3vlxej2o)
- **[Fetched]** An app named "Glassbox Connect for Adobe Analytics" exists on Adobe Exchange; the page body could not be read (JavaScript-rendered, only the title "Adobe Exchange" was returned). — [Adobe Exchange: Glassbox Connect for Adobe Analytics](https://exchange.adobe.com/apps/ec/101752/glassbox-connect-for-adobe-analytics)
- **[Snippet]** A third-party comparison (Lokker) lists Glassbox as offering "Enterprise data export and API access" with no detail on formats, limits, or schema. Competitor/aggregator source; low weight. — [Lokker session replay comparison](https://lokker.com/compare/session-replay-tools)
- **[Snippet]** A Glassbox blog post says platforms built on Lambda or Kafka architectures can push digital experience data into BI systems and data lakes; per the search summary this is framed as a general industry approach, not a documented Glassbox feature. Page not opened. — [Glassbox blog: How to fit digital into the customer analytics landscape](https://www.glassbox.com/blog/customer-analytics-boost-with-digital/)
- **[Fetched]** GIA marketing positions report summarisation as removing the need to export data for separate analysis — implying report-level export exists in the UI, but not describing it. — [Glassbox Insights Assistant](https://www.glassbox.com/platform/glassbox-insights-assistant/)
- **[Fetched]** An AWS Marketplace reviewer noted dashboards have limits on widgets, days of data retrievable, and table rows. — [AWS Marketplace listing B09ZVL2TQB](https://aws.amazon.com/marketplace/pp/B09ZVL2TQB)

### Inferences
- An API exists and is commercially included ("Unlimited API access"), but its surface (session search? event export? report results? admin only?) is undocumented publicly. An integrating product cannot be designed against it without customer-provided docs or a Glassbox partner relationship.
- "Event streaming pipeline" in the AWS listing most plausibly describes Glassbox's internal ingest architecture plus some outbound feed, but the wording does not establish a customer-facing stream to Kafka/Kinesis/etc. Do not treat it as a confirmed outbound streaming product.
- Because no warehouse destination is named anywhere public, there is no evidence that Glassbox data routinely "lands" in customers' Snowflake/BigQuery/Databricks via a first-party connector. Where it does land there, it is likely via customer-built pipelines against the API or a bespoke/professional-services feed (inference, unverified).
- For a BI/AI-analyst product: the "integrate via customer's warehouse" path cannot be assumed to exist per customer; it must be discovered per account. The "integrate via Glassbox directly" path now has a first-party, AI-oriented entry point (MCP Server, see section 5) that is more clearly productised than any bulk export.

### Gaps
- No public page found for any product named "Data Export", "Data Streaming", "Data Hub", or "Data Feed". Could not confirm or deny their existence; docs appear gated.
- No destination list found for Snowflake, BigQuery, Redshift, Databricks, S3, Azure Blob/ADLS, GCS, Kafka, Kinesis, Pub/Sub, or webhooks. Searches restricted to glassbox.com domains returned nothing on these.
- No Snowflake Marketplace / Partner Connect, Databricks Partner, or Google Cloud Marketplace listing was found (not exhaustively searched on each marketplace's own search; only via web search).
- No public API reference: endpoints, auth scheme, rate limits, pagination, and whether raw event-level data is retrievable are all unknown.
- Adobe Exchange page content for "Glassbox Connect for Adobe Analytics" was unreadable.

---

## 2. Exported schema: tables, columns, event types, grain, file formats, partitioning, cadence, latency

### Takeaway
Nothing about an export schema is public. The only data-model facts available are marketing-level: tagless capture of 100% of events, >1,000 events per session on average, and "instant" availability inside the product.

### Cited Findings
- **[Fetched]** Glassbox "captures and indexes 100% of digital events across your website and mobile app", "averaging more than 1000 events per session". — [Glassbox for Data & Analytics teams](https://www.glassbox.com/solutions/data-analytics/)
- **[Fetched]** "Data is captured, indexed and available instantly." Session data is "indexed and searchable". — [Glassbox for Data & Analytics teams](https://www.glassbox.com/solutions/data-analytics/)
- **[Fetched]** Capture is tagless/"Autocapture" across web and mobile including SPAs, React Native and Flutter; analysed signals include dead clicks, errors, slow pages, struggle detection. — [AWS Marketplace listing B09ZVL2TQB](https://aws.amazon.com/marketplace/pp/B09ZVL2TQB)
- **[Fetched]** Evidence types Glassbox Pulse ties answers to: "customer behavior, journey impact, errors, network logs, and server-side failures" — indicating these data classes exist in the platform. — [Glassbox Pulse / MCP Server press release, Jul 29 2026](https://www.glassbox.com/news/glassbox-pulse-mcp-server-operational-intelligence/)
- **[Fetched]** A reviewer (Aug 04 2026) says "the API details, code, and request response information are valuable" — i.e. captured network request/response data is viewable in-product (this refers to captured customer-site API calls, not Glassbox's own API). — [AWS Marketplace listing](https://aws.amazon.com/marketplace/pp/prodview-waapo3vlxej2o)
- **[Fetched]** Retention by plan: session replay 1 / 3 / 3 months and analysis data 1 / 13 / 6 months for Production Operation / Marketing-Business / Product respectively. — [Glassbox Plans & Pricing](https://www.glassbox.com/platform/plans-pricing/)

### Inferences
- Natural grains are session → page/screen → event (clicks, errors, network calls), judging by product descriptions; this is inferred, not from a schema document.
- Retention limits (as low as 1 month for replay/analysis on one plan) bound how much history any API-based connector could backfill; a customer warehouse copy, if one exists, would be the only source of longer history.
- In-product latency is claimed "instant"; no claim exists about export latency.

### Gaps
- Table names, column lists, event taxonomy, IDs/join keys (session ID, user ID, visitor ID), file formats (Parquet/JSON/CSV/Avro), partitioning, delivery cadence, and SLAs: **not found anywhere public.**
- Whether exports are raw events, sessionised rows, or only aggregated report results: unknown.

---

## 3. Deployment model, hosting, underlying storage/query engine, and effect on API/export availability

### Takeaway
Glassbox sells three deployment shapes — multi-tenant cloud, single-tenant cloud on AWS or Azure, and on-premises (also described as "hybrid") — and the pricing page says on-prem, cloud and hybrid are available on all plans. The storage/query engine is not publicly disclosed.

### Cited Findings
- **[Fetched]** Single-tenant cloud is hosted on AWS or Microsoft Azure at the customer's choice ("your Glassbox system is securely isolated"); multi-tenant cloud is positioned as lower-cost for mid-sized organisations; on-premises "offers the greatest control over your data protection." — [Glassbox Data Privacy & Security](https://glassbox.com/platform/data-privacy-security)
- **[Fetched]** Pricing page: "On-Prem, Cloud, and Hybrid services" all included across plans. — [Glassbox Plans & Pricing](https://www.glassbox.com/platform/plans-pricing/)
- **[Fetched]** AWS Marketplace listing: "Deploy in hybrid, single-tenant, or multi-tenant environments"; delivery method SaaS, deployed on AWS, sold by Glassbox. Listed 12-month prices: Basic up to 50K sessions/month, 10 seats, $50,000; Standard up to 100K sessions/month, 15 seats, $75,000; Enterprise from 100K sessions, private offer, $100,000 listed. — [AWS Marketplace listing B09ZVL2TQB](https://aws.amazon.com/marketplace/pp/B09ZVL2TQB)
- **[Snippet]** Glassbox is also listed in the Microsoft Azure Marketplace (announcement predates 2024; exact date not verified). — [Glassbox news: Azure Marketplace](https://www.glassbox.com/news/glassbox-now-available-in-the-microsoft-azure-marketplace/); [Azure Marketplace listing](https://azuremarketplace.microsoft.com/en-us/marketplace/apps/glassbox.glassbox_solution)
- **[Snippet]** A separate AWS Marketplace listing is sold by reseller Ness. — [AWS Marketplace: Glassbox by Ness](https://aws.amazon.com/marketplace/pp/prodview-kjr55rjtnjuni)
- **[Fetched]** Customer base skews to regulated industries (financial services, insurance, government, telecom, airlines, utilities); Glassbox claims seven of the ten largest US banks by assets and customers in 40+ countries. — [AWS Marketplace listing](https://aws.amazon.com/marketplace/pp/B09ZVL2TQB); [Pulse/MCP press release](https://www.glassbox.com/news/glassbox-pulse-mcp-server-operational-intelligence/)
- **[Fetched]** GIA is built on Microsoft's Azure OpenAI Service. — [Glassbox Insights Assistant](https://www.glassbox.com/platform/glassbox-insights-assistant/)

### Inferences
- With large banks as core customers and on-prem/single-tenant offered, a meaningful share of deployments will not be reachable from a third-party SaaS over the public internet without customer network work (allow-listing, private link, or an agent inside the customer network). A connector should assume per-customer base URLs and possibly no inbound reachability.
- Cloud-dependent AI features (GIA on Azure OpenAI; likely Pulse and the MCP Server) may be unavailable or differently delivered on-prem. This is inference; no page states feature parity by deployment model.
- No GCP hosting option is mentioned; only AWS and Azure.

### Gaps
- Hosting regions / data-centre list: not published on the pages read.
- Underlying storage and query engine (Elasticsearch, Cassandra, ClickHouse, Kafka, Hadoop, etc.): **not found.** A "Backend Data Developer" careers posting surfaced in search but returned HTTP 404 when fetched; search snippets named no technologies.
- Whether API access, exports, MCP Server and GIA are available in on-prem and single-tenant deployments: not stated.
- Whether there is a GCP Marketplace listing: not found.

---

## 4. Out-of-the-box integrations with analytics/BI/CDP/APM/VoC tools — direction and mechanism

### Takeaway
Glassbox publishes a list of ~25 named integrations concentrated in web analytics, tag management, APM, VoC, A/B testing and collaboration; none is a BI tool, warehouse, or CDP in the Segment/mParticle sense. Direction and mechanism are almost never stated publicly.

### Cited Findings
Named on Glassbox's own pages (direction "not stated" unless noted):

| Tool | Category (per Glassbox) | Direction / mechanism as publicly described | Source |
|---|---|---|---|
| Adobe Analytics | Web analytics | "Enhance your existing web analytics data with more robust behavioral and performance insights" with no additional tagging; separate "Adobe Segments" integration listed on pricing page; Adobe Exchange app "Glassbox Connect for Adobe Analytics". A reviewer (Jul 21 2026) recommends "using the Adobe API if it is available" over a data-layer setup and says it "would have made finding Adobe segments easier" — suggests Adobe segments are pulled INTO Glassbox via Adobe's API. | [Integrations](https://www.glassbox.com/platform/integrations/); [Pricing](https://www.glassbox.com/platform/plans-pricing/); [Adobe Exchange](https://exchange.adobe.com/apps/ec/101752/glassbox-connect-for-adobe-analytics); [AWS reviews](https://aws.amazon.com/marketplace/pp/prodview-waapo3vlxej2o) |
| Google Analytics / GA4, "GA4 Audiences" | Web analytics | Listed. A reviewer (Aug 03 2026): "Since they launched the integration two years ago, we could not link Google Analytics properly, and we are still waiting." | [Integrations](https://www.glassbox.com/platform/integrations/); [Pricing](https://www.glassbox.com/platform/plans-pricing/); [AWS reviews](https://aws.amazon.com/marketplace/pp/prodview-waapo3vlxej2o) |
| Google Tag Manager, Adobe Launch, Tealium, Ensighten | Tag management | Inbound to Glassbox: "Import tags from your existing tools to capitalize on previous tagging efforts" | [Integrations](https://www.glassbox.com/platform/integrations/) |
| New Relic, Dynatrace | APM | "integrating with your existing platform to monitor technical events"; direction not stated. Reviewer says Glassbox "integrates by sharing session data" with Dynatrace. | [Integrations](https://www.glassbox.com/platform/integrations/); [AWS reviews](https://aws.amazon.com/marketplace/pp/prodview-waapo3vlxej2o) |
| AppDynamics, Splunk, Firebase | APM (pricing page) | Listed only. A reviewer (Jul 28 2026): "we have integrated it with other platforms such as Splunk, Grafana, and AppDynamics". | [Pricing](https://www.glassbox.com/platform/plans-pricing/); [AWS reviews](https://aws.amazon.com/marketplace/pp/prodview-waapo3vlxej2o) |
| Qualtrics, Medallia, SurveyMonkey, Usabilla | Voice of Customer | "integrating with your existing platform to determine which experiences result in positive or negative feedback". For Qualtrics, per search summary of a Glassbox blog, survey responses can feed Glassbox's Augmented Journey Map and users can search recordings for survey-mentioned elements (i.e. VoC data inbound to Glassbox; blog not opened — [Snippet]). | [Integrations](https://www.glassbox.com/platform/integrations/); [Pricing](https://www.glassbox.com/platform/plans-pricing/); [Glassbox blog: Qualtrics](https://www.glassbox.com/blog/glassbox-qualtrics-scaling-customer-feedback) |
| AB Tasty, Adobe Target, Optimizely, Monetate, SiteSpect, VWO, Dynamic Yield, Kameleoon | A/B testing / personalisation | Listed only; mechanism not described. | [Integrations](https://www.glassbox.com/platform/integrations/) |
| Jira, Slack, Microsoft Teams | Collaboration / ticketing | Listed only. | [Integrations](https://www.glassbox.com/platform/integrations/); [Pricing](https://www.glassbox.com/platform/plans-pricing/) |
| Grafana | (reviewer-reported, not on Glassbox pages read) | Customer-built or native unknown. | [AWS reviews](https://aws.amazon.com/marketplace/pp/prodview-waapo3vlxej2o) |

- **[Fetched]** The pricing page does not map integrations to plans. — [Pricing](https://www.glassbox.com/platform/plans-pricing/)
- **[Snippet]** Tealium's site reportedly promotes native support for "Glassbox Detector" (the Glassbox capture tag) — consistent with Tealium deploying Glassbox's tag, not exporting Glassbox data. Tealium page not opened. — [Tealium](https://tealium.com/?p=50020)
- **[Snippet]** Glassbox's partner-network launch names Adobe, Microsoft, AWS and Optimizely among partners. Not opened; date not verified. — [Glassbox Global Partner Network](https://www.glassbox.com/news/glassbox-global-partner-program/)
- **[Snippet]** Medallia sells a competing product (Medallia Digital Experience Analytics); nonetheless Medallia appears in Glassbox's VoC integration list. — [Medallia DXA integrations](https://www.medallia.com/products/digital-experience-analytics/integrations/)

### Inferences
- The typical pattern for this vendor class (and consistent with the fragments above) is ID-stitching: Glassbox's session ID/replay link is pushed into the other tool (an Adobe eVar, a GA custom dimension, a VoC survey embedded field, an APM/Splunk log field, a Jira ticket), and/or segments/audiences from the other tool are imported into Glassbox for filtering. **This is inference from industry norm and reviewer wording; Glassbox's public pages do not state mechanism.**
- Practical consequence: in many customers the most accessible "Glassbox data in the warehouse" is not Glassbox's event data at all but a Glassbox session ID/replay URL column sitting in Adobe Analytics data feeds, GA4 BigQuery export, Qualtrics/Medallia response data, or Splunk logs. An AI-analyst product working from the warehouse could use those as deep links back to Glassbox rather than as analytics data.
- Absence from all public lists: Tableau, Power BI, Looker, Snowflake, BigQuery, Redshift, Databricks, Segment, mParticle, Datadog, Salesforce. No evidence of native connectors for these.

### Gaps
- Direction and mechanism for every integration other than tag import: not publicly stated.
- No evidence found (positive or negative) for Segment, mParticle, Tableau, Power BI, Looker, Datadog, Salesforce integrations.
- Partner-side documentation (Adobe Exchange body, Tealium, Qualtrics, Dynatrace hub, Splunkbase) was not successfully read; a Splunkbase / Dynatrace Hub search was not performed.
- Reviewer statements reflect single customers and may describe custom work.

---

## 5. Recent developments 2024–2026: AI features, MCP server, ownership, roadmap

### Takeaway
Glassbox was taken private by Alicorn Venture Partners in 2024, launched GIA (Azure OpenAI-based assistant) in January 2024, and on July 29, 2026 launched **Glassbox Pulse** and a **Glassbox MCP Server** explicitly meant to give enterprise AI agents "governed access" to Glassbox intelligence *instead of* raw journey-data streams. This is the most relevant data-access development for an AI-analyst integration.

### Cited Findings
- **[Fetched]** Jul 29 2026 (New York): Glassbox announced Glassbox Pulse, an "AI-powered operational intelligence engine" that applies ML first to flag meaningful changes and classify behavioural/technical signals, detecting e.g. stalled applications, failed payments, release-related friction, avoidable service calls, conversion risk; users can ask operational questions in natural language with answers tied to evidence (customer behaviour, journey impact, errors, network logs, server-side failures). — [Glassbox press release](https://www.glassbox.com/news/glassbox-pulse-mcp-server-operational-intelligence/)
- **[Fetched]** Same release: the Glassbox MCP Server gives enterprise AI systems governed access to Glassbox digital experience intelligence via the Model Context Protocol; AI applications and agents can access Glassbox context and evidence "without ingesting a raw stream" of digital journey data. The release does **not** state MCP tool names, auth method, pricing, availability date/GA status, supported clients, or deployment-model availability. — [Glassbox press release](https://www.glassbox.com/news/glassbox-pulse-mcp-server-operational-intelligence/)
- **[Fetched]** Roadmap statement: Glassbox says it will continue expanding Pulse and the capabilities available through the MCP Server. — [Glassbox press release](https://www.glassbox.com/news/glassbox-pulse-mcp-server-operational-intelligence/)
- **[Fetched]** CEO Guy Perry (2026): "Enterprise AI has to move from answers to action"; "Our customers don't need more noise; they need an auditable dataset that binds customer behavior to its technical cause". — [Glassbox press release](https://www.glassbox.com/news/glassbox-pulse-mcp-server-operational-intelligence/); corroborated by [IT Brief, Jul 30 2026](https://itbrief.news/story/glassbox-launches-ai-tools-for-enterprise-digital-data)
- **[Fetched]** Trade commentary (Renascence newsdesk, Aug 12 2026, a curated secondary briefing citing a PR Newswire release) describes Glassbox as a "real-time operational intelligence layer designed to feed enterprise AI systems", with structured in-session signals flowing to AI agents "as behaviour happens"; it gives no mechanism, destination, schema, or latency figure. — [Renascence](https://www.renascence.io/in/news/25504/glassbox-streams-live-cx-data-into-enterprise-ai-systems)
- **[Fetched]** GIA (Glassbox Insights Assistant): chat assistant that searches web/mobile sessions in natural language, summarises sessions and ad-hoc/mobile reports, ranks friction by business impact; "built using Microsoft's Azure OpenAI Service", which is used to "prevent your data from being used to train future models". No GIA API is mentioned. — [Glassbox Insights Assistant](https://www.glassbox.com/platform/glassbox-insights-assistant/)
- **[Snippet]** GIA was announced Jan 10 2024 (Business Wire headline: "Glassbox Delivers Enterprise-Ready Generative AI Assistant with Microsoft Azure OpenAI Service"). — [Business Wire syndication](https://business.minstercommunitypost.com/minstercommunitypost/article/bizwire-2024-1-10-glassbox-delivers-enterprise-ready-generative-ai-assistant-with-microsoft-azure-openai-service)
- **[Snippet]** Ownership: on May 20 2024 Alicorn Venture Partners (an early investor) agreed to acquire Glassbox in an all-cash deal valued at ~US$150M; shares ceased trading on the Tel Aviv Stock Exchange (TASE: GLBX) on Oct 31 2024; then-CEO Yaron Morgenstern and management stayed. Globes reported the price as a 57% loss for TASE investors. — [Glassbox news](https://www.glassbox.com/news/glassbox-to-be-acquired-by-alicorn-venture-partners/); [Business Wire](https://www.businesswire.com/news/home/20240520265468/en/Glassbox-to-Be-Acquired-by-Alicorn-Venture-Partners-to-Accelerate-AI-Investment-and-Product-Growth); [Glassbox news (completion)](https://www.glassbox.com/news/glassbox-alicorn/); [Globes](https://en.globes.co.il/en/article-1001479093)
- **[Snippet]** Nov 4 2025: Business Wire reported Glassbox announced the acquisition of Anodot (real-time anomaly detection). Seen only as a search-result summary; article not opened. — [Business Wire](https://www.businesswire.com/news/home/20251104734652/en)
- **[Fetched]** Certifications claimed in 2026: ISO 42001 (AI management) and ISO/IEC 27701:2019. — [Glassbox press release](https://www.glassbox.com/news/glassbox-pulse-mcp-server-operational-intelligence/)
- **[Snippet]** A job listing titled "AI Platform Engineer - Autonomous Agents" at Glassbox Ltd appeared on a third-party board (not opened). — [Vaia talents](https://talents.vaia.com/companies/glassbox-ltd/ai-platform-engineer-autonomous-agents-109055765/)

### Inferences
- CEO changed between the 2024 take-private (Yaron Morgenstern) and July 2026 (Guy Perry); the date and circumstances of the change were not researched.
- Glassbox's stated direction is curated, governed, evidence-linked context for AI agents over MCP rather than bulk raw export ("without ingesting a raw stream"). An AI-analyst product that speaks MCP as a client aligns with the vendor's roadmap; a product that needs raw tables for SQL is working against the grain of what Glassbox is publicly investing in.
- The Anodot acquisition (if confirmed) plausibly underpins Pulse's anomaly detection — inference only.
- Naming caution: a later, unrelated "Glassbox Solutions" (India) acquisition by Komerz appeared in search results; do not conflate.

### Gaps
- MCP Server specifics: tool list, resources, transport (remote/HTTP vs local), auth (OAuth/API key), tenancy, rate limits, licensing/pricing, GA vs preview, and availability for on-prem/single-tenant: **not published in the sources read.**
- Whether GIA or Pulse are exposed programmatically other than via MCP: unknown.
- The primary PR Newswire release behind the August 2026 "streams live CX data" coverage was not read; whether it describes a distinct streaming product or just restates Pulse/MCP is unresolved.
- Anodot acquisition details and CEO transition not verified from primary pages.

---

## 6. Privacy/compliance constraints on exported data

### Takeaway
Glassbox masks sensitive data by default at capture, lets customers omit fields entirely, and enforces RBAC with audit logging; so a connector can only ever read what the customer's capture and masking configuration allowed to be stored, and access will be role-scoped. No public statement covers export-specific controls, consent handling, or data residency.

### Cited Findings
- **[Fetched]** Sensitive information is masked by default and masking is customisable; customers control what is captured and what is visible to Glassbox users; highly sensitive data can be omitted; data minimisation applied out of the box. — [Glassbox Data Privacy & Security](https://glassbox.com/platform/data-privacy-security)
- **[Fetched]** "Automatically mask sensitive data such as PII, PCI, PHI, and NPI to ensure security and meet regulatory standards." — [AWS Marketplace listing](https://aws.amazon.com/marketplace/pp/prodview-waapo3vlxej2o)
- **[Fetched]** Customer reviewer (Jul 28 2026): "we mask all the fields such as account number, credit card number, social security, and date of birth"; "Glassbox does a very good job in masking that and not capturing that." — [AWS Marketplace reviews](https://aws.amazon.com/marketplace/pp/prodview-waapo3vlxej2o)
- **[Fetched]** Granular role-based access controls with a complete audit log; SSO with MFA via SAML 2.0 or the customer's identity provider. — [Glassbox Data Privacy & Security](https://glassbox.com/platform/data-privacy-security)
- **[Fetched]** Encryption in transit between end-user devices and Glassbox servers; passwords encrypted and excluded from logs. — [Glassbox Data Privacy & Security](https://glassbox.com/platform/data-privacy-security)
- **[Fetched]** Certifications/attestations: ISO 27001, ISO 27017, ISO 27701, ISO 42001, SOC 2 Type II; stated compliance with HIPAA, GLBA, PCI, CCPA, GDPR; biannual audits and regular penetration tests. — [Glassbox Data Privacy & Security](https://glassbox.com/platform/data-privacy-security)
- **[Fetched]** GIA: Azure OpenAI Service used so customer data is not used to train future models. — [Glassbox Insights Assistant](https://www.glassbox.com/platform/glassbox-insights-assistant/)
- **[Fetched]** MCP Server is described as "governed access". — [Glassbox press release](https://www.glassbox.com/news/glassbox-pulse-mcp-server-operational-intelligence/)
- **[Fetched]** Retention ceilings by plan (replay 1–3 months, analysis 1–13 months). — [Glassbox Plans & Pricing](https://www.glassbox.com/platform/plans-pricing/)

### Inferences
- Masking/omission happens at capture (reviewer: "not capturing that"), so masked values are generally unrecoverable downstream; exported/API data should be expected to contain masked placeholders in form fields and free text. Joins to customer identity depend on whichever non-sensitive identifiers the customer chose to capture.
- Regulated customers (banks, insurers) are likely to treat any third-party read access — API key, MCP connection, or warehouse copy — as a formal data-sharing event requiring security review; on-prem customers may refuse outbound access entirely. Inference from customer profile.
- RBAC + audit log implies connector credentials should map to a dedicated, least-privilege service role; whether Glassbox supports service accounts or API tokens distinct from SAML users is unknown.

### Gaps
- Field-level encryption (as opposed to masking/omission): not mentioned on pages read.
- End-user consent mechanics (CMP integration, opt-out honouring) and their effect on stored/exported data: not found.
- Data residency / region pinning: not stated on pages read.
- Whether masking rules apply identically to API/export/MCP outputs as to the UI, and whether unmasking privileges exist for some roles: not found.
- Encryption at rest specifics and key management (BYOK): not found.

---

## Cross-cutting summary of what could NOT be found (for the report writer)

1. Any named bulk export / data feed product and its destinations (warehouses, object stores, message buses, webhooks).
2. Any export schema, file format, partitioning, cadence, or latency specification.
3. A public API reference or developer portal (only the claims "open API" and "Unlimited API access").
4. Storage/query engine and broader tech stack.
5. Hosting regions and data-residency options.
6. MCP Server technical details (tools, auth, availability, pricing, deployment support).
7. Native connectors for BI tools (Tableau, Power BI, Looker), warehouses, CDPs (Segment, mParticle), Datadog, Salesforce — no evidence of existence.
8. Mechanism/direction for most listed integrations.

Research limits: ~21 search/fetch calls; several vendor pages are JavaScript-rendered or gated; marketplace-native searches (Snowflake Marketplace, Databricks Partner Connect, Splunkbase, Dynatrace Hub, Google Cloud Marketplace) were not individually queried, so absence there is "not found via web search", not "confirmed absent". Review sites G2 / Gartner Peer Insights / TrustRadius were not opened.
