# Feedback loop — compact agent landing page

The agent overview previously placed configuration actions across a dense header and rendered the entire primary instruction before conversation starters. The requested landing page centers the agent identity, keeps its title the same size as `/agents`, and puts report creation ahead of optional content.

## Validated baseline

`frontend/components/KnowledgeExplorer.vue`'s `agentView` branch rendered the activity chart, Self Learning, New report, export, sharing and stage controls in its header. Below it, connection chips and all four resource counts preceded the complete `InstructionText`. The primary-instruction empty state and empty conversation-starters section also rendered for readers.

The synthetic browser baseline reproduced that layout with a long instruction. The last section of the instruction was present in full, ahead of the starters. See `media/pr/agent-landing/before.png`.

## Run the verification

Use the installed frontend dependencies and a running development frontend. The harness defaults to port 3100; override `BASE_URL` to use another development port.

```sh
node frontend/tests/instructions/agent-landing.mjs
node frontend/tests/data_sources/connection-signin-status.mjs
```

The harness temporarily creates an unauthenticated page containing the real `KnowledgeExplorer`, then removes that page in `finally`. It intercepts API boundaries with synthetic agents, instructions, permissions, activity and connection states. It does not use real credentials or change real agents. Run it sequentially with other dynamic-route evidence harnesses.

Before the layout change, `BEFORE=1 node frontend/tests/instructions/agent-landing.mjs` captured the baseline. That mode is for the pre-change component, not an alternate production layout.

## Implementation

- `frontend/components/KnowledgeExplorer.vue`: centered, bounded landing column; 18px title matching the `/agents` heading; 28px icon; description and subtle lifecycle/privacy controls; selected resource totals shared with the tree; zero counts omitted and unavailable counts kept distinct from zero.
- The blue New report action is gated by report permission, personal connection access, disabled stage and admin-only visibility. Missing sign-in opens the existing Agent Card. Partial access keeps both New report and Sign in available. Listed agents can open their landing page while personal authentication is pending; protected resource tree sections retain their existing gates.
- Activity remains in the top corner and disappears when empty. The actions menu retains Connections, Settings, Manage connections, starter editing, Self Learning, training and the existing ZIP export, with their permission gates.
- `frontend/components/instructions/AgentInstructionPreview.vue`: unlabeled instruction content, capped at 120px until expanded. It measures rendered content to offer Read more only when needed, responds to resizing, preserves Markdown and references, and expands when a nested control receives keyboard focus. Edit and Change remain available to managers.
- Missing starters render no empty section. Missing primary instructions show only a small Add instruction / Choose existing row for managers.
- `PublishStatusControl.vue` and `PrimaryInstructionPicker.vue` add opt-in subdued appearances. Their default styling remains available to existing callers. The landing picker aligns to the available edge, and the compact lifecycle menu is bounded to narrow viewports.
- New labels are present in all ten locale catalogs. Existing catalog key drift is unchanged.

## Observed verification

The browser loop passes for:

- Centered content and a title matching the `/agents` heading size.
- Instruction count consistency, omitted zero counts, expansion/collapse and edit/create entry with cancellation.
- Downloading the existing agent export using the selected agent ID and expected filename.
- Correct report and conversation-starter request bodies, duplicate-click suppression, and recovery after a synthetic report-service failure.
- Manager, member and view-only action gates; no manager empty-state prompt for members.
- Full sign-in requirements opening Agent Card; partial access retaining usable report actions.
- Empty activity/starters, Hebrew, mobile overflow, narrow menus, and dark instruction-text contrast.
- Shared connection-status regression checks.

The four touched Vue components pass SFC script/template compilation. `git diff --check` passes. This is deterministic frontend verification; report requests use a simulated failure response to inspect submission and recovery without creating real reports. It is not an end-to-end production report execution test.

## Evidence

- `media/pr/agent-landing/before.png`
- `media/pr/agent-landing/after.png`
- `media/pr/agent-landing/empty-manager.png`
- `media/pr/agent-landing/empty-member.png`
- `media/pr/agent-landing/partial-access.png`
- `media/pr/agent-landing/he.png`
- `media/pr/agent-landing/mobile.png`
- `media/pr/agent-landing/dark-mobile.png`
- `media/pr/agent-landing/flow.gif`

## Follow-up — hide instructions before personal sign-in

The landing page initially displayed the primary instruction whenever one existed, even when every connection needed personal sign-in. The tree already hid the instruction group for that state. The screenshot reproduction confirmed that both the instruction preview and its Edit/Change actions remained visible beneath Sign in.

The complete primary-instruction area (preview, editor and manager empty state), plus its count shortcut, now uses the same `agentAccessBlocked` check as the tree. It is not mounted while sign-in blocks access. Signed-in agents and agents with at least one usable connection retain their instruction area; this preserves the existing partial-access behavior. The Agent Card's metadata counts and management permissions are unchanged.

```sh
# Reproduce on the pre-fix component:
SIGNIN_ONLY=1 SIGNIN_BEFORE=1 node frontend/tests/instructions/agent-landing.mjs
# Verify the fixed component:
SIGNIN_ONLY=1 node frontend/tests/instructions/agent-landing.mjs
```

The focused loop checks the unsigned manager with an existing instruction, unsigned manager without an instruction, unsigned member, Hebrew/mobile, and restored visibility for signed-in and partially usable agents. Evidence is in `media/pr/agent-landing/signin-{before,after,he,mobile}.png` and `signin-flow.gif`. This is presentation gating; it does not change backend authorization or delete instruction data.

## Follow-up — show the agent's linked connections

The centered landing page omitted the connections it depends on. A signed-out member could see Sign in without seeing the connection name until opening the Agent Card. `CONNECTIONS_BEFORE=1` reproduced the missing row using the real component and a synthetic linked Power BI connection.

The landing page now displays compact connection chips below the lifecycle/privacy badges, using the agent detail's existing `connections` payload. Each chip shows its connector icon, name, and shared status dot; the localized status is also available in the tooltip and accessible name. Missing personal sign-in stays neutral, while an actual failure remains red. All linked connections remain visible before sign-in, wrap on narrow screens, and open the existing detail modal for that connection. No empty section appears for agents without connections.

Connection changes made through that modal now refresh the open agent detail as well as the lists. This immediately updates the landing page's status, actions, and instruction visibility after sign-out or a query-identity change. Usable agents also reload their selected resource metadata, without starting schema discovery.

```sh
# Capture the baseline before adding the row:
CONNECTIONS_ONLY=1 CONNECTIONS_BEFORE=1 node frontend/tests/instructions/agent-landing.mjs
# Verify the connection row and detail interaction:
CONNECTIONS_ONLY=1 node frontend/tests/instructions/agent-landing.mjs
```

The focused browser loop passes for one, five, and zero connections; connected, sign-in-required, failed, indexing, and unknown statuses; opening the correct connection; member management gates; sign-out updating the chip and hiding the instruction; long-name wrapping; Hebrew/mobile; and dark mode. These checks use synthetic APIs and do not sign out a real account. SFC compilation, the shared status tests, and `git diff --check` also pass.

Evidence: `media/pr/agent-landing/connections-{before,after,detail,signed-out,many,he-mobile,dark-mobile}.png` and `connections-flow.gif`.
