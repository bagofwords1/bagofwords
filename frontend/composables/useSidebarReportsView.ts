/*
 * How the sidebar Reports list is grouped and sorted — a *personal* view
 * preference, so it lives in localStorage per browser (like `bow.chat.*` and
 * `bow.locale`), not in OrganizationSettings. One JSON blob under
 * `bow.sidebar.reports.view`, read once on the client and written on change.
 *
 * Only the view choice is remembered; group collapse is session-only (it is
 * ephemeral UI state the layout keeps, not a saved preference).
 */

export type SidebarGroupBy = 'date' | 'status' | 'none'
export type SidebarSortBy = 'updated' | 'created' | 'name'
export type SidebarDirection = 'asc' | 'desc'

export interface SidebarReportsView {
  groupBy: SidebarGroupBy
  sortBy: SidebarSortBy
  direction: SidebarDirection
}

// Default = today's list, unchanged: a flat, ungrouped list (starred first,
// latest activity first). Grouping and other sorts are opt-in through the
// menu, like Claude's sidebar (whose Group by also defaults to None).
export const DEFAULT_SIDEBAR_REPORTS_VIEW: SidebarReportsView = {
  groupBy: 'none',
  sortBy: 'updated',
  direction: 'desc',
}

const STORAGE_KEY = 'bow.sidebar.reports.view'

function isGroupBy(v: unknown): v is SidebarGroupBy {
  return v === 'date' || v === 'status' || v === 'none'
}
function isSortBy(v: unknown): v is SidebarSortBy {
  return v === 'updated' || v === 'created' || v === 'name'
}
function isDirection(v: unknown): v is SidebarDirection {
  return v === 'asc' || v === 'desc'
}

/*
 * Guarded read: localStorage throws in Safari private mode and in embedded
 * webviews (the Excel add-in), and a bad/partial blob must fall back per field
 * to the default rather than take the sidebar down. A missing or invalid value
 * means "use the default".
 */
function readStored(): SidebarReportsView {
  if (typeof localStorage === 'undefined') return { ...DEFAULT_SIDEBAR_REPORTS_VIEW }
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw === null) return { ...DEFAULT_SIDEBAR_REPORTS_VIEW }
    const parsed = JSON.parse(raw) as Partial<SidebarReportsView>
    return {
      groupBy: isGroupBy(parsed?.groupBy) ? parsed.groupBy : DEFAULT_SIDEBAR_REPORTS_VIEW.groupBy,
      sortBy: isSortBy(parsed?.sortBy) ? parsed.sortBy : DEFAULT_SIDEBAR_REPORTS_VIEW.sortBy,
      direction: isDirection(parsed?.direction) ? parsed.direction : DEFAULT_SIDEBAR_REPORTS_VIEW.direction,
    }
  } catch {
    return { ...DEFAULT_SIDEBAR_REPORTS_VIEW }
  }
}

function writeStored(view: SidebarReportsView) {
  if (typeof localStorage === 'undefined') return
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(view))
  } catch {
    /* Storage denied or full — the choice still applies this session, it just
       will not survive a reload. */
  }
}

export function useSidebarReportsView() {
  // Shared across the layout (grouping) and the menu (control) via useState, so
  // a pick in the menu re-groups the list immediately.
  const view = useState<SidebarReportsView>(STORAGE_KEY, () => ({ ...DEFAULT_SIDEBAR_REPORTS_VIEW }))
  const hydrated = useState<boolean>(`${STORAGE_KEY}.hydrated`, () => false)

  // Client-only, idempotent: SSR starts from defaults, the first mount reads
  // the stored choice. Called from the layout onMounted and from the menu.
  function hydrate() {
    if (hydrated.value) return
    view.value = readStored()
    hydrated.value = true
  }

  const groupBy = computed(() => view.value.groupBy)
  const sortBy = computed(() => view.value.sortBy)
  const direction = computed(() => view.value.direction)

  function setGroupBy(next: SidebarGroupBy) {
    view.value = { ...view.value, groupBy: next }
    writeStored(view.value)
  }
  function setSortBy(next: SidebarSortBy) {
    view.value = { ...view.value, sortBy: next }
    writeStored(view.value)
  }
  function setDirection(next: SidebarDirection) {
    view.value = { ...view.value, direction: next }
    writeStored(view.value)
  }

  // True when the view differs from the default, so the control can show it is
  // doing something without the user opening it.
  const isCustomized = computed(() =>
    groupBy.value !== DEFAULT_SIDEBAR_REPORTS_VIEW.groupBy ||
    sortBy.value !== DEFAULT_SIDEBAR_REPORTS_VIEW.sortBy ||
    direction.value !== DEFAULT_SIDEBAR_REPORTS_VIEW.direction,
  )

  return { view, groupBy, sortBy, direction, setGroupBy, setSortBy, setDirection, isCustomized, hydrate }
}
