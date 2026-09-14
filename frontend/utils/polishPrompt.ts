type Rect = { top: number; left: number; height: number }
type Size = { width: number; height: number }

// Where the Polish prompt box goes (px, relative to the pane the dashboard
// iframe fills) for a picked element whose rect comes from inside that iframe.
// Below the element when the box fits there, else above it; either way clamped
// inside the pane. The box grows with the instruction, so callers re-run this
// on every box height change rather than assuming a fixed size.
export function polishPromptPosition(rect: Rect, box: Size, container: Size, gap = 8): { top: number; left: number } {
  let top = rect.top + rect.height + gap
  if (top + box.height > container.height - gap && rect.top - gap - box.height >= gap) {
    top = rect.top - gap - box.height
  }
  top = Math.max(gap, Math.min(top, container.height - box.height - gap))
  const left = Math.max(gap, Math.min(rect.left, container.width - box.width - gap))
  return { top, left }
}
