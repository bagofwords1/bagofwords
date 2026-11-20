<!--
  Tracked changes rendered as instruction markdown (utils/markdownDiff.ts):
  headings, lists, tables and code look exactly as in InstructionText, with
  insertions / deletions marked inline and each block's direction decided by
  its words (deleted words excluded) instead of by its first letter.
  Presentational only: hosts that act on changes (InstructionTrackedChanges)
  listen on the root element and find a change by its `data-hunk` index.
-->
<template>
  <div
    ref="rootEl"
    class="instruction-prose instruction-diff"
    :class="[variant === 'highlight' ? 'diff-highlight' : 'diff-tracked', compact ? 'diff-compact' : '']"
    :dir="rendered.dir || undefined"
    v-html="rendered.html"
  />
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { DiffPiece } from '~/utils/markdownDiff'
import { renderMarkdownDiff } from '~/utils/markdownDiffRender'

const props = withDefaults(defineProps<{
  pieces: DiffPiece[]
  /** 'highlight': green/red backgrounds (version diffs). 'tracked': underline / strike-through (review). */
  variant?: 'highlight' | 'tracked'
  compact?: boolean
  mentionize?: (text: string) => string
  gapLabel?: (lines: number) => string
  hunkAnchor?: (hunk: number) => string | null | undefined
  /** Change under the pointer: all of its fragments get `.is-hover`. */
  activeHunk?: number | null
  /** Change being resolved: all of its fragments get `.is-resolving`. */
  busyHunk?: number | null
}>(), { variant: 'highlight', compact: false, activeHunk: null, busyHunk: null })

const rootEl = ref<HTMLElement | null>(null)

const rendered = computed(() => renderMarkdownDiff(props.pieces || [], {
  mentionize: props.mentionize,
  gapLabel: props.gapLabel,
  hunkAnchor: props.hunkAnchor,
}))

// A change can be split into several fragments (one per text node), so hover
// and busy state are applied to every fragment of it, after each render.
function syncStateClasses() {
  const root = rootEl.value
  if (!root) return
  root.querySelectorAll<HTMLElement>('[data-hunk]').forEach((el) => {
    const h = Number(el.dataset.hunk)
    el.classList.toggle('is-hover', props.activeHunk !== null && h === props.activeHunk)
    el.classList.toggle('is-resolving', props.busyHunk !== null && h === props.busyHunk)
  })
}
watch([rendered, () => props.activeHunk, () => props.busyHunk], syncStateClasses, { flush: 'post', immediate: true })
</script>

<style scoped>
.instruction-diff.diff-compact { font-size: 12px; line-height: 1.55; }

/* Deleted text inside a line is shown as it was typed, line breaks included. */
.instruction-diff :deep(.md-diff-inline) { white-space: pre-wrap; }

/* Version diffs (TrackedChangesView): the same colours it always used. */
.instruction-diff.diff-highlight :deep(ins.md-diff-ins) {
  background: #dcfce7; color: #166534; text-decoration: none; border-radius: 2px;
}
.instruction-diff.diff-highlight :deep(del.md-diff-del) {
  background: #fee2e2; color: #991b1b; text-decoration: line-through; border-radius: 2px;
}

/* Review (InstructionTrackedChanges): the same marks it always used. */
.instruction-diff.diff-tracked :deep(ins.md-diff-ins) {
  color: #047857;
  text-decoration: underline dotted rgba(52, 211, 153, 0.7);
  text-decoration-thickness: 1px;
  text-underline-offset: 3px;
}
.instruction-diff.diff-tracked :deep(del.md-diff-del) {
  color: rgba(244, 63, 94, 0.7);
  text-decoration: line-through #fda4af;
  text-decoration-thickness: 1px;
}
.instruction-diff :deep([data-hunk]) { border-radius: 3px; transition: background-color 0.15s; }
.instruction-diff :deep([data-hunk].is-hover) { background-color: #fffbeb; }
.instruction-diff :deep([data-hunk].is-resolving) { background-color: #fef3c7; opacity: 0.6; }
/* The spinner the per-change resolve always showed, on the change's first fragment. */
.instruction-diff :deep([data-hunk-anchor].is-resolving::after) {
  content: '';
  display: inline-block;
  width: 0.8em;
  height: 0.8em;
  margin-inline-start: 0.25em;
  vertical-align: -0.05em;
  border: 1.5px solid #9ca3af;
  border-top-color: transparent;
  border-radius: 9999px;
  animation: md-diff-spin 0.8s linear infinite;
}
@keyframes md-diff-spin { to { transform: rotate(360deg); } }

/* Collapsed unchanged lines (InstructionTrackedChanges' collapse mode). */
.instruction-diff :deep(.md-diff-gap) {
  display: block;
  /* Sits on the content's side, whatever its own (UI) direction. */
  text-align: match-parent;
  margin: 0.25rem 0;
  font-size: 11px;
  color: #9ca3af;
  cursor: pointer;
  user-select: none;
}
.instruction-diff :deep(.md-diff-gap:hover) { color: #4b5563; }

/* Code keeps its own colours; a change inside it is still marked. */
.instruction-diff :deep(pre ins.md-diff-ins),
.instruction-diff :deep(pre del.md-diff-del) { border-radius: 2px; }
</style>

<style>
/* `.dark` lives on <html>, outside the scoped tree. */
.dark .instruction-diff.diff-highlight ins.md-diff-ins { background: rgba(34, 197, 94, 0.18); color: #86efac; }
.dark .instruction-diff.diff-highlight del.md-diff-del { background: rgba(239, 68, 68, 0.18); color: #fca5a5; }
.dark .instruction-diff.diff-tracked ins.md-diff-ins { color: #34d399; }
.dark .instruction-diff.diff-tracked del.md-diff-del { color: rgba(251, 113, 133, 0.75); }
.dark .instruction-diff [data-hunk].is-hover { background-color: rgba(245, 158, 11, 0.1); }
.dark .instruction-diff [data-hunk].is-resolving { background-color: rgba(245, 158, 11, 0.2); }
.dark .instruction-diff .md-diff-gap:hover { color: #9ca3af; }
.instruction-diff .instruction-mention {
  background-color: rgba(99, 102, 241, 0.12);
  color: #4338ca;
  border-radius: 4px;
  padding: 1px 4px;
  font-weight: 500;
  font-size: 0.95em;
  white-space: nowrap;
}
.dark .instruction-diff .instruction-mention { background-color: rgba(129, 140, 248, 0.18); color: #c7d2fe; }
</style>
