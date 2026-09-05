<template>
  <div class="doc-print-page">
    <DocViewer
      v-if="markdown"
      :markdown="markdown"
      :visualizations="visualizations"
      :files="files"
      paper
    />
    <div v-else class="doc-print-empty">No document to render.</div>
  </div>
</template>

<script setup lang="ts">
/**
 * Standalone paper rendering of a doc artifact — the page the PDF export
 * renders (backend/app/services/report_pdf_service.py `_render_doc_pdf`).
 *
 * Why a page of its own: printing the app was the bug. The doc lives in an
 * absolutely-positioned pane inside a split-screen shell, and the print
 * stylesheet only hid the chrome with `visibility: hidden` — which keeps every
 * layout box. On paper the invisible chat pane (37% of the window, in px) still
 * ate the sheet, so the document printed as a narrow ribbon down the right
 * margin: a four-page report came out as twenty.
 *
 * Here there is no shell. The document is the page, its measure is the paper's
 * measure, and `@page` sets the margins — so the same DocViewer that renders on
 * screen produces a document-shaped PDF.
 *
 * Data arrives as `window.__BOW_DOC__`, injected by the renderer before
 * navigation (Playwright `add_init_script`). Nothing is fetched: the route is
 * public precisely because it can only ever show what it was handed.
 */
import { onMounted, nextTick, ref } from 'vue'
import DocViewer from '~/components/dashboard/DocViewer.vue'

definePageMeta({ layout: false, auth: false })

// Paper is white and the renderer is not a person with a theme preference.
useHead({ htmlAttrs: { class: 'bow-paper' } })

const payload = (typeof window !== 'undefined' ? (window as any).__BOW_DOC__ : null) || {}
const markdown = ref<string>(payload.markdown || '')
const visualizations = ref<any[]>(payload.visualizations || [])
const files = ref<Record<string, string>>(payload.files || {})

onMounted(async () => {
  // Nuxt color mode may have restored a dark preference from storage; a PDF is
  // printed on white paper either way.
  document.documentElement.classList.remove('dark')

  // The renderer waits for this before it measures anything. It marks the
  // markdown as laid out — charts settle after, on the renderer's own clock.
  await nextTick()
  requestAnimationFrame(() => requestAnimationFrame(() => {
    document.documentElement.setAttribute('data-bow-doc-ready', '1')
  }))
})
</script>

<style>
/* Paper styling. Deliberately global (not scoped): it has to reach inside
   DocViewer's own scoped rules, which are written for a scrolling pane. Every
   rule is prefixed with `.bow-paper` so it can only apply on this page. */

@page {
  size: A4;
  /* Word's default document margins. The document body carries none of its
     own, so this is the only thing standing between text and the sheet edge. */
  margin: 20mm;
}

.bow-paper,
.bow-paper body {
  background: #fff;
  color-scheme: light;
}

/* The document IS the page: no viewport-height box, no scroller, no measure
   borrowed from a pane that does not exist here. */
