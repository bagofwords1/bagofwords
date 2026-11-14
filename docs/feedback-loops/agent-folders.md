# Shared organization folders · agents first

**Design and sandbox verification plan · November 14, 2026 · baseline `78b59fe74`.** Marketing / Acquisition has one identity across the organization. The first release supports creating, sharing and moving folders and filing agents in Knowledge Explorer. A report can use the agents under a selected folder. Later, the dashboard gallery and Automations can show their own items under the same folder ID. The matrices define the target behavior. The implementation evidence at the end records the verified subset and outstanding checks.

**Folder knowledge · implemented in the workspace.** Start with the [folder knowledge permission matrix](#folder-knowledge-permission-matrix), then the [routing verification loop](#routing-verification-loop-with-300-agents). The [verification record](#folder-knowledge-verification) covers instructions, files and eval suites. Saved-query attachments are deferred. Existing agent and eval behavior below is the regression baseline.

## Tree and placement

| Record | Proposed representation | Phase |
|---|---|---|
| Folder | Stable ID, organization ID, nullable parent folder ID, name, creator, inherit-access flag. Parent and child must share an organization. Top-level folders have a null parent. | Agents first |
| ResourceGrant | Folder as a resource type. Store direct grants once per principal and folder. Resolve inherited grants along the parent chain. | Agents first |
| DataSource (agent) | Nullable home folder ID and inherit-folder-access flag. Keep existing agents at the org root on migration. | Agents first |
| Report | Nullable `agent_folder_id` points into the common tree and selects eligible descendant agents for execution. Existing `project_id` and explicit agent pins retain their meanings. | Agents first |
| Artifact | One home on the stable artifact, across versions and published releases. Its project remains the authoring context. | Later |
| Scheduled task / automation | One home on the task or automation definition. Keep execution history associated with its definition. Validate the organization's ownership through the host report where applicable. | Later |

Each folder can hold several resource types. Its row has no library discriminator. Each fileable type can gain a nullable placement FK and inheritance setting when it ships. Index children by organization and parent, and items by home folder. Keep existing databases free of speculative artifact or task migrations in the agent release.

## Folder permission matrix

The UI labels are **Can query** and **Can manage**. The historical role names in this matrix and the folder sharing API remain Viewer and Manager. Assign **Can query** or **Can manage** to a user or group in the organization. Several people and groups can be Managers. Store a single direct role for each principal on a folder. Resolve active membership and applicable grants at request time. Manager includes Viewer. A direct Viewer grant cannot downgrade an inherited Manager.

| Effective role on Marketing | Browse Marketing and subfolders | Query inheriting agents | Manage inheriting agents | Administer Marketing |
|---|---|---|---|---|
| Viewer | Browse and find agents in the picker | Yes, subject to the caller’s credentials, connection and channel rules | No authority from this role | No authority from this role |
| Manager | Same as Viewer | Same as Viewer | Existing agent `manage`, including agent membership, instructions and evals | Create subfolders, rename, organize, share with users/groups, change roles and revoke direct shares |
| Direct agent share only | Find that agent in Shared with me | According to the agent grant | According to the agent grant | No access to its folder or siblings through this share |

Compute the effective role along the path and translate it to agent permissions at the agent. Manager receives both query access and the existing `data_source.manage` implications. Reuse that decision for single-object checks, bulk lists, schema, selectors, tools and eval routes. Keep inherited access derived so moves and group removals take effect without rewriting direct agent grants. A folder role grants no connection administration or credentials. Editing a shared instruction or saved query still requires the authority for its full scope. Future item types have their own operation rules below.

### Folder knowledge permission matrix

**Release gate.** Decide and test authority for each operation before enabling attachment writes or model retrieval. The proposed custom folder capabilities are `manage_instructions`, `manage_files` and `manage_evals`, qualified by resource type and folder ID. Share offers Can query and Can manage. Settings → Roles → Resource permissions also offers Custom. Persist custom capabilities as a set. Preserve existing view/manage grants and all unrelated grants on a role edit.

| Effective grant on Marketing | Browse and query | Instructions | Files | Evals | Folder, agent and delegation authority |
|---|---|---|---|---|---|
| Can query | Browse visible descendants, query eligible agents, consume authorized Marketing context | Published, permitted revisions only | Permitted file metadata, passages and downloads under existing file policy | No run, edit or result access from this grant | No writes or sharing |
| Custom · Manage instructions | Browse Marketing to do this work. Query requires a separate grant. | Create, maintain and publish Marketing-owned rules through the normal review workflow. Attach or detach rules on Marketing. | — | — | No folder sharing or child-agent instruction management |
| Custom · Manage files | Same browse rule | — | Upload Marketing-owned files and curate Marketing file links. Replace or delete a canonical file only with file authority. | — | No agent file or connector management |
| Custom · Manage evals | Same browse rule. Running also requires Can query on Marketing. | — | — | Create and edit Marketing-owned suites/cases, attach or detach suites, and operate authorized folder-routing runs | No independent explicit-agent or global eval authority |
| Can manage | Includes Can query | All three folder content capabilities | Same file and source checks | Same run and result checks | Manage folder sharing, children and placement. Existing inheritance grants agent manage on inheriting agents. |
| Direct agent-only recipient or unrelated member | Existing agent rights only | No Marketing content through that agent | Same | Same | Omit Marketing from breadcrumbs, pickers, searches and counts |
| Full org admin | Org-scoped recovery and administration | Preserve revision lifecycle | Preserve file/connector credential checks | Preserve execution identity and history checks | No cross-org authority |

A dash grants no capability. Custom rights apply on the **exact granted folder**. They confer minimal browse access there, without cascading custom bits, browse or query down the tree. Keep browse separate from the existing inherited view-to-agent-query mapping. A custom-only user may see Marketing as a directly shared entry under a hidden parent. Can query and Can manage retain their existing inheritance boundaries. Compute unions across active direct user, group and assigned-role grants. An independent grant below a boundary can still apply. Removing a membership or narrowing a role must change the next authorized operation. Custom holders cannot set inheritance, move the folder, grant their own capabilities, or qualify as its last sharing manager.

| Operation | Required server checks |
|---|---|
| Create here / attach existing | Exact-folder capability for the item type and org match. For existing content require permission to share or use it in this destination under its canonical policy. Reading a private file alone cannot make it available to another audience. New folder-owned content follows the folder's audience and its own publication or file policy. |
| Detach / edit / delete / restore | Detach requires folder curation authority and changes only that link. Canonical content shared across folders or agents retains its owner, review rules and full-scope edit checks. A Marketing-only editor cannot change Sales through a shared revision, replace a shared blob, delete a suite used elsewhere, or change hidden targets by submitting a partial form. Restoring a version must recheck every restored target. |
| Read and retrieve | Require access to the active context and independent item policy before listing, ranking, counting, reading bytes or building prompts. Apply the same decision to ID lookup, search, cached snippets, previews, citations, export and download issuance. Folder-created content needs an explicit owning context or equivalent recorded authority. Never infer ownership from the first remaining attachment. |
| Assign custom roles | Require normal role administration and delegable authority at each affected folder. Check create, replace, delete, assignment to users/groups and duplication. A role editor cannot grant or remove rights on an inaccessible folder by changing the whole role payload. Reject unknown permission bits and foreign targets atomically. |
| Run a folder eval | Require folder eval operation and query rights, case/suite authority, an authorized nonempty roster, and usable credentials for the original requester. Recheck at enqueue and dispatch. Folder QA can test routing within its own scope. Existing explicit agent cases still require eval authority on every target, global cases retain their org gate. |
| Read history / cancel | Check current eval authority and access to the recorded sources and content exposed in each result, transcript, artifact or stream. Reconnect, retry, cancellation and bulk actions retain requester and per-result boundaries. A folder grant alone cannot reveal an older restricted file excerpt. |

**Permission verification comes first.** Parameterize the matrix by action and fixture identity. Include a query-only analyst, each custom capability alone, QA plus query, a group manager, an overlapping user/group role, an agent-only recipient, unrelated member and foreign-org admin. For each write pair a successful request with a denied request differing in one grant. Verify unchanged DB links, revisions, grants and jobs on denial. Exercise Explorer, item pages, Roles, API and background workers against the same decision. Repeat after role downgrade, group removal, moving a branch and toggling inheritance. Test two simultaneous editors and revocation between queue and dispatch. Keep audit evidence of actor, target, operation and before/after access restricted to authorized readers.

### Exact-folder context and attachment storage

Use typed, many-to-many folder links for instructions, files and suites. Enforce unique item/folder pairs, same-org references, reverse indexes and transactional audit. Keep current agent links, suite home and case targets. One item may appear in Marketing, Sales and an individual agent with one canonical ID. Validate delete behavior for nonempty folders and the loss of an owning folder separately from removal of a reusable link. Backfill explicit global versus targeted instruction scope before enabling folder-only instructions. Snapshot agent and folder targets with the revision/build and include scope changes in version comparison, publish and rollback. Removing the last target leaves a targeted instruction inactive. Verify migration and rollback on SQLite and PostgreSQL with existing global, draft and multi-agent instructions.

Starting a conversation in Marketing activates **Marketing's own attachments plus the authorized descendant agent roster**. A Marketing/Acquisition start activates Acquisition's own context. A direct Attribution start activates that agent's context. Existing applicable global instructions still participate. Advancing from Marketing into agent focus keeps Marketing active. Clearing or changing focus stays within its roster. New turns, resumed runs and queued dispatch re-resolve the caller and scope. Persist the starting folder separately from explicit agent IDs and actual execution targets.

Load authorized, published always-on Marketing instructions before the first planner decision. Surface relevant smart rules through the existing catalog, ranking and authorized read path. Focus adds the chosen agent’s eligible instructions before tools run. Deduplicate shared rules by canonical ID and selected revision across folder and agent. Apply existing instruction priorities and test contradictory rules using that ordering. File retrieval searches the active folder and authorized focused context on demand. Fetched passages and citations carry file identity/version and respect deletion, replacement and ACL changes. Treat reference text as content; it cannot authorize switching agents or tools.

### Routing verification loop with 300 agents

**Existing baseline.** From `backend`, run `TESTING=true .venv/bin/python -m pytest -q tests/e2e/rbac/test_agent_folders.py::test_folder_role_roster_at_scale --db=sqlite`. This covers roster membership, bounded prompt representation, focus and empty scope. The expanded test also checks the focus selected after changing the published folder instruction. Reuse the completion capture boundary in `tests/e2e/test_smart_instructions_planner_prompt.py` and exercise the real report → completion → agent search/focus → tool → result path.

| Planned step | Pass condition and evidence |
|---|---|
| Seed exactly 300 agents | Reuse the 64-folder fixture with 298 initially eligible agents, one external and one restricted agent. Place Attribution beyond the detailed roster and names-only cap. Give Paid Media and Attribution similar names with different descriptions and stable IDs. Seed a Marketing always-on routing rule, a smart rule, an authorized glossary, a restricted file, a restricted primary instruction, a child-only rule and a suite. Give each sensitive field a unique sentinel. |
| Inspect the first planner request | Compare the full runtime ID set with an independently constructed expected set. The bounded prompt count matches that set. The routing rule is available before any focus/tool call. No child-only rule, unauthorized agent name, file snippet, restricted ID or sentinel appears in prompts, tools, references, counts or logs exposed to the user. Compare the UI picker, estimator and runtime for the same identity. |
| Search and describe | Search by intent and inspect returned IDs, one-liners, capability summaries and counts. Cover explicit description, permitted primary-instruction fallback, missing/denied fallback, duplicate display names, long text and stale cached descriptions after edits. Apply authorization before ranking or truncation. Search must find eligible agents omitted from the prompt. A hidden primary instruction must never supply a summary. |
| Focus and retrieve | Ask a CAC-by-channel question. Capture ordered planner/tool events and the chosen stable ID, then verify focused rules before data use, one copy of a shared instruction, smart-rule read on demand and citations only to allowed glossary passages. A reference to an inaccessible agent in a human-authored rule never grants it access. Capture the actual prompt separately from the expected ID set. |
| Prove that the rule changes routing | Run paired cases with the same question, descriptions and roster. Change only the published routing revision and expect the newly designated eligible agent. Keep a deterministic sandbox model double that uses the observed prompt/search results, with no pre-scripted target ID. Missing or rolled-back instructions must change the observed evidence. Separately run blinded paraphrases and ambiguous/no-match prompts against a real configured model, score exact expected ID or acceptable clarification, and report the sample size and errors. A scripted tool call alone proves plumbing, not model choice. |
| Prove the boundaries | Repeat from Marketing, Acquisition and a direct agent start. Change group membership, mark a branch limited, revoke file access after indexing, and rerun under identical prompts. Check the already-established 298 → 148 → empty progression on the baseline fixture. An empty scope fails before model/tool dispatch. A denied item never reappears via stale focus, caches, filenames, citations, result history or inherited descriptions. |
| Run the same question as an eval | Default **Run against Marketing** executes once from the folder without a preselected agent. Record requester, folder, mode, instruction revision/build, resolved roster, focused/used agent IDs and content provenance for historical authorization. Assert routing with the observed tool/usage trace, independent of the answer text. Test a different stream observer, post-enqueue revocation, hidden file, mixed-result reads and a changed roster before retry. Per-agent fan-out would be a separately selected mode. |

**Acceptance order.** First land failing permission and migration tests, then pass the matrix on SQLite and PostgreSQL. Next pass the prompt, search, retrieval and eval evidence above. Finally exercise the same actors in separate Playwright sessions, including a custom-only editor and a query-only analyst. Verify disabled or absent actions alongside direct API denials, shared attachment preservation, source labels, keyboard/touch and RTL. Record sanitized before/after screenshots, exact runtime ID-set comparisons, captured planner/tool traces, result snapshots, query counts and timing for cold/warm runs. Compare retrieval and permission query counts at 30 and 300 agents to detect per-agent DB round trips. Record model-quality outcomes separately from deterministic assertions. The verification record below distinguishes completed checks from outstanding release gates.

### Who can create, share and move?

Evaluate effective user, group and role grants in the same organization. “Manage A” means existing management authority on agent A. An organization admin below means a full admin of that organization.

| Action | Required authority | Example |
|---|---|---|
| Create a private top-level folder | Manage at least one agent, create agents on at least one connection, create agents org-wide, or full org admin | A creator on warehouse C can create Marketing and receives a direct Manager grant. A member who only browses or queries cannot create at root. |
| Create a subfolder | Manage the parent, or full org admin | A delegated Marketing manager can create Acquisition even without agent creation rights. |
| Create an agent directly in a folder | Existing agent creation authority for all attached connections (org creation rule for inline config), plus Manage destination | A creator on C alone can build an agent but needs Manage Marketing to file it there. |
| Share a folder, edit or revoke its grants, toggle its limited access, rename it | Manage that folder, or full org admin | A manager of one agent in Marketing cannot change the Marketing audience through that grant. |
| Edit an agent’s direct grants or limited access | Manage that agent, or full org admin | A Marketing Manager can edit an inheriting agent’s grants. At a limited agent they need independent agent management authority. |
| Move an agent | Manage the agent and each non-root source/destination folder, or full org admin | Root → Marketing requires Manage A and Manage Marketing. Marketing → Sales also requires Manage Sales. |
| Move a folder and its subtree | Manage the moved folder and each non-root source/destination parent, or full org admin. Moving to root also requires top-level creation authority. | Preview the inherited audience change for the branch. Preserve direct grants and limited-access boundaries. |
| Delete an empty folder | Manage folder or full org admin. Check all children and filed item types across the shared tree. | An empty Agents view cannot prove the folder is empty. If a report still selects this folder, preserve an unavailable scope or require reassignment. |

An ordinary child folder can rely on its inherited Managers. A private root and each limited-access folder or agent need a direct Manager with an active org member behind the grant (an individual or a nonempty group). When editing grants, moving a branch or switching on limited access, check the resulting manager set inside the transaction. Allow an inherited Manager to assign a replacement and enable limited access together. If they omit themselves they immediately lose inherited authority. Reject a change that would leave the boundary unmanaged. If later group membership changes remove the last manager, a full org admin can recover access. Losing connection creation rights affects future root creation. An independent grant on an existing folder continues to apply.

## How inheritance and limited access work

Start at the requested folder or agent and include direct grants there. Walk up through its home and ancestors, including grants on the first limited-access node, then stop. This applies equally to Viewer and Manager and to newly filed agents. A direct share on or below a boundary still works, as do independent agent permissions, org-public agent query access and full org admin recovery. Full org admin is the only blanket folder-boundary override. Independent resource rights retain their existing scope. On a move, recalculate the inherited audience from the new path and preserve existing direct grants and boundaries.

| Example | Effective access |
|---|---|
| Marketing grants Viewer to Analysts and Manager to Leads | Both groups can query Campaigns in Marketing / Acquisition. Leads can edit Campaigns and run its evals. Someone in both groups is a Manager. Removing them from Leads leaves Viewer via Analysts. |
| Marketing / Compensation has limited access and a direct Manager grant to HR Leads | Marketing’s grants stop at Compensation. HR Leads manage it and its inheriting agents. An ordinary Marketing Manager cannot inspect it, move its agents, or turn inheritance back on there. |
| Payroll has limited access on the agent itself inside an ordinary folder | The same boundary applies at Payroll. A directly shared recipient can open it via Shared with me without gaining any ancestor names, counts, breadcrumbs or siblings. An existing org-public setting still lets org members query it. |

Managers can inspect and edit the authorized folder’s direct audience. A Viewer can inspect their own effective role. Apply the same visibility rules to generic grant lists, reverse lookups by user/group, unfiltered listings, previews and direct URLs. Scope every response to the caller’s organization. Redact inaccessible source folders and principals. Audit the actor, grant or move, old/new parents and inheritance changes.

## Where it lives

**Knowledge Explorer on /agents is the first management surface.** Add **New → Folder** at the root. Expand Marketing to browse its folders and agent rows. Preserve the existing per-agent detail tree. Selecting a folder opens its agent contents and access summary in the right panel. Rename, Move and Share act on the shared folder across surfaces. Label counts by type and show only authorized metadata in previews. Its menu offers New subfolder, Share, Rename, Move, and Delete when empty. An agent manager can edit its access and can move it when they also manage the required folders. The Move dialog offers only eligible destinations, supports the keyboard, and shows the audience change. Drag-and-drop can call the same flow. Display server-computed capabilities so a folder creator sees New even when they cannot create an agent. The existing instruction-directory and eval-suite controls remain scoped to their respective content.

In **Share**, use one people/group picker and one role menu. Keep the role hint on the menu, the grant source on the audience row, and the limited-access explanation beside its switch.

| UI element | Example copy |
|---|---|
| Role options | **Viewer** · Browse and query agents. **Manager** · Also edit agents and manage sharing. |
| Audience row | **Marketing Leads · Manager** · Inherited from Marketing. If the origin is hidden, show **Inherited**. Edit an inherited grant at its visible source. |
| Inheritance switch | **Limited access** · Parent roles stop here. On an org-public agent also show **Shared with the organization**. |
| Change preview | **Analysts lose access** or **Sales gains Manager access**. Show affected users/groups on demand and recheck the preview at commit. |
| Restricted folder or agent without a manager | **Choose a manager for this item.** Offer the user/group picker in the same action. |

Show Share, Move and role editing only when the server permits the action. A Viewer sees browse and query actions. A Manager sees the agent’s existing editing and eval actions wherever authorized. Reuse the ordinary rows in the picker. Reserve access explanations for Share and for actions that need a decision.

**The prompt picker is for discovery and selection.** Hover can preview on desktop. Click/keyboard/touch drills down. Choosing “Use Marketing folder” saves a live scope restricted to eligible descendant agents. Other item types in the branch never become completion inputs through placement. Choosing named agents saves explicit pins. Direct-only shares work through search or Shared with me. Use the existing agent flyout for agent details. Opening a folder or hovering over it leaves the current run selection alone.

## Creating agents in a folder

`folder_id` is the proposed **placement** input. `agent_folder_id` on a report is its live execution scope. Opening a folder in a picker or running a training conversation scoped to Marketing never implicitly supplies a placement. A “New agent here” action can prefill the destination and show its breadcrumb and effective audience before submission.

| Creation entry point | Proposed request and behavior |
|---|---|
| `POST /api/data_sources`, New Agent wizard, training tool `create_agent` | Accept optional `folder_id` on the public create schema and tool input. Omitted/null creates at root with the caller's existing visibility choice. For a concrete ID require Manage destination in this org plus the existing create checks for the selected connections. Create the agent with that home and audience from its first visible write. Preserve table/tool choices, creator grants, existing response IDs and legacy training attachment on reports without a folder scope. On a folder-scoped report, keep the folder selected after creation. Explicit org-wide sharing is still broader than folder inheritance; show it as such. |
| Prebuilt demo installer, including onboarding and legacy Agents | Accept optional JSON body `{"folder_id": "..."}` on `POST /api/data_sources/demos/{demo_id}`. Existing no-body calls keep their current org-public default. For a new explicitly filed demo default to folder inheritance with org-public off, on both demo creation branches. Preserve the current org create permission. A repeat install returns the existing result to an authorized recipient and preserves its current home and audience, even if another destination was supplied. Guard listing and “already installed” responses against disclosure of private demo agent IDs. Re-filing uses the normal move operation. |
| Generated demo, training tool `create_demo_dataset` | Accept one optional `folder_id`; show the destination on the existing review card and bind the reviewed destination to the confirmation. Apply it to each checked agent that is successfully created. Preserve unchecked/skipped results and the existing connection-only path. Store placement on agents only. Check folder authority before generation and again before committing agents; a stale destination must not cause a root or public fallback. Keep partial-success reporting accurate and make retries of the same confirmed installation safe. |
| Existing connection forms offering “Create a public agent” | Keep their explicit public behavior on omission. If a destination is offered, label the effective org-wide audience; folder choice is separate from that toggle. Connector/table type selection and demo-template selection keep their existing ID types. |

**Directory creation.** Offer a separate `ensure_folder(parent_id, path_segments)` operation, e.g. `POST /api/folders/ensure` with `{"parent_id": null, "path_segments": ["Marketing","Acquisition"]}`. Return the stable leaf ID for `create_agent` or either demo flow. This is the user-facing equivalent of `mkdir -p` in the organization's virtual tree.

- Resolve each segment relative to the supplied parent in the same organization. Use the regular folder-create name rules and a consistent sibling uniqueness key, including siblings at root. Reject empty segments, embedded separators and traversal segments such as `.` and `..`. If a user can see two indistinguishable pre-existing matches, require an explicit parent/leaf ID to disambiguate.
- Reuse existing visible segments and require the root-create capability for a missing top-level component, or Manage parent for each missing child. A top-level creation receives the creator's direct management grant; new children inherit normally. Resolve-only success creates no grants. Filing an agent always performs its own destination check. A hidden name collision yields a generic unavailable destination without returning its ID or creating a duplicate.
- Make one ensure request atomic. If a later segment is invalid or forbidden, roll back newly created intermediate rows. Two authorized concurrent requests for the same path converge on one leaf, including under SQLite and Postgres. Recheck permissions and parent identity if a competing transaction moves a parent. Retrying an ensure after an agent creation failure can reuse its successful leaf. Do not infer permission to create directories from a typed path in an agent name.

Current anchors: `backend/app/ai/tools/schemas/create_agent.py:5`, `backend/app/ai/tools/implementations/create_agent.py:267`, `backend/app/routes/demo_data_source.py:45`, `backend/app/services/demo_data_source_service.py:88`, `backend/app/ai/tools/implementations/create_demo_dataset.py:348`, and `backend/app/services/demo_data/installer.py:220`.

## Selector compatibility matrix

Make live folder selection an explicit opt-in on the **report** composer. A component reused by a task, trigger or eval must use its caller's contract. Folder browsing has no selection event, report write, default-preference write or global-store mutation. Leaves keep their existing ID and object shapes. Disabled, sign-in-required and permission-filtered agents retain their existing treatment. On rehydration, distinguish unavailable explicit targets from an intentional Auto, All, global, or inherit selection.

The inventory groups call sites with the same contract. Paths below are relative to `frontend/` unless prefixed with `backend/`. Run each row through its final action and inspect both the persisted/request value and its observable effect.

| Entry points | Existing contract and required regression |
|---|---|
| Home and new/existing report chat (`pages/index.vue`, `pages/reports/{new,[id]/index}.vue`, `prompt/PromptBoxV2.vue`, `prompt/DataSourceSelector.vue`) | Test ordinary Auto, manual one/many, manually selecting all, and the new live folder. Create, switch, reload, estimate, send, queue and resume. Manual selections serialize as `data_sources`; only explicit folder choice writes `agent_folder_id`. Use a distinct report-folder event. Selecting a folder or hydrating it must never emit the agent-ID event that writes membership defaults. Deliberate pin changes keep their existing default-sync behavior. |
| Alternate report controls (`report/ReportEmptyState.vue`, `report/ReportAgentPanel.vue`, `prompt/MentionInput.vue`, `CommandPalette.vue`, training mode selector, `SearchAgentsTool`/`SetReportAgentsTool`) | Chips toggle actual agents, starters/details/open/connect target the chosen agent, and mentions retain typed IDs. Test desktop/mobile and search/focus after a tool changes focus. Intersect training search/focus with the selected branch and the caller’s managed agents. Creating an agent in an explicitly chosen different folder must preserve the live report scope and leave that agent outside the roster. Distinguish explicit switching to pins from opening or previewing a row. |
| Navigation, member defaults, console and quick-create (`AgentSelector.vue`, `useAgent.ts`, `ConsoleOverview.vue`, `pages/monitoring/cost.vue`, `pages/reports/index.vue`) | Navigation selects agent IDs; member defaults and console scope are separate. Preserve All, concrete IDs, report create defaults and permissions. Check overview, cost requests and downstream recent-instruction/query panels. Folder browsing cannot overwrite the user's default agent list. |
| Knowledge Explorer and legacy agent navigation (`KnowledgeExplorer.vue`, `pages/old_agents/`, connection detail and agent jump links) | Browse filtered tree/search or open a stable per-agent route. A direct-only share opens without parent names. Per-agent child panels still receive agent IDs. A folder row opens a folder panel. |
| Eval case authoring (`monitoring/TestPromptBox.vue`, `TestCaseEditor.vue`, `AddTestCaseModal.vue`, Knowledge Explorer) | Selector is filtered by `manage_evals`. Create, edit and reopen single-, multi-agent and authorized global cases. Persist exactly `data_source_ids_json`, including explicit IDs originating outside the suite's home. Viewer grants query access. Manager grants `manage_evals` on inheriting agents. Require authority on every targeted agent. A denied target or empty folder cannot turn into a global case. |
| Run from a case/editor, selected cases, agent/global panel, legacy Evals or Create Agent tool card (`AgentEvalsPanel.vue`, `TestCaseEditor.vue`, `pages/old_agents/[id]/evals.vue`) | Single/bulk runs submit explicit **case IDs**; editor run preserves its selected build. The Run evals modal chooses suites then deduplicates active case IDs independently of Tests-tab search. Verify the actual cases, targets, build, run results and status. |
| Suite/tree/build/instruction runs (`KnowledgeExplorer.vue`, `instructions/BuildExplorerModal.vue`, `instructions/ResolvedEvalStrip.vue`, `EvalRunDetail.vue`); planner `run_eval` | Exercise the suite-run endpoint, batch by suite/build, resolved cases by IDs/build, and tool execution by cases or suite. Preserve their existing draft/active and filing-vs-target semantics. The instruction strip runs its displayed resolved cases. A folder ID can never stand in for a suite, case, build or agent ID. Validate target authority before execution and test status/results/stop with the correct reader/operator permissions. |
| Scheduled tasks (`ScheduledPromptModal.vue`, `automations/ScheduledTab.vue`) | Embedded composer edits task-local `data_source_ids`; follow-host and explicit picks round-trip. Save, edit and Run now on a host with different agents. Preserve host selection and current field omission/clear semantics. For follow-host, carry the live folder ID to a spawned report in both project and root creation paths. An explicit task override retains its own IDs. Resolve using the scheduled identity at dispatch. |
| Fork report (backend `fork_service.py`) | Copy the live folder ID on fork and resolve for the fork recipient. Preserve explicit pins and ordinary Auto on legacy reports. A revoked folder produces an unavailable scope. Apply stored-output access checks before copying assets or summaries. |
| Trigger editor (`automations/TriggersTab.vue`) | Embedded composer serializes concrete agent IDs into the trigger configuration. Reopen, change and fire a stubbed trigger. Preserve report routing and the existing defaults. |
| Project defaults (`pages/projects/[id]/index.vue`) | Add/remove eligible agent IDs and create a report inside the project. Keep the all-members-can-see check and explicit report override. A project default never confers access. |
| Prompt library and starters (`pages/prompts/index.vue`, `prompt/PromptEditModal.vue`, `DataSourceQuestionsHome.vue`) | Preserve personal/global/agent scope choices offered by each surface, concrete agent assignments, list filters and starter retrieval. Reopen a saved prompt and launch it. |
| Instruction authoring, bulk assignment, filters and builds (`KnowledgeExplorer.vue`, `Instruction*Component.vue`, `ConsoleInstructions.vue`, `instructions/{AllInstructionsModal,BulkScopeModal,InstructionsFilterBar,BuildExplorerModal}.vue`) | Preserve multi-agent association IDs, global scope, permission filters, and distinct filter-only actions. Run bulk edit and reload. Instruction directories, primary-instruction selectors and build selectors keep their own item IDs. |
| Saved queries/entities (`entity/{EntityForm,EntityCreateModal,EntityEditModal,EntityDetailPanel,AgentQueriesPanel}.vue`) | Create/edit with one and multiple agents; retain exact associations and all-agent access checks when viewing or running. |
| Gallery filters and dashboard chat (`dashboards/DashboardFilterPopover.vue`, `dashboard/DashboardSettingsModal.vue`) | Gallery agent chips filter published items. Dashboard chat settings preserve data-only `[]`, Auto reset `["*"]`, and explicit permitted IDs. If any explicit pick is missing on reopen, preserve the saved selection on unrelated edits and require explicit repair to change it. Only a deliberate Auto choice may send the wildcard. Verify the owner allowlist and caller access at execution. |
| Integration routing (`ChannelAgentRouting.vue` in Slack/Teams, `WhatsAppIntegrationModal.vue`) | Persist explicit `agent_ids`. Preserve integration default vs channel override, clear behavior and availability in the target channel. |
| Connected application allowlists (`embed/ConnectedApplicationsPanel.vue`) | Preserve `null` for All versus an explicit array, including empty. Browse/reopen without changing the allowlist. A new folder child never silently expands an explicit application scope. |
| Resource-role target selector (`RolesManager.vue:442`) | Keep the typed resource identity (e.g. `data_source:<id>`) when selecting an agent to grant rights. Reopen the role and exercise its grant. Any future folder target has its own resource type and permissions. |
| Diagnosis pivot (`diagnosis/RunsTable.vue:79`) | Clicking an agent chip emits an agent-name search expression. Verify quoting for names containing spaces and confirm the resulting rows. A folder path must never replace that name. |
| Review feed (`ReviewFeed.vue`) | Keep the single-agent feed filter, mark-read scope and per-agent settings target separate. Check that the API receives the same agent ID after folder navigation. |
| Agent creation/onboarding (`NewAgentWizardModal.vue`, new/legacy agent pages, onboarding demos, `IntegrationConnectionForm.vue`, `MCPConnectionForm.vue`, `CreateDemoDatasetTool.vue`) | Exercise the destination separately from connection IDs, template IDs, and generated suggestion checkboxes. Verify omission preserves each entry point's legacy behavior. |

**Inventory gate.** Search both component use and custom bindings before implementation and repeat on the release branch. From repo root:

```bash
rg -ni 'datasourceselector|agentselector|promptboxv2|testpromptbox|reportagentpanel|useactiveagents|useagent\(' frontend --glob '*.vue' --glob '*.ts'
rg -n 'data_source_ids(_json)?|agent_ids|selected(DataSources?|Agents?)|agentOptions|agentFilter|default_data_source_ids|/data_sources/active|case_ids|suite_id' frontend backend/app/routes backend/app/ai/tools --glob '*.vue' --glob '*.ts' --glob '*.py'
```

Trace events through parent components, including API-only consumers. Classify additional hits as selection, downstream reader, or display-only (for example an agent avatar or a connection-type picker). Add any newly discovered writer or run entry point to the matrix before sign-off.

### Eval execution and historical results

Apply these checks across the editor, bulk, suite, build, instruction, tool, background and streaming paths.

1. At submission, check authority for the requested cases and every explicit target. Record the requester, build, case ID, target mode (explicit, deliberately global, or the proposed exact-folder routing mode) and requested target IDs per result. A global case requires org-level `manage_evals`. Editing a case later affects future runs.
2. At dispatch, recheck the original requester's eval authority, runtime scope and connection usability. For an explicit case, create its stub report and attach exactly the requested agents atomically. Abort that case before any model/tool call if any ID is missing, revoked or unusable, the association fails, or the attached set differs. Persist a failure with no executable unscoped stub. For an authorized global case, resolve Auto under the requester. Record the actual execution scope for historical reads.
3. An authorized observer can follow a run. Opening or reconnecting its stream cannot start work under the observer's identity or grant them operator rights. Deduplicate retries only for the same requester, execution scope, build and case/target snapshot within the org. Distinct credential contexts must run separately.
4. Authorize status, results, transcripts and stream events using current reader authority over each result's immutable executed targets. For pending or failed results use the submitted scope. Keep the org-level gate for global cases. Filter mixed-run responses, events, counts and summaries to authorized results. Stopping the whole run requires operator authority across its targets. For historical records lacking snapshots use verified original run attachments when complete, otherwise require org-level eval authority. Case edits or deletions must never reclassify a historical result as public or global.

## Future resource permission matrix

| Fileable item | Folder Viewer can convey (Manager includes it) | Authority to place or move it | Operations still governed by the resource |
|---|---|---|---|
| Agent (v1) | Browse and query inheriting agents | Manage agent and applicable folders | Folder Manager confers agent management through inheritance. Validate all affected agents for shared content and evals. Connection and tool policies apply at use. |
| Dashboard, deck or document | View an eligible published release, subject to release/source checks | Artifact sharing authority and applicable folder management | Open draft, edit, publish, read authoring conversation, change credentials, make public, or query backing agents |
| Scheduled task | Display title, schedule and status. For untitled tasks use a neutral label so the summary cannot expose a private report title or prompt. | Existing task management authority on its host report and applicable folder management | Read prompt, outputs, recipients or report; run now, pause, reschedule, change recipients, identity or source agents |
| Automation definition | Display permitted name and enabled status | Authority to manage the definition and applicable folder management | Inspect private configuration, secrets or execution history; trigger, enable, disable, edit, or redirect delivery |

The latter three rows are proposed contracts for future phases. Manager administers the containing folder. Publishing artifacts and operating tasks or automations require their own domain permissions. Apply each type's domain checks for the actual action, even when access comes through an ancestor. Filing a task in Marketing gives it a home. Its execution scope continues to come from its own agent selection or its host report. Keep agent-owned configuration and individual automation runs under their existing owners unless a separately manageable definition is introduced.

**Future acceptance cases, as each type ships:**

- Seed one Marketing folder with a private agent, a published dashboard, an unpublished revision, a task, and an automation definition. Confirm each surface returns the same folder ID and authorized type-specific cards. A Viewer can query the inheriting agent, open the eligible release and see permitted task and automation summaries. A direct agent-only recipient can use the agent independently. Test direct URLs as well as lists. Publish another version and verify stable placement. Repeat with direct artifact and project grants.
- Move or restrict one item, then the shared folder. Compare effective rights for a Viewer, a Manager, someone in both groups, and a recipient of a direct child share. Check moves made from the Agents surface also preview the other applicable resource types without exposing hidden names. A folder with a task as its only child cannot be deleted from Knowledge Explorer.
- With a stubbed dispatcher, verify sharing or moving the task changes only its folder audience. Its schedule, run identity, prompt, selected agents, recipients and delivery follow existing policy. Viewers cannot run or modify the task or automation, access private outputs or retrieve webhook credentials. Executions continue to appear in the definition's history. Selecting the mixed folder in the composer never executes these items.

Code references: `backend/app/models/artifact.py:7`, `backend/app/services/dashboard_access.py:157`, `backend/app/models/scheduled_prompt.py:14`, `backend/app/routes/scheduled_prompt.py:79`, `backend/app/models/agent_automation_run.py:42`.

## Agents-first delivery slices

1. **Tree and effective permissions.** Add org-scoped folders with stable IDs, parent, creator and inheritance settings, plus nullable agent home and inheritance setting. Existing agents start at root with existing effective access. Extend the resource grant model for folders and unify direct, inherited, group, org-wide, limited-access and admin checks for listing, detail, mutation, and runtime. Expose the common tree under `/api/folders`. Implement create/list/get/update, grant create/edit/revoke, move preview/commit and empty-folder deletion. Validate resource and principal org membership on every write. In one transaction, recheck authority and tree state, prevent concurrent cycles and duplicate grants, and invalidate affected permission caches after commit, including on user/group membership changes. Keep folder IDs out of connection and agent ID lists.
2. **Creation and Knowledge Explorer.** Wire the placement and ensure contracts above through the existing agent and demo services. Implement tree, folder panel, access editor, direct sharing of one agent, and moves. Use the same effective access across picker search, tree, direct URLs and existing agent views. Scope counts and search to the caller. Translate new strings through repo catalogs, check Hebrew RTL and keyboard/touch navigation.
3. **Report, API and focus.** Proposed optional report field `agent_folder_id` on create/update/read. A non-null folder selects folder scope, explicit nonempty `data_sources` selects pins, and specifying both in one request returns a validation error. A new explicit scope supersedes the prior one. Omitted scope fields on update preserve selection. Clearing the selection deliberately returns to existing Auto semantics. Explicit folder selection takes precedence over project defaults. Keep ordinary completion requests report-scoped. Forks and follow-host spawned reports carry the folder ID through project and root paths. If resolution fails, surface the unavailable scope. Explicit task picks still win.
4. **Runtime parity.** At run start resolve only descendant agents against the running identity, app/channel restrictions and connection usability. Bind agent discovery, instruction reads, saved-query access, agent tools, client construction, and estimates to the authorized working set. Reuse AgentV2 focus: one eligible agent focuses immediately, multiple agents start with a roster unless prior focus is still valid. Global and in-scope connector behavior follow existing policy. Load focused agents' always-on instructions before data use, retain smart retrieval and deduplicate shared instructions. Preserve independent saved-query, MCP, personal-connector, and training/eval permission rules. Clearing focus stays within the selected folder. An empty, deleted, or inaccessible selected folder yields an explicit unavailable scope. Schedules with their own explicit agent selection retain that selection; queued turns and schedules using folder scope re-resolve when they start. Eval suites keep their existing home and case targeting.

5. **Stored outputs and evals.** For a folder-scoped report, persist the agent IDs that contributed data to each stored output. Require the existing report/source policies and current read authority on all contributing agents before exposing that output, including transcripts, copied assets and MCP App resources. Use the union of contributing targets if a response bundles inseparable outputs. Folder selection with empty report attachments must never invoke the legacy text-only/read-any-member shortcut. For a fresh folder report with no results, require folder access plus normal report access. With unverifiable output provenance, withhold the output pending verified attribution. Keep legacy report behavior outside this scope. Use the eval submission, dispatch and read rules above for test runs.

6. **Compatibility gate.** Run each selector row using legacy records and new folder-organized agents. Record the initiating surface, actor, selected value, normalized request, resulting persisted state and actual run/filter result.

## Verified baseline and code map

- Knowledge Explorer renders a flat agent iteration at `frontend/components/KnowledgeExplorer.vue:225`. Its New Agent gate at line 2902 already recognizes org and per-connection creation. The existing directories at `backend/app/models/instruction_directory.py:7` organize instructions within an agent.
- `backend/app/routes/data_source.py:134` checks org/connection creation. `backend/app/core/permission_resolver.py:71` defines agent management implications. `backend/app/routes/rbac.py:344` gates resource-grant changes.
- `backend/app/schemas/report_schema.py:16` and line 57 define creation and update selection. `backend/app/ai/tools/implementations/agent_focus_common.py:147` resolves the run roster. `backend/app/ai/agent_v2.py:1620` scopes instructions, and `backend/app/ai/agents/planner/prompt_builder_v3.py:1146` renders the roster.
- `backend/app/routes/entity.py:124` checks every attached agent for saved-query viewer runs. `backend/app/routes/mcp_apps.py:87` uses a report-access helper. Verify downstream visibility on each surface before shipping inherited query access.

The following are code-review observations at the baseline. The behavioral cases below exercise them during implementation.

| Current path | Gap to exercise |
|---|---|
| `backend/app/core/permission_resolver.py:123`, `backend/app/routes/rbac.py:372` | Any direct agent grant implies view/schema. Derive only the intended agent rights and scope generic grant enumeration, including unfiltered and principal-filtered reads. |
| `frontend/pages/reports/[id]/index.vue:5254`, `frontend/components/dashboard/DashboardSettingsModal.vue:653` | An ID event syncs member defaults. Filtering unavailable dashboard picks can produce the Auto wildcard. |
| `backend/app/ai/tools/implementations/agent_focus_common.py:264`, `backend/app/ai/tools/implementations/create_agent.py:391` | Training search considers all managed agents and creation attempts to attach the new agent to the report. |
| `backend/app/services/fork_service.py:388`, `backend/app/services/scheduled_prompt_service.py:687`, `backend/app/core/derived_access.py:84`, `backend/app/routes/mcp_apps.py:87` | Fork and scheduled spawn copy attachments. Empty attachments can pass the report visibility helper, including for MCP App reads. |
| `backend/app/services/test_run_service.py:238` and `:1836`, `backend/app/routes/test.py:105` | Stub creation commits before attachment and catches attachment errors. Empty attachments feed Auto. Historical authorization loads the current cases. |
| `backend/app/routes/test.py:816`, `backend/app/services/test_run_service.py:815` and `:2296` | A stream reader can reach pending execution with the opener’s ID. In-flight deduplication compares build and cases within the org. |

### Loop A · repeatable baseline

From a fresh sandbox with `BOW_SANDBOX_DEVBOX` unset or zero, use Python 3.13 and the repo's frozen environment. Run the following in one shell. The probes inspect the registered API and request schemas without writing to a database. An existing managed DevBox can use its provisioned backend environment directly.

```bash
cd backend
uv sync --frozen --extra dev
export TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db
uv run python -W ignore - <<'PY'
from main import app
from app.schemas.report_schema import ReportCreate, ReportUpdate
from app.schemas.data_source_schema import DataSourceCreate
from app.ai.tools.schemas.create_agent import CreateAgentInput
from app.ai.tools.schemas.create_demo_dataset import CreateDemoDatasetInput

api = app.openapi()
paths = api["paths"]
assert "post" in paths.get("/api/data_sources", {}), "unexpected app or API prefix"
folder = "00000000-0000-4000-8000-000000000001"
checks = {"public folder create route": "post" in paths.get("/api/folders", {})}
checks["ensure folder route"] = "post" in paths.get("/api/folders/ensure", {})
for schema in (DataSourceCreate, CreateAgentInput, CreateDemoDatasetInput):
    checks[f"{schema.__name__} accepts folder_id"] = "folder_id" in schema.model_fields
demo = paths["/api/data_sources/demos/{demo_id}"]["post"]
body = demo.get("requestBody", {})
def has_field(schema, field):
    if "$ref" in schema:
        return has_field(api["components"]["schemas"][schema["$ref"].split("/")[-1]], field)
    return field in schema.get("properties", {}) or any(
        has_field(part, field) for key in ("anyOf", "allOf", "oneOf") for part in schema.get(key, [])
    )
checks["prebuilt demo accepts optional folder_id body"] = not body.get("required", False) and has_field(
    body.get("content", {}).get("application/json", {}).get("schema", {}), "folder_id"
)
for schema in (ReportCreate, ReportUpdate):
    saved = schema.model_validate({"agent_folder_id": folder}).model_dump(exclude_unset=True)
    checks[f"{schema.__name__} retains folder"] = saved.get("agent_folder_id") == folder
for name, ok in checks.items():
    print(f"{name}: {ok}")
assert all(checks.values()), "folder API contract is missing"
PY
```

Observed at baseline: the existing agent creation route is present. The proposed folder creation route is absent. Both report schemas discard the proposed folder selection. The ensure route, three creation inputs and optional prebuilt-demo body also lack their proposed contracts. All eight checks should become true with slices 1–3. Passing this probe proves API shape only.

### Loop A · behavioral acceptance for implementation

First add API cases in `backend/tests/e2e/rbac/test_agent_folders.py` using the existing auth, organization, grant and SQLite-source fixtures. Run those against base and record the first unmet assertion. Run the same tests after each slice. Use real API/DB boundaries and a deterministic LLM stub for the routing scenario. The file and post-change outputs are implementation deliverables.

| Fixture and action | Observable pass condition |
|---|---|
| A Viewer, manager of agent A only, creator on C only, org-wide creator, unrelated folder Manager and full admin each create at root and under someone else’s folder | Exercise each root and parent creation rule through HTTP, including authority from group membership. A newly created private root has a direct Manager. |
| Grant Viewer and Manager on Marketing to individuals and groups. Overlap the groups, downgrade, revoke, remove a member, and add an agent under Acquisition. | Viewer can browse, read the agent schema and query it, including the new agent. Manager also edits the agent, shares its folder and manages its evals. After group removal or downgrade, verify the next request across list, direct ID, query, mutation and eval routes. Any independent grant still applies. |
| As an inherited Manager, restrict Compensation and separately restrict a single agent. Try no replacement, an empty group, then an active HR group as direct Manager. Later remove that group's final member. | First two edits roll back. The third installs the boundary and direct grant atomically. Marketing Viewers and Managers immediately lose inherited access. HR retains management, full org admin can recover the branch, and an independent direct Viewer still queries. Repeat with an org-public agent and verify its wider query audience. |
| Request the folder’s access panel, generic grant listing, reverse lookup by principal and an unfiltered org grant listing as Viewer, Manager, unrelated member with view_members and foreign-org caller. | Only authorized rows and principals appear. A direct child recipient cannot infer a private parent from a grant ID, group name, breadcrumb or count. |
| A direct-only agent recipient, an Analyst, and a Lead browse/search Marketing with a limited-access Leadership folder and a separately restricted agent | Each sees precisely their authorized items and counts. Direct-only user opens the agent without parent metadata. The same rules apply to direct IDs and the picker. |
| Use a limited agent with a direct manager to vary agent, source and destination management independently. Repeat with an ordinary inheriting agent and with moves to/from root. | Missing any required capability denies the move. For an ordinary agent, inherited management satisfies the agent check. Authorized preview predicts resulting audience. Commit preserves direct grants, boundaries and stable deep links. |
| Move a subtree containing a limited branch; try a cycle, simultaneous reciprocal moves, a foreign-org destination, stale preview and deleting a nonempty folder | Tree remains consistent and old placements survive rejected writes. Hidden descendants survive. An empty folder can be deleted by its manager. |
| Start with pre-migration public and private agents, an existing project default, a pinned report, and an ordinary Auto report | Migration preserves access, identities and selection behavior. Add a folder-selected report to that project and check its explicit scope wins. |
| Pin Marketing; add, move, revoke or disable an agent between turns and before dispatching a queued turn | Roster, schema, scoped instructions, eligible saved queries and MCP tools match the current caller. Clearing focus stays in Marketing. An empty branch cannot widen to org Auto. Compare estimator and real completion. |
| Ask a stubbed CPA question with two eligible agents sharing an instruction, a third private agent, and an eval case targeting two agents | Authorized roster first, relevant agent's rules before tool use, one copy of a shared instruction by ID when both agents are focused, and no routine eval execution. Cross-agent saved queries and restricted training actions retain their existing authorization. |
| Generate a folder-scoped output from A and B with zero explicit report attachments. Access the report, forked assets and MCP App output as the author, an unrelated member and a user who later loses B. | Assert stored-output access against the original contributing IDs and current reader rights. The empty-attachment shortcut never admits the unrelated member. Revoking B prevents reading the combined output. Scheduled delivery also retains its recipient checks. |

### Creation and selector regression cases

| Action in the disposable sandbox | Required observation after implementation |
|---|---|
| Create via the API, wizard and training tool once with no destination and once with an authorized folder. Repeat with Viewer at the destination, a foreign or deleted folder, and a just-revoked grant. | Successful agent has the correct home and effective audience on first read. Denied creates leave no agent or attachment. Existing pins, tables/tools and connection checks still work. |
| Ensure a partly existing path twice and concurrently from two sessions, then try an invalid component, hidden collision, foreign parent and a missing component beneath a read-only parent. | Same leaf ID across successful retries, one row per component, no duplicate manager grants. A failure reveals no hidden paths and leaves no partial new chain. Exercise at root and below an explicit parent on both DB engines. |
| Install a fresh prebuilt demo at root and in a shared/private folder in separate orgs. Retry in an org where it already exists elsewhere. | Legacy root audience survives. Folder-targeted demo inherits its intended audience immediately. No second item, move or visibility change on reinstall. No private demo metadata returned to an unauthorized caller. |
| Generate a demo with two checked agents, one unchecked, no agents, a connection-only creator and a revoked destination after review. Retry after partial progress. | Reviewed successes have the specified home, no duplicates or unintended root placements. Connection-only behavior, skipped-agent reasons and cleanup follow the install outcome. |
| For every selector row, seed an inherited agent, direct-only agent and restricted sibling. Open existing selection, browse, cancel, reopen, select a leaf, save, reload and exercise its downstream action. | Assert emitted IDs, HTTP payload, canonical readback and final result; include desktop and touch. Browse/cancel sends zero selection writes. Repeat empty, all, one, multiple, unavailable and needs-sign-in states wherever meaningful. |
| Seed two agents, a multi-agent eval case in a differently homed suite, an active/draft pair, an authorized global case and evaluators with full vs partial target authority. Move an agent; add an unrelated sibling; revoke authority before the next run. | Exercise editor Run, selected-case Run, Run evals, tree suite, build/batch, instruction strip and tool. Assert actual executed case/agent/build IDs and results. The moved agent retains its ID; new siblings do not join an existing case. Explicit targets never collapse to global. Partial authority cannot execute a mixed-target case. An intentional global case retains its own org-level gate. |
| Set membership defaults to agent X, select Marketing on a report, reload and fork. Run a follow-host schedule in the project and on the project-to-root fallback. Repeat with explicit task agent Y and a revoked folder. | Folder selection only writes the report field. No membership-default PUT occurs, X survives, fork and follow-host keep the folder, and the override uses Y. A missing or empty scope cannot fall back to project or org Auto. |
| As a trainer managing agents inside and outside Marketing, search/focus and create one outside. Reopen dashboard chat settings with an unavailable explicit pick and save an unrelated edit. | Training only offers managed agents within scope. The created agent stays at the chosen destination and the report retains Marketing. Dashboard settings preserve explicit picks. No wildcard is emitted until Auto is deliberately chosen. |
| Queue A+B, then remove B, revoke B, or force the stub attachment to return only A or throw. Repeat with a single lost target and a deliberately global case. | Verify persisted failure and zero model/tool calls for each invalid explicit case. No partial or Auto execution. Authorized global runs record their resolved scope. |
| Two evaluators request the same build and cases with different connection identities. Open each other's streams first, reconnect and retry. Revoke the requester’s access before dispatch in a second run. | Separate runs use their own requester and credentials. Same-requester retries may reuse an identical authorized snapshot. Reconnect never starts a second execution. Revocation stops dispatch even if the observer still has access. |
| Run a mixed suite, retarget a completed A+B case to A only, then delete the case. Query history, status, transcript, stream and stop as operators with full and partial authority. Repeat with a legacy result lacking a trustworthy snapshot. | Current authority on every original target governs each result. Partial readers only see independently authorized results, with matching counts and stream events. A partial operator cannot stop the entire run. Historical authorization survives case edits/deletion and uses the defined legacy fallback. |

Implementation verification commands, from `backend/` once the new cases exist:

```bash
uv run pytest tests/e2e/rbac/test_agent_folders.py tests/e2e/rbac/test_agent_auto_scope.py --db=sqlite -q
uv run pytest tests/e2e/rbac/test_agent_folders.py tests/e2e/rbac/test_agent_auto_scope.py --db=postgres -q
```

Existing regression entry points for the implementation pass (run backend commands from `backend/` and Playwright from `frontend/` against the disposable stack):

```bash
uv run pytest tests/e2e/rbac/test_rbac_evals.py tests/e2e/rbac/test_rbac_eval_tools_scope.py tests/e2e/rbac/test_rbac_eval_suite_authority.py tests/training/test_create_demo_dataset_tool.py -q
```

```bash
npx playwright test tests/home/agent-scope.spec.ts tests/evals/run-evals-modal.spec.ts --project=features
```

These existing tests cover parts of the current contract. The additional matrix cases and per-row actual results are deliverables for implementation.

### Loop B · live UI evidence for implementation

In a fresh sandbox boot with `tools/agent/boot_stack.sh`. Seed a disposable organization with `cd backend && uv run python ../tools/agent/seed_org.py --demo` and create the matrix users and grants through the API. On a managed DevBox use the provisioned stack and org instead. Use separate authenticated browser contexts for the agent manager, folder manager and viewer.

Capture /agents and the Share panel on base and feature at the same viewport. In the role menu verify both short hints. Check a direct and an inherited row, the limited-access switch, and the org-public exception. Confirm keyboard and touch expose the same information as hover. Record one uninterrupted create → nest → share with group → edit role → move → revoke flow. As the recipient, query an inherited agent as Viewer, edit it and run an eval as Manager, then refresh after revocation. Open a directly shared agent and verify tree, search and picker disclosure. Record a second flow choosing Auto in Marketing and losing the last eligible agent before the next turn. Cover English desktop, Hebrew RTL, keyboard and a touch viewport. Store redacted before/after images and flow evidence with the UI change under `media/pr/agent-folders/`. No live provider or warehouse credentials are needed for these scenarios.

## Implementation evidence · November 14

The Loop A schema probe went from zero of eight contracts present on base to eight of eight present. The new HTTP regressions live in `backend/tests/e2e/rbac/test_agent_folders.py`. The browser regression lives in `frontend/tests/data_sources/shared-folders.spec.ts`.

| Verification | Observed |
|---|---|
| SQLite | 37 tests passed across shared folders, Auto scope, eval RBAC, eval tools, suite authority, generated demos and prebuilt demos. |
| PostgreSQL | Both shared-folder HTTP tests passed against a newly created local PostgreSQL database using the external database test fixture. |
| Real HTTP requests | Admin installed a demo in Marketing. An individual Viewer saw the agent and received a denial when starting its eval. A member inheriting Manager from a group created a suite and case and executed it through the stream. The terminal result was pass using a local OpenAI-compatible stub. Removing the group membership removed folder access. Re-adding it restored Manager. An individually restricted agent disappeared for its group Manager. The final dispatch run passed with the exact target. Concurrent ensure requests returned one leaf ID and a cyclic move was rejected. |
| Browser | Separate signed-in accounts opened Marketing. The Manager created, renamed and moved a nested folder and reopened it at the destination. The admin inspected the share dialog. The Viewer browsed in English and Hebrew on a touch viewport. A Manager opened the saved eval result and selected Marketing from the report composer. The existing home Auto, pin, search and Run evals modal specs passed. The new Explorer test passed after scoping its New folder locator to the shared folder region. |
| Evidence | Base and feature screenshots, plus the recorded Manager workflow, are under `media/pr/agent-folders/`. |

The stored-output check currently records a conservative union of the run roster for the entire report. A reader can consequently lose access to earlier outputs that used fewer agents. Mixed-target run views require authority across all results. Direct result reads use the immutable snapshot on that result. These are stricter than the finer-grained designs above. The individual agent inheritance action currently requires an active owner when restricting access.

The full compatibility table remains the release checklist. Dashboard stale picks, scheduled spawn and report forks, generated-demo retries after partial success, concurrent move/revoke races, and every integration-specific picker have not been verified end to end in this pass.

### Deep folder and modal regression

From `frontend`, run `npx playwright test tests/data_sources/shared-folders.spec.ts --project=features` against a seeded stack. The browser creates and renames a folder, then walks a 64-level path with a long terminal name. Under that folder it moves Quarterly review from East / Campaigns to West / Campaigns and verifies the parent ID through the API. The sharing check assigns a direct Manager, turns limited access on and off, and checks the inherited Manager label after each change. The same dialog is checked at 1360 and 390 pixels wide. Screenshots and the deep-tree browser recording are in `media/pr/agent-folders/deep/`.

### Folder roles, pointer moves and planner rosters at 300 agents

Run the following from the corresponding backend and frontend directories. The backend test provisions its own warehouse fixture and stubs the outgoing model call. The browser uses a signed-in admin and creates its own role, nested folder and agent.

```sh
TESTING=true .venv/bin/python -m pytest -q tests/e2e/rbac/test_agent_folders.py --db=sqlite
npx playwright test tests/data_sources/shared-folders.spec.ts --project=features
```

The scale fixture creates exactly 300 agents and 64 folders. One agent belongs outside the branch and one stops inheritance. The real completion endpoint advertised 298 eligible agents, then 148 after a branch was restricted and a fresh report opened. The test checks the complete runtime set separately from the bounded text the model sees. Within the same small folder report the prompt went from two available agents with no focus to one focused agent after a move. Restricting both branches returned HTTP 409 with no planner call. Continuing the original report after losing access to its previous sources returned 404. Prior to the change, the completion wrapper converted the empty-folder response to HTTP 500, and a two-agent branch used the ordinary Auto small-roster threshold.

Roles now exposes manageable folder paths alongside individual resources. Can query and Can manage use the existing folder view/manage grant representation. A group-assigned role propagates to inheriting agents, a limited agent stops it, and granular agent grants remain separately editable. Share shows role-derived grants read-only. Folder management does not grant connection administration. The browser exercised a saved role, a nested hover-to-open drop, a drop to organization root and separate query-only and group-manager sessions. The UI sandbox started with 300 agents and 490 folders before the browser created its own fixtures. Screenshots and the browser recording are under `media/pr/agent-folders/scale/`.

Observed results: four folder integration tests passed on SQLite and PostgreSQL. The related role principal, editable-role, eval, Auto scope and roster suites passed 32 tests. Three Chromium specs passed. The 64-level case also followed the inherited permission back to its source folder. Live model response quality and simultaneous-user load were outside this run.


## Folder knowledge verification

The API persists separate folder links for instructions, uploaded files and eval suites. Folder-owned resources keep their owner identity after detachment. A custom instruction editor can open the folder and author a routing rule. A file curator can upload and manage file links. Eval authors can create suites and cases. Running requires both Can query and Manage evals. Changing a shared suite or case requires edit authority across its folder placements. Saved queries remain outside this release.

The report keeps the selected folder while agent focus changes. Folder knowledge retrieval checks the selected folder, publication and existing resource restrictions. The planner can search authorized descendant agents omitted from the initial prompt. Source history conservatively records eligible folder files, instructions, revisions and the run roster. A later reader can lose access to the entire output after one of those sources becomes unavailable. Folder-launched explicit eval cases retain their agent pins and require target-specific eval authority. The worker checks both branch membership and the saved targets again.

| Check | Evidence |
|---|---|
| Server authorization | The group role test cycles through each custom capability alone, then combines eval curation with query. It checks sibling sections, child folders, active-agent discovery, direct file content and an embed URL issued before revocation. It creates and streams a routing eval, opens its result, downgrades the role and checks that result access is lost. A linked agent-targeted suite rejects an actor missing agent eval authority and succeeds for an authorized actor. |
| Routing through the actual completion API | With 300 seeded agents, the first roster contains 298 eligible agents. A deterministic model double reads the published rule, searches and selects the returned stable ID. Changing the rule changes focus from PortfolioAgent289 to PortfolioAgent287. Restricting one branch leaves 148 eligible. Restricting both returns HTTP 409 before planner dispatch. A child-only rule is absent from the parent prompt. |
| Discovery query budget | Measuring permission-filtered discovery plus exact-folder rule loading on SQLite gave 23 SQL statements on first access and 16 on repeat at both scales. With 30 active portfolio agents the two passes took 15.1 and 5.5 ms. With the full fixture they took 16.4 and 10.8 ms. This is a single-machine sample, with a fresh session and then the same session reused. |
| Live provider quality | The separate two-candidate replay with gpt-4.1-mini matched five of six expected outputs. It asked for clarification on both ambiguous and unrelated prompts. With Attribution designated, “Break down what we paid to win customers by channel” selected Paid Media. This probe measures a small sample of model decisions. It does not execute the 300-agent stack. |
| Live routing inside the 300-agent fixture | The model receives the production planner messages and native search/focus tool definitions. Its returned calls execute against the real application and the test asserts persisted focus after each of two published revisions. The first runs found that the model skipped a rule in the separate context turn and focused the wrong agent. Moving authorized instructions alongside the request improved selection. A repeat exposed two tool issues. Folder search now returns no matches when there are none, and focus accepts an exact unique name from the authorized roster and persists its stable ID. Both target selections passed in each of two consecutive runs after these changes. Other planner tools and the final answer are stubbed in this focused test. |
| Concurrent permission change | A two-client test races revoking a direct Manager grant against creation of a folder instruction. Either serialized write order is valid. It verifies that the final row count matches the accepted writes and that subsequent reads and writes are denied. The race passed on PostgreSQL. |
| Migration and storage | SQLite downgrade from folder06 to folder03 and upgrade to head preserved 303 agent rows. The foreign key check returned no violations. Folder integration tests also ran on isolated PostgreSQL. |
| Browser | Eight Chromium regression checks passed, including 64-level nesting, role assignment, drag-and-drop, reopening a saved instruction and report agent selection. Three independent Chromium sessions exercised admin, custom instruction editor and query-only access in the scale workspace. The admin authored a rule, a suite and a case through the UI. |
| Regression scope | Backend runs included 32 folder, eval RBAC, roster and smart instruction tests plus 11 additional instruction/RBAC/Auto checks. These runs overlap with the focused reruns. Additional focused suites passed 13 instruction-search checks, nine folder/build checks and 14 folder/eval-suite checks. All 27 added locale keys are present in all ten catalogs. The broader catalogs had pre-existing differences. The isolated Nuxt production build completed. The test harness emitted deprecation and SQLite event-loop teardown warnings. |

Run the following against a disposable database from the repository root. The provider replay reads its credential from the environment. The folder integration test uses a deterministic model double.

```sh
(cd backend && TESTING=true .venv/bin/python -m pytest -q tests/e2e/rbac/test_agent_folders.py tests/e2e/rbac/test_rbac_builds.py --db=sqlite)
(cd backend && TESTING=true .venv/bin/python -m pytest -q tests/e2e/rbac/test_agent_folders.py --db=external)
(cd frontend && npx playwright test tests/data_sources/shared-folders.spec.ts tests/home/agent-scope.spec.ts --project=features)
backend/.venv/bin/python tools/agent/check_folder_routing.py
```

To exercise the two real routing choices inside the 300-agent fixture, set `FOLDER_ROUTING_LIVE_KEY` in the environment and run the single `test_folder_role_roster_at_scale` test. The fixture only submits synthetic prompts. Leave the variable unset for deterministic CI.

Set `TEST_DATABASE_URL` to an isolated PostgreSQL database for the external run. Set `OPENAI_API_KEY_TEST` for the six live requests. The browser tests require the authenticated seeded stack described above. Role screenshots and the case editor are in [the screenshot and recording gallery](../../media/pr/agent-folders/index.html).

Outstanding release evidence includes role/group edits racing a queued eval, every integration-specific selector and sustained multi-user load. The matrix above remains the acceptance checklist for these cases.

### Folder activity, agent discovery and authoring

| Surface | Permission and behavior | Regression coverage |
|---|---|---|
| Monitoring and Diagnosis | An agent manager sees a minimal activity row for each managed agent whose use was recorded. A query grant alone does not grant console access. Opening or exporting a conversation checks the whole report, including the roster exposed in planner snapshots and historical folder knowledge. A revoked grant takes effect on the next read. | `test_folder_activity_only_discloses_observed_managed_agent`, console scope and Diagnosis tests |
| Trace overview | The selected folder and observed agents are recorded per execution. The displayed names use current metadata. Historical executions without attribution do not produce activity rows. An MCP call through a connection shared by multiple eligible agents cannot identify one specific agent. | Administrator and single-agent manager browser sessions |
| Training and knowledge capture | Folder instruction curators can open a training report when training is enabled. Without folder query permission its agent roster and query clients are empty. Folder tools require a specific authorized destination. Instruction builds keep their review path. Automated folder evals go into an exact-folder drafts suite. | `test_folder_role_levels_and_agent_exceptions`, `test_folder_authoring_tool_destinations`, and the three exact-folder instruction search modes |
| Agent discovery | Agent detail links to its visible folder. The roster and search show only the visible suffix of the path. Search can restrict candidates to the visible home folder of a known agent. Pins and limited-access boundaries constrain discovery. | `test_folder_role_roster_at_scale`, agent focus scope tests, and the location-link browser check |

The local reproduction initially returned both the parent and child routing instruction in knowledge mode. The exact-folder test now runs in chat, training and knowledge modes. Browser verification also caught a trace modal that opened before it subscribed to the open event. The regression checks exercise opening the populated trace, the restricted activity row and a curator entering training from the folder.

Run the focused checks from the backend directory:

```sh
.venv/bin/pytest -q tests/e2e/rbac/test_agent_folders.py tests/e2e/test_search_instructions_chat_mode.py tests/e2e/rbac/test_rbac_console_scope.py tests/e2e/rbac/test_diagnosis_explorer_scope.py
```

[Seeded browser screenshots](../../media/pr/agent-folders/index.html)

### Knowledge Explorer inventory and instruction scope

On a disposable database copy, compare the management inventory from GET /data_sources
with GET /data_sources/active for the same organization and user. An authorized
inactive agent should remain in the first list. Expand its folder in Knowledge
Explorer and compare the Instructions, Files, and Evals branches with the exact
folder's permission-filtered knowledge endpoints.

For the tool path, run create_instruction with a selected folder and
destination=folder, then edit_instruction on its returned ID with a changed title
and table_names=[]. Inspect the instruction, folder_instructions link and both
instruction_versions. The live scope and both snapshots must be targeted, the
folder ID must match, and clearing table references must preserve the audience.
Repeat create_instruction without any agents, folder or destination and confirm
that it reports an error. Use a database copy so the training build is isolated.

### Folder regression checks for manual verification

Use a disposable organization with a parent and child folder, separate parent and child managers, a query-only member and an instruction curator. Use the application or authenticated API.

* Bulk replace an agent-only instruction’s targets with an empty list. Confirm the persisted scope and new build version are global. Repeat on a rule also attached to a folder and verify it retains its folder target.
* Start a folder report with a text file and no uploads, project files or agent library files. Check the planner catalog and read the file. Revoke folder access before a scheduled run with pinned agents and verify the run stops before planning.
* Move a nested folder to root with only inherited managers. Assign an active direct manager after the refusal, then preview and approve. As a child-only manager, preview moving an agent out of the child and check that no inaccessible ancestor principal appears.
* Remove a folder’s last instruction, then delete the folder even if a historical report references it. Confirm the report retains its original scope. Live knowledge and child agents should still prevent deletion.
* Compare Auto discovery, direct agent detail and report focus for a query-only user and an agent manager with draft and development agents. Check both periods of the console comparison against each period’s plain metrics.
* Create a rule through the folder panel as a curator and inspect the pending build before and after manager approval. Refuse an ambiguous chat-tool destination and read the returned hint. Check custom role grants in the role editor and the read-only role entries in the share dialog.
* For legacy evals, compare the null-scope results before folder09 with the recovered explicit targets afterward. A changed case or inconsistent report attachments must leave the result restricted. Attempt a folder04 downgrade on a disposable backup with folder-only rule history and verify it stops before dropping its scope columns.

### Follow-up manual checks

- With a published agent and an agent subsequently moved to development pinned to one report, run a turn as a query-only member. The runnable agent should remain in scope. A report with only an unavailable pin should stop before planning. The schema and paginated full-schema views should use the same lifecycle visibility as agent detail.
- As an organization instruction manager, attach a global rule to two folders. Remove the first link and inspect its targeted placement. Remove the second and check the global list and live version. Repeat with a folder-owned rule and confirm it stays targeted.
- Open a historical folder report after deleting its empty folder. Check the owner, full admin and a former folder viewer. Retained live agent and table permissions still apply. Starting a new run requires an available scope.
- Publish a curator’s pending rule as the manager of its folder with unrelated instructions on main. Check the build publish button and the individual instruction review. A curator or a manager of a different folder should not be able to approve it.

### Saving one instruction to two folders

In a disposable organization with two folders, use an account that manages both. From Agents, create a published instruction, select both folders, and save. Reopen each folder and check that the same instruction ID appears. Inspect its active build version and confirm both folder IDs and targeted scope.

Before the fix, the create request returned 503. The backend logged a 403 from the folder check in `BuildService.promote_build`: `InstructionService._auto_finalize_build` passed the build but omitted the acting user. After passing the actor, the same create route on a disposable SQLite copy succeeded. A fresh database session confirmed both folder links, published status, targeted scope, and both folder IDs on the main build version.
