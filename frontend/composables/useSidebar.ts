/**
 * Composable for managing sidebar collapse state globally.
 * Uses Nuxt's useState for cross-component reactivity.
 */

// Desktop sidebar width bounds (px). The default matches Tailwind's w-60 (15rem)
// that the sidebar used before it became resizable; collapsed stays at w-14.
export const SIDEBAR_MIN_WIDTH = 200
export const SIDEBAR_MAX_WIDTH = 400
export const SIDEBAR_DEFAULT_WIDTH = 240
// Per-browser persistence of the user's chosen width.
const SIDEBAR_WIDTH_STORAGE_KEY = 'bow.sidebar.width'

const clampSidebarWidth = (w: number) =>
  Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, Math.round(w)))

export function useSidebar() {
  const isCollapsed = useState<boolean>('sidebar-collapsed', () => false)
  const showText = useState<boolean>('sidebar-show-text', () => true)
  // Desktop expanded width in px (ignored while collapsed, which is fixed at
  // w-14). SSR renders the default so the first client paint agrees; the saved
  // value is loaded from localStorage on the client via loadWidth().
  const width = useState<number>('sidebar-width', () => SIDEBAR_DEFAULT_WIDTH)
  // True only during an active pointer drag, so the layout can switch off its
  // width/margin transition and let the sidebar follow the pointer 1:1.
  const isResizing = useState<boolean>('sidebar-resizing', () => false)

  // Read + clamp the saved width from localStorage. Client-only, best-effort:
  // a blocked or empty store just leaves the default in place.
  const loadWidth = () => {
    if (typeof window === 'undefined') return
    try {
      const raw = window.localStorage.getItem(SIDEBAR_WIDTH_STORAGE_KEY)
      if (raw == null) return
      const parsed = Number(raw)
      if (Number.isFinite(parsed)) width.value = clampSidebarWidth(parsed)
    } catch {}
  }

  // Set the width live (during drag / arrow-key nudges), always clamped.
  const setWidth = (w: number) => { width.value = clampSidebarWidth(w) }

  // Persist the current width (called on drag end / after a keyboard nudge).
  const saveWidth = () => {
    if (typeof window === 'undefined') return
    try { window.localStorage.setItem(SIDEBAR_WIDTH_STORAGE_KEY, String(width.value)) } catch {}
  }

  // Double-click the handle: back to the default and persist it.
  const resetWidth = () => {
    width.value = SIDEBAR_DEFAULT_WIDTH
    saveWidth()
  }
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
    toggleMobile,
    width,
    isResizing,
    loadWidth,
    setWidth,
    saveWidth,
    resetWidth
  }
}
