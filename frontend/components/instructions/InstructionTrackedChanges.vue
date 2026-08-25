<!--
  Per-hunk tracked-changes review (immutable cherry-pick model).
  Server-authoritative: fetches GET /instructions/{id}/review-hunks and renders
  every pending suggestion's hunks interleaved on the live text, Google-Docs
  style. Accept = POST hunks/accept (cherry-pick onto main); Reject = hunks/reject.
  Reused by the Knowledge Explorer review and the Report agent instruction view.
-->
<template>
  <div class="flex flex-col min-h-0" :class="compact ? '' : 'flex-1'">
    <!-- Own header. Hidden (`hide-header`) when the host surface already has a
         status bar of its own — otherwise the label stacks on top of an
         identical one (Knowledge Explorer). Such a host renders the count and
         the bulk actions itself, driven by the `state` event below. -->
    <div v-if="!hideHeader" class="flex items-center justify-between border-b border-gray-100 dark:border-gray-800" :class="compact ? 'px-3 py-1.5' : 'px-6 py-3'">
      <div class="flex items-center gap-2 min-w-0">
        <span class="w-1.5 h-1.5 rounded-full bg-amber-500 shrink-0"></span>
        <span class="font-medium text-gray-700 dark:text-gray-300" :class="compact ? 'text-[11px]' : 'text-xs'">{{ $t('agentsPage.pendingReview') }}</span>
        <span class="text-[11px] text-gray-400 dark:text-gray-500 tabular-nums shrink-0">· {{ totalHunks === 1 ? $t('agentsPage.changeCountOne', { n: totalHunks }) : $t('agentsPage.changeCountMany', { n: totalHunks }) }}</span>
        <button v-if="collapseContext && totalHunks" class="text-[10px] text-gray-400 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 shrink-0" @click="expandedAll = !expandedAll">{{ expandedAll ? $t('agentsPage.collapse') : $t('agentsPage.expandAll') }}</button>
      </div>
      <div v-if="canApprove && totalHunks" class="flex items-center gap-1.5 shrink-0">
        <button class="inline-flex items-center gap-1 h-7 px-2.5 rounded-md bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 text-gray-700 dark:text-gray-300 text-[11px] font-medium hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-40 transition-colors" :disabled="busy" @click="resolveAll('reject')"><UIcon name="i-heroicons-x-mark" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500" />{{ $t('agentsPage.rejectAll') }}</button>
        <button class="inline-flex items-center gap-1 h-7 px-2.5 rounded-md bg-emerald-50 dark:bg-emerald-500/10 border border-emerald-200 dark:border-emerald-500/30 text-emerald-700 dark:text-emerald-400 text-[11px] font-medium hover:bg-emerald-100 dark:hover:bg-emerald-500/20 disabled:opacity-40 transition-colors" :disabled="busy" @click="resolveAll('accept')"><UIcon :name="busy ? 'i-heroicons-arrow-path' : 'i-heroicons-check'" :class="['w-3.5 h-3.5', { 'animate-spin': busy }]" />{{ $t('agentsPage.acceptAll') }}</button>
      </div>
    </div>
    <div class="relative min-h-0" :class="compact ? '' : 'flex-1'">
      <div ref="scrollEl" class="min-h-0 overflow-auto" :class="compact ? 'px-3 py-2 max-h-80' : 'h-full px-8 py-6 max-w-3xl'" @scroll.passive="onReviewScroll">
        <div v-if="loading" class="text-center text-xs text-gray-400 dark:text-gray-500 py-10">Loading…</div>
      <!-- A failed fetch is NOT "all resolved". Reporting zero hunks because the
           request errored tells a reviewer their pending changes were dealt
           with when this component has no idea either way — so the error gets
           its own state, and the empty state below now only means the server
           really returned no hunks. -->
        <div v-else-if="loadError" class="text-center text-xs py-6">
          <div class="text-gray-500 dark:text-gray-400">Couldn’t load pending changes.</div>
          <button class="mt-1 text-gray-400 dark:text-gray-500 underline hover:text-gray-700 dark:hover:text-gray-300" @click="load()">Retry</button>
        </div>
        <div v-else-if="!totalHunks" class="text-center text-xs text-gray-400 dark:text-gray-500 py-6">No pending changes — all resolved.</div>
        <!-- dir=auto + plaintext: per-line bidi, same policy as the read view
             (KnowledgeExplorer), so Hebrew prose lays out RTL while code lines stay LTR. -->
        <div v-else ref="contentEl" dir="auto" style="unicode-bidi: plaintext; text-align: start;" class="whitespace-pre-wrap break-words text-gray-800 dark:text-gray-200" :class="compact ? 'text-[12px] leading-[1.55]' : 'text-[13px] leading-[1.6]'">
          <template v-for="(seg, si) in displaySegments" :key="si">
            <span v-if="seg.kind === 'gap'" class="block my-1 text-[10px] text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-400 cursor-pointer select-none" @click="expandedAll = true">··· {{ seg.lines }} unchanged line{{ seg.lines === 1 ? '' : 's' }} ···</span>
            <template v-else-if="seg.kind === 'context'">
              <template v-for="(pt, pi) in mentionParts(seg.text)" :key="pi"><span v-if="pt.mention" class="instr-mention">@{{ pt.mention }}</span><template v-else>{{ pt.t }}</template></template>
            </template>
            <span v-else :id="`htc-${seg.previewKey || seg.key}`" :data-hunk-anchor="seg.previewKey || seg.key" class="relative inline align-baseline rounded-[3px] transition-colors"
                  :class="resolving === seg.key ? 'bg-amber-100 dark:bg-amber-500/20' : 'hover:bg-amber-50 dark:hover:bg-amber-500/10'"
                  @mousemove="onHunkMove(seg, $event)" @mouseleave="scheduleCardHide()">
              <del v-if="seg.before" class="text-rose-500/70 line-through decoration-rose-300 decoration-1"><template v-for="(pt, pi) in mentionParts(seg.before)" :key="pi"><span v-if="pt.mention" class="instr-mention">@{{ pt.mention }}</span><template v-else>{{ pt.t }}</template></template></del>
              <ins v-if="seg.after" class="text-emerald-700 underline decoration-dotted decoration-emerald-400/70 underline-offset-[3px] decoration-1"><template v-for="(pt, pi) in mentionParts(seg.after)" :key="pi"><span v-if="pt.mention" class="instr-mention">@{{ pt.mention }}</span><template v-else>{{ pt.t }}</template></template></ins>
              <span v-if="resolving === seg.key" class="absolute inset-0 rounded bg-white/50 dark:bg-gray-900/50 flex items-center justify-center"><UIcon name="i-heroicons-arrow-path" class="w-3.5 h-3.5 text-gray-500 dark:text-gray-400 animate-spin" /></span>
            </span>
          </template>
        </div>
      </div>
      <!-- Find-in-page style overview markers. They describe the latest-build
           diff already rendered above; they never influence review actions. -->
      <div v-if="markerGutterVisible" class="pointer-events-none absolute inset-y-2 end-2 z-20 w-2" aria-hidden="false">
        <span class="absolute end-[3px] top-0 h-full w-px rounded-full bg-gray-200/70 dark:bg-gray-700/70"></span>
        <button
          v-for="marker in changeMarkers"
          :key="marker.index"
          type="button"
          class="pointer-events-auto absolute end-0 h-1.5 w-2 -translate-y-1/2 rounded-full transition-all hover:w-3 hover:bg-amber-500 focus-visible:w-3 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-400/50"
          :class="activeMarkerIndex === marker.index ? 'bg-amber-500 shadow-sm' : 'bg-amber-400/70 dark:bg-amber-400/60'"
          :style="{ top: `${marker.ratio * 100}%` }"
          :aria-label="$t('agentsPage.changeMarkerLabel', { current: marker.index + 1, total: changeMarkers.length })"
          @click="scrollToChange(marker.index)"
        ></button>
      </div>
    </div>
    <!-- One shared Accept/Reject card, anchored under the hunk fragment the
         pointer is on (a fragmented inline is a broken abs-pos anchor — in RTL
         `left:0` resolves to the LAST fragment while `top:0` is the first, so
         a per-hunk CSS-hover card lands far from the cursor and drops on the
         way over). JS-positioned + a grace timeout keeps it reachable. -->
    <Teleport to="body">
      <div
        v-if="canApprove && hoverCard"
        id="htc-hover-card"
        ref="cardEl"
        class="fixed z-50 cursor-default select-none"
        :style="{ top: hoverCard.top + 'px', left: hoverCard.left + 'px' }"
        @mouseenter="cancelCardHide"
        @mouseleave="scheduleCardHide()"
        @click.stop
      >
        <div class="w-max max-w-xs rounded-lg bg-white dark:bg-gray-900 shadow-md ring-1 ring-gray-200/70 dark:ring-gray-700 p-2">
          <div class="flex items-center gap-1.5 mb-1.5">
            <span class="w-1.5 h-1.5 rounded-full shrink-0" :class="hoverCard.seg.source === 'ai' ? 'bg-violet-500' : 'bg-blue-500'"></span>
            <span class="text-[10px] text-gray-500 dark:text-gray-400 truncate">{{ hoverCard.seg.source === 'ai' ? 'AI suggestion' : 'Proposed' }}<template v-if="hoverCard.seg.created_by"> · {{ hoverCard.seg.created_by.name }}</template><template v-if="hoverCard.seg.created_at"> · {{ fmtWhen(hoverCard.seg.created_at) }}</template></span>
          </div>
          <!-- Brief evidence stamped by the AI when it proposed this change -->
          <div v-if="hoverCard.seg.evidence" class="mb-1.5 text-[10px] leading-snug text-gray-400 dark:text-gray-500 italic line-clamp-3">{{ hoverCard.seg.evidence }}</div>
          <div class="flex items-center gap-1.5">
            <button class="inline-flex items-center gap-1 h-7 px-2.5 rounded-md bg-emerald-50 dark:bg-emerald-500/10 border border-emerald-200 dark:border-emerald-500/30 text-emerald-700 dark:text-emerald-400 text-[11px] font-medium hover:bg-emerald-100 dark:hover:bg-emerald-500/20 disabled:opacity-40 transition-colors" :disabled="busy" @click.stop="resolveFromCard('accept')"><UIcon name="i-heroicons-check" class="w-3.5 h-3.5" />Accept</button>
            <button class="inline-flex items-center gap-1 h-7 px-2.5 rounded-md bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 text-gray-700 dark:text-gray-300 text-[11px] font-medium hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-40 transition-colors" :disabled="busy" @click.stop="resolveFromCard('reject')"><UIcon name="i-heroicons-x-mark" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500" />Reject</button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, shallowRef, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import {
  buildMentionMatcher,
  parseMentionSegments,
  EMPTY_MENTION_MATCHER,
  type MentionMatcher,
} from '~/utils/mentions'

