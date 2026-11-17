# Glassbox product surface and data model (console objects, metrics, filters, exports)

Research date: 2026-11-17. Scope: Glassbox (glassbox.com, formerly Clarisite) digital experience analytics. Builds on `research_notes/Glassbox analytics integration research/glassbox_apis_and_auth.md`; facts already recorded there are not repeated unless extended.

Evidence labels used on every finding:
- **[FETCHED]** = the page was retrieved and read through the fetch tool. The tool returns a model-generated extraction of the page, so quoted strings are as reported by that extraction; treat them as near-verbatim, not character-verified.
- **[SNIPPET]** = seen only in a search-result summary; not verified against the page.
- Dates are the page's own publication date where shown; "page as of Nov 2026" means an undated live marketing page fetched on 2026-11-17 (footer "© 2026 Glassbox").

Budget: 43 search/fetch calls. No vendor console documentation, release notes, academy syllabus or field-level schema is public; everything below is marketing pages, vendor blogs, third-party integration docs and customer reviews.

## 1. Product modules and their objects, metrics and dimensions (as of Nov 2026)

### Takeaway
The public module list is twelve named modules plus six platform capabilities; named metrics are sparse but include a 0-1 Struggle Score, a customer-configurable Experience Score, DAU/WAU/MAU and their ratios, conversion/adoption/abandonment rates, per-element click rate/focus time/errors/struggles/revenue impact, and revenue-impact figures attached to journeys and funnels. "Product analytics" is delivered as User Insights and User Timeline (documented Sept 2024, sold as the CX Engage add-on); no public source shows classic retention-curve or cohort reports.

### Cited Findings

