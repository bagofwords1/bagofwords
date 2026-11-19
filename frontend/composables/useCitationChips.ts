/**
 * Interaction for citation chips in a transcript (see useCitations.ts).
 *
 * The chips are plain links rendered by the markdown renderer, so the
 * behaviour is delegated from a container the host puts the handlers on:
 * hover or keyboard focus previews the cited result, and a click — mouse,
 * touch or Enter — opens it in the host's side pane. Only where there is no
 * pane to open into (a phone layout, or the shared link, which passes no
 * `open`) does a click pin the preview instead; it stays until the reader
 * taps elsewhere or presses Escape.
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { citationKeyFromHref, type CitationSource } from './useCitations'

export interface ActiveCitation {
  source: CitationSource
  number: number | null
  anchor: { top: number; bottom: number; left: number; right: number }
  /** Shown by a tap or click rather than hover: stays until dismissed. */
  pinned: boolean
}

const SHOW_DELAY_MS = 120
const HIDE_DELAY_MS = 160

export function useCitationChips<M>(opts: {
  /** The source a chip of message `m` points at, with its number there. */
  resolve: (m: M, key: string) => { source: CitationSource; number: number | null } | null
  open?: (source: CitationSource) => void
  /** Whether `open` has somewhere to open into right now (default: yes). */
  canOpen?: () => boolean
}) {
  const active = ref<ActiveCitation | null>(null)
  let showTimer: ReturnType<typeof setTimeout> | null = null
  let hideTimer: ReturnType<typeof setTimeout> | null = null

  const clearTimers = () => {
    if (showTimer) { clearTimeout(showTimer); showTimer = null }
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null }
  }

  function chipOf(e: Event): { el: HTMLElement; key: string } | null {
    // Only a rendered chip (ChatLink), never an ordinary link in the answer.
    const el = (e.target as HTMLElement | null)?.closest?.('a.citation-chip') as HTMLElement | null
    const key = el ? citationKeyFromHref(el.getAttribute('href')) : ''
    return el && key ? { el, key } : null
  }

  function show(source: CitationSource, number: number | null, el: Element, pinned = false) {
    clearTimers()
    const r = el.getBoundingClientRect()
    active.value = { source, number, pinned, anchor: { top: r.top, bottom: r.bottom, left: r.left, right: r.right } }
  }

  /** Preview after a short delay, so crossing a chip with the pointer does nothing. */
  function preview(source: CitationSource, number: number | null, el: Element | null) {
    if (!el) return
    if (active.value?.source.key === source.key) { clearTimers(); return }
    clearTimers()
    showTimer = setTimeout(() => show(source, number, el), SHOW_DELAY_MS)
  }

  function hideSoon() {
    if (showTimer) { clearTimeout(showTimer); showTimer = null }
    if (active.value?.pinned) return
    if (hideTimer) clearTimeout(hideTimer)
    hideTimer = setTimeout(() => { active.value = null }, HIDE_DELAY_MS)
  }

  /** Show and keep the preview for a control that has no other click action. */
  function pin(source: CitationSource, number: number | null, target: EventTarget | null) {
    const el = target as Element | null
    if (!el) return
    if (active.value?.pinned && active.value.source.key === source.key) { hide(); return }
    show(source, number, el, true)
  }

  function hide() {
    clearTimers()
    active.value = null
  }

  /** The pointer moved onto the preview itself: keep it. */
  function keep() {
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null }
  }

  function onOver(e: Event, m: M) {
    const chip = chipOf(e)
    const hit = chip && opts.resolve(m, chip.key)
    if (chip && hit) preview(hit.source, hit.number, chip.el)
  }

  function onOut(e: Event) {
    if (chipOf(e)) hideSoon()
  }

  function onFocusIn(e: Event, m: M) {
    const chip = chipOf(e)
    const hit = chip && opts.resolve(m, chip.key)
    if (chip && hit) show(hit.source, hit.number, chip.el)
  }

  // Only a chip losing focus closes its preview — not any other control in
  // the message, and not focus moving into the preview's own tabs.
  function onFocusOut(e: FocusEvent) {
    if (!chipOf(e)) return
    const next = e.relatedTarget as HTMLElement | null
    if (next?.closest?.('[data-testid="citation-preview"]')) return
    hideSoon()
  }

  function onClick(e: MouseEvent, m: M) {
    const chip = chipOf(e)
    if (!chip) return
    // A chip is a link only so it is focusable and announced; it never navigates.
    e.preventDefault()
    const hit = opts.resolve(m, chip.key)
    if (!hit) return
    const showing = active.value?.source.key === hit.source.key
    if (opts.open && (opts.canOpen?.() ?? true)) {
      hide()
      opts.open(hit.source)
    } else if (showing && active.value?.pinned) {
      hide()
    } else {
      show(hit.source, hit.number, chip.el, true)
    }
  }

  // A pinned preview is dismissed by a press anywhere outside it and its chip.
  function onDocumentPointerDown(e: Event) {
    if (!active.value?.pinned) return
    const target = e.target as HTMLElement | null
    if (target?.closest?.('[data-testid="citation-preview"], [data-testid="citation-chip"], [data-testid="run-source"]')) return
    hide()
  }
  // The preview is placed against the chip's position on screen; once the
  // page scrolls it would point at nothing. Scrolling inside the preview
  // itself (a long code tab) is not that.
  function onDocumentScroll(e: Event) {
    if (!active.value) return
    const target = e.target as HTMLElement | null
    if (target?.closest?.('[data-testid="citation-preview"]')) return
    hide()
  }
  onMounted(() => {
    document.addEventListener('pointerdown', onDocumentPointerDown, true)
    document.addEventListener('scroll', onDocumentScroll, { capture: true, passive: true })
  })

  function onKeydown(e: KeyboardEvent) {
    if (e.key === 'Escape' && active.value) hide()
  }

  onBeforeUnmount(() => {
    clearTimers()
    if (typeof document === 'undefined') return
    document.removeEventListener('pointerdown', onDocumentPointerDown, true)
    document.removeEventListener('scroll', onDocumentScroll, { capture: true })
  })

  return { active, preview, pin, hideSoon, hide, keep, onOver, onOut, onFocusIn, onFocusOut, onClick, onKeydown }
}