.bow-paper .doc-print-page { background: #fff; }
.bow-paper .doc-viewer {
  position: static;
  height: auto;
  overflow: visible;
  background: #fff;
}
.bow-paper .bow-doc {
  max-width: none;
  margin: 0;
  padding: 0;
  /* Document-scale text: the on-screen 13px reads as ~9.75pt on paper. */
  font-size: 10.5pt;
  line-height: 1.6;
  color: #1f2937;
}

/* Typography — a document hierarchy in points, not UI pixels. */
.bow-paper .doc-print-page .bow-doc-md h1 {
  font-size: 20pt;
  line-height: 1.25;
  margin: 0 0 12pt;
}
.bow-paper .doc-print-page .bow-doc-md h1:not(:first-child) { margin-top: 24pt; }
.bow-paper .doc-print-page .bow-doc-md h2 {
  font-size: 14pt;
  margin: 18pt 0 6pt;
  padding-bottom: 3pt;
  border-bottom: 0.75pt solid #e5e7eb;
}
.bow-paper .doc-print-page .bow-doc-md h3 { font-size: 12pt; margin: 14pt 0 4pt; }
.bow-paper .doc-print-page .bow-doc-md h4 { font-size: 10.5pt; margin: 12pt 0 3pt; }
.bow-paper .doc-print-page .bow-doc-md p { margin: 0 0 8pt; }
.bow-paper .doc-print-page .bow-doc-md ul,
.bow-paper .doc-print-page .bow-doc-md ol { margin: 0 0 8pt; padding-inline-start: 18pt; }
.bow-paper .doc-print-page .bow-doc-md li { margin: 0 0 3pt; }
.bow-paper .doc-print-page .bow-doc-md blockquote {
  margin: 10pt 0;
  padding: 2pt 0 2pt 10pt;
  border-inline-start: 2pt solid #e5e7eb;
}
.bow-paper .doc-print-page .bow-doc-md hr { margin: 16pt 0; }
.bow-paper .doc-print-page .bow-doc-md code { font-size: 9pt; }
.bow-paper .doc-print-page .bow-doc-md pre {
  margin: 10pt 0;
  padding: 8pt;
  font-size: 9pt;
  background: #f9fafb;
  border: 0.75pt solid #eef0f3;
  /* A code block that scrolls on screen has nowhere to scroll on paper. */
  overflow: visible;
  white-space: pre-wrap;
  word-break: break-word;
}
.bow-paper .doc-print-page .bow-doc-md table { font-size: 9.5pt; margin: 10pt 0; }
.bow-paper .doc-print-page .bow-doc-md a {
  color: #1d4ed8;
  border-bottom: none;
  text-decoration: underline;
}

/* Embedded visualizations. A table is data, not a card — drop the frame so it
   reads like the document's own tables. A chart keeps a light one. */
.bow-paper .doc-viz { margin: 12pt 0; }
.bow-paper .doc-viz:not(.doc-viz--tall) > div {
  border: 0;
  border-radius: 0;
  overflow: visible;
}
.bow-paper .doc-viz--tall > div { border-color: #eef0f3; }
/* The visualization's title belongs to what it titles: never leave it alone at
   the foot of a page. */
.bow-paper .doc-viz > div > div:first-child {
  font-size: 10pt;
  padding: 0 0 4pt;
  break-after: avoid;
}
.bow-paper .doc-viz--tall > div > div:first-child { padding: 8pt 8pt 0; }
.bow-paper .doc-viz-paper-table { font-size: 9.5pt; }

/* Keep blocks whole. `_PREPARE_PRINT_JS` adds break-inside for anything that
   fits a sheet; these are the rules it cannot infer — a heading must not be
   the last thing on a page, and a paragraph must not leave one line behind. */
.bow-paper .doc-print-page .bow-doc-md h1,
.bow-paper .doc-print-page .bow-doc-md h2,
.bow-paper .doc-print-page .bow-doc-md h3,
.bow-paper .doc-print-page .bow-doc-md h4 { break-after: avoid; break-inside: avoid; }
.bow-paper .doc-print-page .bow-doc-md p,
.bow-paper .doc-print-page .bow-doc-md li { orphans: 3; widows: 3; }
/* A chart or a diagram split across a page boundary is unreadable; a table is
   not — it breaks and repeats its header, the way Word breaks one. (The
   renderer clears the blanket rule `_PREPARE_PRINT_JS` puts on tables.) */
.bow-paper .doc-viz--tall,
.bow-paper .doc-mermaid { break-inside: avoid; }
.bow-paper .doc-print-page img,
.bow-paper .doc-print-page svg { max-width: 100%; }

/* Columns are a screen layout; on a narrow sheet they stay side by side but
   need the gap tightened so neither column collapses to one word per line.
   Split across a page boundary the pairing is lost — a two-column block that
   fits a sheet moves whole. */
.bow-paper .doc-columns { gap: 14pt; break-inside: avoid; }

.bow-paper .doc-print-empty {
  padding: 24pt;
  font-size: 10.5pt;
  color: #9ca3af;
}
</style>
