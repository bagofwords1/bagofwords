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

// A Polish request is sent to the agent as a plain chat prompt (the agent needs
// the artifact id and the raw element HTML). Chat history shows a compact card
// in its place, so the build and parse below must stay in sync.
export type PolishRequest = { title: string; artifactId: string; htmlSnippet: string; instruction: string }

export function buildPolishPrompt(r: PolishRequest): string {
  return `Polish the dashboard "${r.title}" (artifact_id: ${r.artifactId}).\nTarget element:\n\`\`\`html\n${r.htmlSnippet}\n\`\`\`\nInstruction: ${r.instruction}`
}

const POLISH_PROMPT_RE = /^Polish the dashboard "(.*)" \(artifact_id: ([^)\n]*)\)\.\nTarget element:\n```html\n([\s\S]*?)\n```\nInstruction: ([\s\S]+)$/

export function parsePolishPrompt(text: string | null | undefined): PolishRequest | null {
  if (!text) return null
  const m = POLISH_PROMPT_RE.exec(text.replace(/\r\n/g, '\n').trim())
  if (!m) return null
  return { title: m[1], artifactId: m[2], htmlSnippet: m[3], instruction: m[4].trim() }
}

const ENTITIES: Record<string, string> = { amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'", apos: "'", nbsp: ' ' }

// Short label for the picked element: its tag and visible text. The snippet is
// capped at 500 chars upstream, so it may be cut mid-tag; plain regex copes.
export function describePolishTarget(html: string, maxText = 60): { tag: string; text: string } {
  const tag = (/^\s*<\s*([a-zA-Z][\w-]*)/.exec(html)?.[1] || 'element').toLowerCase()
  const text = html
    .replace(/<[^>]*>?/g, ' ')
    .replace(/&(amp|lt|gt|quot|#39|apos|nbsp);/g, (_, e) => ENTITIES[e])
    .replace(/\s+/g, ' ')
    .trim()
  return { tag, text: text.length > maxText ? text.slice(0, maxText - 1).trimEnd() + '…' : text }
}