// `buildId` scopes the panel to ONE suggestion: only that build's hunks are
// shown, and Accept all / Reject all resolve only it. Mount it that way
// wherever the surrounding UI presents a single edit — the chat tool block is
// about one specific edit, so an unscoped "Accept all" there published sibling
// suggestions from other turns that the reviewer had never seen (and Reject
// all discarded them just as silently). Left unset, the panel stays
// instruction-wide, which is right for the Knowledge Explorer and the pending
// pills: those show every suggestion, so resolving every suggestion matches
// what the reviewer is looking at.
const props = defineProps<{ instructionId: string; buildId?: string; canApprove?: boolean; compact?: boolean; collapseContext?: boolean; hideHeader?: boolean }>()
// `empty` means the server said there is nothing pending — hosts treat it as a
// resolution signal (mark accepted, drop the review pane), so it must never
// fire for a failed fetch. `error` is the separate signal for that: hosts with
// a better fallback than this component's inline error (the agent panel and the
// Knowledge Explorer both fall back to showing the instruction itself) listen
// for it; hosts that don't just render the retry state.
const emit = defineEmits<{ (e: 'changed'): void; (e: 'empty'): void; (e: 'error'): void; (e: 'loaded'): void; (e: 'state', s: { total: number; busy: boolean }): void }>()

