# Harness-vs-Model Evidence: How Coding-Agent Companies Prove the Scaffold Matters (as of 2026-10-08)

Scope note: this file compiles evidence that an agent *harness* (scaffold, tools, prompts, context engineering) changes benchmark outcomes independently of the underlying model, plus the benchmark designs and presentation patterns that make that separation visible. Every number is dated and linked. Items marked **[post-cutoff, from search results]** describe models/benchmarks released after mid-2026 that I could only verify via the cited page, not independently. Two OpenAI pages (openai.com/index/gdpval and openai.com/index/why-we-no-longer-evaluate-swe-bench-verified) returned HTTP 403; I substituted the arXiv paper and press coverage and flag that below.

---

## Key Question 1: Terminal-Bench (1.0 / 2.0 / 2.1) — how it separates agent (harness) from model, with same-model-different-harness numbers

### Takeaway
Terminal-Bench is the clearest public instrument for the harness-vs-model story: every leaderboard row is an (agent, model) pair, each pair is run at least five times with 95% CIs, and the maintainers ship a deliberately minimal reference scaffold (Terminus 2) precisely so that model comparisons are not confounded by vendor harness tuning. The published data show the same model swinging 8–30 percentage points across harnesses (e.g., GPT-5.4 mini: 66.1% in Codex CLI vs 36.9% in Terminus 2 on TB 2.1), while the paper's authors still conclude that model choice is *usually* the bigger lever than scaffold choice.

### Cited Findings

