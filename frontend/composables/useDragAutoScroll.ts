import type { Ref } from 'vue'

// Edge auto-scroll for a scroll container during an HTML5 drag. Native drag
// auto-scroll is unreliable inside nested overflow containers, so drive it:
// feed every dragover AND dragenter on the container to `onDragOver` (capture
// phase, so a child's stopPropagation can't hide it). dragenter matters: when
// the pointer crosses into a new row the browser fires enter/leave for that
// move instead of dragover, so dragover alone can go quiet mid-scroll.
// While the pointer is inside the `edge`-px band at the top or bottom the
// container scrolls, faster the closer the pointer is to the edge.
//
// An element marked `data-drag-sticky` is a drop target that may sit in the
// band (e.g. a header pinned to the top): the pointer on it never scrolls —
// that would move the list out from under the drop — and while it is pinned
// to the top the top band starts below it.
//
// The loop stops by itself when the pointer leaves the band, when `active()`
// turns false (drop / dragend cleared the drag state), or when dragovers stop
// arriving (pointer left the container) — so it can't run away.
export function useDragAutoScroll(
  container: Ref<HTMLElement | null>,
  opts: { active: () => boolean, edge?: number, maxSpeed?: number },
) {
  const edge = opts.edge ?? 50
  const maxSpeed = opts.maxSpeed ?? 16
  // While the pointer rests, browsers repeat dragover every 350ms ± 200ms
  // (HTML drag-and-drop processing model); the rAF loop scrolls in between.
  const STALE_MS = 1000
  let frame = 0
  let speed = 0
  let lastSeen = 0

  function stop() {
    if (frame) cancelAnimationFrame(frame)
    frame = 0
    speed = 0
  }

  function tick() {
    const el = container.value
    if (!el || !speed || !opts.active() || performance.now() - lastSeen > STALE_MS) return stop()
    el.scrollTop += speed
    frame = requestAnimationFrame(tick)
  }

  function onDragOver(event: DragEvent) {
    const el = container.value
    if (!el || !opts.active()) return stop()
    const rect = el.getBoundingClientRect()
    const y = event.clientY
    let top = rect.top
    const sticky = el.querySelector('[data-drag-sticky]')?.getBoundingClientRect()
    if (sticky?.height) {
      if (y >= sticky.top && y < sticky.bottom) return stop()
      if (sticky.top <= rect.top + 1 && sticky.bottom > rect.top) top = sticky.bottom
    }
    const ramp = (depth: number) => Math.max(1, Math.round(maxSpeed * Math.min(1, depth / edge)))
    speed = 0
    if (y >= top && y < top + edge && el.scrollTop > 0) speed = -ramp(top + edge - y)
    else if (y > rect.bottom - edge && el.scrollTop + el.clientHeight < el.scrollHeight) speed = ramp(y - (rect.bottom - edge))
    if (!speed) return stop()
    lastSeen = performance.now()
    if (!frame) frame = requestAnimationFrame(tick)
  }

  // Leaving the container (not just moving between its children) ends it.
  // Some browsers (Safari) send dragleave without relatedTarget; fall back to
  // the pointer position then.
  function onDragLeave(event: DragEvent) {
    const el = container.value
    if (!el) return stop()
    if (event.relatedTarget instanceof Node) {
      if (!el.contains(event.relatedTarget)) stop()
      return
    }
    const r = el.getBoundingClientRect()
    const { clientX: x, clientY: y } = event
    if (x < r.left || x > r.right || y < r.top || y > r.bottom) stop()
  }

  onBeforeUnmount(stop)
  return { onDragOver, onDragLeave, stop }
}
