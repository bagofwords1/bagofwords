import { ref, onMounted, onBeforeUnmount } from 'vue'
import { INTERCOM_LAUNCHER_SELECTOR, launcherClearance } from '~/utils/intercomLauncher'

// Room (px from the viewport bottom) that Intercom's launcher takes up, 0 when
// there is none. Intercom is booted by the app layouts and survives client-side
// navigation into layout-less pages (e.g. the post-sign-in redirect to /r/{id}),
// where its launcher — z-index ~2^31, same bottom-end corner — covers whatever
// those pages pin there. The DOM is measured rather than a size assumed:
// launcher size, padding and visibility (hidden on mobile / in Excel) all vary.
export function useIntercomLauncherClearance() {
  const clearance = ref(0)
  let observer: MutationObserver | null = null
  let frame = 0

  function measure() {
    frame = 0
    const rects = Array.from(document.querySelectorAll(INTERCOM_LAUNCHER_SELECTOR))
      .map((el) => el.getBoundingClientRect())
    clearance.value = launcherClearance(rects, window.innerHeight)
  }

  function schedule() {
    if (!frame) frame = requestAnimationFrame(measure)
  }

  onMounted(() => {
    measure()
    // Intercom injects (and on hide/show, restyles) its launcher asynchronously.
    observer = new MutationObserver(schedule)
    observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['style', 'class'] })
    window.addEventListener('resize', schedule)
  })

  onBeforeUnmount(() => {
    observer?.disconnect()
    window.removeEventListener('resize', schedule)
    if (frame) cancelAnimationFrame(frame)
  })

  return clearance
}
