# Public benchmarks for text-to-SQL and data-analysis agents (state as of 2026-10-08)

Scope note: this catalog covers official leaderboards fetched directly on 2026-10-08 (Spider 2.0, BIRD, LiveSQLBench, BIRD-Interact, BIRD-CRITIC, TableBench, Spider2-V, Spider 1.0, KramaBench/OpenRCA/ITBench GitHub READMEs) plus arXiv papers and vendor posts. Where a number comes from a secondary aggregator or a vendor's own blog, it is labeled as such. Official leaderboard pages for Spider 2.0 do not print submission dates, so "current" = what the page showed on 2026-10-08.

---

## 1. Spider 1.0 and Spider 2.0 (Lite / Snow / DBT / AIFunc / V): tops, "good" score, who submits, reproduction requirements

### Takeaway
Spider 1.0 is closed and saturated (91.2% EX, leaderboard frozen Feb 2024). Spider 2.0 is the de-facto enterprise text-to-SQL board but its Snow track is now effectively saturated (96.70% top, up from 23.8% at release) and dominated by commercial agent systems, while Lite (76.23%) and DBT (65.60%) still have headroom; audits found ~63-66% of Spider 2.0-Snow gold queries contain annotation errors, and the hosted Snowflake eval account was suspended in Aug 2026, so reproduction is fragile.

### Cited Findings

