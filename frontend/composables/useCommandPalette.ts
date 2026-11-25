// Shared open-state for the global ⌘K / Ctrl+K command palette.
// The palette itself is mounted once in layouts/default.vue (see CommandPalette.vue);
// this composable lets any component open/close/toggle it.

// A page that wants an "In this report" group registers itself here while it
// is mounted (pages/reports/[id]/index.vue). The palette reads it.
export interface CommandPaletteReportContext {
  id: string
  title: () => string
  // Load older pages until the message is rendered, scroll to it, highlight it.
  jumpTo: (completionId: string) => void | Promise<void>
}

export const useCommandPalette = () => {
  const isOpen = useState<boolean>('command-palette-open', () => false)
  // Client-only: set on mount, cleared on unmount (holds functions, never SSR'd).
  const reportContext = useState<CommandPaletteReportContext | null>('command-palette-report', () => null)

  const open = () => { isOpen.value = true }
  const close = () => { isOpen.value = false }
  const toggle = () => { isOpen.value = !isOpen.value }

  const registerReport = (ctx: CommandPaletteReportContext) => { reportContext.value = ctx }
  // Only the page that registered may clear: on report A → report B, B can
  // mount before A unmounts.
  const unregisterReport = (id: string) => {
    if (reportContext.value?.id === id) reportContext.value = null
  }

  return { isOpen, open, close, toggle, reportContext, registerReport, unregisterReport }
}
