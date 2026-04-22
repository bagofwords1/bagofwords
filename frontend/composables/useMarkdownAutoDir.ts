/**
 * Auto-direction helper for markdown-rendered lists.
 *
 * Problem: markstream-vue emits `<ol>/<ul>` with no dir, `<li dir="auto">`,
 * `<p dir="auto">`. Per HTML spec, a `dir="auto"` element's first-strong-char
 * scan EXCLUDES descendants that have their own `dir` attribute. So the
 * innermost `<p>` resolves correctly (e.g. RTL) but the surrounding `<li>`
 * and `<ol>` — looking past the `<p>`'s `dir="auto"` — see no strong chars
 * of their own and fall back to LTR. The list marker follows the <li>'s
 * direction, so markers render on the wrong edge for mixed-locale content.
 *
 * `:dir()` and `:has(:dir())` follow the same spec algorithm, so there is no
 * pure-CSS fix. We inspect `textContent` ourselves (which DOES see through
 * nested `dir="auto"`) and set an explicit `dir` attribute on each list
 * element. The RTL stylesheet has a matching `[dir="rtl"]` rule on
 * `.markstream-vue ol[dir="rtl"]` etc. that swaps the padding side.
 */

const RTL_CHAR = /[\u0590-\u08FF\uFB1D-\uFDFF\uFE70-\uFEFC]/
const LTR_CHAR = /[A-Za-z\u00C0-\u024F\u0370-\u05BF\u0900-\u10FF]/

function firstStrongDir(text: string): 'rtl' | 'ltr' | null {
  if (!text) return null
  for (const ch of text) {
    if (RTL_CHAR.test(ch)) return 'rtl'
    if (/[A-Za-z\u00C0-\u024F]/.test(ch)) return 'ltr'
  }
  return null
}

function applyDir(el: Element) {
  const dir = firstStrongDir(el.textContent || '')
  if (!dir) return
  if (el.getAttribute('dir') !== dir) el.setAttribute('dir', dir)
}

function scan(root: ParentNode) {
  const lists = root.querySelectorAll('.markdown-wrapper ol, .markdown-wrapper ul, .markdown-wrapper li')
  lists.forEach(applyDir)
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
      // Text changes under a list element — re-evaluate the enclosing list/li.
      if (m.type === 'characterData') {
        let node: Node | null = m.target
        while (node && node !== document.body) {
          if (node.nodeType === 1) {
            const el = node as Element
            if (el.matches?.('ol, ul, li') && el.closest('.markdown-wrapper')) {
              schedule(el)
            }
          }
          node = node.parentNode
        }
        continue
      }
      m.addedNodes.forEach((n) => {
        if (n.nodeType !== 1) return
        const el = n as Element
        if (!el.closest?.('.markdown-wrapper') && !el.querySelector?.('.markdown-wrapper')) return
        if (el.matches?.('ol, ul, li')) schedule(el)
        el.querySelectorAll?.('ol, ul, li').forEach((child) => schedule(child))
        // And re-scan any list ancestor whose text just grew.
        const parentList = el.parentElement?.closest?.('ol, ul, li')
        if (parentList && parentList.closest('.markdown-wrapper')) schedule(parentList)
      })
    }
  })

  observer.observe(document.body, { childList: true, subtree: true, characterData: true })
  scan(document)

  return {
    stop() {
      observer.disconnect()
      if (rafId !== null) window.cancelAnimationFrame(rafId)
    },
  }
}
