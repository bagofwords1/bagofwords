/**
 * Direction for markdown-rendered chat content (markstream-vue output).
 *
 * markstream emits `dir="auto"` on every `<p>`, `<li>`, heading, cell and
 * quote. The browser then resolves each one from its FIRST strong character,
 * on its own. On Hebrew/Arabic analysis answers that goes wrong three ways:
 *
 *  1. A sentence that opens with a Latin token ("**Revenue** עלה ב-12%…",
 *     "`orders` היא הטבלה…") resolves LTR.
 *  2. Containers can't see their text: per spec a `dir="auto"` element skips
 *     descendants that carry their own `dir`, so `<ol>`/`<ul>`/`<table>` fall
 *     back to LTR and list markers land on the wrong edge.
 *  3. Pieces of one block disagree: each `<li>` / `<td>` picks its own
 *     direction, so a Hebrew list or table ends up half left, half right.
 *
 * So we decide direction ourselves, once per markdown ROOT (a whole answer),
 * top-down, and stamp an explicit `dir` on every block:
 *
 *  - The root's direction is a word vote over all its prose (code excluded,
 *    quantities like "1.2M" neutral — see utils/textDirection.ts). A reply
 *    carries `data-dir-hint`, the direction of the question it answers
 *    (answers mirror the user's language): the root keeps that direction
 *    unless its text is almost entirely the other script, so a Hebrew answer
 *    neither opens LTR on "**Revenue**" nor flips while an English stretch
 *    streams in. Without a hint, the UI direction stands in until there are
 *    enough words to vote.
 *  - Paragraphs, headings and quotes vote on their own text; blocks of three
 *    words or fewer ("Summary", "Key Drivers") follow the surrounding
 *    direction.
 *  - A list or table decides once; its `<th>` / `<td>` take that direction
 *    and every `<li>` takes that same direction. A list item's own paragraph follows the item, so text and
 *    marker stay together. A list or table keeps the answer's direction
 *    unless it is almost entirely the other script.
 *
 * Stamping happens inside the MutationObserver callback, which runs as a
 * microtask before the next paint — the browser never paints the
 * unstamped `dir="auto"` state of newly streamed text.
 *
 * The RTL stylesheet keys list gutters on each item's own direction, and the
 * chat pages' CSS switches stamped blocks from `unicode-bidi: plaintext` to
 * `isolate` so the explicit dir governs alignment.
 */
import { countDirWords, decideDir, resolveDirTree, type DirCounts, type DirNode, type DirNodeKind, type TextDir } from '~/utils/textDirection'

const BLOCK_SELECTOR = 'ol, ul, li, p, h1, h2, h3, h4, h5, h6, blockquote, table, tr, th, td'
// `.markstream-vue` is the renderer's own root class, so every MarkdownRender
// (note cards, modals) is covered, not only the chat containers. `<MDC>`
// renders carry `.markdown-content`; `.chat-md` is the shared-dashboard chat
// and `.rendered-html` the dashboard text widget.
const SCOPE_SELECTOR = '.markdown-wrapper, .markdown-content, .markstream-vue, .chat-md, .rendered-html'

function isDir(v: string | null | undefined): v is TextDir {
  return v === 'rtl' || v === 'ltr'
}

// Word counts of an element's visible prose. Code spans are identifiers, not
// prose — they don't vote.
export function countElementWords(el: Element): DirCounts {
  const counts: DirCounts = { rtl: 0, ltr: 0 }
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const parent = (node as Text).parentElement
      return parent && parent.closest('code, pre')
        ? NodeFilter.FILTER_REJECT
        : NodeFilter.FILTER_ACCEPT
    },
  })
  let node: Node | null
  while ((node = walker.nextNode())) countDirWords(node.textContent || '', counts)
  return counts
}

/** Dominant direction of an element's prose, with no surrounding context. */
export function dominantDir(el: Element): TextDir | null {
  return decideDir(countElementWords(el))
}

// The outermost markdown scope containing `el` (nested lists render nested
// markstream roots; the whole answer is the unit we decide over).
function rootOf(el: Element | null): Element | null {
  let root: Element | null = el?.closest?.(SCOPE_SELECTOR) ?? null
  while (root) {
    const outer = root.parentElement?.closest(SCOPE_SELECTOR)
    if (!outer) break
    root = outer
  }
  return root
}

function replyHintOf(root: Element): TextDir | null {
  const hint = root.closest('[data-dir-hint]')?.getAttribute('data-dir-hint')
  return isDir(hint) ? hint : null
}

function uiDir(): TextDir | null {
  return document.documentElement.getAttribute('dir') === 'rtl' ? 'rtl' : null
}