const loading = ref(false)
const loadError = ref(false)
const busy = ref(false)
const resolving = ref<string | null>(null)
const mainText = ref('')
const mainBuildId = ref<string | null>(null)
const mainVersionId = ref<string | null>(null)
const suggestions = ref<any[]>([])
const scrollEl = ref<HTMLElement | null>(null)
const contentEl = ref<HTMLElement | null>(null)

type ChangeMarker = { index: number; ratio: number }
const changeMarkers = ref<ChangeMarker[]>([])
const markerGutterVisible = ref(false)
const activeMarkerIndex = ref<number | null>(null)
let markerFrame: number | null = null
let markerResizeObserver: ResizeObserver | null = null

function hunkAnchors(): HTMLElement[] {
  return scrollEl.value
    ? Array.from(scrollEl.value.querySelectorAll<HTMLElement>('[data-hunk-anchor]'))
    : []
}

function anchorDocumentY(anchor: HTMLElement, scroller: HTMLElement): number {
  return anchor.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop
}

function updateActiveMarker() {
  const scroller = scrollEl.value
  const anchors = hunkAnchors()
  if (!scroller || !anchors.length) { activeMarkerIndex.value = null; return }
  const viewportCenter = scroller.scrollTop + scroller.clientHeight / 2
  let nearest = 0
  let distance = Number.POSITIVE_INFINITY
  anchors.forEach((anchor, index) => {
    const nextDistance = Math.abs(anchorDocumentY(anchor, scroller) - viewportCenter)
    if (nextDistance < distance) { distance = nextDistance; nearest = index }
  })
  activeMarkerIndex.value = nearest
}

