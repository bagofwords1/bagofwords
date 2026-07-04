/**
 * Composable for managing sidebar collapse state globally.
 * Uses Nuxt's useState for cross-component reactivity.
 */
export function useSidebar() {
  const isCollapsed = useState<boolean>('sidebar-collapsed', () => false)
  const showText = useState<boolean>('sidebar-show-text', () => true)
  // Mobile drawer state: the sidebar is off-canvas on small screens and slides
  // in when this is true (opened by the mobile top-bar hamburger). Independent
  // of the desktop collapse state above.
  const mobileOpen = useState<boolean>('sidebar-mobile-open', () => false)

  const openMobile = () => { mobileOpen.value = true }
  const closeMobile = () => { mobileOpen.value = false }
  const toggleMobile = () => { mobileOpen.value = !mobileOpen.value }

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
    toggle,
    mobileOpen,
    openMobile,
    closeMobile,
    toggleMobile
  }
}
