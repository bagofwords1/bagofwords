import { ref, type Ref } from 'vue'

/**
 * Arrow/Enter navigation for a picker panel whose rows are rendered by several
 * components (a folder tree, slot rows, static rows). Rather than mirroring
 * every source into one array, it walks the rendered DOM: every element under
 * `root` matching `[data-kbd-option]` that is visible and enabled is a stop,
 * in document order. The active row gets `data-kbd-active` (style it in CSS)
 * and Enter clicks it, so each row keeps its own click handler as the single
 * source of behavior.
 *
 * Bind `onKeydown` on the panel root (focus sits in the panel's search input).
 */
export function useListKeyboardNav(root: Ref<HTMLElement | null>) {
  const active = ref(-1)

  function options(): HTMLElement[] {
    const el = root.value
    if (!el) return []
    return Array.from(el.querySelectorAll<HTMLElement>('[data-kbd-option]')).filter(o =>
      !(o as HTMLButtonElement).disabled && o.getAttribute('aria-disabled') !== 'true' && o.offsetParent !== null,
    )
  }

  function paint(list = options()) {
    list.forEach((o, i) => {
      if (i === active.value) o.setAttribute('data-kbd-active', '')
      else o.removeAttribute('data-kbd-active')
    })
    list[active.value]?.scrollIntoView({ block: 'nearest' })
  }

  /** Move the highlight to `index` (clamped); -1 clears it. */
  function setActive(index: number) {
    const list = options()
    active.value = list.length ? Math.max(-1, Math.min(list.length - 1, index)) : -1
    paint(list)
  }

  /** Highlight the first row not marked `data-kbd-pinned` (e.g. an "Auto" row
      that sits above the results) — the top match while searching. */
  function activateFirstMatch() {
    const list = options()
    active.value = list.findIndex(o => !o.hasAttribute('data-kbd-pinned'))
    paint(list)
  }

  function onKeydown(e: KeyboardEvent) {
    if (e.isComposing) return
    const list = options()
    if (!list.length) return
    // The list can re-render between keys (search, folder expand); re-find the
    // painted row so the highlight follows the element, not a stale index.
    const painted = list.findIndex(o => o.hasAttribute('data-kbd-active'))
    const current = painted >= 0 ? painted : -1
    let next: number | null = null
    if (e.key === 'ArrowDown') next = current < 0 ? 0 : (current + 1) % list.length
    else if (e.key === 'ArrowUp') next = current < 0 ? list.length - 1 : (current - 1 + list.length) % list.length
    else if (e.key === 'Enter' && current >= 0) {
      e.preventDefault()
      list[current].click()
      return
    }
    if (next === null) return
    e.preventDefault()
    active.value = next
    paint(list)
  }

  return { active, setActive, activateFirstMatch, onKeydown }
}
