// A "Fix Error" request is sent to the agent as a plain chat prompt (the agent
// needs the artifact id and the raw render error). Chat history shows a
// compact card in its place, so the build and parse below must stay in sync.
// The text is the exact prompt the pane has always sent, so fix requests that
// are already in chat history get the card too.
export type FixErrorRequest = { title: string; artifactId: string; error: string }

export function buildFixErrorPrompt(r: FixErrorRequest): string {
  return `The dashboard "${r.title}" (artifact_id: ${r.artifactId}) failed to render with this error:\n\`\`\`\n${r.error}\n\`\`\`\nPlease fix the artifact code so it renders correctly.`
}

// The error part is greedy: the fixed suffix anchors the end, so an error that
// itself contains ``` still parses whole.
const FIX_ERROR_PROMPT_RE = /^The dashboard "(.*)" \(artifact_id: ([^)\n]*)\) failed to render with this error:\n```\n([\s\S]*)\n```\nPlease fix the artifact code so it renders correctly\.$/

export function parseFixErrorPrompt(text: string | null | undefined): FixErrorRequest | null {
  if (!text) return null
  const m = FIX_ERROR_PROMPT_RE.exec(text.replace(/\r\n/g, '\n').trim())
  if (!m) return null
  return { title: m[1], artifactId: m[2], error: m[3] }
}

// One-line label for the card: the error's first non-empty line, capped.
export function summarizeError(error: string, max = 120): string {
  const line = (error.split('\n').find(l => l.trim()) || '').trim()
  return line.length > max ? line.slice(0, max - 1).trimEnd() + '…' : line
}
