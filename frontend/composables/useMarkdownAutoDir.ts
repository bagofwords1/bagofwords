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
 *    and its `<li>` do too unless an item is more than three words and almost
 *    entirely the other script. A list item's own paragraph follows the item, so text and
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
import { countDirWords, decideDir, isSignedNumber, SHORT_BLOCK_WORDS, type DirCounts, type TextDir } from '~/utils/textDirection'

const BLOCK_SELECTOR = 'ol, ul, li, p, h1, h2, h3, h4, h5, h6, blockquote, table, th, td'
// `.markstream-vue` is the renderer's own root class, so every MarkdownRender
// (note cards, modals) is covered, not only the chat containers.
const SCOPE_SELECTOR = '.markdown-wrapper, .markdown-content, .markstream-vue'
const CONTAINER_TAGS = new Set(['UL', 'OL', 'TABLE'])
// Below this many voting words the root has no reliable direction yet.
const ROOT_MIN_WORDS = 4

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

export function stampRoot(root: Element) {
  const counts = countElementWords(root)
  const total = counts.rtl + counts.ltr
  const replyHint = replyHintOf(root)
  const rootDir = replyHint
    ? decideDir(counts, { context: replyHint, container: true })
    : total < ROOT_MIN_WORDS
      ? (uiDir() ?? decideDir(counts))
      : decideDir(counts)

  const decided = new Map<Element, TextDir | null>()
  const contextFor = (el: Element): TextDir | null => {
    const parentBlock = el.parentElement?.closest(BLOCK_SELECTOR)
    if (parentBlock && root.contains(parentBlock) && decided.has(parentBlock)) {
      return decided.get(parentBlock) ?? rootDir
    }
    return rootDir
  }

  // querySelectorAll is document order: every block's ancestors are decided
  // before the block itself.
  root.querySelectorAll(BLOCK_SELECTOR).forEach((el) => {
    let dir: TextDir | null
    const parentBlock = el.parentElement?.closest(BLOCK_SELECTOR)
    if (el.tagName === 'LI') {
      // An item keeps its list's direction (markers on one side) unless it
      // is more than a few words and almost entirely the other script — an
      // English sentence bullet in a Hebrew list gets its own LTR marker and
      // text, while "North: 1.2M USD" stays with its Hebrew list.
      const list = el.parentElement?.closest('ul, ol')
      const listDir = (list && decided.get(list)) || contextFor(el)
      dir = decideDir(countElementWords(el), { context: listDir, container: true, minWords: SHORT_BLOCK_WORDS })
    } else if (parentBlock?.tagName === 'LI' && root.contains(parentBlock) && /^(P|H[1-6])$/.test(el.tagName)) {
      // An item's own paragraph aligns with its marker.
      dir = decided.get(parentBlock) || contextFor(el)
    } else if (el.tagName === 'TH' || el.tagName === 'TD') {
      const table = el.closest('table')
      dir = (table && decided.get(table)) || contextFor(el)
      // "+12%" in an RTL table: lay the value out LTR so the sign stays in
      // front; rtl.css keeps it aligned to the table's start (right) edge.
      const signed = dir === 'rtl' && isSignedNumber(el.textContent)
      if (signed) dir = 'ltr'
      el.toggleAttribute('data-bidi-num', signed)
    } else {
      dir = decideDir(countElementWords(el), {
        context: contextFor(el),
        container: CONTAINER_TAGS.has(el.tagName),
      })
    }
    decided.set(el, dir)
    stamp(el, dir)
  })
}

export function useMarkdownAutoDir() {
  if (typeof window === 'undefined') return { stop: () => {} }

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
        if (m.attributeName === 'data-dir-hint') {
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
    attributeFilter: ['dir', 'data-dir-hint'],
  })

  const initial = new Set<Element>()
  document.querySelectorAll(SCOPE_SELECTOR).forEach((el) => {
    const root = rootOf(el)
    if (root) initial.add(root)
  })
  restamp(initial)

  return { stop() { observer.disconnect() } }
}
