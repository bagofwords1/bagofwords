/*
 * `v-text-dir`: give an element — or the input/textarea inside it — the
 * direction of its own text (word majority, see utils/textDirection.ts), for
 * user- and model-written strings: titles, names, previews, free-text fields.
 *
 * Why not plain `dir="auto"`:
 *  - it resolves from the FIRST letter, so "KPI dashboard: תבנה לי…" goes LTR;
 *  - on an EMPTY field it resolves LTR, so a Hebrew placeholder jumps left in
 *    the Hebrew UI. Here an empty field drops `dir` and inherits the UI.
 *
 * Works on a plain element (reads textContent), an <input>/<textarea>, or a
 * component whose root wraps one (UInput, UTextarea): the inner field gets the
 * attribute and is re-evaluated on every keystroke and on v-model updates.
 * Don't combine it with a static `dir` attribute on the same element.
 *
 * Two modes:
 *  - `v-text-dir` — a LABEL (title, name, single-line row or field): the text
 *    reads in its own direction, but stays aligned with the surrounding UI
 *    (`.bidi-label` in assets/css/rtl.css), so a Hebrew instruction title in an English
 *    tree row sits next to its icon instead of jumping to the far edge.
 *  - `v-text-dir.align` — CONTENT (descriptions, previews, multi-line text,
 *    textareas): alignment follows the text too, so a Hebrew paragraph
 *    right-aligns.
 */
import type { Directive } from 'vue'
import { dominantTextDir } from '~/utils/textDirection'

type FieldEl = HTMLInputElement | HTMLTextAreaElement
type DirEl = HTMLElement & { __textDirField?: FieldEl | null; __textDirOnInput?: () => void; __textDirAlign?: boolean }

function fieldOf(el: HTMLElement): FieldEl | null {
  if (el.matches?.('input, textarea')) return el as FieldEl
  return (el.querySelector?.('input, textarea') as FieldEl | null) ?? null
}

function apply(el: DirEl) {
  const field = el.__textDirField
  const target: HTMLElement = field || el
  const dir = dominantTextDir(field ? field.value : el.textContent)
  if (dir) {
    if (target.getAttribute('dir') !== dir) target.setAttribute('dir', dir)
  } else if (target.hasAttribute('dir')) {
    target.removeAttribute('dir')
  }
  // Labels keep the UI's alignment: only the reading order follows the text.
  // (A class, not `text-align: match-parent` — Chromium ignores that and
  // aligns by the element's own direction.) Content mode adds nothing.
  if (!el.__textDirAlign) target.classList.toggle('bidi-label', !!dir)
}

export const vTextDir: Directive<DirEl> = {
  mounted(el, binding) {
    el.__textDirAlign = !!binding.modifiers.align
    el.__textDirField = fieldOf(el)
    if (el.__textDirField) {
      el.__textDirOnInput = () => apply(el)
      el.__textDirField.addEventListener('input', el.__textDirOnInput)
    }
    apply(el)
  },
  updated(el) {
    // A component can swap its inner field (e.g. a textarea that remounts).
    const field = fieldOf(el)
    if (field !== el.__textDirField && el.__textDirOnInput) {
      el.__textDirField?.removeEventListener('input', el.__textDirOnInput)
      field?.addEventListener('input', el.__textDirOnInput)
    }
    el.__textDirField = field
    apply(el)
  },
  beforeUnmount(el) {
    if (el.__textDirOnInput) el.__textDirField?.removeEventListener('input', el.__textDirOnInput)
  },
}

export default defineNuxtPlugin((nuxtApp) => {
  nuxtApp.vueApp.directive('text-dir', vTextDir)
})
