import type { Ref } from 'vue'

/**
 * Dismiss a lightweight (non-modal) teleported menu on an outside press —
 * WITHOUT a full-screen click-catching backdrop. A backdrop eats the next
 * click: with a row menu open, pressing another row's ⋯ (or a link) only
 * closed the menu and needed a second click. Here the press closes the menu
 * and still reaches what was pressed.
 *
 * - `panel`: the menu element; presses inside it are ignored.
 * - `anchor`: the trigger that opened it; presses on it are ignored so the
 *   trigger's own click can toggle the menu closed (otherwise pointerdown
 *   would close and the click would immediately reopen it).
 * - Also closes when an ancestor of the anchor scrolls (the menu is
 *   fixed-positioned from the anchor's rect and would float stale) and on
 *   window resize. Unrelated scrolls — e.g. a streaming transcript — don't.
 *
 * Capture phase, so it runs even when the target stops propagation.
 */
export function useDismissOnOutsidePress(
  open: Ref<boolean>,
  panel: Ref<HTMLElement | null>,
  anchor: Ref<HTMLElement | null>,
  close: () => void,
) {
  const onPointerDown = (e: PointerEvent) => {
    const target = e.target as Node | null
    if (!target) return
    if (panel.value?.contains(target) || anchor.value?.contains(target)) return
    close()
  }
  const onScroll = (e: Event) => {
    const target = e.target as Node | null
    const a = anchor.value
    if (!a || target === document || (target instanceof Node && target.contains(a))) close()
  }

  const detach = () => {
    document.removeEventListener('pointerdown', onPointerDown, true)
    document.removeEventListener('scroll', onScroll, true)
    window.removeEventListener('resize', close)
  }

  watch(open, (v) => {
    if (typeof window === 'undefined') return
    if (v) {
      document.addEventListener('pointerdown', onPointerDown, true)
      document.addEventListener('scroll', onScroll, true)
      window.addEventListener('resize', close)
    } else {
      detach()
    }
  })

  onBeforeUnmount(() => {
    if (typeof window !== 'undefined') detach()
  })
}