**Spider 1.0 (dead/saturated)**
- Dataset: 10,181 questions, 5,693 unique SQL queries, 200 databases, 138 domains (SQLite). Top test Execution-with-Values: MiniSeek (Anonymous) 91.2 (Nov 2, 2023); DAIL-SQL + GPT-4 + Self-Consistency (Alibaba) 86.6; DIN-SQL + GPT-4 85.3. Exact Set Match top: MiniSeek 81.5 test. — [Spider 1.0 site](https://yale-lily.github.io/spider)
- "A February 5, 2024 news item says the team will no longer accept Spider 1.0 evaluation submissions or update its leaderboard"; test set has been released. — [Spider 1.0 site](https://yale-lily.github.io/spider)
- Spider 2.0 paper: an o1-preview-based agent "successfully solves only 21.3% of the tasks, compared with 91.2% on Spider 1.0 and 73.0% on BIRD" (2024/early-2025 figures). — [Spider 2.0 paper, ICLR 2025](https://proceedings.iclr.cc/paper_files/paper/2025/file/46c10f6c8ea5aa6f267bcdabcb123f97-Paper-Conference.pdf)

**Spider 2.0 — setup, sizes, dialects, cost**
- Spider 2.0-Snow: 547 examples, all Snowflake, "NO COST!". Spider 2.0-Lite: 547 examples — BigQuery (214), Snowflake (198), SQLite (135), "Some cost incurred". Spider 2.0-DBT: 68 tasks on DuckDB (dbt code-agent setting), "NO COST!". — [xlang-ai/Spider2 README](https://github.com/xlang-ai/Spider2); [spider2-sql.github.io](https://spider2-sql.github.io/)
- Snowflake access: fill out the "Spider2 Snowflake Access form" and follow assets/Snowflake_Guideline.md; BigQuery requires your own credentials (assets/Bigquery_Guideline.md). Lite needs both steps; Snow only the Snowflake step. — [xlang-ai/Spider2 README](https://github.com/xlang-ai/Spider2)
- Snow is free but "queries are queued"; since 2025-10-29 you can optionally host the Snowflake data in your own project at your own cost (Spider2_Data_Host.md) to avoid the queue. 2025-06-10: a tool-call Spider-Agent for Snow "requires no Docker and significantly improves runtime performance"; no timings given. — [xlang-ai/Spider2 README](https://github.com/xlang-ai/Spider2)
- **Access instability**: 2026-08-12 news: "Apology for ongoing disruption to Spider 2.0-Snow access. A Snowflake evaluation account suspension is being resolved with Snowflake." 2025-11-06: Snowflake password & MFA policy changes broke Web UI login and Python credential access. — [xlang-ai/Spider2 README](https://github.com/xlang-ai/Spider2); GitHub issue: "MFA authentication is required, but none of your current MFA methods are supported for programmatic authentication" — [Spider2 issue #143](https://github.com/xlang-ai/Spider2/issues/143)
- Evaluation: for Lite and Snow, agents output CSV result files (not SQL) that are compared against gold results (2024-12-26 note). Only partial gold SQL is released (121 of 547 Snow examples have public gold queries). Ground-truth tables were released 2025-04-20; results using them must be labeled "oracle tables" and are excluded from ranking. Gold SQL "not recommended" for SFT (2025-01-07) because it may affect fairness. — [spider2-sql.github.io](https://spider2-sql.github.io/); [arXiv 2601.08778](https://arxiv.org/html/2601.08778v3)
- Submission: "Officially validated entries are submitted through a linked submission guidance document" (Google Doc); the page "does not describe verification steps or say whether entries are self-reported." The leaderboard prints no dates. Scores "may change slightly over time" as examples are revised (2025-07-13 spider2-snow.jsonl ambiguity fix; 2025-10-29 "Major update" fixed an evaluation-suite issue and refreshed all leaderboard methods). — [spider2-sql.github.io](https://spider2-sql.github.io/); [xlang-ai/Spider2 README](https://github.com/xlang-ai/Spider2)

**Spider 2.0-Snow leaderboard (fetched 2026-10-08; 61 entries; metric labelled "Score", understood to be execution accuracy %)**
- 1. Genloop's Sentinel Agent v2 Pro (Genloop) 96.70; 2. Native mini (usenative.ai) 96.53; 3. QUVI-3 + Gemini-3-pro-preview (DAQUV) 94.15; 4. TCDataAgent-SQL (Tencent Cloud Big Data) 93.97; 5. Prism Swarm w/ Deepthink + Claude-Sonnet-4.5 (Paytm) 90.49; 6. Genloop Sentinel v2 88.48; 7. QUVI-3 + Claude-Opus-4.6 86.28; 8. Ask Data w/ Relational Knowledge Graph (AT&T CDO & RelationalAI) 86.28; 9. ByteBrain-Agent (ByteDance) 84.10; 12. Deepinsight Agent (Ant Group) 83.00; 17. Arctic-FLEX (Snowflake AI Research) 75.14; 21. PExA (Bloomberg) 70.20; 22. DIA (C3 AI) 69.47; 23. Chicory AI Agent + Claude Sonnet 4.5 + Opus 4.5 judge 67.28; 27. ReFoRCE + o3 (Hao AI Lab x Snowflake) 62.89; 37. Chat2DB-Agent + Claude-4-Sonnet 38.39; 42. Spider-Agent + Claude-4-Sonnet 25.78; 52. Spider-Agent + GPT-4o 12.98; 58. Dail-SQL + GPT-4o 2.20; 59. CHESS + GPT-4o 1.28; 60. DIN-SQL + GPT-4o 0.00. — [spider2-sql.github.io](https://spider2-sql.github.io/)
- Argo-Bench (TextQL, Oct 2026) summarises: the best Spider 2.0-Snow score "has risen from 23.8% at release" to 96.7%. — [Argo-Bench, arXiv 2610.02122](https://arxiv.org/html/2610.02122v1)

**Spider 2.0-Lite leaderboard (fetched 2026-10-08; 46 entries)**
- 1. Tianqiong Data Agent + GLM 5.2 (Tencent Data Computing Platform) 76.23; 2. DecisionX Agent (DecisionX AI) 74.95; 3. ktx (Kaelio) 73.67; 4. DivSkill-SQL (Snowflake AI Research x UCSD) 73.13; 5. SOMA-SQL (Oracle OCI AI Science) 72.02; 7. Databao Agent (JetBrains) 69.65; 8. QUVI-2.3 + Claude-Opus-4.5 65.81; 11. **Claude Code Agent + Sonnet (Nodal Data) 61.20**; 13. ReFoRCE + o3 55.21; 14. CoFD-SQL + GPT-5 (Samsung SDS) 54.66; 21. Spider-Agent + Claude-Sonnet-4.5 41.86; 30. Spider-Agent + Claude-4-Sonnet 27.79; 38. Spider-Agent + GPT-4o 13.16; 43. DailSQL + GPT-4o 5.68; 45. DIN-SQL + GPT-4o 1.46. — [spider2-sql.github.io](https://spider2-sql.github.io/)
- Oracle published a launch-style post claiming #1 on Lite with SOMA-SQL at 72.02% "EX@1" (now #5). — [Oracle blog](https://blogs.oracle.com/cloud-infrastructure/oci-gen-ai-tops-spider-2-lite) (page returned 403 on direct fetch; score and claim from search-result snippet)
- Secondary aggregators disagree with the official page: benchlm/Interfaze list "Interfaze Beta" leading Lite at 52.9% from provider self-reports. Treat the official page as authoritative. — [Interfaze Spider-2.0-Lite leaderboard](https://interfaze.ai/leaderboards/spider-2-lite); [benchlm Spider2-Lite](https://benchlm.ai/benchmarks/spider2lite)

**Spider 2.0-DBT leaderboard (fetched 2026-10-08; 16 entries; 68 tasks)**
- 1. SignalPilot Agent (signalpilot.ai) 65.60; 2. Databao Agent (JetBrains) 60.29; 3. Shadowfax-DBT-Agent + GPT-5 41.18; 4. Spider-Agent-Extended + GPT-5 39.71; 5. USTC-KCIL DBT Agent + GPT-5.4 39.71; 6. DIA (C3 AI) 37.50; 8. Spider-Agent-DBT + GPT-5.4 35.29; 9. Chicory AI Agent + Claude Sonnet 4.5 35.29; 11. Spider-Agent + Claude-3.7-Sonnet 14.70; 14. Spider-Agent + GPT-4o 7.35. — [spider2-sql.github.io](https://spider2-sql.github.io/)

**Who submits**: predominantly commercial agent/product teams (Genloop, usenative.ai, DAQUV, Tencent, Paytm, AT&T/RelationalAI, ByteDance, Ant Group, Snowflake AI Research, Bloomberg, C3 AI, Chicory, Oracle OCI, JetBrains Databao, DecisionX, Kaelio, Nodal Data, SignalPilot, Chat2DB) plus academic agents (ReFoRCE/Hao AI Lab, AutoLink/HUST, RSL-SQL). Snowflake + Hao AI Lab publicly announced #1 on Snow in Jan 2025 (ReFoRCE, then 31.26). — [spider2-sql.github.io](https://spider2-sql.github.io/); [Hao AI Lab on X](https://x.com/haoailab/status/1878948359622549550); [Baris Gultekin (Snowflake) on X](https://x.com/barisg/status/1879001356851372177)

**ReFoRCE (the canonical open agent) efficiency**: "requiring only 3.52 LLM and 3.89 DB calls per example"; with column exploration off, "1.69 calls and 15K tokens per example". Paper-reported scores 35.83 Snow / 36.56 Lite (v5, Jun 2025) vs 62.89 Snow / 55.21 Lite with o3 on the live board. — [ReFoRCE arXiv 2502.00675](https://arxiv.org/pdf/2502.00675); [Snowflake-Labs/ReFoRCE](https://github.com/Snowflake-Labs/ReFoRCE)

**Spider 2.0-AIFunc (new, Jul 2026)**: 465 verified instances across 125 real-world databases, six types of AI functions, Snowflake, execution accuracy; strongest proprietary models 67–70%, best open-source 58.1% (10 models evaluated); no leaderboard/submission process described; data at GitHub Leolty/Spider2-AIFunc. Authors include Spider 2.0 authors (Fangyu Lei, Tao Yu) and Snowflake-affiliated researchers (Zhewei Yao, Yuxiong He). — [arXiv 2607.06229](https://arxiv.org/abs/2607.06229)

**Spider2-V (GUI/tool workflows; stale)**: 494 tasks across 20 enterprise applications (BigQuery, dbt, Airbyte, ...), execution-based evaluators. Leaderboard top: Learn-by-interact (Google Cloud) 16.6 (Jan 16, 2025); GPT-4V 14.0; GPT-4o 13.8; Claude-3-Opus 8.1. Most recent entry Jan 2025. — [spider2-v.github.io](https://spider2-v.github.io/)

**Annotation-error audits (apply to Spider 2.0-Snow and BIRD)**
- UIUC (Jin, Choi, Zhu, Kang): error rate 62.8% on Spider 2.0-Snow (76 of 121 examples with public gold), 52.8% on BIRD Mini-Dev (263 of 498). On a corrected 100-example BIRD Dev subset, 16 open-source agents' EX changed −7% to +31% relative; rank changes −9 to +9 (avg 5); CHESS rose 62%→81% and from 7th to tied-1st; Spearman between original and corrected ranking r_s = 0.32 (p=0.23). Audit cost ~$0.44 (BIRD) and $1.11 (Spider) per example with their SAR-Agent. Corrected data at GitHub uiuc-kang-lab/text_to_sql_benchmarks. — [arXiv 2601.08778v3](https://arxiv.org/html/2601.08778v3)
- CIDR 2026 version of the same work estimates 52.8% (BIRD) and 66.1% (Spider 2.0-Snow, 80 of 121 flagged); rankings shifted by up to three positions. — [CIDR 2026 paper](https://www.vldb.org/cidrdb/papers/2026/p5-jin.pdf)
- SpotIt (formal verification) found test-based evaluation overstates correctness: differentiating databases for 16 and 8 query pairs deemed correct for OmniSQL and GPT-5 respectively. — [SpotIt arXiv 2510.26840](https://arxiv.org/pdf/2510.26840)

### Inferences
- Spider 2.0-Snow is saturated and heavily contested by vendor marketing; a new entrant below ~85% will not rank in the top 10. Lite (top 76.23) and DBT (top 65.60, only 16 entries) are the tracks where a product can still land top-5/top-10 with a visible gap to "Spider-Agent + frontier model" baselines (41.86 Lite with Claude Sonnet 4.5; 35.29 DBT with GPT-5.4).
- The "Claude Code Agent + Sonnet (Nodal Data) 61.20" Lite entry is the most relevant comparator for an agentic product: it shows a generic coding agent lands mid-table, so a purpose-built analytics agent beating ~61 on Lite is a credible "we beat Claude Code" story.
- Reproduction risk is high: Snow needs the shared (recently suspended) Snowflake account or self-hosting the data at your own cost; Lite needs your own BigQuery billing; ~63% of released Snow gold has annotation errors, so disagreements on individual items are expected.
- DBT (68 DuckDB tasks, free, Docker) is cheap, has few entries, and matches BoW's "connect to warehouse + transform" story; however it is a code-agent setting (editing dbt projects) rather than ad-hoc analytics.

### Gaps
- Spider 2.0 submission Google Doc could not be fetched; the exact verification procedure (whether the organizers re-run submitted code) is unconfirmed. The official page states nothing about self-reported vs verified.
- No submission dates on the Spider 2.0 leaderboard; cannot date when Genloop/Native/Tencent entries were posted. Secondary source nl2sql.ai (503 on fetch) claims the public repo history stops in July and Lite top was 76.23 as of 2026-09-24.
- No dollar cost or wall-clock figure for running a full Spider 2.0 track was found in any source; only ReFoRCE's call/token counts.

---

## 2. BIRD family: BIRD (dev/test/mini-dev), BIRD-Interact, BIRD-CRITIC, LiveSQLBench — status and tops

### Takeaway
BIRD's classic test leaderboard is near human level (82.95 vs 92.96 human) and dominated by mostly-Chinese enterprise text-to-SQL systems with a ~10-day emailed test evaluation; the BIRD team's newer, harder, "Verified"-badged suites (LiveSQLBench 48.00 top, BIRD-Interact ~25 normalized reward, BIRD-CRITIC 35.5 SR) are far from saturated, PostgreSQL-based, and explicitly accept agent scaffolds, which makes them the most product-friendly text-to-SQL boards today.

### Cited Findings

**BIRD (classic)**
- 12,751 question-SQL pairs, 95 databases, 33.4 GB, 37+ domains; dev 1,534; train 9,428; Mini-Dev 500 (SQLite, MySQL, PostgreSQL). Test size not stated. Metric: Execution Accuracy (EX) plus R-VES (reward-based valid efficiency score) for test. Human performance (Data Engineers + DB Students): 92.96 EX, 83.26 R-VES. — [bird-bench.github.io](https://bird-bench.github.io/)
- Overall EX top 10 (test): 1. GrainSQL (Sarim Chaudhry, Purdue) 82.95 (Sep 07, 2026); 2. DataGallery-Text2SQL (Huawei 2012 Labs) 82.39 (Aug 22, 2026); 3. SiriusAI-SQL (Tencent) 82.28 (Dec 16, 2025); 4. AskData + GPT-4o (AT&T CDO) 81.95 (Sep 25, 2025); 5. Agentar-Scale-SQL (Ant Group) 81.67; 6. Sber Text2SQL 81.33; 7. Xiaomi Text2SQL 80.83; 8. RAS (Adya AI) 79.82; 9. DeepEye (HKUST-GZ) 79.09; 10. MarkovSQL (Anonymous) 78.70. CHASE-SQL + Gemini (Google Cloud) 76.02 (Apr 3, 2026). GrainSQL R-VES 84.10 exceeds human 83.26. — [bird-bench.github.io](https://bird-bench.github.io/)
- Single Trained Model track: Gemini-SQL2 (Google Research & Cloud) 80.04 test; ReToolSQL (JPMorganChase) 78.14; Databricks RLVR 32B 75.68; Arctic-Text2SQL-R1-32B (Snowflake) 73.84. — [bird-bench.github.io](https://bird-bench.github.io/)
- Mini-Dev leaderboard top: Jitto Build 75.60 SQLite (Sep 22, 2026); Ontology2SQL 70.20 SQLite / 65.80 PostgreSQL (Aug 21, 2026); GPT-4 baseline 47.80/40.80/35.80 (SQLite/MySQL/PG). Mini-Dev accepts PR-based updates. — [bird-bench.github.io](https://bird-bench.github.io/)
- Submission: follow Submission Guidelines (Google Doc), email bird.bench23@gmail.com for hidden-test evaluation; "results usually return within about 10 days". Newer entries flag "New Dev" (bird-sql-dev-1106 split), not comparable to old dev. — [bird-bench.github.io](https://bird-bench.github.io/)
- Argo-Bench summary: "the top BIRD entry reaches 82.4% against a human 93.0%". — [arXiv 2610.02122](https://arxiv.org/html/2610.02122v1)
- Annotation errors: 52.8% of BIRD Mini-Dev examples flagged; corrected rankings differ substantially (see Section 1). — [arXiv 2601.08778v3](https://arxiv.org/html/2601.08778v3)

**LiveSQLBench (BIRD team, "contamination-free, continuously evolving")**
- Versions: Base-Lite 270 tasks (180 SELECT, 90 management/CRUD), 18 DBs, PostgreSQL, released 2025-05-28; Base-Lite-SQLite 2025-07-28; Base-Full v1 600 tasks / 22 new DBs 2025-09-04; Large-v1 480 tasks / 18 DBs (~1K columns, ~54 tables each, ~84K avg prompt tokens, "Business Rule Drift") 2026-03-02. Each DB has a hierarchical knowledge base (HKB). Each release has an open dev set and a hidden test set that becomes the next release's open set. — [livesqlbench.ai](https://livesqlbench.ai/)
- Metric: Success Rate (SELECT: execution result vs gold; management SQL: test cases), micro-averaged; Cost/Task shown when available. Two categories: Model Base and Agent (Agent I: full DB context, can execute SQL, 20-step limit; Agent II: no up-front context). "Verified" badge requires submitting your codebase for pipeline evaluation by the BIRD team; "Reported" = vendor self-reported. — [livesqlbench.ai](https://livesqlbench.ai/)
- Leaderboard (Base set, fetched 2026-10-08): 1. DIA (C3 AI) 48.00 (2026-05-29); 2. Jitto Build (Claude Opus 5.5, high) (JamLabs) 41.33, $0.6731/task (2026-09-26); 3. MiniMax M3 (Claude Code), Reported 40.17; 4. Claude Opus 4.6 (OpenHands CLI) 38.00; 5. OpenCode-Grok4.7 37.50, ~$0.0976; 6. Gemini 3.1 Pro, Verified 36.50, $0.0507; 7. Claude Opus 4.6 35.50, $0.0979; 9. GPT-5.5 (low), Verified 33.50, $0.0896; 10. GPT-5.5 (xhigh) 33.33, $0.2220; 18. Gemini 2.5 Pro 28.67; 21. GPT-5 25.50, $0.0216; 26. Claude Sonnet 4.5 23.83, $0.0598; 41. GPT-4o 15.50. — [livesqlbench.ai](https://livesqlbench.ai/)
- Announcements: LiveSQLBench-Agent and LiveSQLBench-CLI (2026-04-04); "Data Intelligence Index" (2026-03-05); LiveSQLBench 1.5 and 2.0 seeking contributors (news dated 2026-10-05). Page inconsistency: "Last Updated 2026-03-02" yet entries dated through 2026-09-29. — [livesqlbench.ai](https://livesqlbench.ai/)

**BIRD-Interact (ICLR 2026 Oral; interactive/ambiguity resolution with user simulator)**
- Sizes: Lite 300 tasks (GitHub README says 270 tasks, 18 DBs, 175 tables, 2,286 columns, ~207 MB), Full 600 tasks (22 DBs, 244 tables, 2,011 columns, ~272 MB), both PostgreSQL; Mini-Interact SQLite (released 2025-11-13). Two modes: c-Interact (fixed conversational protocol) and a-Interact (agentic, model decides when to query the user simulator; budget in "bird-coins"). Metrics: Success Rate, normalized Reward, efficiency (turns / coins). "up to 11,796 interactions". — [bird-interact.github.io](https://bird-interact.github.io/); [GitHub bird-bench/BIRD-Interact](https://github.com/bird-bench/BIRD-Interact); [arXiv 2510.05318](https://arxiv.org/abs/2510.05318)
- Results (Full, Normalized Reward, GitHub README): c-Interact — Gemini-2.5-Pro 20.92 ($0.04/task), o3-mini 20.27, Claude-Sonnet-4 18.35 ($0.29), GPT-5 12.58; a-Interact — GPT-5 25.52 ($0.24/task), Claude-Sonnet-4 23.28 ($0.51), Claude-Sonnet-3.7 17.45 ($0.60), Gemini-2.5-Pro 17.33 ($0.22). Abstract: GPT-5 completes 8.67% of tasks in c-Interact and 17.00% in a-Interact. News (2025-08-26): best LLMs reach 16.33% SR on Full. — [GitHub bird-bench/BIRD-Interact](https://github.com/bird-bench/BIRD-Interact); [arXiv 2510.05318](https://arxiv.org/abs/2510.05318)
- "Interaction-Time Scaling": site states only claude-3-7-sonnet currently satisfies the ITS law. Submission: custom agent scaffolds and user simulators accepted; "Verified" badge after pipeline evaluation of submitted code; GT SQL/test cases sent automatically by email within 30 min on request ([bird-interact-lite GT&Test Cases]). 2026-03-29: BIRD-Interact-ADK released (Google ADK, LiteLLM-compatible). Live leaderboard rows did not render in the fetched page (filters: user simulators Gemini-2.0-Flash, Gemini-3-Flash-Preview, GPT-4o, Claude-Haiku-4-5, Bird-chat-8B). — [bird-interact.github.io](https://bird-interact.github.io/); [GitHub](https://github.com/bird-bench/BIRD-Interact)

**BIRD-CRITIC / SWE-SQL (NeurIPS 2025; SQL debugging)**
- Variants: Flash 200 (PostgreSQL), Open 570 (PostgreSQL, MySQL, SQL Server, Oracle), PostgreSQL 530, BigQuery 200, SQLite 500 (2026-03-23), Effi-SQL 300 slow/fast pairs (2026-06-18). Metric: SR (%); Effi-SQL adds R-VES, Coverage@2x, GMS. — [bird-critic.github.io](https://bird-critic.github.io/)
- Open (570) verified leaderboard: o1-preview 35.5 (2025-04-20); deepseek-reasoner (r1) 32.0; gpt-4o-2024-11-20 27.5; o1-mini 25.0; claude-3-5-sonnet 21.5. Flash: o1-preview 38.5. Human (DB experts, no AI) 78.87 Single / 80.00 Flash; experts with AI tools 83.33 Open, 87.90 PG, 90.00 Flash (July 2025). 2026-03-06: baseline outputs added for Claude Opus 4.6, Kimi-K2.5, GLM-4.7 (scores not on page). Tiers: Leading / Elite (top 15%) / Superior / Advanced / Standard / Basic. — [bird-critic.github.io](https://bird-critic.github.io/)

### Inferences
- Classic BIRD is near-saturated relative to human (82.95 vs 92.96) and its leaderboard turnaround (~10 days by email) and SQLite focus make it a weak launch vehicle for a warehouse-native product; its mini-dev PostgreSQL/MySQL split is the only dialect-diverse piece.
- LiveSQLBench is the strongest "SOTA-on-a-text-to-SQL-board" opportunity: top score only 48.00 and held by an agent product (C3 AI DIA); frontier coding agents (Claude Code, OpenHands) sit at 35–40; per-task costs are published ($0.05–$0.67), and a "Verified" badge is attainable by submitting code. 600 Base-Full tasks at ~$0.10–0.70/task implies roughly $60–$420 in model spend per full run.
- BIRD-Interact directly exercises clarification/ambiguity handling (what BoW's chat flow does) and has very low absolute scores (<26 normalized reward), so even modest success is headline-worthy; cost per task is low ($0.04–0.60) but simulator setup (Docker, PostgreSQL, several-minute DB init) adds engineering time.
- BIRD-CRITIC's leaderboard appears stale (top entries dated Apr 2025, frontier 2026 models only added as "baseline outputs"), which means a modern agent could plausibly post a new top SR — but the task (fixing broken user SQL) is adjacent to, not central to, an analytics product.

### Gaps
- BIRD-Interact live leaderboard rows were not readable from the page; only README-reported numbers (2025 models) are available. Unknown whether any 2026 frontier/agent entries exist.
- "Data Intelligence Index" (BIRD news 2026-03-05) could not be found in any search result; its definition is unknown.
- BIRD-CRITIC scores for Claude Opus 4.6 / Kimi-K2.5 / GLM-4.7 baselines (added 2026-03-06) were not shown on the page.

---

## 3. Data-analysis agent benchmarks: DABstep, DA-Code, InfiAgent-DABench, DSBench, DataSciBench, KramaBench, BLADE, DiscoveryBench, TableBench, Spider2-V, MLE-bench, DS-1000, Tapilot, 2026 newcomers (Argo-Bench, DAComp, AgenticDataBench, FDABench)

### Takeaway
DABstep (Adyen/HF) was the flagship data-agent leaderboard but is now saturated and gamed (16 entries at 100% Hard as of Aug 2026, instant unverified grading); TableBench remains active with an 80.64 top vs 85.91 human; the credible unsaturated options are enterprise-scale newcomers — Argo-Bench (TextQL, Oct 2026, 7.49B-row warehouse on BigQuery/Snowflake/DuckDB, best model 34.8% solved at $4.71/task), DAComp (210 tasks, <20% DE / <40% DA), KramaBench (104 tasks, ~22–56% depending on harness) — none of which has a verified public leaderboard yet.

### Cited Findings

**DABstep (Adyen + Hugging Face)**
- Released Feb 4, 2025; "450+ real-world tasks" in the payments domain (HF dataset: 450 default + 10 dev rows); two levels Easy/Hard (NVIDIA: 16% easy / 84% hard, ≈72/378). Context: payments.csv (~138k transactions), fees.json (1,000 fee structures), manual.md, reference tables; data synthetic. Scoring: binary factoid with adaptive numeric tolerance, fuzzy string match, element-wise list compare ("Exact Text Match with strict formatting" per NVIDIA). — [HF blog: DABstep](https://huggingface.co/blog/dabstep); [HF dataset adyen/DABstep](https://huggingface.co/datasets/adyen/DABstep); [NVIDIA blog](https://huggingface.co/blog/nvidia/nemo-agent-toolkit-data-explorer-dabstep-1st-place)
- Launch baselines (Hard): o3-mini 16%, DeepSeek R1 13%, Claude Sonnet 12%, DeepSeek V3 6%; paper (Jun 2025) best o4-mini 14.55% Hard. Human ~62% on Easy after 3+ hours; Llama 70B zero-shot >90% Easy. — [HF blog: DABstep](https://huggingface.co/blog/dabstep); [arXiv 2506.23719](https://arxiv.org/html/2506.23719v1)
- **Cost to run full benchmark (paper Table 2)**: o1 $435 ($0.967/task); Claude 3.5 Sonnet $90; o3-mini $85; GPT-4o $50; Claude 3.5 Haiku $35; GPT-4o-mini $3; DeepSeek R1 $3; DeepSeek V3 $2. — [HF blog: DABstep](https://huggingface.co/blog/dabstep)
- Submission: "A real-time leaderboard hosted on Hugging Face grades submissions instantly"; test set held out; answers submitted via form as JSONL (agent_answer + reasoning_trace). No verification process described. — [HF blog: DABstep](https://huggingface.co/blog/dabstep); [HF dataset](https://huggingface.co/datasets/adyen/DABstep)
- Progression: Google DS-STAR (Gemini-2.5-Pro) 45.2% (Nov 6, 2025 blog; 45.24 Hard); OceanBase DataPilot 87.57 Hard (early 2026, self-reported); NVIDIA NeMo Agent Toolkit "Data Explorer" + Claude Haiku 4.5: 89.95 Hard / 87.5 Easy, 20 s/task vs Claude Code + Opus 4.5 at 66.93 Hard / 90.2 Easy and 10 min/task (Mar 13, 2026, self-described as #1 at that time). — [Google Research DS-STAR](https://research.google/blog/ds-star-a-state-of-the-art-versatile-data-science-agent/); [NVIDIA blog](https://huggingface.co/blog/nvidia/nemo-agent-toolkit-data-explorer-dabstep-1st-place); OceanBase post [en.oceanbase.com](https://en.oceanbase.com/blog/24781555459) (429 on fetch; figure from search snippet)
- **Saturation/gaming (aggregator snapshot of the official HF Space, 2026-08-26)**: 16 entries at 100.00 Hard (e.g., ByteDance Lark Base Agent (doubao-seed-1.8), OceanBase DataPilot (Qwen3), Genesis Data Agent (gpt5-2), openGauss Data Agent (deepseek-v4-flash), Dipeak AskRui, plus accounts named "test", "lwz", "speta", "VeigaPunk-GPT5-CodeAgent"); entries named "ThinkEvolve Spoofer" (99.21), many "aa"/"test" accounts; NVIDIA's 89.95 had fallen to #60; Claude Opus 4.6-based "Actioneer v0.5 Agent" 94.44; H2O Data Analyst (claude sonnet 4.6) 91.8; Zoom ZDA_V6 (gpt-oss-120b) 88.62; "Code Agent2" GPT-5.5 79.37. The Space listed 108 agents in Mar 2026. — [benchmarklist DABstep snapshot](https://benchmarklist.com/benchmarks/dabstep/) (secondary, "Imported" from the HF Space); [NVIDIA blog](https://huggingface.co/blog/nvidia/nemo-agent-toolkit-data-explorer-dabstep-1st-place)
- Community tooling relevant to BoW's self-improving loops: "DABStep-loop: A Claude Agent SDK agent for the DABstep benchmark, with a scored, self-improving eval loop". — [GitHub nmp-dsci/DABStep-loop](https://github.com/nmp-dsci/DABStep-loop)

**DA-Code**
- 500 code-generation data-science tasks from real-world sources; DS-STAR reports 38.5% vs best alternative 37.0%. — [Google Research DS-STAR](https://research.google/blog/ds-star-a-state-of-the-art-versatile-data-science-agent/); [DA-Code paper](https://www.researchgate.net/publication/386202035_DA-Code_Agent_Data_Science_Code_Generation_Benchmark_for_Large_Language_Models)

**KramaBench (MIT)**
- 104 tasks, 633 sub-tasks, 1,764 files (~1.7 GB) across archaeology, astronomy, biomedical, environment, legal, wildfire; 61% "hard". Scoring: exact-match measures (BLEU/ROUGE/MRAE), LLM-generated unit tests on sub-tasks, bootstrapped rubric partial credit. README leaderboard (older models): DS-GURU self-correcting + GPT-o3 22.08%; Claude-3.5 14.35%; naive GPT-o3 9.64%. No formal submission; run `evaluate.py --sut <name>`. — [GitHub mitdbg/KramaBench](https://github.com/mitdbg/KramaBench)
- Other reported scores: smolagents + Claude 3.7 55.83% end-to-end (original paper, full-data setting); DS-STAR 44.7% vs 39.8%; ADP-MA with Sonnet 4.5 44.8% vs smolagents + o3 41.4% (105-pipeline version). — [KramaBench paper](https://www.alphaxiv.org/abs/2506.06541); [DS-STAR blog](https://research.google/blog/ds-star-a-state-of-the-art-versatile-data-science-agent/); [Autonomous Data Processing using Meta-Agents](https://arxiv.org/pdf/2602.00307)

**TableBench (active leaderboard)**
- 886 test cases, 18 categories across Fact Checking, Numerical Reasoning, Data Analysis, Visualization. Human overall 85.91. Top (Methodology board): MUSE PULSE (museai.im) 80.64 (Sep 14, 2026); Data Analysis Agent (ByteDance Lark Base) 79.44 (Aug 3, 2026); WPS AI (Kingsoft) 75.05 (Sep 17, 2026); ReVi-Agent (SCU) 74.57; JoyDataAgent (JD) 74.25; JTDA-Agent (CMCC) 73.77; raccoonAgent (SenseTime) 72.75; ExcelClaw-Agent (Alibaba) 71.72; ButtonAgent (Distyl AI) 64.14; baselines o4-mini-high + DP 61.69, GPT-5 + DP 59.94. Submission by email to tablebench2025@gmail.com; verification status unstated. — [tablebench.github.io](https://tablebench.github.io/)

**DSBench / InfiAgent-DABench / DataSciBench / MLE-bench (older or model-only)**
- DSBench (2024): best agent 34.12% on data-analysis tasks, 34.74% RPG on modeling tasks; uses LLM-as-judge for analysis. No 2026 update found. — [DSBench arXiv 2409.07703](https://arxiv.org/pdf/2409.07703); [project page](https://liqiangjing.github.io/dsbench.github.io/)
- InfiAgent-DABench (ICML 2024): gpt-4-0613 78.72% accuracy by questions; GPT-4-era leaderboard. — [infiagent.github.io](https://infiagent.github.io/)
- DataSciBench: published Findings of ACL 2026; evaluated set shows GPT-4o leading, DeepAnalyze-8B top open-source; no frontier 2026 models. — [ACL Anthology 2026.findings-acl.181](https://aclanthology.org/2026.findings-acl.181/)
- MLE-bench: 75 Kaggle competitions; o1-preview + AIDE medals in 16.9% (2024). Low relevance to analytics. — [MLE-bench arXiv 2410.07095](https://arxiv.org/pdf/2410.07095)

**2026 newcomers**
- **Argo-Bench (TextQL, arXiv 2610.02122, submitted 2026-10-01)**: simulated NYC food-delivery platform exported to an Oracle EBS 12.2-style warehouse of 235 tables / 7.49B rows / 81M orders / 3.4M customers; 210 tasks across trust & safety, FP&A, marketplace, accounting, growth; agents work in sandboxed Python and file actions (bans, budgets, forecasts, dashboard data sources) through a mission-control interface; scored 0–100 per expectation, "solved" ≥95. Runs used BigQuery; warehouse released as 1,219 Parquet files with setup for BigQuery, DuckDB, Snowflake, Trino, Delta, Iceberg. Baselines (solved % / score / $ per task excl. BigQuery): Claude Opus 5.5 34.8 / 59.5 / $4.71; GPT-6 Astra 27.6 / 51.8 / $2.71; Claude Sonnet 5.5 28.6 / 51.8 / $3.74; GPT-6.1 Sol 24.8 / 49.5 / $0.49; Claude Haiku 4.5 1.4 / 5.5 / $0.20. Official grading uses a privately seeded second world; simulator/graders not released; submissions scored "best-effort"; demo at argo-bench.com. — [arXiv 2610.02122](https://arxiv.org/html/2610.02122v1)
- **DAComp (arXiv 2512.04324, Dec 2025)**: 210 tasks spanning data engineering (execution-based, industrial schemas; success <20%) and data analysis (open-ended, hierarchical-rubric LLM judge; avg <40%); data/code at da-comp.github.io; no leaderboard described. — [arXiv 2512.04324](https://arxiv.org/abs/2512.04324)
- **AgenticDataBench (arXiv 2607.01647, Jul 2026)**: skills-organized tasks from 15 vertical domains incl. 5 real B2B fintech use cases; evaluated 4 harnesses; "the LLM achieving the best score varies across the four evaluated agent harnesses", Claude 4.6 best within DA-Agent and Claude Code; no leaderboard. — [arXiv 2607.01647](https://arxiv.org/abs/2607.01647)
- **FDABench (KDD'26, arXiv 2509.02473)**: 2,007 analytical tasks over six modalities (databases, documents, web, images, video, audio); code at github.com/fdabench/FDAbench; no leaderboard. — [arXiv 2509.02473](https://arxiv.org/abs/2509.02473)
- Snowflake/Databricks have not published results on any of these public data-agent benchmarks (see Section 6).

### Inferences
- DABstep is no longer a differentiator: with 16+ perfect Hard scores, unmoderated instant grading and obvious throwaway accounts, a strong result is unverifiable and indistinguishable from gaming. Its best remaining use is as a cheap internal regression suite (full run $2–$90 for most models, ~450 tasks) and for the "we beat Claude Code (66.93 Hard)" narrative, with the leaderboard caveat stated explicitly.
- TableBench is the only data-analysis board still active with dated entries, a human ceiling (85.91) and a clear baseline tier (GPT-5 + DP 59.94); it is table-QA over small tables rather than warehouse analytics, but it covers Visualization as a category.
- Argo-Bench is the most launch-relevant newcomer for a warehouse-connected product (BigQuery/Snowflake/DuckDB setup, business-persona tasks, dashboards as outputs) and has essentially no competition yet; costs are the highest in this catalog (~$0.50–$4.90 model spend per task ⇒ roughly $100–$1,000 per 210-task run plus BigQuery scan costs), and official scores require TextQL's private grading.
- BLADE, DiscoveryBench, DS-1000, Tapilot, Text2Analysis were not covered by any fetched source in this pass; their current status is unknown (see Gaps).

### Gaps
- Could not render the live DABstep HF Space; the 2026-08-26 snapshot is from a secondary aggregator. Whether Adyen has moderated or frozen the board is unknown.
- No current (2026) numbers found for BLADE, DiscoveryBench, DS-1000, Tapilot-Crossing, Text2Analysis, DA-Code leaderboard beyond DS-STAR's 38.5%.
- AgenticDataBench task count, metric and scores were not in the abstract; full paper not read.
- No Kaggle-hosted data-agent benchmark was found (Kaggle hosts FACTS, DeepSearchQA, ITBench, ARC); "Game Arena" did not surface for data agents.

---

## 4. Dashboard / chart / visualization and root-cause-analysis benchmarks

### Takeaway
Visualization benchmarks have moved from nvBench/VisEval-style single-chart generation to agentic suites (DV-World, Apr 2026: 260 tasks incl. dashboard building and clarification, SOTA <50%; DashboardQA, EACL 2026: 38.69% best) that lack live leaderboards; RCA benchmarks (OpenRCA/OpenRCA 2.0, ITBench, ORCA-bench) are telemetry-heavy SRE tasks with ~20–49% top scores, 80 GB+ data, and no public leaderboard — relevant for a "deep analysis" story only by analogy.

### Cited Findings

**Visualization / dashboards**
- DV-World (CAS Institute of Automation et al., arXiv 2604.25914, Apr 28, 2026): 260 tasks in three domains — DV-Sheet (native spreadsheet chart creation, repair, dashboard building), DV-Evol (reference image + new data → Python/Vega-Lite/D3 code), DV-Inter (ambiguous requests resolved via clarifying questions to a user simulator). Metrics: Table-value Alignment (numeric fidelity) + MLLM-as-judge with expert rubrics; interaction success rate for DV-Inter. Peak per-domain scores: DV-Sheet 40.48, DV-Evol 51.44, DV-Inter 40.43; "state-of-the-art models reach less than 50% overall". Project page dv-world-project.github.io; no leaderboard/submission described. — [arXiv 2604.25914](https://arxiv.org/html/2604.25914)
- DV-World's comparison table (sizes): ChartMimic 4,800 (image-to-code, multi-level); nvBench 2.0 7,878 (chart generation, MLLM-judge); VisEval 2,524 (multi-level); Text2Vis 1,985 (multi-level); PlotCraft 982; Plot2Code 132 (MLLM-judge); MatPlotBench 100; DA-Code 500 (table-to-chart, rule-based); DAComp-DA 210 (report/chart, rubric LLM-judge). — [arXiv 2604.25914](https://arxiv.org/html/2604.25914)
- DashboardQA (Kartha, Masry et al., arXiv 2508.17398; Findings of EACL 2026): 292 tasks on 112 interactive dashboards, 405 QA pairs across multiple-choice, factoid, hypothetical, multi-dashboard, conversational; best agent (Gemini-Pro-2.5) 38.69%, OpenAI CUA 22.69%. — [arXiv 2508.17398](https://arxiv.org/pdf/2508.17398); [ACL Anthology](https://preview.aclanthology.org/fix-8040/2026.findings-eacl.177)
- nvBench 2.0 (ambiguous text-to-vis, stepwise reasoning): 7,878 NL queries, 24,076 visualizations, 780 tables, 153 domains; arXiv revision Jan 2026. — [arXiv 2503.12880](https://arxiv.org/abs/2503.12880)
- Text2Vis (arXiv 2507.19969): criticises VisEval as "constrained by its small set of tables (146), limited chart variety, and explicit chart-type mentions in queries" and argues prior benchmarks lack "multi-step analytical reasoning" and "alignment with real-world workflows". — [arXiv 2507.19969](https://arxiv.org/pdf/2507.19969)
- ChartMimic (2024) measures chart-to-code reconstruction fidelity, not intent satisfaction; leaderboard from 2024 paper. — [arXiv 2406.09961](https://arxiv.org/pdf/2406.09961)
- A SOTA aggregator lists "RL-Text2Vis-14B" at 96 (code execution success) on nvBench test (entry 2026.01) — unverified against a primary source. — [sota2](https://www.sota2.com/research/sota/text-to-visualization-on-nvbench-test)
- TableBench includes a Visualization (VIZ) category within its 886 cases (see Section 3). — [tablebench.github.io](https://tablebench.github.io/)

**Root-cause analysis**
- OpenRCA (Microsoft, ICLR 2025): systems Telecom, Bank, Market (cloudbed-1/2); telemetry = KPI metrics, trace graphs, logs; predict root-cause datetime, component, reason; recommends ≥80 GB storage and 32 GB RAM; no leaderboard on README; custom agents may be submitted (must disclose open-source status / tools). Paper: 335 cases. — [GitHub microsoft/OpenRCA](https://github.com/microsoft/OpenRCA)
- OpenRCA 2.0 (arXiv 2606.27154, Jun 2026): 500 instances, step-wise causal annotations; across 11 frontier LLMs, exact root-cause-set recovery averages 20.7%; lenient "AnySvc" 76.0%. — [arXiv 2606.27154](https://arxiv.org/abs/2606.27154)
- Failure study on OpenRCA: 1,675 agent runs over five LLMs; 12 pitfall types; "hallucinated data interpretation and incomplete exploration persist across all models regardless of capability tier"; structural framework changes help, prompt-only changes do not. — [arXiv 2602.09937](https://arxiv.org/html/2602.09937v2)
- ITBench (IBM, ICML 2025 oral): SRE 6 scenarios / 21 mechanisms, CISO 4 categories, FinOps 1 scenario; Kubernetes-based managed environments; available on Kaggle (Dec 2, 2025); ITBench-AA launched May 27, 2026 with 59 SRE tasks "where all evaluated models scored below 50%"; submit outputs to agent-bench-automation@ibm.com. — [GitHub ibm/itbench](https://github.com/ibm/itbench)
- ORCA-bench (arXiv 2607.28545): on-call benchmark; GPT-5.5 highest at 48.8% "RCA depth" (partial credit). — [arXiv 2607.28545](https://arxiv.org/pdf/2607.28545)
- RCAEval: 735 cases across metrics/logs/traces, mostly LLM-free causal baselines. Reporting-protocol audit warns "pooled leaderboards hide system-specific winners" in offline RCA benchmarks. — [arXiv 2606.29193](https://arxiv.org/html/2606.29193v1); [arXiv 2606.29159](https://arxiv.org/pdf/2606.29159)

### Inferences
- No visualization or dashboard benchmark currently offers a public, dated leaderboard a product can "top"; DV-World is the closest fit to BoW's dashboard-building and clarification features (DV-Sheet dashboard tasks + DV-Inter simulator) and, with SOTA <50% and no entries beyond the paper, a product run would be a first-mover result — but it would be self-reported.
- Existing RCA benchmarks are SRE/observability-oriented (microservice telemetry, Kubernetes faults), not business-metric root-cause analysis ("why did revenue drop"); there is no public benchmark for business-KPI root-cause analysis, so BoW's deep-analysis capability has no direct leaderboard — the DA (open-ended, rubric-judged) halves of DAComp and Argo-Bench's FP&A/growth tasks are the nearest proxies.

### Gaps
- No live leaderboard found for nvBench 2.0, VisEval, Text2Vis, ChartMimic, Plot2Code, DashboardQA or DV-World; model-level SOTA for these is only in papers.
- No benchmark for BI-dashboard generation from a warehouse (multi-chart, filters, layout) was found other than DV-World's DV-Sheet subset and Argo-Bench's "dashboard data sources" filings.
- No business-metric root-cause-analysis benchmark was found; "RCA-bench-like" for analytics appears not to exist publicly.

---

## 5. Multi-turn / ambiguity / clarification benchmarks for data questions

### Takeaway
The active, submittable option is BIRD-Interact (ICLR 2026 Oral; user simulator, c-/a-Interact, Verified badges, <26 normalized reward); the rest (AmbiQT, AMBROSIA, AmbiSQL, CLARITY, TIDE-Bench, TACO, DySQL-Bench, PRACTIQ, nvBench 2.0, DV-World DV-Inter) are paper-only datasets without leaderboards, and CLAMBER is a general-LLM ambiguity benchmark rather than a text-to-SQL one.

### Cited Findings
- BIRD-Interact: see Section 2 — 300/600 PostgreSQL tasks, function-driven user simulator, Priority-Questions SR (ambiguity-resolution phase), Follow-Ups SR, Stress Mode with stated budget; GPT-5 8.67% c-Interact / 17.00% a-Interact; Verified badge via submitted code. — [bird-interact.github.io](https://bird-interact.github.io/); [arXiv 2510.05318](https://arxiv.org/abs/2510.05318)
- AmbiQT (Bhaskar et al., 2023) "characterizes lexical and structural ambiguity arising from overlapping schema names and join paths"; AMBROSIA (Saparina & Lapata, 2024) "grounds three linguistic ambiguity types in controlled databases". — cited in [TACO arXiv 2606.14201](https://arxiv.org/html/2606.14201v1); [AMBROSIA arXiv 2406.19073](https://arxiv.org/pdf/2406.19073)
- CLARITY (ACL 2026 Industry Track; arXiv 2604.22313, Apr 24, 2026; authors incl. Avirup Sil, Katrin Kirchhoff): framework + benchmark generating queries with "multi-faceted ambiguities and diverse user behaviors" in single- and multi-turn settings over Spider and BIRD; "leading NL2SQL systems ... degrade significantly under multi-faceted ambiguity"; no scores, code or leaderboard on abstract page. — [arXiv 2604.22313](https://arxiv.org/abs/2604.22313)
- TIDE-Bench (arXiv 2608.29543): conversational text-to-SQL under "chain ambiguity and intent drift", paired factorial design. — [arXiv 2608.29543](https://arxiv.org/pdf/2608.29543)
- AmbiSQL (arXiv 2508.15276, 2026): fine-grained ambiguity taxonomy with interactive multiple-choice resolution, two-stage clarification pipeline. — [arXiv 2508.15276](https://arxiv.org/pdf/2508.15276)
- TACO (arXiv 2606.14201, Jun 2026): open-domain text-to-SQL with ambiguous and cross-database queries; not multi-turn. — [arXiv 2606.14201](https://arxiv.org/html/2606.14201v1)
- ACL 2026 Findings: "Dynamic Multi-turn SQL Interaction for Real-world ..." — multi-turn benchmark with user simulator, dynamic evaluation and real-world databases. — [ACL Anthology 2026.findings-acl.1654](https://aclanthology.org/2026.findings-acl.1654.pdf)
- DySQL-Bench (Sun et al., 2025) evaluates dynamic refinement under evolving user intent; PRACTIQ (Dong et al., 2025) pairs ambiguous and unanswerable questions. — cited in [CLARITY](https://arxiv.org/html/2604.22313) and [TACO](https://arxiv.org/html/2606.14201v1)
- Visualization-side ambiguity: nvBench 2.0 (ambiguous text-to-vis, 7,878 queries) and DV-World DV-Inter (clarifying questions to a user simulator; peak 40.43). — [arXiv 2503.12880](https://arxiv.org/abs/2503.12880); [arXiv 2604.25914](https://arxiv.org/html/2604.25914)
- CLAMBER did not appear in any text-to-SQL result; it is a general benchmark for identifying/clarifying ambiguous information needs (not DB-specific). — search-level finding only; no primary source fetched.

### Inferences
- For a launch, BIRD-Interact is the only clarification benchmark with a maintained board and third-party verification; a-Interact (agent decides when to ask) maps directly onto BoW's chat behaviour, and absolute scores are low enough that a credible product result is newsworthy.
- "MT-Spider" does not appear to exist as a named benchmark; the multi-turn successors of Spider are SParC/CoSQL (older) and the 2026 simulator-based suites above.

### Gaps
- No scores/leaderboards for CLARITY, TIDE-Bench, TACO, AmbiSQL, DySQL-Bench, PRACTIQ were found in abstracts fetched.
- AmbiQT and CLAMBER primary pages were not fetched; sizes and current SOTA unknown.

---

## 6. Benchmarks used by competing products in launch posts

### Takeaway
Warehouse vendors (Snowflake, Databricks) launch on internal, unpublished question sets and position against "frontier coding agents via MCP" rather than public boards; public leaderboards are used instead by Oracle OCI (Spider 2.0-Lite), Snowflake AI Research (Spider 2.0 via ReFoRCE/Arctic, BIRD via Arctic-Text2SQL), Databricks (BIRD single-model RLVR 32B), Google (BIRD CHASE-SQL/Gemini-SQL, DABstep/KramaBench/DA-Code via DS-STAR), NVIDIA (DABstep), C3 AI (LiveSQLBench, Spider 2.0), JetBrains Databao (Spider 2.0), and many Chinese data-agent teams (TableBench, DABstep, Spider 2.0).

### Cited Findings
- Snowflake Cortex Sense (blog Jun 30, 2026; announced Jun 2, 2026 Summit, private preview): "Cortex Sense improved accuracy from 24.1% to 86.3% on our benchmark" and "reduced costs from $1.76 to $0.59 per query" vs a frontier agent; Snowflake's data team measured ~25% with no context layer and "Anthropic independently measured 21%"; internal "hard questions" product-analytics set, size unstated; Spider 2.0/BIRD not mentioned. — [Snowflake blog](https://www.snowflake.com/en/blog/enterprise-ai-agents-grounded-context/)
- Secondary coverage of Cortex Sense: CoWork/CoCo 83% with Sense vs 47% without vs 23% for frontier coding agents using only Snowflake's MCP connector. — [Atlan explainer](https://atlan.com/know/snowflake/snowflake-cortex-sense/); [TechTimes](https://www.techtimes.com/articles/318625/20260618/snowflake-agentic-ai-beats-claude-code-its-own-benchmark-what-that-means.htm)
- Databricks Genie One (Data + AI Summit, Jun 16, 2026): internal benchmark of 28 real business questions — Genie One 84.5% first-try accuracy vs 52.4% for "the strongest general-purpose coding agent" and 25% for a context-starved agent; competitors anonymized; no independent verification. — [HPCwire/BigDataWire](https://www.hpcwire.com/bigdatawire/this-just-in/databricks-launches-genie-one-all-new-agentic-coworker-for-every-team/); [Pebblous analysis](https://blog.pebblous.ai/blog/databricks-genie-one-governed-data/en/)
- Databricks Genie Code (Mar 2026): "on its internal benchmark of real-world data science tasks, Genie Code more than doubles the success rate of leading coding agents"; secondary report gives 32.1%→77.1%. — [Databricks blog: Introducing Genie Code](https://databricks.com/blog/introducing-genie-code)
- Databricks also publishes a customer-facing "Genie space benchmarks" feature (users author their own question sets). — [Databricks blog](https://www.databricks.com/blog/building-confidence-your-genie-space-benchmarks-and-ask-review)
- Oracle OCI: "OCI Generative AI Tops Spider 2.0 Lite" with SOMA-SQL 72.02% EX@1. — [Oracle blog](https://blogs.oracle.com/cloud-infrastructure/oci-gen-ai-tops-spider-2-lite)
- Snowflake + Hao AI Lab announced #1 on Spider 2.0 (Jan 2025, ReFoRCE); Snowflake AI Research holds Arctic-FLEX (75.14 Snow), DivSkill-SQL (73.13 Lite) and Arctic-Text2SQL-R1-32B (73.84 BIRD single-model). — [X: Baris Gultekin](https://x.com/barisg/status/1879001356851372177); [spider2-sql.github.io](https://spider2-sql.github.io/); [bird-bench.github.io](https://bird-bench.github.io/)
- Databricks RLVR 32B appears on BIRD single-trained-model track at 75.68. — [bird-bench.github.io](https://bird-bench.github.io/)
- Google: CHASE-SQL + Gemini 76.02 BIRD test (Apr 3, 2026), Gemini-SQL2 80.04 single-model; DS-STAR blog claims SOTA on DABStep (45.2%), KramaBench (44.7%), DA-Code (38.5%) (Nov 6, 2025). — [bird-bench.github.io](https://bird-bench.github.io/); [Google Research blog](https://research.google/blog/ds-star-a-state-of-the-art-versatile-data-science-agent/)
- NVIDIA (NeMo Agent Toolkit) launch-style post: #1 on DABstep Hard (89.95) with Haiku 4.5, 30x faster than Claude Code + Opus 4.5 (Mar 13, 2026). — [NVIDIA HF blog](https://huggingface.co/blog/nvidia/nemo-agent-toolkit-data-explorer-dabstep-1st-place)
- C3 AI "DIA (Data Intelligence Agents)" tops LiveSQLBench (48.00, 2026-05-29) and appears on Spider 2.0-Snow (69.47) and DBT (37.50). JetBrains Databao appears on Spider 2.0-Lite (69.65) and DBT (60.29). AT&T CDO + RelationalAI "Ask Data" on Snow (86.28) and BIRD (81.95). — [livesqlbench.ai](https://livesqlbench.ai/); [spider2-sql.github.io](https://spider2-sql.github.io/); [bird-bench.github.io](https://bird-bench.github.io/)
- ByteDance Lark Base, Kingsoft WPS AI, JD, SenseTime, Alibaba, CMCC post on TableBench; ByteDance Lark Base, OceanBase, Genesis Computing, H2O.ai, Zoom, getdot.ai post on DABstep. — [tablebench.github.io](https://tablebench.github.io/); [benchmarklist DABstep snapshot](https://benchmarklist.com/benchmarks/dabstep/)
- Independent small head-to-head: Snowflake Cortex Agent 26.2/30 vs Databricks Genie 19.5/30 on 30 clinical-trial questions (single individual's repo). — [GitHub curious-bigcat](https://github.com/curious-bigcat/cortex-agent-vs-databricks-genie)

### Inferences
- The dominant 2026 launch narrative is "our context layer beats a frontier coding agent (Claude Code / MCP-only) on real enterprise questions", with vendor-internal sets of 28–150 questions; public boards are used by labs and smaller vendors. An open-source product can differentiate by doing both: a verified public-board result plus an open, reproducible internal set.
- The recurring baseline across vendors (Snowflake 24.1%/21%, Databricks 25%/52.4%, Spider 2.0-Lite Claude Code 61.20, DABstep Claude Code 66.93, LiveSQLBench Claude Code/OpenHands 35–40) is "generic coding agent with raw DB access" — this is the comparator to beat in any BoW launch chart.

### Gaps
- Databricks' primary Genie One announcement blog could not be located; numbers come from press coverage. One outlet conflated the 52.4% figure with a BIRD score — unresolved.
- Oracle blog returned 403; publication date not confirmed.

---

## 7. Realistic cost (API $ and hours) to run each benchmark end-to-end through an agent product

### Takeaway
Published per-task costs range from ~$0.004 (small models on LiveSQLBench) to $4.71 (Claude Opus 5.5 on Argo-Bench); full-run model spend is roughly $2–$435 for DABstep (450 tasks), ~$25–$400 for LiveSQLBench Base-Full (600), ~$25–$360 for BIRD-Interact Full (600), and ~$100–$1,000+ for Argo-Bench (210) excluding warehouse compute; Spider 2.0-Snow/DBT data access is free but Lite incurs BigQuery charges, and nobody publishes wall-clock figures except NVIDIA's 20 s vs 10 min per DABstep task.

### Cited Findings
- DABstep full-benchmark costs (paper Table 2): o1 $435 ($0.967/task); Claude 3.5 Sonnet $90; o3-mini $85; GPT-4o $50; Claude 3.5 Haiku $35; GPT-4o-mini $3; DeepSeek R1 $3; DeepSeek V3 $2. HF offered 1k free inference requests/day. — [HF blog: DABstep](https://huggingface.co/blog/dabstep)
- DABstep time: NVIDIA Data Explorer 20 s/task vs Claude Code + Opus 4.5 10 min/task ("30x speedup"). — [NVIDIA blog](https://huggingface.co/blog/nvidia/nemo-agent-toolkit-data-explorer-dabstep-1st-place)
- LiveSQLBench Cost/Task (Base): Jitto Build (Claude Opus 5.5 high) $0.6731; GPT-5.5 xhigh $0.2220; o1 $0.2283; o3 $0.1583; Claude Opus 4.6 $0.0979; GPT-5.5 low $0.0896; Claude Sonnet 4.5 $0.0598; Gemini 3.1 Pro $0.0507; GPT-5 $0.0216; Kimi K2.6 $0.0364; Qwen3 Coder 480B $0.0038. Large-v1 prompts average ~84K tokens. — [livesqlbench.ai](https://livesqlbench.ai/)
- BIRD-Interact avg cost/task (Full): c-Interact $0.04 (Gemini-2.5-Pro) to $0.29 (Claude Sonnet); a-Interact $0.06 (o3-mini/DeepSeek) to $0.60 (Claude-Sonnet-3.7); 1,968–5,496 interaction turns per evaluation; DB init "several minutes"; ADK implementation supports parallel execution. — [GitHub bird-bench/BIRD-Interact](https://github.com/bird-bench/BIRD-Interact)
- Argo-Bench avg API spend per task (excl. BigQuery): Claude Opus 5.5 $4.71; Muse Spark 1.3 $4.89; Kimi K3 $4.52; Gemini 3.8 Flash $3.94; Claude Sonnet 5.5 $3.74; GPT-6 Astra $2.71; GPT-6.1 Sol $0.49; Claude Haiku 4.5 $0.20; GPT-6 Luna $0.06. — [arXiv 2610.02122](https://arxiv.org/html/2610.02122v1)
- Spider 2.0: Snow and DBT "NO COST!" for data access (shared Snowflake account, queued; account suspended Aug 2026); Lite "Some cost incurred" (own BigQuery credentials). ReFoRCE: 3.52 LLM calls + 3.89 DB calls per example; 1.69 calls / 15K tokens per example without column exploration. — [xlang-ai/Spider2](https://github.com/xlang-ai/Spider2); [ReFoRCE](https://arxiv.org/pdf/2502.00675)
- Snowflake's own comparison: frontier agent $1.76/query vs $0.59 with Cortex Sense (internal, Jun 2026). — [Snowflake blog](https://www.snowflake.com/en/blog/enterprise-ai-agents-grounded-context/)
- Benchmark-auditing cost reference: SAR-Agent ~$0.44 (BIRD) and $1.11 (Spider 2.0-Snow) per example. — [arXiv 2601.08778v3](https://arxiv.org/html/2601.08778v3)
- BIRD test evaluation turnaround ~10 days by email; BIRD-Interact/CRITIC GT delivered automatically within 30 minutes; DABstep grades instantly. — [bird-bench.github.io](https://bird-bench.github.io/); [bird-interact.github.io](https://bird-interact.github.io/); [HF blog: DABstep](https://huggingface.co/blog/dabstep)
- OpenRCA needs ≥80 GB storage / 32 GB RAM for telemetry; ITBench needs Kubernetes environments (IBM offers managed ones). — [GitHub microsoft/OpenRCA](https://github.com/microsoft/OpenRCA); [GitHub ibm/itbench](https://github.com/ibm/itbench)

### Inferences
- Order-of-magnitude budget for a 3–5 benchmark launch sweep with a frontier model through an agent product: Spider 2.0-Lite (547 tasks; ReFoRCE-like ~3.5 LLM calls/task, agentic products typically 10–30 calls/task) likely $100–$600 in model spend plus BigQuery scan costs; Spider 2.0-DBT (68 tasks) <$100; LiveSQLBench Base-Full (600) $60–$400; BIRD-Interact Full (600) $25–$360; DABstep (450) $50–$450; Argo-Bench (210) $100–$1,000 + BigQuery. Wall-clock is dominated by per-task agent latency (DABstep baseline 10 min/task with Claude Code ⇒ ~75 hours serial for 450 tasks; parallelism cuts this) and by environment setup (Docker/PostgreSQL for BIRD suites; Snowflake/BigQuery credentials for Spider 2.0).
- Costs scale with context strategy: LiveSQLBench-Large's ~84K-token prompts and Argo-Bench's 235-table warehouse make schema-retrieval/semantic-layer efficiency (a BoW strength) directly visible in $/task columns that both boards publish.

### Gaps
- No source publishes wall-clock hours for a full Spider 2.0, BIRD, LiveSQLBench or BIRD-Interact run.
- No per-task dollar figure for Spider 2.0 (any track) exists in the sources found; only call/token counts.
- BigQuery scan cost for Spider 2.0-Lite / Argo-Bench is not quantified anywhere.

---

## 8. Short-list signal for a BoW launch (synthesis across sections)

### Takeaway
Based on saturation, verifiability, dialect fit (Snowflake/BigQuery/PostgreSQL/DuckDB) and cost, the benchmarks with the best launch value are LiveSQLBench (Verified badge, agent category, top 48.00, published $/task), Spider 2.0-Lite and/or -DBT (brand recognition, warehouse dialects, coding-agent comparator at 61.20 Lite), BIRD-Interact (clarification story, Verified badge, ICLR 2026 Oral), Argo-Bench (new, enterprise warehouse, dashboards/FP&A tasks, no competition), with DABstep usable only as a cheap internal regression suite because its public board is saturated and unverified.

### Cited Findings
- (All supporting numbers are cited in Sections 1–7 above; key anchors: Spider 2.0-Snow top 96.70 / Lite 76.23 / DBT 65.60 — [spider2-sql.github.io](https://spider2-sql.github.io/); LiveSQLBench top 48.00 with Verified/Reported labels — [livesqlbench.ai](https://livesqlbench.ai/); BIRD-Interact Verified badge and a-Interact GPT-5 17.00% — [bird-interact.github.io](https://bird-interact.github.io/); DABstep 16 entries at 100 Hard — [benchmarklist snapshot](https://benchmarklist.com/benchmarks/dabstep/); Argo-Bench Claude Opus 5.5 34.8% solved — [arXiv 2610.02122](https://arxiv.org/html/2610.02122v1); TableBench top 80.64 vs human 85.91 — [tablebench.github.io](https://tablebench.github.io/).)

### Inferences
- Verification matters for credibility: only the BIRD-team suites (LiveSQLBench, BIRD-Interact, BIRD-CRITIC) publicly distinguish "Verified" from "Reported"; Spider 2.0 says nothing; DABstep and TableBench accept unverified submissions.
- For a "self-improving instruction loop" story, LiveSQLBench's Business Rule Drift (Large-v1), BIRD-Interact's HKB-driven ambiguity, and Argo-Bench's hidden business rules are the benchmarks whose design rewards learned context rather than raw model capability.

### Gaps
- No single source compares these benchmarks' verification policies; the comparison above is assembled from each site's own text.