function updateMarkerLayout() {
  markerFrame = null
  const scroller = scrollEl.value
  const anchors = hunkAnchors()
  if (!scroller || !anchors.length || scroller.scrollHeight <= scroller.clientHeight + 8) {
    changeMarkers.value = []
    markerGutterVisible.value = false
    activeMarkerIndex.value = null
    return
  }
  const height = Math.max(scroller.scrollHeight, 1)
  changeMarkers.value = anchors.map((anchor, index) => ({
    index,
    ratio: Math.min(0.985, Math.max(0.015, anchorDocumentY(anchor, scroller) / height)),
  }))
  markerGutterVisible.value = true
  updateActiveMarker()
}

function scheduleMarkerLayout() {
  if (markerFrame !== null) cancelAnimationFrame(markerFrame)
  markerFrame = requestAnimationFrame(updateMarkerLayout)
}

function onReviewScroll() {
  hoverCard.value = null
  updateActiveMarker()
}

function scrollToChange(index: number) {
  const scroller = scrollEl.value
  const anchor = hunkAnchors()[index]
  if (!scroller || !anchor) return
  const target = anchorDocumentY(anchor, scroller) - scroller.clientHeight / 2 + anchor.offsetHeight / 2
  activeMarkerIndex.value = index
  scroller.scrollTo({ top: Math.max(0, target), behavior: 'smooth' })
}

// ── Shared floating Accept/Reject card ────────────────────────────────────────
const hoverCard = ref<{ seg: any; top: number; left: number } | null>(null)
const cardEl = ref<HTMLElement | null>(null)
let cardHideTimer: ReturnType<typeof setTimeout> | null = null

