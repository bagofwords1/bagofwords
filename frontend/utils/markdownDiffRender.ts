// Browser half of the markdown-aware diff (see utils/markdownDiff.ts): render
// the sentinel-marked source with the instruction markdown pipeline, sanitize
// it, then turn the sentinels into well-nested <ins>/<del> elements.
import DOMPurify from 'dompurify'
import { createInstructionMarkdown } from './instructionMarkdown'
import {
  buildMarkdownDiff,
  diffVoteText,
  HAS_SENTINEL,
  parseSentinels,
  stripSentinels,
  type BuildOptions,
  type DiffMark,
  type DiffPiece,
  type MarkdownDiffSource,
} from './markdownDiff'
import type { TextDir } from './textDirection'

const diffMd = createInstructionMarkdown({ voteText: diffVoteText })

export interface RenderOptions extends BuildOptions {
  /** Label for a collapsed run of unchanged lines. */
  gapLabel?: (lines: number) => string
  /** id / anchor value for a change's first fragment (scroll targets, overview markers). */
  hunkAnchor?: (hunk: number) => string | null | undefined
}

export interface RenderedDiff { html: string; dir: TextDir | null }

// percent-encoded U+E000–U+E0FF, as markdown-it writes them into a link target
const ENCODED_SENTINEL = /%EE%8[0-3]%[89AB][0-9A-F]/i
const ENCODED_SENTINELS = /%EE%8[0-3]%[89AB][0-9A-F]/gi

export function renderMarkdownDiff(pieces: DiffPiece[], opts: RenderOptions = {}): RenderedDiff {
  if (typeof document === 'undefined') return { html: '', dir: null }
  let result = renderOnce(pieces, opts)
  // A change can still end up with no element to hover — its only text was in
  // a link target or an HTML attribute. Render once more with a ¶ for it.
  if (result.missing.size) {
    result = renderOnce(pieces, { ...opts, forceVisible: new Set([...(opts.forceVisible || []), ...result.missing]) })
  }
  return { html: result.html, dir: result.dir }
}

function renderOnce(pieces: DiffPiece[], opts: RenderOptions): RenderedDiff & { missing: Set<number> } {
  const src = buildMarkdownDiff(pieces, opts)
  const env: any = {}
  const raw = diffMd.render(src.source, env)
  // Same sanitizing as InstructionText: instruction text is partly
  // model-written and rendered with html:true.
  const tpl = document.createElement('template')
  tpl.innerHTML = DOMPurify.sanitize(raw)
  applyDiffMarks(tpl.content, src, opts)
  const rendered = new Set<number>()
  tpl.content.querySelectorAll('[data-hunk]').forEach(el => rendered.add(Number(el.getAttribute('data-hunk'))))
  const missing = new Set([...src.hunks].filter(h => !rendered.has(h)))
  const holder = document.createElement('div')
  holder.appendChild(tpl.content)
  // Sanitize again AFTER marking: removing the sentinels changes text and
  // attributes the first pass already approved (a link target only becomes
  // `javascript:` once a sentinel inside it is gone), so the first pass alone
  // is not enough. Our own marks (ins/del, data-*, id) pass through.
  return { html: DOMPurify.sanitize(holder.innerHTML), dir: env.docDir ?? null, missing }
}

function applyDiffMarks(root: DocumentFragment, src: MarkdownDiffSource, opts: RenderOptions) {
  const doc = root.ownerDocument || document
  const anchored = new Set<number>()

  const markEl = (mark: DiffMark, hunk: number | undefined, text: string, inline: boolean) => {
    const el = doc.createElement(mark)
    el.className = `md-diff-${mark}${inline ? ' md-diff-inline' : ''}`
    if (hunk != null) {
      el.setAttribute('data-hunk', String(hunk))
      if (!anchored.has(hunk)) {
        anchored.add(hunk)
        const anchor = opts.hunkAnchor?.(hunk)
        if (anchor) {
          el.id = `htc-${anchor}`
          el.setAttribute('data-hunk-anchor', anchor)
        }
      }
    }
    el.textContent = text
    return el
  }

  const gapEl = (index: number, block: boolean) => {
    const el = doc.createElement(block ? 'div' : 'span')
    el.className = 'md-diff-gap'
    el.setAttribute('data-diff-gap', '')
    // UI text, not instruction text: it reads in the UI's direction (an
    // English label inside a Hebrew instruction must not be reordered).
    el.setAttribute('dir', document.documentElement.getAttribute('dir') === 'rtl' ? 'rtl' : 'ltr')
    const lines = src.gaps[index] ?? 0
    el.textContent = opts.gapLabel ? opts.gapLabel(lines) : `··· ${lines} ···`
    return el
  }

  const walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  const nodes: Text[] = []
  while (walker.nextNode()) nodes.push(walker.currentNode as Text)

  let open: { mark: DiffMark; hunk?: number } | null = null
  for (const node of nodes) {
    if (!HAS_SENTINEL.test(node.data)) {
      if (open && node.data) node.replaceWith(markEl(open.mark, open.hunk, node.data, false))
      continue
    }
    const frag = doc.createDocumentFragment()
    for (const tok of parseSentinels(node.data)) {
      if (tok.type === 'text') frag.append(open ? markEl(open.mark, open.hunk, tok.value, false) : doc.createTextNode(tok.value))
      else if (tok.type === 'open') open = { mark: tok.mark, hunk: tok.hunk }
      else if (tok.type === 'close') open = null
      else if (tok.type === 'inline') {
        const m = src.inlineMarks[tok.index]
        if (m) frag.append(markEl(m.mark, m.hunk, m.text, true))
      } else if (tok.type === 'gap') {
        const parent = node.parentElement
        // A gap alone in its paragraph replaces the paragraph.
        if (parent && parent.tagName === 'P' && stripSentinels(parent.textContent || '').trim() === '') {
          parent.replaceWith(gapEl(tok.index, true))
          open = null
          break
        }
        frag.append(gapEl(tok.index, false))
      }
    }
    if (node.parentNode) node.replaceWith(frag)
  }

  // A sentinel can end up in an attribute (a change inside a link target).
  root.querySelectorAll('*').forEach((el) => {
    for (const attr of Array.from(el.attributes)) {
      if (HAS_SENTINEL.test(attr.value) || ENCODED_SENTINEL.test(attr.value)) {
        el.setAttribute(attr.name, stripSentinels(attr.value).replace(ENCODED_SENTINELS, ''))
      }
    }
  })
}
