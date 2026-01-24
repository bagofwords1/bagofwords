/**
 * Composable for managing sidebar collapse state globally.
 * Uses Nuxt's useState for cross-component reactivity.
 */
export function useSidebar() {
  const isCollapsed = useState<boolean>('sidebar-collapsed', () => false)
  const showText = useState<boolean>('sidebar-show-text', () => true)

  const collapse = () => {
    showText.value = false
    isCollapsed.value = true
  }

  const expand = () => {
    isCollapsed.value = false
    setTimeout(() => {
      showText.value = true
    }, 300) // Match the transition duration
  }

  const toggle = () => {
    if (isCollapsed.value) {
      expand()
    } else {
      collapse()
    }
  }

  return {
    isCollapsed,
    showText,
    collapse,
    expand,
    toggle
  }
}
