/**
 * Auto-direction helper for markdown-rendered chat content.
 *
 * Problem 1 (lists): markstream-vue emits `<ol>/<ul>` with no dir,
 * `<li dir="auto">`, `<p dir="auto">`. Per HTML spec, a `dir="auto"` element's
 * first-strong-char scan EXCLUDES descendants that have their own `dir`
 * attribute. So the innermost `<p>` resolves correctly (e.g. RTL) but the
 * surrounding `<li>` and `<ol>` — looking past the `<p>`'s `dir="auto"` — see
 * no strong chars of their own and fall back to LTR. The list marker follows
 * the <li>'s direction, so markers render on the wrong edge for mixed-locale
 * content. `:dir()` and `:has(:dir())` follow the same spec algorithm, so
 * there is no pure-CSS fix.
 *
 * Problem 2 (paragraphs/headings): `dir="auto"` resolves from the FIRST
 * strong character only. Hebrew/Arabic analysis answers constantly open a
 * sentence with a Latin token — a system name, a ticket id, a column in
 * inline code, a bolded English term — which flips the entire paragraph to
 * LTR even when the rest of it is RTL prose.
 *
 * Fix for both: estimate the DOMINANT direction ourselves (word-based count
 * of strong-RTL vs strong-LTR words, skipping code/pre, RTL winning at ≥40%
 * — RTL text embeds far more Latin tokens than the reverse) and set an
 * explicit `dir` attribute on each block element inside a markdown surface.
 * The RTL stylesheet has matching `[dir="rtl"]` rules on list elements that
 * swap the marker padding side, and the chat pages' CSS switches stamped
 * elements from `unicode-bidi: plaintext` to `isolate` so the explicit dir
 * governs alignment.
 */

const RTL_CHAR = /[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFC]/
const LTR_CHAR = /[A-Za-z\u00C0-\u024F]/

// Block elements whose direction we manage, inside any markdown surface
// (`.markdown-wrapper` chat containers and `.markdown-content` markstream
// roots — the latter also covers thinking/reasoning boxes).
const BLOCK_SELECTOR = 'ol, ul, li, p, h1, h2, h3, h4, h5, h6, blockquote, table'
const SCOPE_SELECTOR = '.markdown-wrapper, .markdown-content'
// RTL wins below 50% because RTL prose routinely embeds Latin identifiers,
// acronyms and URLs; mostly-Latin text with a stray RTL word stays LTR.
const RTL_WORD_THRESHOLD = 0.4

function wordDir(word: string): 'rtl' | 'ltr' | null {
  for (const ch of word) {
    if (RTL_CHAR.test(ch)) return 'rtl'
    if (LTR_CHAR.test(ch)) return 'ltr'
  }
  return null
}

// Dominant direction of an element's visible text. Counts whole words by
// their first strong character (Closure-style estimation) instead of taking
// the element's single first strong char. Code spans are direction-neutral
// identifiers, not prose — exclude them from the vote.
export function dominantDir(el: Element): 'rtl' | 'ltr' | null {
  let rtl = 0
  let ltr = 0
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const parent = (node as Text).parentElement
      return parent && parent.closest('code, pre')
        ? NodeFilter.FILTER_REJECT
        : NodeFilter.FILTER_ACCEPT
    },
  })
  let node: Node | null
  while ((node = walker.nextNode())) {
    for (const word of (node.textContent || '').split(/\s+/)) {
      const d = wordDir(word)
      if (d === 'rtl') rtl++
      else if (d === 'ltr') ltr++
    }
  }
  const total = rtl + ltr
  if (total === 0) return null
  return rtl / total >= RTL_WORD_THRESHOLD ? 'rtl' : 'ltr'
}

function applyDir(el: Element) {
  const dir = dominantDir(el)
  if (!dir) return
  if (el.getAttribute('dir') !== dir) el.setAttribute('dir', dir)
}

function inScope(el: Element): boolean {
  return !!el.closest(SCOPE_SELECTOR)
}

function scan(root: ParentNode) {
  root.querySelectorAll(BLOCK_SELECTOR).forEach((el) => {
    if (inScope(el)) applyDir(el)
  })
}

export function useMarkdownAutoDir() {
  if (typeof window === 'undefined') return { stop: () => {} }

  let rafId: number | null = null
  const pending = new Set<Element>()

  const flush = () => {
    rafId = null
    pending.forEach(applyDir)
    pending.clear()
  }
  const schedule = (el: Element) => {
    pending.add(el)
    if (rafId === null) rafId = window.requestAnimationFrame(flush)
  }

  const observer = new MutationObserver((mutations) => {
    for (const m of mutations) {
      // Vue patches can restore markstream's static `dir="auto"` — re-stamp.
      if (m.type === 'attributes') {
        const el = m.target as Element
        if (el.matches?.(BLOCK_SELECTOR) && inScope(el)) schedule(el)
        continue
      }
      // Text changes — re-evaluate every managed ancestor (streaming can
      // shift a paragraph's dominant direction as more words arrive).
      if (m.type === 'characterData') {
        let node: Node | null = m.target
        while (node && node !== document.body) {
          if (node.nodeType === 1) {
            const el = node as Element
            if (el.matches?.(BLOCK_SELECTOR) && inScope(el)) schedule(el)
          }
          node = node.parentNode
        }
        continue
      }
      m.addedNodes.forEach((n) => {
        // Typewriter streaming appends bare text nodes — re-evaluate the
        // block they landed in (a paragraph's dominant direction can flip
        // as more words arrive).
        if (n.nodeType === 3) {
          const parentBlock = n.parentElement?.closest?.(BLOCK_SELECTOR)
          if (parentBlock && inScope(parentBlock)) schedule(parentBlock)
          return
        }
        if (n.nodeType !== 1) return
        const el = n as Element
        if (el.matches?.(BLOCK_SELECTOR) && inScope(el)) schedule(el)
        el.querySelectorAll?.(BLOCK_SELECTOR).forEach((child) => {
          if (inScope(child)) schedule(child)
        })
        // And re-scan any managed ancestor whose content just grew.
        const parentBlock = el.parentElement?.closest?.(BLOCK_SELECTOR)
        if (parentBlock && inScope(parentBlock)) schedule(parentBlock)
      })
    }
  })

  observer.observe(document.body, {
    childList: true,
    subtree: true,
    characterData: true,
    attributes: true,
    attributeFilter: ['dir'],
  })
  scan(document)

  return {
    stop() {
      observer.disconnect()
      if (rafId !== null) window.cancelAnimationFrame(rafId)
    },
  }
}