function cancelCardHide() {
  if (cardHideTimer) { clearTimeout(cardHideTimer); cardHideTimer = null }
}
// Grace timeout: the pointer needs to cross the small gap between the hunk
// text and the card without the card flickering away.
function scheduleCardHide(delay = 250) {
  cancelCardHide()
  cardHideTimer = setTimeout(() => { hoverCard.value = null; cardHideTimer = null }, delay)
}
function onHunkMove(seg: any, e: MouseEvent) {
  if (!props.canApprove) return
  cancelCardHide()
  const el = e.currentTarget as HTMLElement
  const rects = Array.from(el.getClientRects()).filter(r => r.width > 0 && r.height > 0)
  if (!rects.length) return
  // Anchor to the line fragment under the pointer — a wrapped hunk has one
  // rect per line, and the card must open next to the one being read.
  const under =
    rects.find(r => e.clientY >= r.top && e.clientY <= r.bottom && e.clientX >= r.left - 4 && e.clientX <= r.right + 4) ||
    rects.reduce((a, b) => (Math.abs((a.top + a.bottom) / 2 - e.clientY) <= Math.abs((b.top + b.bottom) / 2 - e.clientY) ? a : b))
  const top = Math.round(under.bottom + 4)
  // Keep the card still while the pointer stays on the same line of the same hunk.
  if (hoverCard.value?.seg?.key === seg.key && Math.abs(hoverCard.value.top - top) < 2) return
  const width = cardEl.value?.offsetWidth || 220
  const left = Math.round(Math.min(Math.max(8, e.clientX - width / 2), window.innerWidth - width - 8))
  hoverCard.value = { seg, top, left }
}
function resolveFromCard(action: 'accept' | 'reject') {
  const seg = hoverCard.value?.seg
  hoverCard.value = null
  cancelCardHide()
  if (seg) _resolve(seg, action)
}
onBeforeUnmount(() => cancelCardHide())

const enrichedHunks = computed(() => suggestions.value.flatMap((s: any) =>
  (s.hunks || []).map((h: any) => ({
    ...h,
    build_id: s.build_id,
    source: s.source,
    created_by: s.created_by,
    created_at: s.created_at,
    evidence: s.evidence,
    rank: s.build_number ?? 0,
  })),
))

function rangesOverlap(a: any, b: any): boolean {
  const as = Number(a.start), ae = Math.max(Number(a.end), as)
  const bs = Number(b.start), be = Math.max(Number(b.end), bs)
  if (as === ae && bs === be) return as === bs
  if (as === ae) return as > bs && as < be
  if (bs === be) return bs > as && bs < ae
  return as < be && ae > bs
}

// These are the exact server actions represented on screen. Newer suggestions
// win visual overlap; hidden overlaps are not counted and can never be included
// by Accept/Reject all.
const actionableHunks = computed(() => {
  const kept: any[] = []
  for (const h of [...enrichedHunks.value].sort((a, b) => (b.rank - a.rank) || (a.start - b.start))) {
    if (kept.some(existing => rangesOverlap(h, existing))) continue
    kept.push(h)
  }
  return kept.sort((a, b) => a.start - b.start)
})

const totalHunks = computed(() => actionableHunks.value.length)

// Mirror what the header shows to a `hide-header` host, so it can render the
// count and drive Accept/Reject all (via the exposed `resolveAll`) from its own
// status bar without reaching into this component's internals.
watch([totalHunks, busy], ([total, b]) => emit('state', { total, busy: b }), { immediate: true })

// Suggestion creation time rendered in the ORG timezone (Settings → General),
// falling back to the viewer's browser timezone when unset — via useFormatDate.
const _df = useFormatDate()
const fmtWhen = (s?: string) => { if (!s) return ''; try { return _df.format(s, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) } catch { return s } }

// Split text into plain runs + @mention chips so references render like the
// normal instruction view. Handles both `@name` / `@"label"` and the TipTap
// `<span data-type="mention" label="x">` HTML form. Multi-word names are
// resolved against the instruction's own reference names (see utils/mentions.ts).
const mentionMatcher = shallowRef<MentionMatcher>(EMPTY_MENTION_MATCHER)

