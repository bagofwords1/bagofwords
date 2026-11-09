/**
 * A per-block "copy" button on every rendered code block (a `<pre>`) inside a
 * markdown surface — the grey code frames in chat answers, notes, and the
 * eval/trace markdown.
 *
 * The chat renders markdown with markstream-vue's `render-code-blocks-as-pre`
 * flag, which drops the library's own rich code-block toolbar (and the copy
 * button that comes with it) in favour of a plain `<pre><code>`. So a single
 * snippet the model emits — a DAX measure, a SQL query — has nowhere to copy
 * from: the whole-answer copy button in `pages/reports/[id]/index.vue` copies
 * the entire reply as one blob, not one block.
 *
 * This mirrors useMarkdownAutoDir: one MutationObserver for the whole app,
 * started once by `plugins/markdown-code-copy.client.ts`, idempotent per
 * `<pre>`. We inject the button into the DOM rather than wrap MarkdownRender so
 * it reaches markstream's internal `<pre>` without touching the eight call
 * sites, and so it keeps working while a code block streams in (the text is
 * read from the live `<code>` at click time).
 */

// Same scopes useMarkdownAutoDir stamps: `.markstream-vue` is the renderer's
// own root (chat, notes, modals), `.markdown-content` the MDC wrapper.
const SCOPE_SELECTOR = '.markdown-wrapper, .markdown-content, .markstream-vue, .chat-md, .rendered-html'

type Labels = { copy: string; copied: string }

// heroicons (outline) — same icons as the whole-answer copy button in the
// report footer, so a per-block copy reads as the same affordance.
const COPY_ICON =
  '<svg class="bow-code-copy-i bow-code-copy-copy" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15.666 3.888A2.25 2.25 0 0 0 13.5 2.25h-3c-1.03 0-1.9.693-2.166 1.638m7.332 0c.055.194.084.4.084.612v0a.75.75 0 0 1-.75.75H9a.75.75 0 0 1-.75-.75v0c0-.212.03-.418.084-.612m7.332 0c.646.049 1.288.11 1.927.184 1.1.128 1.907 1.077 1.907 2.185V19.5a2.25 2.25 0 0 1-2.25 2.25H6.75A2.25 2.25 0 0 1 4.5 19.5V6.257c0-1.108.806-2.057 1.907-2.185a48.208 48.208 0 0 1 1.927-.184"></path></svg>'
const CHECK_ICON =
  '<svg class="bow-code-copy-i bow-code-copy-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m4.5 12.75 6 6 9-13.5"></path></svg>'

const STYLE_ID = 'bow-code-copy-style'

function injectStyleOnce() {
  if (document.getElementById(STYLE_ID)) return
  const style = document.createElement('style')
  style.id = STYLE_ID
  // Tokens from assets/css/tokens.css flip with the theme, so no dark: needed.
  // A copy button at the bottom-end of each block. Styled like a plain ghost
  // icon button — the same quiet grey clipboard/check the whole-answer copy
  // button uses; no colour, no shadow.
  style.textContent = `
.bow-code-copy{position:absolute;inset-inline-end:.375rem;z-index:1;display:inline-flex;align-items:center;justify-content:center;width:1.5rem;height:1.5rem;padding:0;margin:0;border:none;border-radius:6px;background:transparent;color:rgb(var(--bow-ink-muted-rgb));cursor:pointer;opacity:.6;transition:opacity 150ms ease,background-color 150ms ease;}
.bow-code-copy--bottom{bottom:.375rem;}
.bow-code-copy:hover{opacity:1;background:rgb(var(--bow-bg-muted-rgb));}
.bow-code-copy:focus-visible{opacity:1;}
.bow-code-copy-i{width:.875rem;height:.875rem;pointer-events:none;}
.bow-code-copy-check{display:none;}
.bow-code-copy[data-copied="true"] .bow-code-copy-copy{display:none;}
.bow-code-copy[data-copied="true"] .bow-code-copy-check{display:inline;}
.bow-code-copy[data-copied="true"]{opacity:1;}
`
  document.head.appendChild(style)
}

function makeButton(pre: HTMLElement, where: 'top' | 'bottom', labels: () => Labels) {
  const { copy } = labels()
  const btn = document.createElement('button')
  btn.type = 'button'
  btn.className = `bow-code-copy bow-code-copy--${where}`
  btn.setAttribute('aria-label', copy)
  btn.title = copy
  btn.innerHTML = COPY_ICON + CHECK_ICON

  let resetTimer: ReturnType<typeof setTimeout> | null = null
  btn.addEventListener('click', (e) => {
    e.preventDefault()
    e.stopPropagation()
    const code = pre.querySelector('code')
    const text = (code?.textContent ?? pre.textContent ?? '').replace(/\n$/, '')
    if (!text) return
    void navigator.clipboard?.writeText(text)
    const { copied } = labels()
    // Reflect the result on BOTH buttons of this block.
    pre.querySelectorAll<HTMLElement>(':scope > .bow-code-copy').forEach((b) => {
      b.dataset.copied = 'true'
      b.setAttribute('aria-label', copied)
      b.title = copied
    })
    if (resetTimer) clearTimeout(resetTimer)
    resetTimer = setTimeout(() => {
      const { copy: c } = labels()
      pre.querySelectorAll<HTMLElement>(':scope > .bow-code-copy').forEach((b) => {
        delete b.dataset.copied
        b.setAttribute('aria-label', c)
        b.title = c
      })
    }, 1500)
  })
  return btn
}

function enhance(pre: HTMLElement, labels: () => Labels) {
  // Idempotent: skip if the button is already there. (If a Vue/markstream
  // patch strips it, a later pass re-adds it.)
  if (pre.querySelector(':scope > .bow-code-copy')) return
  if (!pre.closest(SCOPE_SELECTOR)) return

  if (getComputedStyle(pre).position === 'static') pre.style.position = 'relative'

  pre.appendChild(makeButton(pre, 'bottom', labels))
}

let active: { stop: () => void } | null = null

/**
 * Starts (once) the observer that keeps a copy button on every code block.
 * `labels` is read lazily so the button text follows the active locale.
 */
export function useCodeBlockCopy(labels: () => Labels) {
  if (typeof window === 'undefined') return { stop: () => {} }
  if (active) return active

  injectStyleOnce()

  const enhanceIn = (node: Node | null) => {
    if (!node || node.nodeType !== 1) return
    const el = node as Element
    if (el.matches?.('pre')) enhance(el as HTMLElement, labels)
    el.querySelectorAll?.('pre').forEach((p) => enhance(p as HTMLElement, labels))
  }

  const observer = new MutationObserver((mutations) => {
    for (const m of mutations) {
      m.addedNodes.forEach(enhanceIn)
      // A patch that replaced a <pre>'s children (and our button with them)
      // leaves the <pre> itself as the mutation target — re-check it.
      if (m.type === 'childList' && (m.target as Element)?.matches?.('pre')) {
        enhance(m.target as HTMLElement, labels)
      }
    }
  })

  observer.observe(document.body, { childList: true, subtree: true })

  document.querySelectorAll(`${SCOPE_SELECTOR}`).forEach((scope) => {
    scope.querySelectorAll('pre').forEach((p) => enhance(p as HTMLElement, labels))
  })

  active = { stop() { observer.disconnect(); active = null } }
  return active
}
