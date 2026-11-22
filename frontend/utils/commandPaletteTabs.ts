// Category tabs of the ⌘K palette (CommandPalette.vue): which groups a tab
// shows, what it fetches, and how many rows. Pure, so it can be unit-tested.

export type PaletteTab = 'all' | 'reports' | 'agents' | 'instructions' | 'dashboards'
export type PaletteGroupKey = 'inReport' | 'actions' | 'reports' | 'prompts' | 'agents' | 'instructions' | 'dashboards'
export type PaletteFetch = 'inReport' | 'reports' | 'prompts' | 'instructions' | 'dashboards'
export type PaletteAction = 'newReport' | 'newInstruction'

// "All" first and selected on open: it is today's mixed list, so
// ⌘K → type → Enter (= New report) keeps working.
export const PALETTE_TABS: PaletteTab[] = ['all', 'reports', 'agents', 'instructions', 'dashboards']

interface TabContext {
  // A report page registered itself (the "In this report" group exists).
  inReport: boolean
  // The query starts with `report:` — that explicit scope wins over the tab.
  scoped: boolean
}

const GROUPS: Record<PaletteTab, PaletteGroupKey[]> = {
  all: ['inReport', 'actions', 'reports', 'prompts', 'agents', 'instructions', 'dashboards'],
  // Saved prompts start reports, so they live with them.
  reports: ['inReport', 'actions', 'reports', 'prompts'],
  agents: ['agents'],
  instructions: ['actions', 'instructions'],
  dashboards: ['dashboards'],
}

// Groups to render, in order. Empty groups are dropped by the caller.
export function groupsForTab(tab: PaletteTab, ctx: TabContext): PaletteGroupKey[] {
  if (ctx.inReport && ctx.scoped) return ['inReport']
  return GROUPS[tab].filter(g => g !== 'inReport' || ctx.inReport)
}

// Server searches to run for a query. Agents are filtered client-side from
// the shared list, so they never appear here.
export function fetchesForTab(tab: PaletteTab, ctx: TabContext): PaletteFetch[] {
  const groups = groupsForTab(tab, ctx)
  return (['inReport', 'reports', 'prompts', 'instructions', 'dashboards'] as PaletteFetch[])
    .filter(f => groups.includes(f))
}

// Only the action that fits the tab ("New report" on Reports, …).
export function actionsForTab(tab: PaletteTab): PaletteAction[] {
  if (tab === 'all') return ['newReport', 'newInstruction']
  if (tab === 'reports') return ['newReport']
  if (tab === 'instructions') return ['newInstruction']
  return []
}

// A focused tab has the whole list to itself, so it shows more rows.
const LIMITS = {
  all: { reports: 3, prompts: 5, instructions: 3, dashboards: 3, agents: 6 },
  focused: { reports: 10, prompts: 5, instructions: 10, dashboards: 10, agents: 20 },
}
export function limitFor(tab: PaletteTab, group: keyof typeof LIMITS.all): number {
  return LIMITS[tab === 'all' ? 'all' : 'focused'][group]
}

// Arrow-key navigation between tabs, wrapping around. `key` is the physical
// arrow; in RTL the row is mirrored, so ArrowLeft moves forward.
export function stepTab(tab: PaletteTab, key: 'ArrowLeft' | 'ArrowRight', rtl = false): PaletteTab {
  const forward = (key === 'ArrowRight') !== rtl
  const i = PALETTE_TABS.indexOf(tab)
  const n = PALETTE_TABS.length
  return PALETTE_TABS[(i + (forward ? 1 : -1) + n) % n]
}