**Module inventory**
- Site navigation lists these modules (blog page dated 2024-09-06, navigation as of Nov 2026): Customer Journey Analytics ("Discover the Augmented Journey Map"), Mobile App Analytics, Session Replay, Performance Analytics, Product Analytics, Interaction Maps & Heatmaps, Glassbox Accessibility (tagged New), Rapid Response (tagged New), Glassbox Insights Assistant, Funnel Analysis, Struggle & Error Analysis, Voice of the Silent. Other platform capabilities listed: Digital Record Keeping, Tagless Data Capture, AI Inside, Integrations, Data Privacy & Security, Plans & Pricing. [FETCHED] — [Glassbox blog/nav](https://www.glassbox.com/blog/feature-matrix/)
- Named packages: "CX Engage" (add-on containing User Insights and User Timeline; whitepaper dated 2024-09-04) and "CX Listen" (contains Voice of the Silent and the native VoC tool; page as of Nov 2026). [FETCHED] — [Unified digital analytics](https://glassbox.com/blog/unified-digital-analytics), [Voice of the Silent](https://glassbox.com/platform/voice-of-the-silent)
- The product analytics page also calls the combined offering the "Product Intelligence module". [FETCHED] — [Product analytics](https://glassbox.com/platform/product-analytics/)

**Struggle Score, Experience Score, struggle and error analysis**
- Struggle Score is "proprietary"; it "precisely measures how usability issues and technical errors impact customer experiences". No formula, weights or thresholds are published. [FETCHED] — [Struggle & error analysis](https://glassbox.com/platform/struggle-error-analysis)
- Scale: reported as 0 (no struggle) to 1 (high struggle). [SNIPPET] — [Glassbox struggle analysis blog](https://www.glassbox.com/blog/struggle-analysis/)
- Signals: "more than 30" frustration behaviours, with rage clicks, slow load times and Ajax errors named on the product page; the Sept 2024 blog adds complex navigation, confusing forms, repeated dead clicks and long focus times. Zoom and form re-entry are not named in any source read. [FETCHED] — [Struggle & error analysis](https://glassbox.com/platform/struggle-error-analysis), [Struggle and experience scores, 2024-09-20](https://glassbox.com/blog/struggle-and-experience-scores)
- Method statement (2024-09-20): a "Struggle Score Engine" "analyzes the correlation between the dozens of touchpoints comprising each user's unique journey". The score is usable as a filter "across all Glassbox modules". [FETCHED] — [Struggle and experience scores](https://glassbox.com/blog/struggle-and-experience-scores)
- Experience Score (2024-09-20): a customer-configured score assigned to behaviours/events per session, marking journey elements positive or negative; the one example is that "clicking on 'request support' could be scored to indicate an overall customer experience issue". Usable as a filter. The struggle page also mentions "custom experience scoring models for business journeys". [FETCHED] — [Struggle and experience scores](https://glassbox.com/blog/struggle-and-experience-scores), [Struggle & error analysis](https://glassbox.com/platform/struggle-error-analysis)
- Error reporting: dashboards for form validation errors, console errors and display errors "with advanced filtering"; server-side technical events are captured and correlated with behaviour. [FETCHED] — [Struggle & error analysis](https://glassbox.com/platform/struggle-error-analysis)
- A reviewer (AWS Marketplace page) describes the struggle score as aggregating behavioural and technical triggers such as rage clicks and Ajax failures (already in prior notes). — [AWS Marketplace](https://aws.amazon.com/marketplace/pp/B09ZVL2TQB)

**Customer Journey Analytics / Augmented Journey Map (page as of Nov 2026)**
- Objects: journeys, steps/nodes (pages or screens), audiences/segments, dimensions. "Filter journeys based on over 25 standard dimensions or create your own." Node analysis: "See how journeys flow from, through or to any page or screen." Cross-channel: web, mobile app, and journeys spanning both. [FETCHED] — [Customer journey analytics](https://glassbox.com/platform/customer-journey-analytics)
- Metrics shown on the map: conversions, drop-offs, struggles, errors, acquisition, conversion rate, and "integrated revenue metrics that can be customized to your business model" ("quantify captured or lost revenue across the journey"; "how much value is attributed to each journey"). [FETCHED] — [Customer journey analytics](https://glassbox.com/platform/customer-journey-analytics)
- "Path Analysis" is a named capability. Journeys can be viewed by audience segments, VoC feedback or A/B tests through integrations. Drill-down from aggregate view to session replay. [FETCHED] — [Customer journey analytics](https://glassbox.com/platform/customer-journey-analytics)

**Funnel Analysis (page as of Nov 2026)**
- "Funnel Studio" is a drag-and-drop builder. Funnel types: ad-hoc funnels ("without complex queries"), retroactive funnels over already-captured data, and monitored "multivariable business flows that can be tracked going forward". [FETCHED] — [Funnel analysis](https://glassbox.com/platform/funnel-analysis)
- Steps can be "technical events or user interactions"; funnels include "server-side data, such as AJAX, display errors and performance data". [FETCHED] — [Funnel analysis](https://glassbox.com/platform/funnel-analysis)
- Metrics: conversion, abandonment, revenue impact, affected-user counts. Breakdown dimensions named: "OS, device, location and more". GIA produces summaries "of any ad-hoc report". [FETCHED] — [Funnel analysis](https://glassbox.com/platform/funnel-analysis)

**Interaction Maps & Heatmaps (blog, date not captured)**
- Per-element metrics: "click rates, focus time, errors, struggles and revenue impact". Features: Zone Analysis (custom element groups), Exposure Tracking (scroll depth; share of users who viewed vs. clicked an element), Behavior Comparison (time periods, audience segments, A/B variants), a Chrome Extension overlaying metrics on the live site, and "Data Export" (see section 3). [FETCHED] — [Interaction Maps blog](https://www.glassbox.com/blog/glassbox-interaction-maps/)

**Performance Analytics (page as of Nov 2026)**
- "See Core Web Vitals scores in real time (rather than Google's 30-day look back)." Individual vitals (LCP, INP, CLS, etc.) are not named on the page. Positioned as Real User Monitoring; anomaly alerts; "business impact metrics to prioritize efforts based on ROI"; "Glassbox can use data from existing APM tools". [FETCHED] — [Performance analytics](https://glassbox.com/platform/performance-analytics)

**Mobile App Analytics (page as of Nov 2026)**
- Features: interaction maps, ad-hoc funnels, journey map, crash analytics ("Replicate crashes and understand what caused them"), performance analytics ("how software, network, OS and device impact user experience"), version insights ("version adoption trends, response times and errors"), session replay, GIA summaries "from any mobile report". [FETCHED] — [Mobile app analytics](https://glassbox.com/platform/mobile-app-analytics)
- Dimensions implied: screen, element, app version, OS, device, network. Platforms: Android, iOS, React Native, Flutter, Xamarin, Cordova, Ionic, Capacitor, Jetpack Compose. ANRs are not mentioned. [FETCHED] — [Mobile app analytics](https://glassbox.com/platform/mobile-app-analytics)

**Voice of Customer: CX Listen / Voice of the Silent (page as of Nov 2026)**
- CX Listen includes a native VoC tool for building surveys (feedback types: free text, NPS, CSAT; custom branding; experience-triggered prompts) and pre-built integrations with major VoC vendors. [FETCHED] — [Voice of the Silent](https://glassbox.com/platform/voice-of-the-silent)
- Voice of the Silent takes sessions with feedback and finds sessions with "nearly identical experiences that didn't leave feedback". VoC data links to sessions, struggles, impact and revenue; GIA interprets feedback alongside session data. [FETCHED] — [Voice of the Silent](https://glassbox.com/platform/voice-of-the-silent)

**Product analytics: User Insights and User Timeline**
- User Insights (whitepaper 2024-09-04; blog undated): "cross-session analysis of pages, actions and users, segmented according to your specifications". Five feature areas: Segments Analysis; Track Adoption and Usage (pages/actions over time, per segment); Track Conversions (goals, key pages, actions, per segment); Compare Page and Action Performance (including page versions/products); Engagement Analysis (trend reports). [FETCHED] — [User Insights blog](https://glassbox.com/blog/user-insights), [Unified digital analytics](https://glassbox.com/blog/unified-digital-analytics)
- Named metrics: DAU, WAU, MAU, DAU/WAU and WAU/MAU ratios, conversion rates, adoption rates. Objects: segments, pages, actions, goals, users. [FETCHED] — [User Insights blog](https://glassbox.com/blog/user-insights)
- User Timeline: cross-session view "combining all sessions into a single view"; tracks engagement, revenue and struggle scores across sessions and devices (e.g. web to mobile). [FETCHED] — [Unified digital analytics](https://glassbox.com/blog/unified-digital-analytics)
- Retention of this data: "Data is automatically retained for 13 months". [FETCHED] — [Product analytics](https://glassbox.com/platform/product-analytics/)
- Segmentation variables named: "ranging from geography to VoC sentiment". [FETCHED] — [Product analytics](https://glassbox.com/platform/product-analytics/)
- Cohorts and retention curves are not described on any page read; "cohort" appears only in an image filename on the product analytics page. [FETCHED] — [Product analytics](https://glassbox.com/platform/product-analytics/)

**Dashboards, alerts, anomaly detection**
- Alerts: threshold/"custom triggers for real-time notifications" plus AI anomaly detection that "learns the typical patterns and parameters of your site or app". [FETCHED] — [Struggle & error analysis](https://glassbox.com/platform/struggle-error-analysis), [Tagless data capture](https://glassbox.com/platform/tagless-data-capture)
- Dashboards consist of widgets including tables; a Wells Fargo reviewer (2026-07-28) reports limits on widget count, days of data and table rows (see section 6). [FETCHED] — [AWS Marketplace reviews](https://aws.amazon.com/marketplace/reviews/reviews-list/B09ZVL2TQB?page=18)

**GIA (Glassbox Insights Assistant) and newer AI surfaces**
- GIA (page as of Nov 2026): chat interface with suggested prompts; session search for web and mobile; session summaries with follow-up questions; summaries of "any ad-hoc or mobile report"; friction sources ranked by business impact; built on Microsoft Azure OpenAI Service; data not used for model training. Searchable parameters and example prompts are not listed. [FETCHED] — [GIA page](https://glassbox.com/platform/glassbox-insights-assistant)
- Rapid Response (tagged New; no launch date on page): real-time "Page Content Triggers" and "Struggle-Based Triggers"; actions are redirect, show promotion, show tutorial, trigger live chat; human hand-off passes a "GIA Profile" (text summary of the error, narrative of the user's goal, link to the session replay). [FETCHED] — [Rapid Response](https://glassbox.com/platform/rapid-response)
- Glassbox Pulse and the MCP Server (2026-07-29) are covered in the prior notes. — [Glassbox news](https://www.glassbox.com/news/glassbox-pulse-mcp-server-operational-intelligence/)

### Inferences
- The analytical grain is the session; user-level analysis (User Insights/User Timeline) is a later, separately packaged layer. A connector should expect session-, page/screen-, element-, event- and (if CX Engage is licensed) user-level aggregates.
- Revenue figures are customer-configured ("customized to your business model"), so any "revenue impact" number depends on tenant setup and is not comparable across tenants.
- Struggle Score and Experience Score are the two session-level scores most likely to be exposed as filterable attributes on any export.
- Because retention/cohort reports are absent from public material, "Product Analytics" at Glassbox should be read as engagement, adoption and conversion by segment rather than a Mixpanel/Amplitude-equivalent.

### Gaps
- Struggle Score formula, weights, thresholds and the full list of 30+ signals; the 0-1 scale is snippet-only.
- The list of the "over 25 standard dimensions" for journeys.
- Individual Core Web Vitals and performance metric names; mobile ANR support.
- Launch dates of User Insights, CX Engage, CX Listen, Rapid Response and Glassbox Accessibility (pages are undated; the earliest dated reference to User Insights found is 2024-09-04).
- Dashboard widget types and the dashboards object model; alert definition fields.
- No Forrester/Gartner/IDC report content was reviewed.

## 2. Session search: searchable attributes and query syntax

### Takeaway
Publicly named search keys are user ID, session ID (with a time window), correlation ID, browser, Struggle Score, customer-defined attributes, segments and free text over any value that appeared in the session; GIA adds natural-language search. No query syntax is documented, and reviewers report that user-ID, partial-match and free-text searches behave inconsistently.

### Cited Findings
- Vendor (page as of Nov 2026): "Pinpoint a session by filtering by user ID, browser or Struggle Score" and "use free text search for any value that appears in the session thanks to tagless data capture". [FETCHED] — [Session replay](https://glassbox.com/platform/session-replay)
- GIA: "effortlessly search for sessions based on any parameters"; "find any session with a specific struggle". [FETCHED] — [Session replay](https://glassbox.com/platform/session-replay), [Struggle & error analysis](https://glassbox.com/platform/struggle-error-analysis)
- Other replay features: struggle flags to jump to critical points, starred sessions, navigation from a replay into interaction maps and ad-hoc funnels, replays that "include the technical and server-side events that influenced each journey". [FETCHED] — [Session replay](https://glassbox.com/platform/session-replay)
- Session ID search with a time window (reviewer, 2026-07-29): "entered that session ID into Glassbox along with the proper timing to minimize search duration". [FETCHED] — [AWS Marketplace reviews](https://aws.amazon.com/marketplace/reviews/reviews-list/B09ZVL2TQB?page=18)
- API request/response content is visible inside a session (reviewer, 2026-08-04): "check API request responses for potential problems"; "the API details, code, and request response information are valuable". [FETCHED] — [AWS Marketplace reviews](https://aws.amazon.com/marketplace/reviews/reviews-list/B09ZVL2TQB?page=32)
- Custom attributes (reviewer, June 2023): "I am able to search sessions using created attributes but I cannot actually filter by those attributes". Another (June 2023): the replay shows "every event and API call, along with customer events and attributes". [FETCHED] — [Software Finder reviews](https://softwarefinder.com/analytics-software/glassbox/reviews?page=2)
- Reviewer complaints: user-ID search "didn't work" and partial searches are not supported; free-style querying "shows unrelated sessions"; an "active sessions" filter is selected by default and can mislead. [SNIPPET] — [AWS Marketplace reviews](https://aws.amazon.com/marketplace/reviews/reviews-list/B09ZVL2TQB?page=31), [G2](https://g2.com/products/glassbox/reviews?page=2)
- Digital Record Keeping describes sessions as "time-stamped, tamper-proof, and indexed for searching" with automatic categorisation and a "search-engine-like experience". [FETCHED] — [Digital record keeping](https://glassbox.com/platform/digital-record-keeping)
- An in-session "expert view" showing events is mentioned by a Splunk Community user (prior notes; snippet only). — [Splunk Community](https://community.splunk.com/t5/Monitoring-Splunk/integration-of-splunk-with-glassbox/m-p/660054)

### Inferences
- Free-text search over rendered and transmitted values implies a full-text index over captured page content and network payloads, subject to masking; masked or omitted fields will not be searchable.
- The default "active sessions" filter suggests the session list distinguishes live from completed sessions; an API equivalent may need an explicit status parameter.

### Gaps
- No documented search or query syntax (operators, wildcards, field names) was found.
- No public confirmation of IP address, geo, device model or app version as session-search fields (geo, OS and device are confirmed only as funnel/segment dimensions).
- Whether free-text search covers request/response bodies or only on-screen text is not stated.

## 3. Export and sharing features visible in the UI

### Takeaway
Export exists but is thinly documented: a "Data Export" capability (video title and one sentence on Interaction Maps), per-session sharing from the replay, session links embedded in third-party tools, and alert notifications. No public source confirms CSV/Excel/PDF buttons, scheduled email reports, link expiry rules or export destinations; reviewers ask for more export flexibility.

### Cited Findings
- Interaction Maps "Data Export": bring selected elements into flows and funnels, "or export data for further analysis". No format given. [FETCHED] — [Interaction Maps blog](https://www.glassbox.com/blog/glassbox-interaction-maps/)
- A Glassbox video titled "Transform Your Data: Reduce Issues, Fraud, and Revenue Loss With Glassbox Data Export" is linked from the Digital Record Keeping page; the YouTube page returned only the title (no description, date or transcript). [FETCHED, title only] — [YouTube](https://www.youtube.com/watch?v=Ofl9gy1WUP4), [Digital record keeping](https://glassbox.com/platform/digital-record-keeping)
- Session sharing: "Share any session with colleagues directly from the replay." [FETCHED] — [Session replay](https://glassbox.com/platform/session-replay)
- GIA is pitched as removing the need to export: "no need to analyze or export the report", which implies report export exists. [FETCHED] — [Funnel analysis](https://glassbox.com/platform/funnel-analysis), [GIA page](https://glassbox.com/platform/glassbox-insights-assistant)
- New Relic quickstart: the dashboard queries New Relic `JavaScriptError` events carrying two Glassbox-supplied attributes, `GlassboxSessionId` and `GlassboxSessionReplay` (a replay link), alongside `errorClass`, `errorMessage`, `appName`, `deviceType`, `userAgentName`, `userAgentOS`, `userAgentVersion`. Setup is in a gated Glassbox support article "How to Connect New Relic". [FETCHED] — [newrelic-quickstarts dashboard.json](https://raw.githubusercontent.com/newrelic/newrelic-quickstarts/main/dashboards/glassbox/dashboard.json), [New Relic quickstart page](https://newrelic.com/instant-observability/glassbox)
- Blue Triangle: integration needs a Custom Variable plus a "Partner Domain" set to "the Glassbox link for that customer"; a Glassbox link then appears per session in RUM Performance Detail. The actual link format is shown only in screenshots. [FETCHED] — [Blue Triangle help](https://help.bluetriangle.com/help-center/blue-triangle-and-glassbox)
- Medallia: integration uses a customer-supplied URL pattern; an example reportedly points at a clarisite.com host under a `webinterface` path with session/replay parameters. [SNIPPET; page blocked by browser check] — [Medallia docs](https://docs.medallia.com/en/medallia-digital-feedback/medallia-digital-web/analytics-solution/session-recording-software/glassbox)
- Alert/notification and ticketing targets: Slack and Jira are named integrations (prior notes); a reviewer (July 2023) values alerting "whenever there's a spike"; Rapid Response hands a session link to support agents. [FETCHED] — [Glassbox integrations](https://glassbox.com/platform/integrations), [Software Finder reviews](https://softwarefinder.com/analytics-software/glassbox/reviews?page=2), [Rapid Response](https://glassbox.com/platform/rapid-response)
- Compliance evidence: Session Vault lets teams retrieve and replay a customer's session to "provide proof and resolve the issue definitively"; no PDF/download format is described. [FETCHED] — [Digital record keeping](https://glassbox.com/platform/digital-record-keeping)
- Reviewer requests: "more flexibility in exporting the information in the exact format I am looking for" (July 2023) [FETCHED]; wanting to share sessions outside the tool and "better data export" [SNIPPET]. — [Software Finder reviews](https://softwarefinder.com/analytics-software/glassbox/reviews?page=2), [AWS Marketplace reviews](https://aws.amazon.com/marketplace/reviews/reviews-list/B09ZVL2TQB?page=32)

### Inferences
- The New Relic pattern shows the usual integration direction: Glassbox's client-side script injects its session ID and replay URL into other tools, rather than other tools reading from Glassbox. The session ID is therefore the practical join key for an AI analyst combining Glassbox with APM/BI data.
- Session links probably require a Glassbox login and tenant access, since the vendor frames sharing as "with colleagues" and reviewers ask to share outside the tool.

### Gaps
- CSV/Excel/PDF export of reports and session lists; scheduled email reports; session download; screenshots: not confirmed by any source read.
- Session link URL format, expiry and permission model.
- Data Export destinations, formats, cadence, fields and licensing; "Data Streamer"/"Export API" as product names were not found.
- Microsoft Teams, ServiceNow and generic webhook targets for alerts: not found.

## 4. Events, tagless capture, user identification and session stitching

### Takeaway
"Tagless capture" means everything (user interactions plus technical/server-side events, averaging more than 1,000 events per session) is recorded without per-element tags, and analysis objects are defined afterwards and applied retroactively. The public record names "attributes", "actions", "goals", "customer events", page classification and business flows as configurable objects, but not how they are authored.

### Cited Findings
- Tagless capture "automatically captures 100% of digital interactions", "every digital event for every session, averaging more than 1,000 events per session", covering user behaviours and technical events on web and mobile; no tagging of elements, events or conversion points. [FETCHED] — [Tagless data capture](https://glassbox.com/platform/tagless-data-capture)
- "Glassbox automatically captures 100% of front- and back-end events across web and mobile apps." "Every click, tap, zoom and swipe is captured out-of-the box." [FETCHED] — [Product analytics](https://glassbox.com/platform/product-analytics/), [Funnel analysis](https://glassbox.com/platform/funnel-analysis)
- Retroactivity: ad-hoc funnels measure impact "even for issues you weren't aware of"; a reviewer (May 2023) praises "the ability for newly added attributes or actions to work retroactively". [FETCHED] — [Tagless data capture](https://glassbox.com/platform/tagless-data-capture), [Software Finder reviews](https://softwarefinder.com/analytics-software/glassbox/reviews?page=2)
- Configured objects named in public sources: attributes and actions (reviewers), "customer events and attributes" (reviewer), goals / significant pages / significant actions (User Insights), business flows (Funnel Studio), custom zones (Interaction Maps), custom dimensions (journeys), custom segments, experience scoring models. [FETCHED] — [Software Finder reviews](https://softwarefinder.com/analytics-software/glassbox/reviews?page=2), [User Insights blog](https://glassbox.com/blog/user-insights), [Funnel analysis](https://glassbox.com/platform/funnel-analysis), [Interaction Maps blog](https://www.glassbox.com/blog/glassbox-interaction-maps/), [Customer journey analytics](https://glassbox.com/platform/customer-journey-analytics)
- Page classification can be a manual, vendor-assisted process: a Nespresso reviewer (2026-08-03) says "Every time we have a new page, we have to submit the Excel to them" and "they have to cluster this manually". [FETCHED] — [AWS Marketplace reviews](https://aws.amazon.com/marketplace/reviews/reviews-list/B09ZVL2TQB?page=32)
- External segments: the same reviewer says markets "need to create segments from Google Analytics to Glassbox" and "we could not link Google Analytics properly"; another (2026-07-21) recommends "using the Adobe API if it is available" to bring Adobe segments in. [FETCHED] — [AWS Marketplace reviews](https://aws.amazon.com/marketplace/reviews/reviews-list/B09ZVL2TQB?page=18)
- Alerts fire on event thresholds (reviewer, 2026-08-04): "as soon as there is an event where a particular issue is occurring and it crosses the threshold". [FETCHED] — [AWS Marketplace reviews](https://aws.amazon.com/marketplace/reviews/reviews-list/B09ZVL2TQB?page=18)
- Cross-session and cross-device: User Timeline combines "all sessions into a single view" and shows movement between devices (web to mobile). How identities are matched is not explained. [FETCHED] — [Unified digital analytics](https://glassbox.com/blog/unified-digital-analytics)
- A third-party guide (Webeyez, a competitor's SEO page) discusses "Glassbox events" in generic terms and cites no Glassbox documentation; low reliability. [SNIPPET] — [Webeyez](https://webeyez.com/insights/guides/glassbox-events-guide)

### Inferences
- Events appear to be defined as rules over already-captured data (page content, interactions, network traffic) rather than as instrumentation calls, which is why new attributes/actions can apply retroactively. This is inferred from the retroactivity claims; the rule mechanism (CSS selector, URL pattern, request/response matcher) is not public.
- User stitching most plausibly relies on a customer-mapped user ID attribute, given "user ID" is a first-class search filter; unconfirmed.

### Gaps
- How events/attributes/actions are authored (no-code designer, selectors, request/response rules), and what metadata they carry.
- Event taxonomy and names of auto-captured event types.
- User-identification configuration and cross-device stitching logic.
- Web JS and mobile SDK custom-event API names.

## 5. Administration relevant to integrations

### Takeaway
Publicly confirmed: granular role-based access control, a complete audit log, SSO with MFA via SAML 2.0, default-on configurable masking/omission, category-based retention in Session Vault, and three deployment models (single-tenant cloud on AWS or Azure, multi-tenant cloud, on-premises). API users, service accounts and IP restrictions are not mentioned anywhere public.

### Cited Findings
- "Granular role-based access controls with a complete audit log." "SSO with multi-factor authentication via SAML 2.0 or your preferred identity provider." [FETCHED] — [Data privacy & security](https://glassbox.com/platform/data-privacy-security)
- Masking: sensitive data masked by default; "Omit what's highly sensitive and mask additional data based on pre-set rules or business needs." [FETCHED] — [Data privacy & security](https://glassbox.com/platform/data-privacy-security)
- Deployment: single-tenant cloud on AWS or Microsoft Azure; multi-tenant cloud; on-premises. Certifications: SOC 2 Type II, ISO 27001, 27017, 27701, 42001; stated compliance with HIPAA, GLBA, PCI, CCPA, GDPR. [FETCHED] — [Data privacy & security](https://glassbox.com/platform/data-privacy-security)
- Retention: Session Vault lets customers choose which sessions to keep by attributes/categories (e.g. onboarding, transactions, complaints) and set retention per category "based on policies or regulations", with "expandable storage" and per-organisation access control. [FETCHED] — [Digital record keeping](https://glassbox.com/platform/digital-record-keeping)
- Retention figures seen: 13 months for User Insights/product analytics data (vendor); "session data is only kept in storage for thirty days" (reviewer, July 2023); "the current limit is not enough" (reviewer, Aug 2023). [FETCHED] — [Product analytics](https://glassbox.com/platform/product-analytics/), [Software Finder reviews](https://softwarefinder.com/analytics-software/glassbox/reviews?page=2)
- Multi-application structure: a reviewer (2026-04-20) refers to tracking "different journeys, different dashboards, and different applications"; the New Relic dashboard facets by `appName` (a New Relic attribute, not necessarily a Glassbox one). [FETCHED] — [AWS Marketplace reviews](https://aws.amazon.com/marketplace/reviews/reviews-list/B09ZVL2TQB?page=32), [newrelic-quickstarts dashboard.json](https://raw.githubusercontent.com/newrelic/newrelic-quickstarts/main/dashboards/glassbox/dashboard.json)
- Seats are licensed (10/15 seats on AWS tiers; prior notes). — [AWS Marketplace](https://aws.amazon.com/marketplace/pp/B09ZVL2TQB)

### Inferences
- Retention is tiered: short default replay retention (about 30 days in one 2023 report), longer aggregate retention (13 months), and policy-driven long-term retention for vaulted sessions. A connector must not assume raw sessions are available for the same window as aggregates.
- With on-premises and single-tenant options, base URL, version and available features will vary per customer.

### Gaps
- Role names and permission granularity; whether API/service accounts exist; IP allow-listing; audit-log contents and exportability.
- The tenant hierarchy terms (applications, domains, projects) used in the console.
- Current default retention per plan.

## 6. What reviewers say about reporting, export, API and limits

### Takeaway
Reviewers consistently praise replay and visibility of API calls but flag reporting as the weak area: dashboard caps (widgets, days, table rows), a past 8-day report date range, short session retention, limited export flexibility, slow-loading sessions/reports and a steep learning curve for building custom reports. Customers do integrate Glassbox with Splunk, Grafana, AppDynamics and Dynatrace.

### Cited Findings
All [FETCHED] unless marked.
- Wells Fargo reviewer (PeerSpot via AWS, 2026-07-28): "some limitations with dashboards, such as how many widgets can be added, how many days of data can be retrieved" and "how many rows in a table can be inserted"; "could definitely combine features such as funnels and reports"; "we have integrated it with other platforms such as Splunk, Grafana, and AppDynamics"; "Glassbox has a lot of good training materials". — [AWS Marketplace reviews](https://aws.amazon.com/marketplace/reviews/reviews-list/B09ZVL2TQB?page=18)
- Marriott-related reviewer (2026-07-29): connects "with tools like Dynatrace and other platforms through pre-built connectors and open APIs"; "loading sessions can take too long"; wants "reducing lag during complex session replays". — [AWS Marketplace reviews](https://aws.amazon.com/marketplace/reviews/reviews-list/B09ZVL2TQB?page=18)
- Nespresso reviewer (2026-08-03): "reports are not loading and there are login errors"; "You have to filter or do some strange checks and cross-checks of dimensions"; "creating reports apart from the session recordings is necessary". — [AWS Marketplace reviews](https://aws.amazon.com/marketplace/reviews/reviews-list/B09ZVL2TQB?page=32)
- 2026-07-21 reviewer: custom reports are hard for non-technical staff ("complicates training for non-technical staff to build custom reports"). 2026-06-08 reviewer: "Additional customization options in reporting and dashboards would also be beneficial." — [AWS Marketplace reviews](https://aws.amazon.com/marketplace/reviews/reviews-list/B09ZVL2TQB?page=18)
- 2026-04-20 reviewer: a Google Analytics/Mixpanel-style dashboard "is quite expensive and complex"; "steep learning curve that requires training" but "many available courses and content". — [AWS Marketplace reviews](https://aws.amazon.com/marketplace/reviews/reviews-list/B09ZVL2TQB?page=18)
- May 2023: wants "the reporting expanded beyond the current 8 consecutive-day date range". July 2023: "more flexibility in exporting the information in the exact format I am looking for"; "session data is only kept in storage for thirty days"; "the reports showing missing sessions"; reports "did take a while" to set up. — [Software Finder reviews](https://softwarefinder.com/analytics-software/glassbox/reviews?page=2)
- G2 (809 reviews, 4.9/5): reviewers cite lack of customizable reporting, want more customizable dashboards and longer session retention; one values that "unlimited API calls are captured". [SNIPPET; G2 returned HTTP 403] — [G2](https://g2.com/products/glassbox/reviews?page=2)
- TrustRadius: a reviewer describes exporting "all interaction data in real-time" and wants filtering to manage export volume (prior notes). [SNIPPET] — [TrustRadius](https://www.trustradius.com/products/glassbox/reviews)

### Inferences
- The 2023 limits (8-day report range, 30-day retention) may have been relaxed since; the 2026 Wells Fargo comment shows day/row/widget caps still exist in some form, so the vendor should be asked for current numbers.
- Report slowness suggests aggregate queries are computed on demand; an AI analyst issuing many ad-hoc queries may hit latency or caps.

### Gaps
- No reviewer states numeric caps (rows, widgets, days) for 2026.
- No reviewer describes using a Glassbox read API, CSV export or scheduled report in concrete terms.
- Gartner Peer Insights, Capterra and TrustRadius full review text were not read.

## 7. Training, enablement and customer evidence of BI/data-lake use

### Takeaway
No public Glassbox Academy/University course list, certification syllabus or conference deck was found; training is referenced only by reviewers and through self-guided product tours. No case study describing a BI or data-lake pipeline from Glassbox was found.

### Cited Findings
- Self-guided tours exist for the platform, e.g. session replay. [SNIPPET] — [Glassbox session replay tour](https://www.glassbox.com/platform-tour/session-replay-tour)
- Reviewers mention live online training, webinars, documentation and videos; some ask for more certification classes and courses. [SNIPPET] — [Capterra](https://www.capterra.com/p/157868/Glassbox), [AWS Marketplace reviews](https://aws.amazon.com/marketplace/reviews/reviews-list/B09ZVL2TQB?page=67)
- GIA resources listed on its page: datasheet, whitepaper, guided demo, capability video, self-guided tour. A "User Insights Datasheet" image is referenced on the User Insights blog. [FETCHED] — [GIA page](https://glassbox.com/platform/glassbox-insights-assistant), [User Insights blog](https://glassbox.com/blog/user-insights)
- Case studies named on vendor pages (titles only, not read): Sainsbury's ("recovers lost revenue and enhances efficiency"), SoFi ("Prevents $9M in Potential Revenue Loss"), Marriott (activation and login optimisation), Calm (3X retention). [FETCHED, titles only] — [Struggle & error analysis](https://glassbox.com/platform/struggle-error-analysis), [Product analytics eBook page](https://glassbox.com/resource-center/product-analytics-upgrades-to-make-your-customers-stick)
- Customer-side tool integrations reported by reviewers: Splunk, Grafana, AppDynamics (Wells Fargo), Dynatrace (Marriott-related). [FETCHED] — [AWS Marketplace reviews](https://aws.amazon.com/marketplace/reviews/reviews-list/B09ZVL2TQB?page=18)
- Gated vendor documentation exists by title: support article "How to Connect New Relic". [FETCHED] — [New Relic quickstart page](https://newrelic.com/instant-observability/glassbox)

### Inferences
- Detailed enablement content sits behind the customer portal (support.glassbox.com); a licensed customer could supply course lists and admin guides that would answer most gaps in this file.

### Gaps
- Glassbox Academy/University catalogue, certification syllabi, webinar and YouTube titles/transcripts, user-conference decks: none retrieved (searches returned nothing; YouTube page gave title only).
- Case studies describing Glassbox data in BI tools or data lakes: none found.
- Customer job postings describing Glassbox reporting duties and LinkedIn posts from solution consultants: searches returned nothing usable.
- Pages that failed to load and may help if opened in a browser: Medallia Glassbox page (browser check), G2 reviews (HTTP 403), `glassbox.com/platform/interaction-maps` (HTTP 404; the correct slug was not found), the Data Export YouTube video description.
