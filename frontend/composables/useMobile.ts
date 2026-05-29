import { ref, readonly, onMounted, onBeforeUnmount } from 'vue'

const MOBILE_BREAKPOINT_PX = 768

const globalIsMobile = ref(false)
let listenerInstalled = false

function update() {
  if (typeof window === 'undefined') return
  globalIsMobile.value = window.innerWidth < MOBILE_BREAKPOINT_PX
}

function ensureListener() {
  if (typeof window === 'undefined') return
  if (listenerInstalled) return
  listenerInstalled = true
  update()
  window.addEventListener('resize', update)
}

export const useMobile = () => {
  onMounted(ensureListener)
  // Also try synchronously so the first read on the client is correct.
  ensureListener()

  return {
    isMobile: readonly(globalIsMobile)
  }
}