// Last value we stamped per element, so our own attribute writes don't
// trigger another pass.
const stamped = new WeakMap<Element, TextDir>()

function stamp(el: Element, dir: TextDir | null) {
  if (!dir) return
  stamped.set(el, dir)
  if (el.getAttribute('dir') !== dir) el.setAttribute('dir', dir)
}

const KIND_BY_TAG: Record<string, DirNodeKind> = {
  UL: 'list', OL: 'list', LI: 'item', TABLE: 'table', TR: 'row', TH: 'cell', TD: 'cell', BLOCKQUOTE: 'quote',
}

// The answer's blocks as a tree (each block under its nearest block
// ancestor), in document order, for resolveDirTree.
function blockTree(root: Element): DirNode<Element>[] {
  const roots: DirNode<Element>[] = []
  const byEl = new Map<Element, DirNode<Element>>()
  root.querySelectorAll(BLOCK_SELECTOR).forEach((el) => {
    const kind = KIND_BY_TAG[el.tagName] ?? 'block'
    const node: DirNode<Element> = {
      kind,
      counts: kind === 'row' ? { rtl: 0, ltr: 0 } : countElementWords(el),
      text: kind === 'cell' ? el.textContent || '' : undefined,
      children: [],
      ref: el,
    }
    byEl.set(el, node)
    const parentEl = el.parentElement?.closest(BLOCK_SELECTOR)
    const parent = parentEl && root.contains(parentEl) ? byEl.get(parentEl) : undefined
    ;(parent ? parent.children : roots).push(node)
  })
  return roots
}

export function stampRoot(root: Element) {
  const tree = blockTree(root)
  // A reply carries the question's direction as a hint and may still be
  // streaming; anything else (notes, eval/trace markdown, MDC) is settled.
  const hint = replyHintOf(root)
  const savedDirection = root.closest('[data-response-dir]')?.getAttribute('data-response-dir')
  resolveDirTree(tree, countElementWords(root), {
    hint, direction: isDir(savedDirection) ? savedDirection : null,
    fallback: uiDir(), settled: !hint,
  })
  const apply = (node: DirNode<Element>) => {
    stamp(node.ref, node.dir ?? null)
    // "+12%" in an RTL table is laid out LTR; rtl.css keeps it right-aligned.
    if (node.kind === 'cell') node.ref.toggleAttribute('data-bidi-num', !!node.signedNumber)
    node.children.forEach(apply)
  }
  tree.forEach(apply)
}

// One observer for the whole app (plugins/markdown-auto-dir.client.ts starts
// it); later calls reuse it.
let active: { stop: () => void } | null = null

export function useMarkdownAutoDir() {
  if (typeof window === 'undefined') return { stop: () => {} }
  if (active) return active

  const restamp = (roots: Set<Element>) => {
    roots.forEach((root) => { if (root.isConnected) stampRoot(root) })
  }

  const observer = new MutationObserver((mutations) => {
    const roots = new Set<Element>()
    const add = (node: Node | null) => {
      const el = node && (node.nodeType === 1 ? (node as Element) : node.parentElement)
      const root = rootOf(el ?? null)
      if (root) roots.add(root)
    }
    for (const m of mutations) {
      if (m.type === 'attributes') {
        const el = m.target as Element
        if (m.attributeName === 'data-dir-hint' || m.attributeName === 'data-response-dir') {
          el.querySelectorAll(SCOPE_SELECTOR).forEach((s) => add(s))
          continue
        }
        // Our own write, or a Vue patch restoring markstream's `dir="auto"`.
        if (!el.matches?.(BLOCK_SELECTOR)) continue
        if (stamped.get(el) === el.getAttribute('dir')) continue
        add(el)
        continue
      }
      add(m.target)
      m.addedNodes.forEach((n) => {
        add(n)
        // A whole message mounted at once: pick up the roots inside it.
        if (n.nodeType === 1) (n as Element).querySelectorAll?.(SCOPE_SELECTOR).forEach((s) => add(s))
      })
    }
    // Runs before paint (MutationObserver callbacks are microtasks).
    restamp(roots)
  })

  observer.observe(document.body, {
    childList: true,
    subtree: true,
    characterData: true,
    attributes: true,
    attributeFilter: ['dir', 'data-dir-hint', 'data-response-dir'],
  })

  const initial = new Set<Element>()
  document.querySelectorAll(SCOPE_SELECTOR).forEach((el) => {
    const root = rootOf(el)
    if (root) initial.add(root)
  })
  restamp(initial)

  active = { stop() { observer.disconnect(); active = null } }
  return active
}
