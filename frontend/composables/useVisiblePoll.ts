// Run `check` while the page is on screen: every `intervalMs`, and at once
// when the tab comes back into view or the window regains focus. A hidden
// tab never polls, and a slow check is never stacked on itself.
//
// Used to notice a dashboard version saved somewhere this view hears no
// stream from (another tab, another conversation, the dashboard's own chat),
// so the open dashboard can offer it instead of silently going stale.
export function useVisiblePoll(
  check: () => unknown,
  intervalMs = 15000,
  enabled: () => boolean = () => true,
) {
  let timer: ReturnType<typeof setInterval> | null = null
  let running = false

  async function run() {
    if (running || !enabled()) return
    if (typeof document !== 'undefined' && document.hidden) return
    running = true
    try { await check() } catch { /* a failed check must never break the page */ }
    finally { running = false }
  }
  const onVisibility = () => { if (!document.hidden) run() }

  onMounted(() => {
    timer = setInterval(run, intervalMs)
    document.addEventListener('visibilitychange', onVisibility)
    window.addEventListener('focus', run)
  })
  onBeforeUnmount(() => {
    if (timer) clearInterval(timer)
    timer = null
    document.removeEventListener('visibilitychange', onVisibility)
    window.removeEventListener('focus', run)
  })

  return { run }
}