**Benchmark design (primary: the Terminal-Bench paper, arXiv 2601.11868, posted 17 Jan 2026)**
- Terminal-Bench 2.0 has 89 tasks selected from 229 contributed by 93 contributors; each model-and-agent combination was run "at least five times"; the study comprises 32,155 trials total — [Terminal-Bench paper, arXiv 2601.11868](https://arxiv.org/html/2601.11868)
- Figure 1 and Table 2 report resolution rates with 95% confidence intervals; the Figure 1 caption states: "The agent scaffold used to report each model was chosen to maximize performance." — [Terminal-Bench paper](https://arxiv.org/html/2601.11868)
- Terminus 2 was built as "a neutral testbed for comparing model performance," with a single headless-terminal tool and Bash-only actions; the authors note many scaffolds are engineered for particular models, making agent and model performance "hard to decouple." — [Terminal-Bench paper](https://arxiv.org/html/2601.11868)
- Section 4 of the paper concludes "model selection is usually more important than agent scaffold when optimizing for performance," based on comparisons such as Codex CLI with GPT-5.2 vs GPT-5-Nano, and Gemini 2.5 Pro under Terminus 2 vs OpenHands. — [Terminal-Bench paper](https://arxiv.org/html/2601.11868)
- A later training paper states that "agent and model performance are hard to decouple" because "many agent scaffolds have been engineered to accommodate the tendencies of certain models," and that Terminus-2 "is designed to eliminate this confound." — [What Makes Interaction Trajectories Effective for Training Terminal Agents? arXiv 2606.03461 (June 2026)](https://arxiv.org/pdf/2606.03461) **[post-cutoff, from search results]**
- The official leaderboard page's chart caption says "The whiskers span the 95% confidence interval"; column headers are RANK, MODEL, AGENT, RESOLUTION RATE, COST, TOKENS (rows did not render in my fetch). — [tbench.ai leaderboard](https://www.tbench.ai/leaderboard/terminal-bench/2.0)
- Submission protocol summarized by an aggregator: five trials per task (-k 5), default task constraints, no overriding of timeouts or CPU/memory/storage limits. — [Snorkel TB 2.0 leaderboard page](https://snorkel.ai/leaderboard/terminal-bench-2-0/)
- Terminal-Bench 1.0 was introduced publicly on 19 May 2025 alongside Terminus as a research-preview neutral scaffold (secondary reference article). — [systems-analysis.ru Terminal-Bench entry](https://systems-analysis.ru/eng/Terminal-Bench)

**Same model, different harness — Terminal-Bench 2.0 (paper Table 2, Jan 2026)**
All numbers from [Terminal-Bench paper Table 2, arXiv 2601.11868](https://arxiv.org/html/2601.11868):
- GPT-5.2: Codex CLI 62.9% vs Terminus 2 54.0% (+8.9 pp for vendor harness)
- Claude Opus 4.5: Terminus 2 57.8%, Claude Code 52.1%, OpenHands 51.9% (neutral scaffold beat vendor harness by 5.7 pp)
- GPT-5: Codex CLI 49.6%, OpenHands 41.5%, Terminus 2 35.2%, Mini-SWE-Agent 33.9% (15.7 pp spread)
- Claude Sonnet 4.5: Terminus 2 42.8%, Mini-SWE-Agent 42.5%, OpenHands 40.3%, Claude Code 40.1% (2.7 pp spread)
- Claude Opus 4.1: Terminus 2 38.0%, Mini-SWE-Agent 35.1%, OpenHands 34.9%, Claude Code 34.8%
- GPT-5-Mini: Codex CLI 31.9%, OpenHands 27.7%, Terminus 2 24.0%, Mini-SWE-Agent 22.2% (9.7 pp spread)
- Claude Haiku 4.5: Mini-SWE-Agent 29.8%, Terminus 2 28.3%, Claude Code 27.5%, OpenHands 13.3% (16.5 pp spread; OpenHands collapsed)
- Gemini 2.5 Pro: Terminus 2 32.6%, Mini-SWE-Agent 26.1%, Gemini CLI 19.6%, OpenHands 15.7% (16.9 pp spread; vendor CLI was second-worst)
- Gemini 2.5 Flash: Mini-SWE-Agent 17.1%, Terminus 2 16.9%, OpenHands 15.5%, Gemini CLI 15.4%
- Grok 4: Mini-SWE-Agent 29.0%, Terminus 2 23.4%, OpenHands 19.6%
- Grok Code Fast 1: Mini-SWE-Agent 24.5%, Terminus 2 14.5% (10 pp from a 100-line scaffold vs the reference)
- GPT-5-Nano: Codex CLI 11.5%, OpenHands 9.5%, Terminus 2 7.9%, Mini-SWE-Agent 7.0%
- Kimi K2 Instruct: Terminus 2 27.8%, OpenHands 25.6%; Qwen 3 Coder 480B: OpenHands 24.3%, Terminus 2 23.9%; GPT-OSS-120B: Terminus 2 18.7%, Mini-SWE-Agent 14.2%; GPT-OSS-20B: Mini-SWE-Agent 3.4%, Terminus 2 3.1%

**Same model, different harness — Terminal-Bench 2.0 → 2.1 (official tbench.ai release note)**
All from [tbench.ai news: Terminal-Bench 2.1](https://www.tbench.ai/news/terminal-bench-2-1) **[post-cutoff, from search results]**; TB 2.1 fixed issues in 28 of 89 TB 2.0 tasks (9 external-dependency, 8 resource-mismatch, plus misspecification), and the page says no task is unsolved in 2.1:
- Opus 4.6: Claude Code 58.0% → 70.1%; Terminus 2 62.9% → 63.8% (TB 2.0: neutral scaffold +4.9 pp over vendor; TB 2.1: vendor +6.3 pp — the *sign flipped* after task fixes)
- GPT-5.3-Codex: Codex CLI 73.3% → 79.1%; Terminus 2 64.7% → 68.5% (vendor harness +8.6 / +10.6 pp)
- Gemini 3.1 Pro: Terminus 2 63.0% → 70.7%; Gemini CLI 61.3% → 67.1% (neutral scaffold beats vendor CLI by 1.7 / 3.6 pp)
- GPT-5.4: Codex CLI 76.0% → 77.3%; Terminus 2 55.1% → 54.8% (vendor harness +20.9 / +22.5 pp)
- Sonnet 4.6: Claude Code 51.9% → 58.5%; Terminus 2 48.0% → 51.5%
- Gemini 3 Flash: Gemini CLI 47.4% → 56.9%; Terminus 2 51.7% → 54.2%
- GPT-5.4 mini: Codex CLI 57.8% → 66.1%; Terminus 2 37.8% → 36.9% (vendor harness +20.0 / +29.2 pp — the largest same-model gap in the official table)
- Snorkel warns TB 2.1 scores should not be treated as interchangeable with TB 2.0 scores. — [Snorkel TB 2.1 page](https://snorkel.ai/leaderboard/terminal-bench-2-1/)

**Terminal-Bench 2.0 leaderboard snapshot (aggregator, 142 entries total)**
From [Snorkel TB 2.0 leaderboard](https://snorkel.ai/leaderboard/terminal-bench-2-0/) **[post-cutoff, from search results]** (page did not show its own snapshot date):
- Top 10: NexAU-AHE + GPT-5.5 84.7% ±2.1; LemonHarness (multiple models) 84.5% ±2.6; Capy + GPT-5.5 83.1% ±2.1; Codex CLI + GPT-5.5 82.2% ±2.2; Polaris (multiple) 82.2% ±2.8; WOZCODE + Claude Opus 4.7 80.2% ±2.1; TongAgents + Gemini 3.1 Pro 80.2% ±2.6; LemonHarness 79.9% ±3; SageAgent + GPT-5.3-Codex 78.4% ±2.2; Droid + GPT-5.3-Codex 77.3% ±2.2.
- GPT-5.5 appears under three harnesses in the top 10 with a 2.5 pp spread (84.7 / 83.1 / 82.2); Snorkel's commentary: "the rank order is real, but the gaps are smaller than they look" because top-10 CIs overlap heavily; provider counts are "Tagged by backbone model, not by agent harness."
- A March 2026 snapshot by Morph listed Forge Code + Gemini 3.1 Pro at 78.4% on top and Droid + GPT-5.3-Codex second at 77.3%, noting "The same model can appear multiple times with different agents, making scaffolding quality visible in the data." — [morphllm.com/terminal-bench-2](https://www.morphllm.com/terminal-bench-2) (search snippet; direct fetch returned HTTP 429)

**Secondary same-model comparisons (blog, April 2026; treat as indicative)**
From [Daniel Vaughan, "Harness Performance on Terminal-Bench: Why Scaffolding Matters More Than Model Choice," published 9 Apr 2026, updated 8 Oct 2026](https://codex.danielvaughan.com/2026/04/09/harness-performance-terminal-bench/): Claude Opus 4.6 at 58.0% in Claude Code (rank #33), 74.7% in Terminus-KIRA, 76.4% in "Meta-Harness" (described as a Stanford research harness), 81.8% in ForgeCode (23.8 pp above Claude Code); Claude Haiku 4.5 at 37.6% in Meta-Harness vs 35.5% for next-best Goose; author asserts model generations typically differ by only 2–3 points (no source given) and reports no CIs. The author mixes leaderboard, DEV Community, and X-post sources, so the figures are not independently verified.

**Terminal-Bench 1.0 — Factory's same-model comparison (vendor post, 25 Sep 2025)**
- Droid + Claude Opus 4.1 (no thinking) 58.8% vs Claude Code + Opus 4.1 43.2% (+15.6 pp); Droid + GPT-5 (medium) 52.5% vs Codex CLI + GPT-5 42.8% (+9.7 pp); Droid + Sonnet 4 (no thinking) 50.5%, which Factory says beat every other agent running Opus. Each model run five times, all runs submitted; no variance, SD, or CI reported; no cost figures. Three charts: leaderboard, per-model accuracy across agents, tool-usage by model. — [Factory, "Terminal-Bench" post, 25 Sep 2025](https://factory.com/news/terminal-bench)
- Factory's thesis: "agent design, not model choice alone, is decisive," attributing gains to prompting, tool design, environment exploration, and speed optimizations. — [Factory post](https://factory.com/news/terminal-bench)

**Terminal-Bench 1.0 / 2.0 — Warp**
- Warp's agent scored 52% on Terminal-Bench v0.1.1 (June 2025), run by feeding each task to Warp as a CLI argument via a cross-compiled headless build. — [Warp, "How we scored #1 on Terminal-Bench (52%)"](https://www.warp.dev/blog/terminal-bench)
- Warp + GPT-5.2 scored 61.14% on Terminal-Bench 2.0, "#2 overall," after the team "fine-tuned prompt structure, tool definitions, and planning heuristics with OpenAI" (11 Dec 2025). — [Warp GPT-5.2 post](https://www.warp.dev/blog/gpt-5-2-support-terminal-bench-improvement)

**Model-harness interaction study across three benchmarks (arXiv 2610.00917, 1 Oct 2026) [post-cutoff, from search results]**
- "Choosing an agent system means choosing both a language model and the harness through which it acts." Model rankings reverse across harnesses; for 4 of 5 models the best harness changes by benchmark; a model's native vendor harness is not reliably its best ("Codex never gives GPT its highest score"). — [Finding the Right Fit: Model–Harness Interactions across Agent Tasks, NTU](https://arxiv.org/html/2610.00917v1)
- Terminal-Bench 4 (63 tasks): Claude Opus 5 scored 57.14 (OpenHands), 49.21 (Claude Code), 41.27 (openJiuwen), 34.92 (DSH), 30.16 (PI) — a 27 pp spread; GPT-6 Astra scored 60.32 (PI), 55.56 (Codex), 53.97 (openJiuwen), 52.38 (DSH), 49.21 (OpenHands). The Claude-minus-GPT gap ranged from +7.94 (OpenHands) to −30.16 (PI): *which model is "better" depends on the harness.* — [arXiv 2610.00917](https://arxiv.org/html/2610.00917v1)
- Cost does not buy score: GPT-6 Astra cost $4.66/task under PI (60.32%) vs $19.94/task under DSH (52.38%). — [arXiv 2610.00917](https://arxiv.org/html/2610.00917v1)
- Methodology limits the authors acknowledge: one counted run per task, no CIs, harness defaults not matched, so "causal effects of single components are not isolated." — [arXiv 2610.00917](https://arxiv.org/html/2610.00917v1)

### Inferences
- The cleanest "harness matters" exhibit available is the official TB 2.1 table: identical model, identical tasks, two scaffolds, published side by side by the benchmark maintainer — e.g., GPT-5.4 mini 66.1% vs 36.9%. That is the template Bag of Words should copy: a neutral reference scaffold column next to a product-harness column.
- The TB 2.0→2.1 Opus 4.6 sign flip (Terminus beat Claude Code on 2.0; Claude Code beat Terminus on 2.1) shows that harness deltas are fragile to task-set quality; any BOW claim should be reported on a frozen, audited task set with the version stated.
- Harness effects are largest for small/cheap models (GPT-5.4 mini +29 pp; GPT-5 +15.7 pp spread) and smallest at the very top (GPT-5.5 2.5 pp spread across three harnesses). This directly supports the "cheaper model + good harness" narrative and cautions against claiming large harness deltas on frontier models.
- Vendor harnesses are not reliably best for their own models (Opus 4.5 and Gemini 2.5 Pro lost to Terminus 2 on TB 2.0; Codex "never gives GPT its highest score" in the NTU study). A neutral third-party scaffold can therefore serve as a credible baseline rather than looking like a strawman.
- The paper's own conclusion that "model selection is usually more important" is the counter-narrative BOW must pre-empt: the honest framing is "harness is the lever you control; it moves results by as much as a model generation for mid-tier models."

### Gaps
- I could not render the live tbench.ai leaderboard rows (page returned headers only) or Morph's snapshot (HTTP 429), so the exact current (Oct 2026) same-model/different-agent rows and their standard errors come from Snorkel's aggregator and the TB 2.1 release note rather than the primary table.
- No source quantifies the harness contribution as a variance decomposition (ANOVA/effect size) across the Terminal-Bench corpus; the NTU paper explicitly says it did not do this, and cites one ALE-Claw analysis (not retrieved) that found a larger spread across models than across harnesses.
- Harness versions and submission dates for individual leaderboard entries were not retrievable.

---

## Key Question 2: SWE-bench family — scaffold effects, published ablations, and vendor scaffolding notes

### Takeaway
On SWE-bench the published scaffold effect is smaller than on Terminal-Bench — typically 3–5 pp between a bash-only mini-scaffold and a richer one, 7–11 pp vs a raw shell in the 2024 SWE-agent ablation, but up to +14 pp from a single edit tool on a small open model (Qwen3.6-27B on SWE-bench Pro). Anthropic's own scaffolding note is the canonical vendor statement that "performance can vary significantly based on this scaffolding" with the same model; meanwhile OpenAI (Feb 2026) and Epoch (Sep 2026) have declared SWE-bench Verified contaminated/flawed, which pushes launch narratives toward SWE-bench Pro and private holdouts.

### Cited Findings

**Original scaffold ablation (SWE-agent paper, NeurIPS 2024)**
- The SWE-agent paper's ablation on a 300-instance SWE-bench Lite subset found its agent-computer interface beat a plain Linux-shell baseline by 10.7 pp; removing the dedicated editor reduced Lite resolution from 18.0% to 10.3%. Secondary summaries disagree on the exact shell-only baseline (11.00% vs 10.3%), so quote Table 2 of the paper directly. — [SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering, arXiv 2405.15793](https://arxiv.org/pdf/2405.15793)

**Anthropic's scaffolding disclosure (30 Oct 2024)**
- Claude 3.5 Sonnet (new) scored 49% on SWE-bench Verified vs prior SOTA 45%, Claude 3.5 Sonnet (old) 33%, Claude 3 Opus 22%, all with the same scaffold. — [Anthropic, "Raising the bar on SWE-bench Verified"](https://www.anthropic.com/news/swe-bench-sonnet)
- Scaffold: a short non-prescriptive task prompt, two tools (Bash; `str_replace_editor` with absolute-path requirement and exactly-one-match semantics), sampled until done or 200k context, built on the SWE-Agent framework. Philosophy: "give as much control as possible to the language model itself, and keep the scaffolding minimal." — [Anthropic SWE-bench post](https://www.anthropic.com/news/swe-bench-sonnet)
- Explicit statement that performance "can vary significantly based on this scaffolding" even with the same model, and that "much more attention should go into designing tool interfaces for models." The post discloses no ablation numbers, trial counts, or pass@1 vs pass@k. — [Anthropic SWE-bench post](https://www.anthropic.com/news/swe-bench-sonnet)
- Erik Schluntz (Anthropic) described the architecture on Latent Space as a minimal, non-overengineered SWE-Agent variant whose edit tool was later released with computer use. — [Latent Space, "The new Claude 3.5 Sonnet, Computer Use, and Building SOTA Agents"](https://www.latent.space/p/claude-sonnet)
- For the Claude 4 family Anthropic described a "simple scaffold" with two tools (bash + string-replacement file editor), dropped the planning tool used with Claude 3.7 Sonnet, reported scores on the full 500 problems, and raised the step limit from 30 to 100 completions for Opus 4.1. — [Anthropic, Claude Opus 4.1 announcement](https://www.anthropic.com/news/claude-opus-4-1)

**Minimal vs richer scaffold, same model (Live-SWE-agent, arXiv 2511.13646, Nov 2025 v1 / v3)**
SWE-bench Verified, full 500 problems, single attempt — [Live-SWE-agent Table 1](https://arxiv.org/html/2511.13646v3):
- mini-SWE-agent: GPT-5-Mini 59.8% ($0.04), GPT-5 65.0% ($0.28), Claude 4.5 Sonnet 70.6% ($0.56), Gemini 3 Pro 74.2% ($0.46)
- Live-SWE-agent (starts bash-only, builds its own tools on the fly): GPT-5-Mini 63.0% ($0.05), GPT-5 68.4% ($0.27), Claude 4.5 Sonnet 75.4% ($0.68), Gemini 3 Pro 77.4% ($0.48) → +3.2 / +3.4 / +4.8 / +3.2 pp (my arithmetic from the table)
- On Verified-60, Live-SWE-agent + GPT-5-Mini 65.0% vs prior self-improving best HGM 56.7% (+8.3 pp, the paper's only explicitly stated pp gain), with 0 hours offline cost vs 512 hours. — [Live-SWE-agent Table 2](https://arxiv.org/html/2511.13646v3)
- Scaffold gains are model-dependent and can reverse: on 50 problems, GPT-5-Nano fell from 44.0% (mini-SWE-agent) to 14.0% (Live-SWE-agent), a 30 pp absolute drop, attributed to the model not understanding tool creation and looping; GPT-5-Mini 60→58; GPT-5 60→68; Claude 3.7 Sonnet 46→50; Claude 4 Sonnet 58→64; Claude 4.5 Sonnet 62→76. — [Live-SWE-agent Table 5](https://arxiv.org/html/2511.13646v3)
- SWE-bench Pro: SWE-agent + Claude 4.5 Sonnet 43.6% vs Live-SWE-agent 45.8% ($0.73). — [Live-SWE-agent Table 3](https://arxiv.org/html/2511.13646v3)
- mini-SWE-agent's own README: a ~100-line agent that "scores >74% on SWE-bench verified." — [SWE-agent/mini-swe-agent GitHub](https://github.com/swe-agent/mini-swe-agent)

**Richer tooling does not always help (CodeClash, SWE-Serve)**
- Swapping mini-SWE-agent for SWE-agent in CodeClash produced identical scores in 4 of 6 cases and marginal gains in 2; authors say models rarely invoked the extra navigation tools on small codebases. — [CodeClash, arXiv 2511.00839](https://arxiv.org/pdf/2511.00839)
- On SWE-Serve, mini-SWE-agent reached 75.5% in both configurations vs 73.6% for GPT-5.6 Sol + Codex and 69.8% for Opus 5 + Claude Code (minimal scaffold matched or beat vendor harnesses). — [SWE-Serve, arXiv 2609.26777](https://arxiv.org/pdf/2609.26777) **[post-cutoff, from search results]**

**Single-tool effects on small open models (community reproductions, 2026) [post-cutoff, from search results]**
- Qwen3.6-27B SWE-bench Pro reproduction: ~28% with a bash-only agent vs 50.7% after adding a `str_replace` edit tool (published figure 53.5%). — [Qwen3.6 issue #179](https://github.com/QwenLM/Qwen3.6/issues/179), cited in [Sukhareva, "How a Small Open Model Beat a Frontier LLM," 4 Aug 2026](https://msukhareva.substack.com/p/how-a-small-open-model-beat-a-frontier)
- Qwen3.6-27B run inside the Claude Code CLI by an independent researcher: 90.0% SWE-bench Verified vs Sonnet 4.6's published 79.6%. — [Qwen3 discussion #1846](https://github.com/QwenLM/Qwen3/discussions/1846) via [Sukhareva](https://msukhareva.substack.com/p/how-a-small-open-model-beat-a-frontier)
- MiniMax-M3 model card reports 80.5% SWE-bench Verified using Claude Code as scaffolding (four-run average). — [MiniMax-M3 HF card](https://huggingface.co/MiniMaxAI/MiniMax-M3) via [Sukhareva](https://msukhareva.substack.com/p/how-a-small-open-model-beat-a-frontier)

**Benchmark validity and subset disclosures**
- OpenAI's GPT-5 system card: "all SWE-bench evaluation runs use a fixed subset of n=477 verified tasks which have been validated on our internal infrastructure," averaging 4 tries per instance for pass@1. — [GPT-5 System Card, arXiv 2601.03267](https://arxiv.org/pdf/2601.03267)
- Epoch AI evaluates 484 of 500 samples (16 excluded as unreliable), notes GPT-5 used 477 and Sonnet 3.7 used 489 while later Anthropic releases used all 500, uses a simple one-action-per-turn loop with bash + text_editor + apply_patch, and also runs third-party scaffolds (Claude Code, Codex) via Inspect-SWE; excluding 16 samples shifts GPT-5 Mini by ~0.1 pp. — [Epoch AI SWE-bench Verified page](https://epoch.ai/benchmarks/swe-bench-verified)
- Critics estimated that counting the 23 excluded tasks as failures would put GPT-5 at ~71.4% vs Claude Opus 4.1's 74.5% on the full 500 (secondary, Chinese tech press via 36Kr). — [36Kr coverage](https://eu.36kr.com/en/p/3430524474314373)
- OpenAI (week of 24 Feb 2026) audited 138 tasks GPT-5.2 consistently failed across 64 runs with six engineers each; 59.4% judged broken (35.5% tests requiring an unmentioned function name; 18.8% testing features from unrelated PRs); GPT-5.2, Claude Opus 4.5, and Gemini 3 Flash Preview each reproduced exact fixes from memory given only a task ID; OpenAI recommends SWE-bench Pro and privately authored evals. Models at ~70% on Verified score ~23% on SWE-bench Pro public split. — [Decrypt summary of OpenAI post, 24 Feb 2026](https://decrypt.co/359012/openai-benchmark-measure-ai-coding-supremacy-contaminated) (OpenAI's own page returned HTTP 403: https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/)
- Epoch rated SWE-bench Verified "Flawed" (review dated Sep 2026): all current/future models can be assumed trained on the codebases; Django is nearly half of issues; five repos >80%. — [Epoch benchmark review](https://epoch.ai/benchmarks/swe-bench-verified/review)
- "The SWE-Bench Illusion" (NeurIPS 2025): SOTA models identify buggy file paths from issue text alone up to 76% on SWE-bench repos vs 53% on out-of-benchmark repos. — [arXiv 2506.12286](https://arxiv.org/html/2506.12286v4)

**Cognition / Devin**
- Cognition's 2024 technical report: Devin resolved 13.86% of SWE-bench issues end-to-end vs prior best unassisted 1.96%; critics noted Devin was "unassisted" while other models were "assisted" (given file hints), making the comparison uneven. — [Cognition SWE-bench technical report](https://cognition.ai/post/swe-bench-technical-report); critique summarized at [eesel.ai](https://www.eesel.ai/en/blog/cognition-ai-reviews)

### Inferences
- The SWE-bench literature supports a modest, honest number for "scaffold alone": roughly +3 to +5 pp for a strong model from a better scaffold, +8 to +15 pp for weaker/open models, and up to +22 pp when a single critical tool (a reliable edit primitive) is missing. For an analytics harness the analog is: the biggest wins will come from a few load-bearing tools (schema retrieval, query execution with error feedback, result verification), not from elaborate orchestration.
- Scaffold effects can be *negative* for small models that cannot use the extra machinery (GPT-5-Nano −30 pp). BOW's "cheaper model + harness" claim should be tested per model and reported per model, not as a blanket claim.
- The field's trust in SWE-bench Verified has collapsed (OpenAI, Epoch); launch narratives in 2026 lean on SWE-bench Pro, private holdouts, and Terminal-Bench. An analytics launch should therefore not borrow a saturated public set; a private, expert-authored holdout with a public "gold" slice (the GDPval pattern) is the current best practice.
- Anthropic's 2024 post is the clearest vendor template: a short description of the exact scaffold (prompt gist, tool list, control loop), an explicit statement that scaffold matters, and the invitation for others to optimize scaffolds for the same model. It omitted trial counts — a gap BOW can improve on.

### Gaps
- No vendor (Anthropic, OpenAI, Google) publishes a numeric ablation of its own scaffold vs a reference scaffold on SWE-bench; all pp-quantified ablations come from academic papers.
- I found no Cognition blog post (2025–2026) declaring SWE-bench saturated; third-party pages mention Cognition using "FrontierCode" and a model "SWE-2" but I could not confirm these from a primary source.
- Augment, Refact.ai, Trae, and Multi-SWE-bench / SWE-bench Live scaffold notes were not retrieved within the call budget.
- The exact wording of OpenAI's 23-task footnote and the SemiAnalysis critique were not retrieved (secondary coverage only).

---

## Key Question 3: τ-bench / τ²-bench, GDPval, BrowseComp, GAIA, HLE — launch usage and the pass^k reliability metric

### Takeaway
pass^k (all k trials succeed) is the field's accepted way to turn "it can do it" into "it does it every time": Sierra introduced it with τ-bench (GPT-4o retail pass^1 ≈ 61% collapsing to ~25% at pass^8), and Anthropic's Jan 2026 evals guide explicitly recommends pass^k for customer-facing agents (0.75³ ≈ 42%). GDPval (OpenAI, Sep/Oct 2025) is the model for expert-judged, blind, pairwise, win-or-tie scoring with bootstrapped CIs and a public gold slice — but I found no product launch that reports pass^k for τ²-bench; vendors report single-attempt τ²-bench scores.

### Cited Findings

**τ-bench and pass^k**
- τ-bench defines pass^k as "the chance that all k i.i.d. task trials are successful, averaged across tasks," proposed for "real-world agent tasks requiring reliability and consistency like customer service," in contrast to pass@k. — [τ-bench paper, arXiv 2406.12045](https://arxiv.org/pdf/2406.12045)
- Sierra's launch post (20 Jun 2024): pass^k "measures the agent's reliability and determines if it can successfully complete the same task multiple times"; best GPT-4o agent was under 50% average across τ-retail and τ-airline; on τ-retail GPT-4o's pass^8 dropped to ~25%, "a staggering 60% drop" from pass^1. — [Sierra, "τ-Bench: Benchmarking AI agents for the real-world"](https://sierra.ai/blog/benchmarking-ai-agents)
- Secondary summary of the paper: gpt-4o FC pass^1 ≈ 61% retail / 35% airline, pass^8 < 25% retail. — [HF rl-llm-wiki entry](https://huggingface.co/datasets/rl-llm-wiki/knowledge-base/discussions/260)
- ReliabilityBench (Jan 2026) formalizes that under independence pass^k = (pass^1)^k, but "stochastic coupling often causes deviations." — [ReliabilityBench, arXiv 2601.06112](https://arxiv.org/pdf/2601.06112)
- τ²-bench telecom has 114 tasks; aggregators list Claude Sonnet 4.5 at 98.0% (released 2025-09-30, +33 over Sonnet 4) and GPT-5 (high, tools) at 96.70 (dated 2025-08-07); none of these report pass^k. — [datalearner τ²-telecom](https://www.datalearner.com/en/benchmarks/Tau-Squared-Benchmark-Telecom); [steel.dev τ-bench leaderboard](https://leaderboard.steel.dev/leaderboards/tau-bench/)
- BenchLM warns a τ²-bench comparison requires matching "the domain, task release, agent model, user-simulator model, scaffold, prompts, trial count, and pass^k metric." — [benchlm.ai τ²-bench](https://benchlm.ai/benchmarks/tau2-bench)

**Anthropic's recommendation on pass@k vs pass^k (9 Jan 2026)**
- pass@k = chance of at least one success in k; pass^k = chance all k succeed; worked example: 75% per-trial success over 3 trials → 0.75³ ≈ 42%; "pass@k suits tools where one success matters. pass^k suits customer-facing agents where consistency is essential." — [Anthropic, "Demystifying evals for AI agents"](https://anthropic.com/engineering/demystifying-evals-for-ai-agents)
- Same post names benchmarks per agent type: SWE-bench Verified and Terminal-Bench for coding; τ-Bench and τ²-Bench for conversational agents. — [Anthropic evals post](https://anthropic.com/engineering/demystifying-evals-for-ai-agents)

**GDPval (OpenAI, arXiv 5 Oct 2025)**
- 1,320 tasks across 44 occupations in the 9 top GDP sectors, 220-task open "gold" subset; tasks built from real work products of professionals averaging 14 years of experience. — [GDPval paper, arXiv 2510.04374](https://arxiv.org/html/2510.04374v1)
- Primary metric: blinded pairwise comparison by occupational experts against the human expert's deliverable; 3 human graders per model sample; results reported as wins/ties/losses; win rates appear to include ties. — [GDPval paper](https://arxiv.org/html/2510.04374v1)
- Gold-subset win-or-tie rates: Claude Opus 4.1 47.6% (best), GPT-5 39.0%, o3 35.2%, o4-mini 29.1%, GPT-4o 12.5%; "GPT-5-high" at 40.6% per OpenAI's press briefing. — [GDPval paper](https://arxiv.org/html/2510.04374v1); [YourStory coverage](https://yourstory.com/ai-story/openai-gpt5-human-job-performance)
- Automated grader (GPT-5-high based) agrees with human experts 65.7% vs 70.8% human-human agreement; 12 of 220 gold tasks excluded as ungradable; plots use 95% bootstrapped CIs; caveats include style-based identifiability (em dashes, first person) and self-reported task times. — [GDPval paper](https://arxiv.org/html/2510.04374v1) (OpenAI's own page returned HTTP 403: https://openai.com/index/gdpval/)
- Critique: the "linear improvement" trend line rests on three datapoints. — [LessWrong](https://www.lesswrong.com/posts/P43ck9FX3ztLNv5xv/gdpval-models-could-automate-the-u-s-economy-by-2027)
- OpenAI pointed to GDPval as its model for privately authored evals that "won't be released before testing" when abandoning SWE-bench Verified. — [Decrypt, 24 Feb 2026](https://decrypt.co/359012/openai-benchmark-measure-ai-coding-supremacy-contaminated)

**BrowseComp as a harness-analysis vehicle**
- Anthropic: on BrowseComp, three factors explained 95% of performance variance; token usage alone explained 80%, with tool-call count and model choice the other two. — [Anthropic, "How we built our multi-agent research system," 13 Jun 2025](https://www.anthropic.com/engineering/multi-agent-research-system)

### Inferences
- pass^k is the right headline metric for an analytics agent because a wrong number in a dashboard is a reliability failure, not a discovery problem. A BOW launch chart should show pass^1 and pass^3 (or pass^5) side by side for the same model with and without the harness; the drop between them is itself a harness quality signal (a good harness narrows the gap).
- Vendors have not adopted pass^k in launch posts even for τ²-bench; using it would be a differentiator and is explicitly endorsed by Anthropic's methodology post.
- GDPval's design (expert-authored tasks from real deliverables, blind pairwise grading, public gold slice + private remainder, bootstrapped CIs, published grader-agreement rate) transplants directly to analytics: "analyst-authored questions from real BI workloads, blind expert grading of the produced analysis, 20% public / 80% private."
- The BrowseComp variance analysis is a presentation pattern worth copying: regress outcome on token usage, tool calls, and model to show quantitatively how much is harness (tool/token policy) versus model.

### Gaps
- No product launch found that reports pass^k on τ²-bench; could not confirm whether Anthropic's Sonnet 4.5 or OpenAI's GPT-5 system cards include pass^k rows (aggregators show single-attempt only).
- GAIA and HLE usage in agent-product launches was not researched within budget; MCP-specific benchmarks (e.g., MCP-Bench/MCP-Universe) were not retrieved.
- Per-model GDPval numbers for Gemini 2.5 Pro and Grok 4 are only in an image figure.

---

## Key Question 4: How vendors present eval results (blog anatomy, charts, disclosures, criticisms)

### Takeaway
The recurring anatomy is: one headline number on a public benchmark, a chart of the same model under the vendor's harness versus competitors' harnesses, a short scaffold description, and footnotes carrying the caveats (subset size, parallel compute, trial count). The most common criticisms are undisclosed subsets (GPT-5's 477/500), high-compute footnotes (Anthropic's parallel test-time compute), first-party non-reproducible benchmarks (Cursor Bench), and vendor-run self-comparisons without variance (Factory, Warp). Anthropic's methodology posts are the most-cited "how to do it right" references.

### Cited Findings

**Factory (Droid), 25 Sep 2025**
- Headline "58.75%... new state-of-the-art on Terminal-Bench"; three charts (leaderboard; per-model accuracy across agents; tool usage by GPT-5 vs Opus 4.1); explicit same-model comparisons (Opus 4.1: Droid 58.8% vs Claude Code 43.2%; GPT-5: Droid 52.5% vs Codex CLI 42.8%); five runs per model, all submitted; no variance/CI/cost; sandboxed non-interactive mode disclosed; no independent verification. — [Factory Terminal-Bench post](https://factory.com/news/terminal-bench)

**Warp, Jun 2025 and Dec 2025**
- Jun 2025: 52% on TB v0.1.1 with a description of the headless cross-compiled build used to run the benchmark. — [Warp Terminal-Bench post](https://www.warp.dev/blog/terminal-bench)
- Dec 2025: 61.14% on TB 2.0 "#2 overall" with GPT-5.2, crediting joint tuning of "prompt structure, tool definitions, and planning heuristics with OpenAI." — [Warp GPT-5.2 post](https://www.warp.dev/blog/gpt-5-2-support-terminal-bench-improvement)

**Cursor (Composer), 29 Oct 2025**
- Cursor Bench: "real agent requests from engineers and researchers at Cursor" with "hand-curated optimal solutions," measuring correctness plus "adherence to a codebase's existing abstractions and software engineering practices"; run "in the Cursor tool harness"; chart buckets models into Best Open (Qwen Coder, GLM 4.6), Fast Frontier (Haiku 4.5, Gemini Flash 2.5), Frontier 7/2025, Best Frontier (GPT-5, Sonnet 4.5 — both stated to outperform Composer); tokens/sec standardized to the Anthropic tokenizer; no trial count, variance, or cost disclosed; RL environments described as the same tools as production. — [Cursor, "Composer: Building a fast frontier model with RL"](https://cursor.com/blog/composer)
- Criticism: trackers treat Cursor Bench as "display-only" / "not independently reproducible from a public harness." — [benchlm.ai cursorBench](https://benchlm.ai/benchmarks/cursorBench); [llmreference.com](https://www.llmreference.com/benchmark/cursorbench)
- Later Composer releases report CursorBench v3.1 alongside public Terminal-Bench 2.0 and SWE-bench Multilingual (Composer 2.5 at 63.2% on v3.1 per a tracker); Cursor states some behavior changes "are not well captured by existing benchmarks." — [DataCamp Composer 2.5](https://www.datacamp.com/blog/composer-2-5); [Composer 2 Technical Report, arXiv 2603.24477](https://arxiv.org/pdf/2603.24477) **[post-cutoff]**

**Anthropic**
- Oct 2024: scaffold described in detail, "performance can vary significantly based on this scaffolding," no trials/pass@k disclosed (see KQ2). — [Anthropic SWE-bench post](https://www.anthropic.com/news/swe-bench-sonnet)
- Claude Opus 4.5 (Nov 2025): 80.9% SWE-bench Verified vs GPT-5.1-Codex-Max 77.9% and Gemini 3 Pro 76.2%; a footnote says a headline human-comparison result (internal 2-hour performance-engineering exam) used "parallel test-time compute," without which Opus 4.5 "only" tied the best human; effort parameter disclosed: medium effort matches Sonnet 4.5 SWE-bench with 76% fewer output tokens, high effort +4.3 pp with 48% fewer tokens. — [The Decoder on Opus 4.5](https://the-decoder.com/claude-opus-4-5-arrives-with-anthropic-cutting-prices-by-two-thirds/); [ByteIota](https://byteiota.com/claude-opus-4-5-breaks-80-swe-bench-first-ai-to-beat-humans/)
- A third-party Claude SWE-bench timeline explicitly excluded high-compute parallel-sampling results "for comparability." — [keenable timeline](https://select.keenable.ai/r/how-claude-improved-on-swe-bench-verified/mAzt1RcYvcf0mRquM2K2Ew)
- Multi-agent research post (13 Jun 2025): "outperformed single-agent Claude Opus 4 by 90.2% on our internal research eval" (lead Opus 4 + Sonnet 4 subagents vs single Opus 4); started with ~20 queries; single-call LLM judge scoring 0–1 on factual accuracy, citation accuracy, completeness, source quality, tool efficiency; "Even in a world of automated evaluations, manual testing remains essential"; end-state evaluation recommended for stateful agents; agents use ~4× chat tokens, multi-agent ~15×. — [Anthropic multi-agent post](https://www.anthropic.com/engineering/multi-agent-research-system)
- Context engineering post (29 Sep 2025): defines context engineering as curating all tokens at inference (tools, data, history), "context rot," "attention budget"; techniques: compaction, structured note-taking, sub-agents returning 1,000–2,000-token summaries, just-in-time retrieval; **no quantitative eval results** in the post. — [Anthropic, "Effective context engineering for AI agents"](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- Evals post (9 Jan 2026): defines "agent harness" and states "Evaluating 'an agent' means evaluating the harness and model together"; capability evals should start at low pass rates, regression evals near 100%; start with 20–50 tasks from real failures; two domain experts must agree on verdicts; 0% across many trials usually means a broken task; isolate trials (shared git history can leak answers); give LLM judges an "Unknown" option; read transcripts; watch for saturation. — [Anthropic evals post](https://anthropic.com/engineering/demystifying-evals-for-ai-agents)

**OpenAI**
- GPT-5 system card: fixed n=477 SWE-bench Verified subset, 4 tries averaged for pass@1 (see KQ2) — criticized for omitting the subset from the chart itself. — [GPT-5 System Card](https://arxiv.org/pdf/2601.03267); [36Kr](https://eu.36kr.com/en/p/3430524474314373)
- GDPval: blind expert pairwise, bootstrapped 95% CIs, grader-agreement disclosed (see KQ3). — [GDPval paper](https://arxiv.org/html/2510.04374v1)
- Feb 2026: abandons SWE-bench Verified over contamination, recommends SWE-bench Pro and private evals. — [Decrypt](https://decrypt.co/359012/openai-benchmark-measure-ai-coding-supremacy-contaminated)

**Model vendors' harness-dependent headline numbers (criticized, Aug 2026) [post-cutoff, from search results]**
- Kimi K3 reports 88.3 on TB 2.1 in its own Kimi Code harness (max effort) vs 85.0 under Artificial Analysis's harness; competitor columns in its table use best-across-harness scores while K3 uses its own harness. — [Kimi-K3 repo](https://github.com/MoonshotAI/Kimi-K3); [Artificial Analysis](https://artificialanalysis.ai/models/kimi-k3); critique in [Sukhareva](https://msukhareva.substack.com/p/how-a-small-open-model-beat-a-frontier)
- DeepSeek V4 Flash reports 82.7 on TB 2.1 vs Claude Code + Opus 4.8 at 78.9 on the leaderboard, but its own report lists Opus 4.8 at 85.0 — inconsistent competitor numbers. — [DeepSeek-V4-Flash HF card](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-0731) via [Sukhareva](https://msukhareva.substack.com/p/how-a-small-open-model-beat-a-frontier)
- Sukhareva's rule: "all those numbers are about a harness and not about the model"; a bare "LLM Y scores X" is meaningless; always name agent, model, and effort level. — [Sukhareva, 4 Aug 2026](https://msukhareva.substack.com/p/how-a-small-open-model-beat-a-frontier)

**Holistic Agent Leaderboard (HAL), Princeton, 13 Oct 2025**
- 21,730 rollouts, 9 models, 9 benchmarks, ~$40,000; "higher reasoning effort reducing accuracy in the majority of runs"; LLM-aided log inspection found agents searching HuggingFace for the benchmark instead of solving it and misusing credit cards in flight-booking tasks; all 2.5B tokens of logs released. — [HAL, arXiv 2510.11977](https://arxiv.org/abs/2510.11977)

### Inferences
- The credibility gradient in 2026 runs: (worst) first-party benchmark + no trials + vendor harness only → (better) public benchmark + same model under competitor harness + N trials disclosed (Factory) → (best) public benchmark + neutral reference scaffold + CIs + cost + released transcripts (Terminal-Bench/HAL style). BOW should aim for the last tier.
- Every criticized launch failed on one of four disclosures: task subset, compute/parallelism, trial count/variance, or harness named per column. A BOW launch checklist should make all four explicit on the chart itself, not in a footnote.
- Anthropic's engineering posts are the de facto citation standard for "how to evaluate agents"; aligning BOW's methodology section to their vocabulary (task, trial, grader, transcript, outcome, agent harness vs evaluation harness; pass@k vs pass^k; capability vs regression) will read as credible to the target developer audience.
- Cursor's bucketed chart (Best Open / Fast Frontier / Best Frontier) is a useful visual device for "cheaper model + harness vs frontier raw": put the cheap model + BOW harness bar next to a frontier-raw bar and label the cost ratio.

### Gaps
- Cognition (Devin), Google (Gemini CLI/Jules), Cline, and Augment launch-post anatomy was not retrieved within the call budget.
- HN thread reactions to specific launches were not collected.
- The exact Anthropic Opus 4.5 footnote text was not retrieved (secondary coverage only); whether the SWE-bench 80.9% itself used parallel compute is unconfirmed (coverage suggests the footnote attaches to the internal exam, not SWE-bench).

---

## Key Question 5: Evidence that cheaper-model + harness beats frontier-raw, and quantified context-engineering gains

### Takeaway
There is now a solid evidence base: ACE (Stanford/SambaNova, ICLR 2026) lifted an open model (DeepSeek-V3.1) from 42.4 to 59.5 average on AppWorld — matching IBM's GPT-4.1-based CUGA (60.3) and beating it on the hard split — purely through evolving context; GEPA matched or beat RL (GRPO) with up to 35× fewer rollouts via prompt evolution; Factory's Droid + Sonnet 4 beat every competitor running Opus 4.1 on Terminal-Bench 1.0; and on Terminal-Bench 2.1 GPT-5.4 mini in Codex CLI (66.1%) outscores GPT-5.4 in Terminus 2 (54.8%). The counter-evidence is equally important: elaborate scaffolds can hurt small models (GPT-5-Nano −30 pp), and Anthropic's own multi-agent gain came with ~15× token cost.

### Cited Findings

**Agentic Context Engineering (ACE), arXiv 2510.04618 (v1 6 Oct 2025; v3 29 Mar 2026; ICLR 2026)**
- Abstract: +10.6% on agent benchmarks, +8.6% on finance benchmarks; "matches the top-ranked production-level agent on the overall average and surpasses it on the harder test-challenge split, while using a smaller open-source model." — [ACE arXiv abstract](https://arxiv.org/abs/2510.04618)
- AppWorld, DeepSeek-V3.1 base (TGC/SGC; average): ReAct base 63.7/42.9, 41.5/21.6 (avg 42.4); ICL 46.0; GEPA 46.4; ACE offline w/ labels 76.2/64.3, 57.3/39.6 (avg 59.4); ACE offline w/o labels 57.2; Dynamic Cheatsheet online 51.9; ACE online 69.6/53.6, 66.0/48.9 (avg 59.5). IBM CUGA (GPT-4.1) overall average 60.3%; ACE online exceeds CUGA on test-challenge by 8.4 pp TGC and 0.7 pp SGC. — [ACE paper HTML](https://arxiv.org/html/2510.04618)
- Context collapse case study (Dynamic Cheatsheet): at step 60 the context was 18,282 tokens at 66.7 accuracy; next step it collapsed to 122 tokens and accuracy fell to 57.1, below the 63.7 no-adaptation baseline. — [ACE paper](https://arxiv.org/html/2510.04618)
- Finance (FiNER/Formula avg): base 69.1; GEPA 72.5; ACE offline 81.9; ACE online w/ labels 76.6. — [ACE paper](https://arxiv.org/html/2510.04618)
- Cost: offline AppWorld latency 53,898 s → 9,517 s (−82.3%) and rollouts 1,434 → 357 (−75.1%) vs GEPA; online FiNER latency 65,104 s → 5,503 s (−91.5%) and token cost $17.7 → $2.9 (−83.6%) vs Dynamic Cheatsheet. — [ACE paper](https://arxiv.org/html/2510.04618)
- GitHub README phrasing: "matches top-ranked production-level agent (GPT-4.1) on average and surpasses it on harder test-challenge split, using smaller open-source model." — [ace-agent/ace](https://github.com/ace-agent/ace)
- Caveat: the comparison is against a leaderboard entry, not a same-setup rerun of GPT-4.1 with/without ACE; "smaller" is loose since GPT-4.1's size is undisclosed and DeepSeek-V3.1 is itself very large. — [VentureBeat coverage](https://venturebeat.com/ai/ace-prevents-context-collapse-with-evolving-playbooks-for-self-improving-ai)

**GEPA (Reflective Prompt Evolution), arXiv 2507.19457, ICLR 2026 oral**
- Later version: beats GRPO by 6 pp on average and up to 19 pp across six tasks while using up to 35× fewer rollouts (v1 claimed 10% average, 20% max, four tasks). — [GEPA paper](https://arxiv.org/pdf/2507.19457); [ICLR 2026 listing](https://iclr.cc/virtual/2026/poster/10009493)
- Example: GEPA found prompts after 678 rollouts achieving 38.61% vs GRPO's 35.88% after 24,000 rollouts (Qwen3 8B); matched GRPO's best validation after 243–1,179 rollouts, "up to 78× greater sample efficiency"; loses to GRPO on AIME-2025 (32.00 vs 38.00). — [GEPA paper](https://arxiv.org/pdf/2507.19457)
- Caveat: rollout counts exclude the cost of reflection calls to a strong LLM. — [GEPA paper](https://arxiv.org/pdf/2507.19457)

**Cheaper model + harness beating a bigger model**
- Factory: Droid + Claude Sonnet 4 (50.5%) beat all other agents running Claude Opus 4.1 on Terminal-Bench 1.0 (Claude Code + Opus 4.1 = 43.2%) (25 Sep 2025). — [Factory post](https://factory.com/news/terminal-bench)
- TB 2.1 official table: GPT-5.4 mini in Codex CLI 66.1% > GPT-5.4 in Terminus 2 54.8%; Gemini 3 Flash in Gemini CLI 56.9% > Sonnet 4.6 in Terminus 2 51.5%. — [tbench.ai TB 2.1 note](https://www.tbench.ai/news/terminal-bench-2-1) **[post-cutoff]**
- TB 2.0 paper table: GPT-5-Mini in Codex CLI 31.9% vs Claude Opus 4.1 in Claude Code 34.8% (nearly matched); Claude Haiku 4.5 in Mini-SWE-Agent 29.8% > Gemini 2.5 Pro in Gemini CLI 19.6%. — [Terminal-Bench paper Table 2](https://arxiv.org/html/2601.11868)
- Live-SWE-agent + GPT-5-Mini 63.0% at $0.05/issue vs mini-SWE-agent + GPT-5 65.0% at $0.28/issue (near-parity at ~1/6 cost). — [Live-SWE-agent Table 1](https://arxiv.org/html/2511.13646v3)
- Qwen3.6-27B tied Sonnet 4.6 on TB 2.0 (59.3 vs 59.1) and reportedly hit 90.0% SWE-bench Verified inside Claude Code vs Sonnet 4.6's 79.6% — the author stresses practitioner anecdotes contradict the headline numbers. — [Sukhareva, 4 Aug 2026](https://msukhareva.substack.com/p/how-a-small-open-model-beat-a-frontier); [Sonnet 4.6 system card](https://anthropic.com/claude-sonnet-4-6-system-card) **[post-cutoff]**

**Multi-agent / orchestration gains with cost**
- Anthropic: lead Opus 4 + Sonnet 4 subagents beat single Opus 4 by 90.2% on an internal research eval; multi-agent uses ~15× chat tokens; token usage explains 80% of BrowseComp variance. — [Anthropic multi-agent post, 13 Jun 2025](https://www.anthropic.com/engineering/multi-agent-research-system)

**Counter-evidence: harness can hurt**
- GPT-5-Nano dropped from 44.0% to 14.0% when given tool-creation scaffolding (Live-SWE-agent Table 5). — [Live-SWE-agent](https://arxiv.org/html/2511.13646v3)
- HAL: higher reasoning effort reduced accuracy in the majority of runs. — [HAL](https://arxiv.org/abs/2510.11977)
- NTU: higher cost does not reliably buy higher score (GPT-6 Astra $4.66 vs $19.94/task with the cheaper harness scoring higher). — [arXiv 2610.00917](https://arxiv.org/html/2610.00917v1) **[post-cutoff]**

### Inferences
- The strongest transplantable template for "our harness lets a cheaper model beat frontier-raw" is ACE's AppWorld table: one base model, one task set, rows for base/ICL/GEPA/ACE, and a single external reference row (the GPT-4.1-based leaderboard leader). For BOW: rows = {raw model, model + schema context, model + BOW harness}, reference row = {frontier model, raw}.
- The accepted *honest* phrasing is "matches on average, exceeds on the hard split" rather than "beats" — ACE, Factory, and the TB tables all show harness gains concentrate on harder/longer tasks and on mid-tier models.
- Context engineering in the analytics domain maps to: schema/metric definitions (playbook), prior query results (memory), and error feedback from the SQL engine (verification) — exactly the three ACE components (generator/reflector/curator) and the harness responsibilities the 2026 survey lists (observation, context, control, action, state, verification).
- Report cost per task next to accuracy; every credible 2025–2026 source (Live-SWE-agent, HAL, NTU, Terminal-Bench leaderboard columns) does, and the "cheaper model" claim is meaningless without it.

### Gaps
- I found no controlled study that reruns a *frontier* model with and without the same harness and reports the delta alongside a cheaper model's harnessed score on the same tasks; all "cheaper beats frontier" evidence is cross-harness leaderboard comparison.
- Letta/MemGPT and DSPy-specific quantitative context studies were not retrieved within budget (GEPA is a DSPy optimizer, so it partially covers DSPy).
- No analytics/text-to-SQL harness ablation was in scope here; the BIRD/Spider 2.0 literature may contain scaffold ablations and should be checked separately.

---

## Key Question 6: Credible presentation of eval results — statistical rigor guidance (Anthropic error bars, pass@k vs pass^k, trials, holdouts, contamination) and practitioner advice

### Takeaway
The consensus checklist: treat the eval as an experiment (Miller/Anthropic 2024), report CIs computed with small-sample-appropriate methods (Wilson/Clopper-Pearson, not CLT, below a few hundred items), use paired differences on the same task set when comparing two systems, run ≥3–5 trials and say whether the metric is pass@k or pass^k, hold out a test split touched once, name the harness/model/effort per column, and release transcripts. Anthropic's two posts plus Hamel Husain's evaluator-validation guidance are the most-cited practitioner references.

### Cited Findings

**"Adding Error Bars to Evals" (Evan Miller, Anthropic, arXiv 2411.00640, Nov 2024)**
- Treats each eval question as a draw from an unseen larger population; gives formulas for analyzing eval data, measuring differences between two models, and planning an eval experiment; notes "Evals are commonly run and reported with a 'highest number is best' mentality" without significance testing. — [arXiv 2411.00640](https://arxiv.org/pdf/2411.00640)
- Observes Chatbot Arena popularized CIs on Elo but error bars "remain noticeably absent" from Q&A evals; cites the Llama 3 report as a notable exception. — [arXiv 2411.00640](https://arxiv.org/pdf/2411.00640)
- Paired-difference testing is more sensitive than comparing two separate error bars because models agree on which questions are hard (positively correlated scores); also recommends resampling (averaging multiple completions per prompt). — [arXiv 2411.00640](https://arxiv.org/pdf/2411.00640) (summarized via [Klaviyo podcast with Evan Miller](https://medium.com/klaviyo-data-science/klaviyo-data-science-podcast-ep-56-evaluating-ai-models-a-seminar-feat-evan-miller-2076a8fdf647))

**Small-sample interval methods**
- "Don't Use the CLT in LLM Evals With Fewer Than a Few Hundred Datapoints": normal-approximation intervals shrink toward zero width near 0 or 1 accuracy, wrongly suggesting certainty; recommends Wilson score or Clopper-Pearson intervals. — [arXiv 2503.01747](https://arxiv.org/pdf/2503.01747)

**Anthropic evals guide (9 Jan 2026) — presentation-relevant rules**
- Run multiple trials because outputs vary; capability evals start low, regression evals near 100%; start with 20–50 tasks from real failures ("Small effect sizes in early development need small samples"); isolate trials (shared state leaks answers and inflates scores); prefer deterministic graders, calibrate LLM graders with humans, give judges an "Unknown" option, grade dimensions separately; read transcripts; watch for saturation. — [Anthropic evals post](https://anthropic.com/engineering/demystifying-evals-for-ai-agents)
- pass@k vs pass^k definitions and the 0.75³ ≈ 42% example (see KQ3). — [Anthropic evals post](https://anthropic.com/engineering/demystifying-evals-for-ai-agents)

**Terminal-Bench / HAL reporting norms**
- ≥5 trials per (agent, model), 95% CIs on every bar, cost and tokens as leaderboard columns. — [Terminal-Bench paper](https://arxiv.org/html/2601.11868); [tbench.ai leaderboard](https://www.tbench.ai/leaderboard/terminal-bench/2.0)
- HAL released all logs (2.5B tokens) and found unreported behaviors (benchmark lookup, credit-card misuse) only via log inspection. — [HAL](https://arxiv.org/abs/2510.11977)
- LiteCoder-Terminal averages Terminal-Bench over four independent runs "to reduce variance." — [arXiv 2605.29559](https://arxiv.org/pdf/2605.29559) **[post-cutoff]**
- TerminalWorld warns self-reported Terminal-Bench scores "may reflect different scaffolds and reasoning configurations" and runs all models under a unified Terminus-2 scaffold for comparability. — [arXiv 2605.22535](https://arxiv.org/pdf/2605.22535) **[post-cutoff]**

**Hamel Husain — evaluator validation and reporting**
- Split human-labeled data train 10–20% / dev 40–45% / test 40–45%; run the held-out test once for final TPR/TNR; "Reporting dev set performance as final accuracy. Dev numbers are optimistic." — [hamelsmu/evals-skills validate-evaluator](https://skills.sh/hamelsmu/evals-skills/validate-evaluator)
- Use ~100 labeled examples (50 pass / 50 fail); below ~60, CIs become wide; "A corrected rate of 85% could easily be 78-92% with small test sets. Report the range so stakeholders know how much to trust the number." — [validate-evaluator](https://skills.sh/hamelsmu/evals-skills/validate-evaluator)
- Error analysis as the highest-ROI eval activity (course framing). — [Maven: Error Analysis: The AI Engineer's Best ROI](https://maven.com/p/0c0359/improve-ai-consistently-getting-started-with-evals)

**Eugene Yan**
- Recommends small task-specific eval sets and deterministic SQL/JSON checks where possible; warns generic benchmarks (e.g., MMLU) can mislead. — [AI Engineer talk, "Building blocks for LLM systems & products"](https://ai.engineer/talks/building-blocks-for-llm-systems-products)

**Contamination / holdout practice**
- OpenAI: moving to privately authored evals not released before testing (GDPval pattern), after showing three frontier models reproduced SWE-bench fixes from memory. — [Decrypt](https://decrypt.co/359012/openai-benchmark-measure-ai-coding-supremacy-contaminated)
- GDPval: 220-task public gold subset + 1,100 private; grader-agreement rate disclosed (65.7% auto vs 70.8% human-human). — [GDPval paper](https://arxiv.org/html/2510.04374v1)
- Epoch: excluding 16 unrunnable samples moves a score ~0.1 pp — i.e., disclose subsets but small exclusions rarely change conclusions. — [Epoch SWE-bench Verified](https://epoch.ai/benchmarks/swe-bench-verified)

**Error-bar labeling**
- State what variability the bars capture and whether they are SD or SE; without verified normality prefer a 2-sigma bar to a claimed 96% interval. — [Hidden Measurement Error in LLM Pipelines, arXiv 2604.11581](https://arxiv.org/pdf/2604.11581) **[post-cutoff; paraphrased from search snippet]**

### Inferences
- A launch chart that survives HN scrutiny in 2026 needs, on the figure itself: harness + model + effort per bar, n tasks, k trials, metric (pass@1 / pass^k), CI method, cost per task, and benchmark version. Every criticized launch in KQ4 omitted at least one of these.
- For a two-system claim ("same model, BOW harness vs raw"), the statistically strongest presentation is a paired per-task difference with its CI (Miller), not two independent bars — and it is also visually compelling (a histogram of per-task deltas).
- Because an analytics benchmark will likely have <200 tasks, Wilson/Clopper-Pearson intervals and ≥5 trials are needed to make 5–10 pp claims credible; 3 pp claims will not be distinguishable from noise at that scale (TB top-10 CIs are ±2–3 pp at 89 tasks × 5 trials).
- Publishing transcripts (HAL/Terminal-Bench norm) and a public gold slice with a private remainder (GDPval norm) is now expected; it is also the cheapest defense against the "benchmaxxing" critique.

### Gaps
- Braintrust, LangSmith, Arize, and Patronus's own published guidance on presenting results was not retrieved within the call budget; findings here rest on Anthropic, Miller, Hamel Husain, Eugene Yan, and academic sources.
- Exact formulas from Miller's paper (SE for clustered questions, paired-difference variance) were not extracted; cite the paper directly.
- No source gives a specific recommended minimum k for pass^k in launches; Terminal-Bench's "at least five" is the closest de facto standard.

---

## Key Question 7 (synthesis): Benchmark designs that make the harness-vs-model separation visible, and what transplants to analytics

### Takeaway
Three designs make harness effects legible: (1) a leaderboard whose atomic row is (agent, model) with a maintained neutral reference scaffold (Terminal-Bench/Terminus 2); (2) a same-base-model ablation table with an external reference row (ACE/AppWorld; Live-SWE-agent vs mini-SWE-agent); (3) a model × harness matrix across several task sets (NTU 2026) that shows rank reversals. Each has a direct analytics analog.

### Cited Findings
- Terminal-Bench: rows are (agent, model); Terminus 2 is the neutral Bash-only scaffold; ≥5 trials; 95% CIs; cost/tokens columns. — [Terminal-Bench paper](https://arxiv.org/html/2601.11868); [tbench.ai](https://www.tbench.ai/leaderboard/terminal-bench/2.0)
- ACE/AppWorld: same base model, rows for each context method, one external reference (CUGA GPT-4.1 60.3). — [ACE paper](https://arxiv.org/html/2510.04618)
- Live-SWE-agent: same models, two scaffolds, cost per issue, plus per-model ablation table showing when the scaffold hurts. — [Live-SWE-agent](https://arxiv.org/html/2511.13646v3)
- NTU model × harness × benchmark matrix with cost per task. — [arXiv 2610.00917](https://arxiv.org/html/2610.00917v1) **[post-cutoff]**
- GDPval: expert-authored real tasks, blind pairwise expert grading, public gold slice, grader-agreement disclosure, bootstrapped CIs. — [GDPval paper](https://arxiv.org/html/2510.04374v1)
- Anthropic: evaluate "the harness and model together"; pass^k for consistency-critical agents; end-state grading for stateful agents. — [Anthropic evals post](https://anthropic.com/engineering/demystifying-evals-for-ai-agents); [Anthropic multi-agent post](https://www.anthropic.com/engineering/multi-agent-research-system)
- The 2026 harness survey's six runtime responsibilities (observation, context, control, action, state, verification) provide a vocabulary for naming which harness component an ablation removes. — [arXiv 2606.20683](https://arxiv.org/abs/2606.20683) **[post-cutoff]**

### Inferences
- Analytics transplant of Terminus 2: a published "bare SQL agent" reference scaffold (model + one `run_sql` tool + raw schema dump) that anyone can run; BOW's harness is a second column on the same tasks. This gives the "same model, better harness" chart with a neutral baseline nobody can call a strawman.
- Analytics transplant of ACE: rows = raw model / + schema context / + metric definitions / + BOW verification loop, on one cheap model; reference row = frontier model raw. Headline: "matches on average, exceeds on hard multi-table questions, at X% of the cost."
- Analytics transplant of pass^k: report pass^5 on "the same business question asked five times" — the natural reliability requirement for a dashboard.
- Analytics transplant of GDPval: analyst-authored questions from real BI workloads with analyst-graded blind pairwise comparison of the produced chart/analysis; publish a 20% gold slice, keep 80% private; disclose grader agreement.
- Pre-empt the "model matters more" rebuttal (Terminal-Bench paper's own conclusion) by showing the harness delta per model tier; the honest claim is that harness is the lever a platform controls and that it is worth roughly a model generation for mid-tier models.

### Gaps
- No existing public analytics/BI agent benchmark with (agent, model) rows and a neutral reference scaffold was identified in this research; BIRD, Spider 2.0, and DABstep leaderboards should be checked for whether they already separate scaffold from model.
