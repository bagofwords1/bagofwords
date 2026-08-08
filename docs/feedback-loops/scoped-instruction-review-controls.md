# Scoped instruction review controls

## Reported behavior

A user with a resource-level `manage_instructions` grant could see an
instruction's pending change in a report, but the same card exposed no Accept
or Reject controls. Organization administrators did see them.

## Root cause

The report tool cards and several sibling instruction-review surfaces checked
only the organization-level permission. That disagreed with the backend rule:
an attached instruction is manageable when the user has
`manage_instructions` on **every** attached agent; only a global instruction
requires the organization-level permission.

The shared frontend rule now lives in
`frontend/composables/usePermissions.ts:140`. It requires an explicitly loaded
instruction scope, accepts both `data_sources` and `data_source_ids`, and fails
closed while scope data is absent.

## Reproduce -> fix -> verify

### Live localhost loop

1. Open the affected report as the scoped SSO member.
2. Expand its pending Edit instruction card.
3. Count the visible review controls without resolving the hunk.

Before:

```text
pending=1, accept=0, reject=0
```

After:

```text
pending=1, accept=1, reject=1
```

The post-fix browser capture also showed the existing Run eval control. No
Accept or Reject action was clicked, so the pending instruction data remained
unchanged during verification.

### Static authority loop

The affected review surfaces must derive authority from the complete
instruction scope rather than a broad organization-only or "any agent" check:

```bash
rg -n ':can-approve="canCreateInstructions"|:can-approve="canApprove"' \
  frontend/components/tools \
  frontend/components/KnowledgeGroup.vue \
  frontend/components/InstructionSuggestions.vue \
  frontend/components/KnowledgeExplorer.vue \
  frontend/components/report/ReportAgentPanel.vue \
  frontend/components/InstructionModalComponent.vue
```

Expected result: no matches.

### Production build

```bash
cd frontend
npm run build
```

Expected result: exit code 0 and `Build complete!`. The repository's existing
duplicate-key and bundle-size warnings may still be printed.

## Changed surfaces

- Edit instruction report cards
- Create instruction report cards
- Grouped report instruction changes
- Instruction suggestion cards
- Knowledge Explorer review and version-diff controls
- Report agent panel review controls
- Instruction edit modal read-only decision

No eval execution logic changed; eval controls continue to use the existing
resource-aware `manage_evals` path.

## Follow-up: read-only viewers see only approved state

### Reported behavior

A member who could view a public agent, but could not manage its instruction,
still entered Knowledge Explorer's pending-review mode. The approved text was
`$100`, while the unpublished proposal changed it to `$500`; the member saw the
red/green `$100` -> `$500` draft diff and a second Pending review status.

### Frontend-only containment

Knowledge Explorer now treats review hunks, pending rows, unpublished-build
status, and version history as management surfaces. For a selected instruction
without complete `manage_instructions` authority it clears draft-derived state,
does not load review hunks or history, and renders the ordinary approved detail
response. Request results are selection- and permission-guarded so an in-flight
manager response cannot flash after switching to a read-only instruction.

### Live localhost loop

Open instruction `d1b01dcb-1361-4921-8207-5126ee951f34` as the read-only SSO
member and inspect the visible state:

```text
approved $100 text = visible
draft $500 text    = absent
Pending review     = absent
Version history    = absent
Accept / Reject    = absent
status             = Active (header and Details)
```

Then open the same instruction as an administrator:

```text
Pending review     = visible
change count       = 1
Accept / Reject    = visible
Version history    = visible
Edit               = visible
```

Finally run `cd frontend && npm run build`; expected result is exit code 0.
This is containment at the presentation layer only. Backend review endpoints
retain their existing access behavior for a later server-side hardening pass.
