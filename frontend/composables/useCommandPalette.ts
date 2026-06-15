// Shared open-state for the global ⌘K / Ctrl+K command palette.
// The palette itself is mounted once in layouts/default.vue (see CommandPalette.vue);
// this composable lets any component open/close/toggle it.
export const useCommandPalette = () => {
  const isOpen = useState<boolean>('command-palette-open', () => false)

  const open = () => { isOpen.value = true }
  const close = () => { isOpen.value = false }
  const toggle = () => { isOpen.value = !isOpen.value }

  return { isOpen, open, close, toggle }
}
