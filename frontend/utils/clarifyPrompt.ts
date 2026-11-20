// Answering a clarify form sends the agent a plain chat prompt: one "Q: … A: …"
// block per question. The agent needs that text; chat history shows a compact
// card in its place, so the build and parse below must stay in sync.
export type ClarifyAnswer = { question: string; answers: string[] }

// One pick per line: options may themselves contain commas.
export function buildClarifyPrompt(pairs: ClarifyAnswer[]): string {
  return pairs
    .map(({ question, answers }) => {
      const answer = answers.length > 1 ? answers.map((a) => `\n- ${a}`).join('') : (answers[0] ?? '')
      return `Q: ${question}  \nA: ${answer}`
    })
    .join('\n\n')
}

const BLOCK_RE = /^Q: ([\s\S]*?) {2}\nA: ?([\s\S]*)$/

// Null unless the whole text is clarify blocks — an ordinary message that merely
// mentions "Q:" keeps rendering as the prose it is.
export function parseClarifyPrompt(text: string | null | undefined): ClarifyAnswer[] | null {
  if (!text) return null
  const blocks = text.replace(/\r\n/g, '\n').trim().split(/\n\n(?=Q: )/)
  const pairs: ClarifyAnswer[] = []
  for (const block of blocks) {
    const m = BLOCK_RE.exec(block)
    if (!m) return null
    const question = m[1].trim()
    const raw = m[2]
    const answers = (raw.startsWith('\n- ') ? raw.slice(3).split('\n- ') : [raw])
      .map((a) => a.trim())
      .filter(Boolean)
    if (!question || !answers.length) return null
    pairs.push({ question, answers })
  }
  return pairs
}
