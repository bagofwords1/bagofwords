# Glassbox MCP Server, Glassbox Pulse, and other Glassbox AI/agent-facing capabilities (state as of 17 Nov 2026)

Scope: Glassbox the digital experience analytics / session replay vendor (glassbox.com, formerly Clarisite). Research date 2026-11-17. Passive public sources only; no Glassbox MCP endpoint or tenant host was contacted or probed.

Confidence legend:
- **[Read]** = page opened and read (through an automated page summariser, so quotes are as relayed and may be truncated).
- **[Snippet]** = seen only in a search-result summary; treat as a lead, not a fact.
- **[Directory]** = returned by the Claude connector-directory search tool (partial coverage of the directory, by the tool's own statement).

**Headline for the report writer:** Three and a half months after the 29 Jul 2026 announcement, **no public technical information about the Glassbox MCP Server exists beyond the press release.** No endpoint URL, transport, auth scheme, tool list, client list, availability status, price, or deployment-model statement was found on glassbox.com, in any MCP registry/directory, or in any press coverage. glassbox.com's platform navigation, AI page, GIA page and pricing page do not mention Pulse or the MCP Server at all (read 17 Nov 2026). Every press article found is a rewrite of the release. A connector cannot be designed from public sources; it requires Glassbox-supplied documentation (customer portal at support.glassbox.com, or a partner contact). The competitor benchmark in the last section is the best available proxy for the likely shape.

---

## 1. What do Glassbox's own pages and the press release say about the MCP Server and Pulse?

### Takeaway
The only primary source is the 29 Jul 2026 press release (PR Newswire no. 302837278, mirrored on glassbox.com/news). It says Pulse is an ML-first "AI-powered operational intelligence engine" with natural-language Q&A tied to evidence, and that the MCP Server gives enterprise AI systems "governed access" to that intelligence "without ingesting a raw stream of digital journey data". It contains no availability wording, no named AI client or partner, no auth/permission/masking detail, no example question, no sample output, no pricing and no deployment statement.

### Cited Findings
- **[Read]** 29 Jul 2026, New York, PR Newswire: Glassbox announced "Glassbox Pulse, an AI-powered operational intelligence engine, and the Glassbox MCP Server", the latter giving enterprise AI systems "governed access to Glassbox digital experience intelligence". Sub-headline: Pulse turns "customer behavior, technical cause and business impact" into real-time operational intelligence; the MCP Server makes that "governed context" available to enterprise AI systems. — [PR Newswire](https://www.prnewswire.com/news-releases/glassbox-turns-digital-experience-into-real-time-digital-operational-intelligence-for-enterprise-ai-302837278.html); same text at [glassbox.com news](https://www.glassbox.com/news/glassbox-pulse-mcp-server-operational-intelligence/)
- **[Read]** How Pulse works, per the release: it "applies machine learning first" to identify meaningful changes, classify behavioural and technical signals, and "surface only the issues that require attention" — framed as reducing noise and limiting the irrelevant data AI systems must process. — [PR Newswire](https://www.prnewswire.com/news-releases/glassbox-turns-digital-experience-into-real-time-digital-operational-intelligence-for-enterprise-ai-302837278.html)
- **[Read]** What Pulse detects, per the release: stalled applications, failed payments, broken releases / release-related friction, unresolved chatbot interactions, hidden technical errors, avoidable service calls, conversion risk. "Teams can ask operational questions in natural language"; answers are tied to evidence "such as customer behavior, journey impact, errors, network logs, and server-side failures". — [PR Newswire](https://www.prnewswire.com/news-releases/glassbox-turns-digital-experience-into-real-time-digital-operational-intelligence-for-enterprise-ai-302837278.html)
- **[Read]** MCP Server paragraph, in full substance: through the Model Context Protocol, AI applications and agents can access relevant Glassbox context and evidence "without ingesting a raw stream of digital journey data". Roadmap sentence: Glassbox "will continue expanding Pulse and the capabilities available through the Glassbox MCP Server". — [PR Newswire](https://www.prnewswire.com/news-releases/glassbox-turns-digital-experience-into-real-time-digital-operational-intelligence-for-enterprise-ai-302837278.html)
- **[Read]** CEO Guy Perry quotes: "Enterprise AI has to move from answers to action"; "Glassbox gives organizations the trusted context to resolve issues faster"; customers need "an auditable dataset that binds customer behavior to its technical cause". — [PR Newswire](https://www.prnewswire.com/news-releases/glassbox-turns-digital-experience-into-real-time-digital-operational-intelligence-for-enterprise-ai-302837278.html)
- **[Read]** Explicitly absent from the release (checked): no "available now" / "generally available" / "early access" wording; no named AI client or partner; no authentication, permission or masking detail beyond the phrase "governed access"; no example questions; no sample MCP output; no deployment, pricing or packaging statement. The only links on the glassbox.com copy go to the home page, Trust Center, demo request and platform overview — no product page or docs link. — [PR Newswire](https://www.prnewswire.com/news-releases/glassbox-turns-digital-experience-into-real-time-digital-operational-intelligence-for-enterprise-ai-302837278.html); [glassbox.com news](https://www.glassbox.com/news/glassbox-pulse-mcp-server-operational-intelligence/)
- **[Read]** Boilerplate: seven of the ten largest US banks by assets are customers; ISO 42001 (AI management) and ISO/IEC 27701:2019 certifications; customers in 40+ countries. — [PR Newswire](https://www.prnewswire.com/news-releases/glassbox-turns-digital-experience-into-real-time-digital-operational-intelligence-for-enterprise-ai-302837278.html)
- **[Read, 17 Nov 2026]** glassbox.com platform page and site navigation list no Pulse or MCP Server product page. AI items in navigation are only "Glassbox Insights Assistant" and "AI Inside"; items marked "New" are "Glassbox Accessibility" and "Rapid Response". — [Glassbox platform](https://www.glassbox.com/platform/)
- **[Read, 17 Nov 2026]** The "AI Inside" page does not mention Pulse, MCP or agents. It describes GIA, anomaly detection ("Glassbox continuously learns the normal patterns for your website or app's performance and user behavior" and alerts on deviations), struggle identification tied to revenue impact, Voice of the Silent, automatic page grouping, and an "Autonomous CX" vision of turning "insight into action without human intervention" (presented as direction, not a shipped feature). No model/provider named. — [Glassbox AI Inside](https://www.glassbox.com/platform/artificial-intelligence/)
- **[Read, 17 Nov 2026]** The pricing page has no row for Pulse, MCP Server, GIA or anomaly detection. "Unlimited API access", "On-Prem Services", "Cloud Services" and "Hybrid Services" are checked for all three plans. — [Glassbox Plans & Pricing](https://www.glassbox.com/platform/plans-pricing/)
- **[Read, 17 Nov 2026]** Glassbox's events page lists a single event: CXFS Connect, 1–2 Dec 2026, JW Marriott Bonnet Creek, Orlando. No webinar or event about Pulse/MCP between July and November 2026 is listed; no past events were visible. — [Glassbox events](https://www.glassbox.com/events/)
- **[Read]** trust.glassbox.com returned only its heading to the fetcher (JavaScript-rendered); no content on AI, subprocessors or MCP could be read. — [Glassbox Trust Center](https://trust.glassbox.com/)
- **[Read]** 24 Sep 2026: Glassbox earned Microsoft's "Solutions Partner with certified software" designation for financial services AI (Microsoft AI Cloud Partner Program). The article repeats the Pulse/MCP description from the July release verbatim in substance and adds nothing technical; it says GIA is "expanding" and is powered by Azure OpenAI Service. Perry: "With the certified software designation, our alignment with Microsoft reaches a new collaborative level"; "Since launching on Azure Marketplace, we've partnered closely with Microsoft". Microsoft's Andrew Smith (GM, partner programs) gives a generic programme quote. Copilot and Copilot Studio are not mentioned. — [Channel Insider](https://www.channelinsider.com/channel-business/glassbox-earns-new-designation-in-microsoft-ai-cloud-partner-program/)

### Inferences
- "Without ingesting a raw stream" plus "surface only the issues that require attention" indicates the MCP Server is designed to return curated findings and supporting evidence (Pulse output), not raw event tables. Whether it also returns tabular aggregates (funnel conversion, struggle counts, revenue-impact numbers) is not stated anywhere.
- The absence of any availability wording, product page, docs link, pricing row or demo three and a half months after launch suggests the MCP Server is being sold through direct account engagement (possibly limited availability) rather than self-serve. This is inference; no source states the status.
- Because Glassbox's public materials name no AI client, a third-party product should not assume Glassbox has tested or allow-listed any particular client.

### Gaps
- Endpoint/URL shape, transport, authentication, tool/resource/prompt list, parameters, outputs, limits, supported clients, GA/beta status, licensing, deployment-model support, masking behaviour over MCP: **none published in any source found.**
- No glassbox.com product page, blog post, webinar, release note or demo video for Pulse or the MCP Server was found. The Glassbox blog index was not crawled page by page; site-restricted search returned only the press release.
- LinkedIn posts by Glassbox or its executives and YouTube videos: none surfaced in search; LinkedIn/YouTube were not directly browsable. Absence is "not found via web search", not "confirmed absent".
- Customer documentation at support.glassbox.com is login-gated and was not accessed.
- Trust Center content (subprocessors, AI documentation) unreadable.

---

## 2. Is the Glassbox MCP Server listed in any MCP registry, directory or marketplace?

### Takeaway
No listing for the Glassbox (glassbox.com) MCP Server was found in any registry or directory. Every "glassbox" MCP entry found belongs to an unrelated project. Direct competitors Fullstory, Contentsquare, Amplitude and Pendo are in the Claude connector directory; Glassbox did not appear.

### Cited Findings
- **[Directory, 17 Nov 2026]** A Claude connector-directory search for "glassbox", "session replay", "digital experience analytics" returned Fullstory, Contentsquare, Subtext, Amplitude and Pendo, and no Glassbox entry. The tool states its search covers only part of the directory. — (tool result; no public URL)
- **[Snippet]** Web search across Smithery, mcp.so, PulseMCP, AWS Marketplace, Copilot Studio and AgentExchange terms returned no Glassbox-DXA listing; all hits were unrelated namesakes. — search results listing e.g. [PulseMCP: Glassbox Framework by TheBarmaEffect](https://www.pulsemcp.com/servers/thebarmaeffect-glassbox)
- Unrelated "glassbox" MCP projects found, and how each was ruled out:
  - **[Snippet]** "Glass Box Framework" (TheBarmaEffect / Built-By-Aura; npm `@glassbox-framework/mcp`): "runtime constitutional verification for AI answers", Trust Cards, audit logs. AI-answer verification, not analytics; different publisher. — [GitHub](https://github.com/Built-By-Aura/glassbox); [Glama](https://glama.ai/mcp/servers/TheBarmaEffect/glassbox)
  - **[Read]** `mcp-glassbox` by ResakaGit: local TypeScript server in the Testing & QA / observability category (runs test processes, Docker savepoints, telemetry query). No reference to glassbox.com; individual GitHub author. — [Glama](https://glama.ai/mcp/servers/ResakaGit/mcp-glassbox/schema)
  - **[Snippet]** "Glassbox AI" (multi-agent orchestration), reviewed by AgentSeal. — [AgentSeal](https://agentseal.org/mcp/glassbox-ai)
  - **[Snippet]** "Glassbox Studio" from Glass Box Computer (glassboxcomputer.com), server id `glassbox-studio`. Different company and domain. — [Glass Box Computer docs](https://glassboxcomputer.com/docs/mcp)
  - **[Snippet]** "glassbox-agent-arena" (philipwright1295-wq), published to the official MCP Registry under a personal namespace. — [GitHub PR](https://github.com/philipwright1295-wq/glassbox-agent-arena/pull/11)
  - **[Snippet]** Other community entries on Glama (hesselb20-ctrl/glassbox; a browser-daemon "glassbox" for UI verification) and aggregator pages (claudecodeagents.com, conare.ai) that mirror these. — [Glama](https://glama.ai/mcp/servers/hesselb20-ctrl/glassbox)
  - Rule used: an entry counts as Glassbox's only if published by Glassbox Ltd / the glassbox.com domain or describing digital-experience/session-replay data. None met either test.
- **[Read, earlier pass]** Glassbox's two AWS Marketplace listings are ordinary SaaS listings for the analytics platform; neither is an "AI agents and tools" MCP listing. — [AWS Marketplace](https://aws.amazon.com/marketplace/pp/prodview-waapo3vlxej2o); [AWS Marketplace](https://aws.amazon.com/marketplace/pp/B09ZVL2TQB)

### Inferences
- Non-listing is consistent with a per-tenant, customer-specific endpoint (plausible for a vendor with single-tenant and on-prem deployments) rather than one global hosted URL like `mcp.vendor.com`. Inference only.

### Gaps
- Not individually searched on their own sites: registry.modelcontextprotocol.io, GitHub modelcontextprotocol/servers, Smithery, mcp.so, mcpservers.org, Cursor directory, ChatGPT apps directory, Microsoft Copilot Studio / Azure AI Foundry catalogue, AWS Marketplace "AI agents and tools" category, Salesforce AgentExchange, Google Agentspace / Gemini Enterprise. Coverage was via general web search plus the Claude directory tool, so the finding is "not found", not "confirmed absent".

---

## 3. Which AI clients and partners does Glassbox name? Any Microsoft co-marketing?

### Takeaway
Glassbox names no AI client for the MCP Server (not Claude, ChatGPT, Copilot, Gemini, Agentforce, ServiceNow or Adobe). The only AI partner named anywhere is Microsoft: GIA runs on Azure OpenAI Service, Glassbox is on Azure Marketplace, and in September 2026 it obtained a Microsoft certified-software designation for financial-services AI.

### Cited Findings
- **[Read]** The July 2026 release names no AI client or partner. — [PR Newswire](https://www.prnewswire.com/news-releases/glassbox-turns-digital-experience-into-real-time-digital-operational-intelligence-for-enterprise-ai-302837278.html)
- **[Read]** GIA "was built using Microsoft's Azure OpenAI Service"; compliance badges on the page include "Azure OpenAI". — [Glassbox Insights Assistant](https://www.glassbox.com/platform/glassbox-insights-assistant/)
- **[Read]** 24 Sep 2026 Microsoft "Solutions Partner with certified software" designation for financial services AI; Perry cites partnership "since launching on Azure Marketplace". No Copilot / Copilot Studio / Azure AI Foundry integration is claimed. — [Channel Insider](https://www.channelinsider.com/channel-business/glassbox-earns-new-designation-in-microsoft-ai-cloud-partner-program/)
- **[Snippet]** GIA launch headline, 10 Jan 2024: "Glassbox Delivers Enterprise-Ready Generative AI Assistant with Microsoft Azure OpenAI Service". The Business Wire page returned HTTP 403 and was not read. — [MarketScreener mirror](https://m.marketscreener.com/quote/stock/GLASSBOX-LTD-123640090/news/Glassbox-Delivers-Enterprise-Ready-Generative-AI-Assistant-with-Microsoft-Azure-OpenAI-Service-45716365/)

### Inferences
- Given the Azure OpenAI base and the Microsoft designation, a Microsoft-hosted agent surface (Copilot Studio / Azure AI Foundry) is the most likely first-party-endorsed client, but nothing public says so.

### Gaps
- No statement found on Claude, ChatGPT, Gemini, Agentforce, ServiceNow or Adobe Agent Orchestrator support.
- Whether Pulse or the MCP Server use Azure OpenAI (as GIA does) or another model provider: not stated.

---

## 4. What example questions, use cases and returned artefacts are shown? Aggregated metrics or narrative only?

### Takeaway
No example prompt, screenshot, demo or sample output for Pulse or the MCP Server is public. The release describes use-case categories and evidence classes only. Whether the server returns tabulatable metrics is unknown; the wording points to curated findings plus evidence.

### Cited Findings
- **[Read]** Use-case categories: stalled applications, failed payments, release-related friction, unresolved chatbot interactions, hidden technical errors, avoidable service calls, conversion risk. Evidence classes: customer behaviour, journey impact, errors, network logs, server-side failures. Perry says the dataset links errors to conversion drops and lost revenue. — [PR Newswire](https://www.prnewswire.com/news-releases/glassbox-turns-digital-experience-into-real-time-digital-operational-intelligence-for-enterprise-ai-302837278.html)
- **[Read]** Competitor Quantum Metric's comparison page (first published 27 Oct 2025, updated 24 Aug 2026) states that Pulse and the MCP Server are newly announced "with no public demos or customer proof points" as of writing. Competitor source, but consistent with this research. It misdates the announcement as August 2026. — [Quantum Metric blog](https://www.quantummetric.com/blog/glassbox-alternatives)
- **[Read]** Existing in-product artefacts that could plausibly be surfaced: GIA session summaries, report summaries, friction ranked "by business impact"; Rapid Response hands support reps a "GIA Profile" with "a short text summary of the error" and a session-replay link. — [GIA](https://www.glassbox.com/platform/glassbox-insights-assistant/); [Rapid Response](https://www.glassbox.com/platform/rapid-response/)

### Inferences
- Likely returned artefacts, by analogy with GIA/Rapid Response and the release wording: issue/anomaly descriptions, affected-journey impact, error and network-log evidence, session-replay links. Quantified impact (conversion drop, revenue) is implied by Perry's quote but not confirmed as a structured field.
- An AI-analyst product should plan for narrative-plus-evidence responses and treat tabular aggregates as a possible bonus to be verified against real tool schemas.

### Gaps
- No tool schema, sample response, demo video or customer case study found.

---

## 5. Press, analyst and customer coverage: any technical detail, criticism or availability information?

### Takeaway
Coverage is thin and entirely derivative of the release. No tier-one tech press, no analyst-firm note, no Israeli tech-press piece and no customer quote was found. The only critical commentary comes from a competitor (Quantum Metric).

### Cited Findings
- **[Read]** IT Brief / TechDay, 30 Jul 2026: says the products have "launched"; no GA/beta status, pricing, clients, auth or tools; relies on company statements; no independent view. Syndicated on CMOtech and CFOtech. — [IT Brief](https://itbrief.news/story/glassbox-launches-ai-tools-for-enterprise-digital-data)
- **[Read]** Daily AI Brief, 29 Jul 2026: brief rewrite; adds nothing. — [Daily AI Brief](https://dailyaibrief.com/news/glassbox-launches-pulse-and-mcp-server-for-enterprise-ai-context-dz5Eidnu)
- **[Read]** Renascence newsdesk, 12 Aug 2026 ("Glassbox streams live CX data into enterprise AI systems"): editorial paraphrase of the PR Newswire release; describes in-session signals (clicks, hesitations, errors, drop-offs) converted into structured signals for "AI agents and automated systems"; names no mechanism, product sub-brand, partner or availability. Search shows Renascence published several near-duplicate pieces (ids 25504, 25609, 9567, 40618) off the same release. This resolves the earlier open question: there is **no separate August streaming product announcement**; it is the July release re-covered. — [Renascence](https://www.renascence.io/in/news/25504/glassbox-streams-live-cx-data-into-enterprise-ai-systems)
- **[Snippet]** Other mirrors of the release: Yahoo Finance, MarketScreener, MarTech Cube, Customer Service Manager, YourTechDiet, AI Journal. — e.g. [Yahoo Finance](https://finance.yahoo.com/technology/ai/articles/glassbox-turns-digital-experience-real-125800500.html)
- **[Read]** Quantum Metric, "Best agentic analytics platforms for 2026" (dated 12 Aug 2026; evaluation "current as of July 30, 2026"): rates Glassbox Partial for autonomous investigation, Limited for multi-step reasoning, Strong for context architecture, None for reasoning visibility and tool use, Strong for governance. Verdict: "Strong autonomous detection paired with a conversational assistant." GIA is "primarily conversational"; "there is no verified evidence that GIA independently selects and invokes" Glassbox integrations/APIs; "Public materials do not document an inspectable investigation plan, evidence graph, or intermediate analytical trace." It does not mention Pulse or the MCP Server. Competitor marketing; weight accordingly. — [Quantum Metric blog](https://www.quantummetric.com/blog/best-agentic-analytics-platforms-2026)
- **[Read]** Quantum Metric's Glassbox-alternatives page criticises Glassbox for legacy infrastructure that "can strain under peak traffic", reliance on manual replay review, and limited replay fidelity / inconsistent mobile coverage. Competitor marketing. — [Quantum Metric blog](https://www.quantummetric.com/blog/glassbox-alternatives)
- **[Snippet]** Search for Forrester, Gartner, IDC, CMSWire, Destination CRM, Calcalist and Globes coverage of Pulse/MCP returned nothing; the nearest analyst-style item is a Futurum Group interview with Glassbox's CMO that predates the launch. — [Futurum](https://futurumgroup.com/news/glassbox-helps-tell-the-why-in-user-behavior)

### Inferences
- The lack of a named launch customer or analyst briefing coverage is weak evidence that the MCP Server was early in its rollout at announcement.

### Gaps
- TechCrunch, VentureBeat, MarTech.org: no coverage found. Paywalled analyst notes cannot be ruled out.
- No customer quote about Pulse or MCP found. AWS Marketplace reviews from Jul–Aug 2026 (read in the earlier pass) do not mention either.

---

## 6. GIA specifics, its relationship to Pulse and the MCP Server, and verification of the Anodot acquisition and CEO change

### Takeaway
GIA is an in-product chat assistant (session search, session summaries, report summaries, friction prioritisation) built on Azure OpenAI Service, with no documented API or embedding option and no pricing row. Glassbox has not publicly explained how GIA, Pulse and the MCP Server relate. The Anodot acquisition (4 Nov 2025) and Guy Perry's appointment as CEO (announced Oct 2025) are both confirmed from Glassbox's own news pages; the Anodot release says its engine "will be embedded" in the platform but does not name Pulse.

### Cited Findings
GIA
- **[Read, 17 Nov 2026]** Features: natural-language search for any web or mobile session; chat-based session summaries with follow-up questions ("Quickly summarize any user session with a chat-based interface"); key takeaways from ad-hoc or mobile reports; friction ranked by business impact; "single-click suggested prompts"; report insights without exporting data. Built "using Microsoft's Azure OpenAI Service", which prevents customer data being used "to train future models". No example prompts, no API or embedding option, no deployment restriction, no pricing/plan statement, and no mention of Pulse, MCP, agents or anomaly detection on the page. — [Glassbox Insights Assistant](https://www.glassbox.com/platform/glassbox-insights-assistant/)
- **[Snippet]** A search summary of the GIA page says GIA is "immediately available within the Glassbox digital experience intelligence platform for all customers". Not confirmed in the page read. — [GIA page (alt URL)](https://glassbox.com/platform/glassbox-insights-assistant)
- **[Read]** GIA output is reused in Rapid Response (marked "New"): rule-based real-time triggers ("Page Content Triggers" and "Struggle-Based Triggers" such as rage clicks) fire interventions (redirect, promo code, tutorial, live chat), and support reps receive a "GIA Profile" with a short error summary and replay link. The page names no API, webhook, Slack/Teams/Jira hook, Pulse, MCP or Anodot. — [Rapid Response](https://www.glassbox.com/platform/rapid-response/)
- **[Read]** 24 Sep 2026: GIA described as "expanding" and powered by Azure OpenAI Service. — [Channel Insider](https://www.channelinsider.com/channel-business/glassbox-earns-new-designation-in-microsoft-ai-cloud-partner-program/)

Anodot
- **[Read]** 4 Nov 2025, New York: Glassbox announced the acquisition of Anodot, a real-time anomaly detection company founded in 2014 ("autonomous business monitoring"; unsupervised ML that detects anomalies without manual thresholds or predefined rules). Seller and terms not disclosed. Anodot's engine "will be embedded" in the Glassbox platform to detect anomalies in captured digital-behaviour data — early signals of customer friction, performance drops, conversion declines, rising frustration — with "built-in workflow integrations". No product name, timeline, agent, MCP or API is mentioned. Named Glassbox customers in the release: Citi, SoFi, Credit.com. — [Glassbox news](https://glassbox.com/news/glassbox-anodot-acquisition/)
- **[Snippet]** Business Wire carried the release the same day; Mergr lists the seller as Dancap Private Equity (third-party deal tracker; unverified). — [Business Wire](https://www.businesswire.com/news/home/20251104734652/en/Glassbox-Strengthens-Anomaly-Detection-With-Acquisition-of-Anodot); [Mergr](https://mergr.com/transaction/glassbox-acquires-anodot)
- **[Snippet, low-quality source, unverified]** A search summary of a secondary site reports that in 2026 attackers used stolen Anodot credentials to access 12+ Snowflake customer environments. Not opened, not corroborated; flagged only because, if true, it bears on security review of any Anodot-derived component. — [tech-insider.org](https://tech-insider.org/rockstar-games-snowflake-breach-shinyhunters-anodot-2026/)

CEO change
- **[Read]** Glassbox appointed Guy Perry as CEO; release datelined 15 Oct 2025, page dated 23 Oct 2025 (Business Wire copy also 23 Oct 2025). Perry was previously CEO and president of Surecomp, earlier at NCR and Motorola Solutions. Chairman Alex Assim is quoted. The page does **not** name the predecessor or state what happened to him, and contains no AI strategy statement. — [Glassbox news](https://www.glassbox.com/news/glassbox-names-new-ceo/); [Business Wire](https://www.businesswire.com/news/home/20251023474860/en/Glassbox-Names-Guy-Perry-as-CEO-to-Accelerate-Growth-and-Digital-Experience-Innovation)
- **[Snippet]** Trade coverage (Research Live, MrWeb) frames Perry as succeeding Yaron Morgenstern, CEO since 2015; MarketScreener lists Perry's start as 22 Oct 2025. Morgenstern's subsequent role not found. — [Research Live](https://www.research-live.com/article/news/new-ceo-at-glassbox/id/5144043)

### Inferences
- Timeline (new CEO Oct 2025 → Anodot Nov 2025 → Pulse Jul 2026) and the match between Anodot's function (unsupervised anomaly detection without thresholds) and Pulse's description ("applies machine learning first to identify meaningful changes") make it likely that Pulse is built on or around Anodot's engine. No Glassbox source states this.
- Likely layering: Anodot-derived detection → Pulse (detection + classification + NL Q&A) → MCP Server (external agent access to Pulse context). GIA appears to remain the in-UI assistant for session/report questions. Whether GIA's functions (session search, session summary) are also exposed as MCP tools is unknown.
- Rapid Response shows a second, rule-based real-time path that is separate from Pulse.

### Gaps
- No GIA API, SDK, embedding or pricing information found. GIA's underlying model version is not named.
- Whether GIA / Pulse / MCP Server are available on on-prem or single-tenant deployments: not stated anywhere. All three plausibly depend on cloud LLM services, which would matter for bank customers running on-prem.
- The 10 Jan 2024 GIA launch release could not be read (HTTP 403).
- The "AI Platform Engineer – Autonomous Agents" job posting noted in the earlier pass now returns HTTP 404; its technology stack could not be read.

---

## 7. Connection facts for an external AI-analyst product: what is known vs unknown

### Takeaway
Every connection parameter is unknown from public sources. The table records this explicitly and gives the benchmark-based expectation, labelled as inference.

### Cited Findings
| Parameter | Public status for Glassbox (17 Nov 2026) | Source |
|---|---|---|
| Endpoint / URL shape | Not published | [PR Newswire](https://www.prnewswire.com/news-releases/glassbox-turns-digital-experience-into-real-time-digital-operational-intelligence-for-enterprise-ai-302837278.html) |
| Transport | Not published | same |
| Authentication | Not published; only "governed access". Platform-level: SSO with MFA via SAML 2.0 / customer IdP, granular RBAC, full audit log (read in earlier pass) | same; [Data Privacy & Security](https://glassbox.com/platform/data-privacy-security) |
| Tools / resources / prompts | Not published | same |
| Supported clients | None named | same |
| Availability (GA / beta) | Not stated; "announced"/"launched" only | same; [IT Brief](https://itbrief.news/story/glassbox-launches-ai-tools-for-enterprise-digital-data) |
| Licensing / price | Not stated; no pricing-page row | [Pricing](https://www.glassbox.com/platform/plans-pricing/) |
| Deployment-model support | Not stated; platform itself offers on-prem, cloud, hybrid on all plans | [Pricing](https://www.glassbox.com/platform/plans-pricing/) |
| Masking / governance over MCP | Not stated beyond "governed"; platform masks sensitive data by default at capture (earlier pass) | [Data Privacy & Security](https://glassbox.com/platform/data-privacy-security) |
| Rate / size limits | Not published | — |

### Inferences
- Benchmark expectation (from section 8): a remote Streamable-HTTP endpoint, OAuth 2.x browser sign-in tied to the user's existing platform login and permissions, optionally an API key / service account for server-to-server use, an admin toggle to enable MCP, and roughly 3–50 read-oriented tools. For Glassbox specifically, expect a per-tenant or per-region host (and possibly no public reachability for single-tenant/on-prem customers), and expect SAML SSO to sit behind any OAuth flow.
- A connector should therefore be built as a generic MCP client with configurable URL, OAuth (with and without dynamic client registration) and static bearer-token auth, discovering tools at runtime, rather than hard-coding Glassbox tool names.
- Since masking happens at capture, MCP outputs will inherit it; whether additional output filtering applies is unknown.

### Gaps
- All rows above. The practical next step is non-public: obtain the MCP documentation through a Glassbox customer (customer portal) or Glassbox partner/sales contact.

---

## 8. How direct competitors expose MCP servers (benchmark)

### Takeaway
Competitors converge on a hosted remote endpoint over Streamable HTTP with OAuth browser sign-in inheriting the user's permissions, usually an API-key or service-account alternative, an admin enablement switch, and tools that return both aggregated metrics (funnels, metrics, top errors) and session-level evidence. Microsoft Clarity is the outlier (local npx server with a Data Export API token). Glassbox is the only vendor in the set with nothing published.

### Cited Findings
| Vendor | Endpoint / transport | Auth | Representative tools / capabilities | Notes | Source and confidence |
|---|---|---|---|---|---|
| Contentsquare (also owns Heap and Hotjar) | `https://api.contentsquare.com/mcp`, Streamable HTTP (per third-party directory) | OAuth browser sign-in; 401 points to OAuth protected-resource metadata; admins only; inherits project permissions; access lapses after 30 days | `computeSiteMetrics`, `computeFunnel`, `getTopErrorsBySessionsWithErrors`, `getTopErrorsByImpactOnGoal`, `getTopErrorsByMissedOpportunity`, `getTopPageGroupsByLostConversions`, `getTopPagesBySessionsWithErrors` (21 tools) | In Claude directory; official support article returned HTTP 403 | [Snippet] [mcpservers.org](https://mcpservers.org/remote-mcp-servers/contentsquare); [support article](https://support.contentsquare.com/hc/en-us/articles/41563169756945); tool names [Directory] |
| Fullstory | `https://api.fullstory.com/mcp/fullstory` (EU host `api.eu1.fullstory.com`) per third-party listings | OAuth (default) or API key for gateways/shared services | `build_segment`, `build_metric`, `build_multi_metric`, `build_funnel`, `compute_funnel`, `build_journey`, `build_dashboard`, `compute_dashboard` (about 25–46 tools depending on source); aggregated counts/trends/breakdowns and session retrieval with event transcripts | Requires StoryAI and MCP toggles enabled by an admin; otherwise connects with zero tools. Clients named: Claude, Claude Code, Cursor, Codex, Gemini CLI, VS Code. Separate "Customer Agents" server at `api.fullstory.com/mcp/activations` (OAuth 2.1 + PKCE, read-only) | Auth, prerequisites, clients [Read] [Fullstory developer docs](https://developer.fullstory.com/mcp/introduction/); URL [Snippet]; tool names [Directory] |
| Quantum Metric | URL not published on product page | Not specified; "Every request runs through the same access controls, privacy settings, and audit trail"; agents "see only what each user is allowed to see" | Ad hoc and saved analytics queries; metric/event catalogue; session and user-history summaries; replay links and screenshots; funnels; dashboards; alerts, incidents and affected sessions; background agents/workflows; docs search | Included in all customer agreements; extra tool calls purchasable. Clients named: Claude, Gemini. A QM blog said external MCP "remained beta" as of 16 Jul 2026 | [Read] [Quantum Metric MCP](https://www.quantummetric.com/platform/mcp); [QM blog](https://www.quantummetric.com/blog/best-agentic-analytics-platforms-2026) |
| Amplitude | `https://mcp.amplitude.com/mcp` (EU `https://mcp.eu.amplitude.com/mcp`) | OAuth 2.0 with dynamic client registration (per Nango) | `get_amplitude_charts`, `get_amp_session_replay_info`, `get_amp_taxonomy`, `get_amp_user_data`, `get_data_warehouse_destinations`; query, chart and dashboard tools (15–48 tools depending on source) | In Claude directory | [Snippet] [Amplitude docs](https://amplitude.com/docs/en/amplitude-ai/amplitude-mcp); [Nango](https://nango.dev/docs/api-integrations/amplitude-mcp); tool names [Directory] |
| Mixpanel | `https://mcp.mixpanel.com/mcp` (EU `mcp-eu.mixpanel.com/mcp`, India `mcp-in.mixpanel.com/mcp`), HTTP transport | OAuth with PKCE; service accounts in beta for scripts/CI | Query and report tools (15–54 depending on source) | — | [Snippet] [Mixpanel docs](https://docs.mixpanel.com/docs/mcp) |
| PostHog | `https://mcp.posthog.com/mcp`; `features` query parameter narrows tool set | OAuth recommended (auth server routes US/EU); or personal API key as Bearer token | Analytics, feature-flag and session-replay tools (15–57 depending on source) | — | [Snippet] [PostHog docs](https://posthog.com/docs/model-context-protocol) |
| Heap | No separate Heap MCP server found; Heap is part of Contentsquare | — | — | Not researched separately | (gap) |
| Pendo | `https://app.pendo.io/mcp/v0/shttp` (regional hosts, e.g. `us1.app.pendo.io`), Streamable HTTP | OAuth via Pendo login; service account for agents/scripts | `accountQuery`, `activityQuery`, `guideMetrics`, `segmentList`, `searchEntities`, `list_all_applications` | Subscription admin must enable under Settings > Subscription settings > AI access | [Snippet] [Pendo support](https://support.pendo.io/hc/en-us/articles/41102236924955); tool names [Directory] |
| LogRocket | `https://mcp.logrocket.com/mcp`; optional org/project path scoping; `?toolsets=sessions,metrics` | Browser sign-in, or API key as Bearer header | 3 tools by default; "under active development" | — | [Snippet] [LogRocket docs](https://docs.logrocket.com/docs/mcp) |
| Microsoft Clarity | Local stdio server, npm `@microsoft/clarity-mcp-server` via npx | Clarity Data Export API token (`--clarity_api_token`), per project | `get-clarity-data` | Not a hosted server | [Snippet] [Glama mirror of Microsoft repo](https://glama.ai/mcp/servers/@microsoft/clarity-mcp-server) |

### Inferences
- The closest peers by customer type (Contentsquare, Fullstory, Quantum Metric) all return tabulatable aggregates (funnels, site metrics, top errors by impact) alongside session evidence. Glassbox's "curated context, not raw stream" framing suggests a narrower, findings-first surface, at least initially.
- Common gating patterns a connector should handle: admin-only enablement, AI-feature opt-in prerequisites, regional hosts, permission inheritance, token expiry (30 days at Contentsquare), and metered tool calls (Quantum Metric).
- Tool counts vary widely between sources for the same vendor, so runtime tool discovery is the only reliable approach.

### Gaps
- Most competitor rows rest on search snippets and third-party directories; only Fullstory's introduction page and Quantum Metric's product page were read. Tool names marked [Directory] come from the Claude connector directory and are partial lists.
- Contentsquare's official support article (HTTP 403) and Heap were not verified.

---

## Summary of what could not be found (17 Nov 2026)
1. Any technical documentation for the Glassbox MCP Server: URL, transport, auth, tools, resources, prompts, parameters, outputs, limits.
2. Availability status, licensing/pricing, supported or tested AI clients, deployment-model support.
3. A glassbox.com product page, blog post, webinar, release note, demo video or LinkedIn/YouTube content for Pulse or the MCP Server.
4. Any registry, directory or marketplace listing for the Glassbox MCP Server.
5. Independent press, analyst-firm or Israeli-press coverage, and any customer quote.
6. A GIA API or embedding option; GIA pricing; the relationship between GIA, Pulse and the MCP Server.
7. Explicit confirmation that Pulse uses Anodot technology.
8. Trust Center content, the Jan 2024 GIA release, the Contentsquare MCP support article and the Glassbox AI-engineer job posting (unreadable: JS-rendered, 403, 403, 404).

Research effort: about 41 search/fetch calls.
