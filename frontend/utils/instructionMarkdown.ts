// The markdown-it pipeline that renders an instruction read-only, shared by
// InstructionText (the instruction itself) and the tracked-changes diff views
// (utils/markdownDiff.ts) so a diff renders exactly like the instruction it
// changes: same tables, same per-block direction.
import MarkdownIt from 'markdown-it'
import { countDirWords, resolveDirTree, type DirNode, type DirNodeKind, type TextDir } from './textDirection'

export interface InstructionMarkdownOptions {
  /**
   * Maps the content of a text / code_inline token to the text that votes on
   * direction and the plain text a table cell is judged by (signed-number
   * check). Called for every such token in document order, with the render
   * env, so it can carry state across tokens. Unset, the content is used as is.
   */
  voteText?: (content: string, env: any) => { vote: string; text: string }
}

// Per-block direction: the same rules as chat markdown and the instruction
// editor (resolveDirTree in utils/textDirection.ts) — one decision for the
// whole instruction by word majority, lists and tables on one side, items and
// cells following them, short blocks following the text around them. Each
// block token gets an explicit dir, so RTL blocks right-align and list markers
// / blockquote bars flip to the correct edge (via the logical CSS in
// instruction-prose.css). Code doesn't vote and stays LTR.
export const DIR_KIND_BY_TOKEN: Record<string, DirNodeKind> = {
  paragraph_open: 'block', heading_open: 'block', blockquote_open: 'quote',
  bullet_list_open: 'list', ordered_list_open: 'list', list_item_open: 'item',
  // Tables need it most: without a dir of their own an RTL table inherits the
  // document's LTR and lays its columns out left-to-right.
  table_open: 'table', tr_open: 'row', th_open: 'cell', td_open: 'cell',
}

/**
 * A markdown-it instance configured for instruction prose. After a render,
 * `env.docDir` holds the direction the whole document resolved to.
 */
export function createInstructionMarkdown(opts: InstructionMarkdownOptions = {}): MarkdownIt {
  const md = new MarkdownIt({ html: true, breaks: false, linkify: false })
  const voteText = opts.voteText

  // A table wider than the panel has nowhere to go: it is squeezed until every
  // cell wraps. Wrap each one in its own scroll container (the editor gets the
  // equivalent for free from the Table extension's `.tableWrapper`).
  md.renderer.rules.table_open = (tokens, idx, options, _env, self) =>
    '<div class="md-table-wrap">' + self.renderToken(tokens, idx, options)
  md.renderer.rules.table_close = (tokens, idx, options, _env, self) =>
    self.renderToken(tokens, idx, options) + '</div>'

  md.core.ruler.push('block_dir', (state) => {
    const roots: DirNode<any>[] = []
    const stack: DirNode<any>[] = []
    const docCounts = { rtl: 0, ltr: 0 }
    for (const token of state.tokens) {
      const kind = DIR_KIND_BY_TOKEN[token.type]
      if (kind) {
        const node: DirNode<any> = { kind, counts: { rtl: 0, ltr: 0 }, text: '', children: [], ref: token }
        ;(stack.length ? stack[stack.length - 1].children : roots).push(node)
        stack.push(node)
        continue
      }
      if (token.nesting === -1 && stack.length && token.type.replace(/_close$/, '_open') === stack[stack.length - 1].ref.type) {
        stack.pop()
        continue
      }
      if (token.type !== 'inline') continue
      for (const child of token.children || []) {
        if (child.type !== 'text' && child.type !== 'code_inline') continue
        const mapped = voteText ? voteText(child.content, state.env) : null
        const text = mapped ? mapped.text : child.content
        for (const node of stack) node.text += text
        if (child.type === 'code_inline') continue // identifiers don't vote
        const vote = mapped ? mapped.vote : child.content
        countDirWords(vote, docCounts)
        for (const node of stack) countDirWords(vote, node.counts)
      }
    }
    const uiDir = typeof document !== 'undefined' && document.documentElement.getAttribute('dir') === 'rtl' ? 'rtl' : null
    const docDir: TextDir | null = resolveDirTree(roots, docCounts, { fallback: uiDir, settled: true })
    if (state.env && typeof state.env === 'object') state.env.docDir = docDir
    const apply = (node: DirNode<any>) => {
      if (node.dir) node.ref.attrSet('dir', node.dir)
      if (node.signedNumber) node.ref.attrSet('data-bidi-num', '')
      node.children.forEach(apply)
    }
    roots.forEach(apply)
  })

  return md
}