function mentionParts(text: string): Array<{ t?: string; mention?: string }> {
  const norm = (text || '')
    .replace(/<span[^>]*data-type=["']mention["'][^>]*label=["']([^"']+)["'][^>]*>\s*<\/span>/g, '@$1')
    .replace(/<span[^>]*data-type=["']mention["'][^>]*>([^<]*)<\/span>/g, '@$1')
  if (!norm.includes('@')) return [{ t: norm }]
  return parseMentionSegments(norm, mentionMatcher.value).map(seg =>
    seg.type === 'mention' ? { mention: seg.label } : { t: seg.value }
  )
}

// Interleave every suggestion's hunks (server-positioned by char offset) onto the
// live text. On overlap, the newest suggestion wins (build_number rank).
const segments = computed(() => {
  const cur = mainText.value || ''
  const segs: any[] = []
  let cursor = 0
  for (const h of actionableHunks.value) {
    if (h.start < cursor) continue
    if (h.start > cursor) segs.push({ kind: 'context', text: cur.slice(cursor, h.start) })
    segs.push({ kind: 'hunk', ...h })
    cursor = Math.max(cursor, h.end)
  }
  if (cursor < cur.length) segs.push({ kind: 'context', text: cur.slice(cursor) })
  return segs
})

// Collapse mode: hide unchanged regions, keeping ~2 lines of context on each
// side of a change. Far-from-change context becomes a clickable "N unchanged
// lines" gap. Expand-all reveals everything.
const expandedAll = ref(false)
const CONTEXT_LINES = 2
const displaySegments = computed(() => {
  if (!props.collapseContext || expandedAll.value) return segments.value
  const segs = segments.value
  const out: any[] = []
  for (let i = 0; i < segs.length; i++) {
    const s = segs[i]
    if (s.kind !== 'context') { out.push(s); continue }
    const hasPrev = i > 0                    // a hunk precedes this context
    const hasNext = i < segs.length - 1      // a hunk follows it
    const lines = (s.text || '').split('\n')
    const keepHead = hasPrev ? CONTEXT_LINES : 0
    const keepTail = hasNext ? CONTEXT_LINES : 0
    const hidden = lines.length - keepHead - keepTail
    if (hidden <= 1) { out.push(s); continue }   // nothing meaningful to hide
    if (keepHead) out.push({ kind: 'context', text: lines.slice(0, keepHead).join('\n') + '\n' })
    out.push({ kind: 'gap', lines: hidden })
    if (keepTail) out.push({ kind: 'context', text: '\n' + lines.slice(lines.length - keepTail).join('\n') })
  }
  return out
})

watch([displaySegments, expandedAll], () => { nextTick(scheduleMarkerLayout) }, { flush: 'post' })
watch(contentEl, (next, previous) => {
  if (previous) markerResizeObserver?.unobserve(previous)
  if (next) markerResizeObserver?.observe(next)
  nextTick(scheduleMarkerLayout)
})

// `silent` swaps the hunk data in place WITHOUT flipping `loading` (which would
// blank the pane to a "Loading…" placeholder). Used after accept/reject so the
// view updates the resolved hunk away smoothly instead of flickering.
async function load(opts: { silent?: boolean } = {}) {
  if (!props.instructionId) return
  if (!opts.silent) loading.value = true
  try {
    const { data, error } = await useMyFetch<any>(`/api/instructions/${props.instructionId}/review-hunks`, { method: 'GET' })
    // 404 (instruction deleted / not visible) and any other failure land here.
    // Keep whatever is on screen and surface the error instead of collapsing
    // to an authoritative-looking "all resolved".
    if (error.value || !data.value) {
      loadError.value = true
    } else {
      loadError.value = false
      const d = data.value
      mainText.value = d.main_text || ''
      mainBuildId.value = d.main_build_id || null
      mainVersionId.value = d.main_version_id || null
      const all = d.suggestions || []
      // Scoped mount: show only this suggestion, so what is displayed and what
      // the resolve buttons act on are the same set.
      suggestions.value = props.buildId
        ? all.filter((s: any) => String(s.build_id) === String(props.buildId))
        : all
      mentionMatcher.value = buildMentionMatcher((d.reference_names || []).map((name: string) => ({ name })))
    }
  } finally { if (!opts.silent) loading.value = false }
  // Only a real "server returned no hunks" is emptiness. Emitting `empty` after
  // a failed fetch made hosts resolve the change away on nothing more than a
  // dropped request — KnowledgeGroup marks it rejected, and the chat's
  // EditInstructionTool marks it ACCEPTED.
  if (loadError.value) emit('error')
  else if (!totalHunks.value) emit('empty')
  // Fires when the pane has real content (or knows it has none) — hosts use
  // it to keep their previous stable view up until this exact moment instead
  // of showing this component's own "Loading…" placeholder.
  emit('loaded')
}
defineExpose({ reload: (opts?: { silent?: boolean }) => load(opts), resolveAll: (mode: 'accept' | 'reject') => resolveAll(mode) })

async function _resolve(seg: any, action: 'accept' | 'reject') {
  const top = scrollEl.value?.scrollTop ?? 0
  resolving.value = seg.key
  try {
    const url = `/api/instructions/${props.instructionId}/hunks/${action}`
    const body = {
      build_id: seg.build_id,
      hunk_key: seg.key,
      against_main_build_id: mainBuildId.value,
      against_main_version_id: mainVersionId.value,
    }
    const { error } = await useMyFetch(url, { method: 'POST', body })
    if (error.value) throw new Error((error.value as any)?.data?.detail || 'Failed')
    // Tell the host BEFORE reloading ourselves — see resolveAll.
    emit('changed')
    await load({ silent: true })
    await nextTick()
    if (scrollEl.value) scrollEl.value.scrollTop = top
  } catch (e: any) {
    useToast?.().add?.({ title: 'Couldn’t apply change', description: e?.message, color: 'red' })
  } finally { resolving.value = null }
}
async function resolveAll(mode: 'accept' | 'reject') {
  if (busy.value) return
  busy.value = true
  const top = scrollEl.value?.scrollTop ?? 0
  try {
    // Submit the exact action units represented on screen. The server validates
    // every pair against this main version and never discovers extra pending
    // builds during the mutation.
    const url = `/api/instructions/${props.instructionId}/hunks/${mode}-all`
    const body = {
      against_main_build_id: mainBuildId.value,
      against_main_version_id: mainVersionId.value,
      hunks: actionableHunks.value.map(h => ({ build_id: h.build_id, hunk_key: h.key })),
    }
    const { error } = await useMyFetch(url, { method: 'POST', body })
    if (error.value) throw new Error((error.value as any)?.data?.detail || 'Failed')
    // Tell the host the resolve landed BEFORE reloading ourselves. Reloading
    // first emits `empty`/`error` on the way, and every host answers those by
    // dropping this pane — so by the time `changed` was reached it was being
    // emitted from a component the host had already torn down, and the host's
    // own refresh never ran. It showed a stale copy of the instruction, with
    // its "Pending review" badge, until the page was reloaded by hand.
    // Rejecting a create suggestion makes that certain rather than likely: the
    // instruction is retired, so the reload below is guaranteed to 404.
    emit('changed')
    await load({ silent: true })
    await nextTick()
    if (scrollEl.value) scrollEl.value.scrollTop = top
  } catch (e: any) {
    useToast?.().add?.({ title: `Couldn’t ${mode} all`, description: e?.message, color: 'red' })
  } finally { busy.value = false }
}

// Switching to a different instruction shows the loading state; resolves reload
// silently (see `load`). Wrap so the watcher's args aren't passed as `opts`.
watch(() => [props.instructionId, props.buildId], () => load())
onMounted(() => {
  markerResizeObserver = new ResizeObserver(scheduleMarkerLayout)
  if (scrollEl.value) markerResizeObserver.observe(scrollEl.value)
  if (contentEl.value) markerResizeObserver.observe(contentEl.value)
  window.addEventListener('resize', scheduleMarkerLayout)
  load()
})
onBeforeUnmount(() => {
  markerResizeObserver?.disconnect()
  window.removeEventListener('resize', scheduleMarkerLayout)
  if (markerFrame !== null) cancelAnimationFrame(markerFrame)
})
</script>

<style scoped>
.instr-mention {
  background-color: rgba(99, 102, 241, 0.12);
  color: #4338ca;
  border-radius: 4px;
  padding: 1px 4px;
  font-weight: 500;
  font-size: 0.95em;
  white-space: nowrap;
}
</style>
