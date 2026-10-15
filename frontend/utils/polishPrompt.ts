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

export type PolishElement = { htmlSnippet: string; info?: boolean }

// The chat prompt a Polish pick sends. A pick on the ⓘ icon carries its
// owner's snippet: the icon is runtime chrome, styled through the owner's
// props/attributes, so that is the code the model has to change.
export function buildPolishPrompt(title: string, artifactId: string, el: PolishElement, instruction: string): string {
  const target = el.info
    ? 'Target: the ⓘ info icon of this element. Style it with infoClassName (color) / infoSize ("sm" | "md" | "lg") on its component, or data-bow-info-class / data-bow-info-size on custom data-bow-viz markup; hide it with info={false} / data-bow-info="off" (keep viz).'
    : 'Target element:'
  return `Polish the dashboard "${title}" (artifact_id: ${artifactId}).\n${target}\n\`\`\`html\n${el.htmlSnippet}\n\`\`\`\nInstruction: ${instruction}`
}
