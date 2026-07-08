<!--
  Per-hunk tracked-changes review (immutable cherry-pick model).
  Server-authoritative: fetches GET /instructions/{id}/review-hunks and renders
  every pending suggestion's hunks interleaved on the live text, Google-Docs
  style. Accept = POST hunks/accept (cherry-pick onto main); Reject = hunks/reject.
  Reused by the Knowledge Explorer review and the Report agent instruction view.
-->
<template>
  <div class="flex flex-col min-h-0" :class="compact ? '' : 'flex-1'">
    <div class="flex items-center justify-between border-b border-gray-100 dark:border-gray-800" :class="compact ? 'px-3 py-1.5' : 'px-6 py-3'">
      <div class="flex items-center gap-2 min-w-0">
        <span class="w-1.5 h-1.5 rounded-full bg-amber-500 shrink-0"></span>
        <span class="font-medium text-gray-700 dark:text-gray-300" :class="compact ? 'text-[11px]' : 'text-xs'">Pending review</span>
        <span class="text-[11px] text-gray-400 dark:text-gray-500 tabular-nums shrink-0">· {{ totalHunks }} change{{ totalHunks === 1 ? '' : 's' }}</span>
        <button v-if="collapseContext && totalHunks" class="text-[10px] text-gray-400 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 shrink-0" @click="expandedAll = !expandedAll">{{ expandedAll ? 'Collapse' : 'Expand all' }}</button>
      </div>
      <div v-if="canApprove && totalHunks" class="flex items-center gap-1.5 shrink-0">
        <button class="inline-flex items-center gap-1 h-7 px-2.5 rounded-md bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-800 text-gray-700 dark:text-gray-300 text-[11px] font-medium hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-40 transition-colors" :disabled="busy" @click="resolveAll('reject')"><UIcon name="i-heroicons-x-mark" class="w-3.5 h-3.5 text-gray-400 dark:text-gray-500" />Reject all</button>
        <button class="inline-flex items-center gap-1 h-7 px-2.5 rounded-md bg-emerald-50 dark:bg-emerald-500/10 border border-emerald-200 dark:border-emerald-500/30 text-emerald-700 dark:text-emerald-400 text-[11px] font-medium hover:bg-emerald-100 dark:hover:bg-emerald-500/20 disabled:opacity-40 transition-colors" :disabled="busy" @click="resolveAll('accept')"><UIcon :name="busy ? 'i-heroicons-arrow-path' : 'i-heroicons-check'" :class="['w-3.5 h-3.5', { 'animate-spin': busy }]" />Accept all</button>
      </div>
    </div>
    <div ref="scrollEl" class="min-h-0 overflow-auto" :class="compact ? 'px-3 py-2 max-h-80' : 'flex-1 px-8 py-6 max-w-3xl'" @scroll.passive="hoverCard = null">
      <div v-if="loading" class="text-center text-xs text-gray-400 dark:text-gray-500 py-10">Loading…</div>
      <div v-else-if="!totalHunks" class="text-center text-xs text-gray-400 dark:text-gray-500 py-6">No pending changes — all resolved.</div>
      <!-- dir=auto + plaintext: per-line bidi, same policy as the read view
           (KnowledgeExplorer), so Hebrew prose lays out RTL while code lines stay LTR. -->
      <div v-else dir="auto" style="unicode-bidi: plaintext; text-align: start;" class="whitespace-pre-wrap break-words text-gray-800 dark:text-gray-200" :class="compact ? 'text-[12px] leading-[1.55]' : 'text-[13px] leading-[1.6]'">
        <template v-for="(seg, si) in displaySegments" :key="si">
          <span v-if="seg.kind === 'gap'" class="block my-1 text-[10px] text-gray-400 dark:text-gray-500 hover:text-gray-600 dark:hover:text-gray-400 cursor-pointer select-none" @click="expandedAll = true">··· {{ seg.lines }} unchanged line{{ seg.lines === 1 ? '' : 's' }} ···</span>
          <template v-else-if="seg.kind === 'context'">
            <template v-for="(pt, pi) in mentionParts(seg.text)" :key="pi"><span v-if="pt.mention" class="instr-mention">@{{ pt.mention }}</span><template v-else>{{ pt.t }}</template></template>
          </template>
          <span v-else :id="`htc-${seg.key}`" class="relative inline align-baseline rounded-[3px] transition-colors"
                :class="resolving === seg.key ? 'bg-amber-100 dark:bg-amber-500/20' : 'hover:bg-amber-50 dark:hover:bg-amber-500/10'"
                @mousemove="onHunkMove(seg, $event)" @mouseleave="scheduleCardHide()">
            <del v-if="seg.before" class="text-rose-500/70 line-through decoration-rose-300 decoration-1"><template v-for="(pt, pi) in mentionParts(seg.before)" :key="pi"><span v-if="pt.mention" class="instr-mention">@{{ pt.mention }}</span><template v-else>{{ pt.t }}</template></template></del>
            <ins v-if="seg.after" class="text-emerald-700 underline decoration-dotted decoration-emerald-400/70 underline-offset-[3px] decoration-1"><template v-for="(pt, pi) in mentionParts(seg.after)" :key="pi"><span v-if="pt.mention" class="instr-mention">@{{ pt.mention }}</span><template v-else>{{ pt.t }}</template></template></ins>
            <span v-if="resolving === seg.key" class="absolute inset-0 rounded bg-white/50 dark:bg-gray-900/50 flex items-center justify-center"><UIcon name="i-heroicons-arrow-path" class="w-3.5 h-3.5 text-gray-500 dark:text-gray-400 animate-spin" /></span>
          </span>
        </template>
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
            <span class="text-[10px] text-gray-500 dark:text-gray-400 truncate">{{ hoverCard.seg.source === 'ai' ? 'AI suggestion' : 'Proposed' }}<template v-if="hoverCard.seg.created_by"> · {{ hoverCard.seg.created_by.name }}</template></span>
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
import { ref, computed, watch, onMounted, onBeforeUnmount } from 'vue'

const props = defineProps<{ instructionId: string; canApprove?: boolean; compact?: boolean; collapseContext?: boolean }>()
const emit = defineEmits<{ (e: 'changed'): void; (e: 'empty'): void }>()

const loading = ref(false)
const busy = ref(false)
const resolving = ref<string | null>(null)
const mainText = ref('')
const mainVersionId = ref<string | null>(null)
const suggestions = ref<any[]>([])
const scrollEl = ref<HTMLElement | null>(null)

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

const totalHunks = computed(() => suggestions.value.reduce((n, s) => n + s.hunks.length, 0))

// Split text into plain runs + @mention chips so references render like the
// normal instruction view. Handles both `@name` / `@"label"` and the TipTap
// `<span data-type="mention" label="x">` HTML form.
const MENTION_RE = /@([A-Za-z_][A-Za-z0-9_]*(?:[.\-][A-Za-z0-9_]+)*|"[^"]+")/g
function mentionParts(text: string): Array<{ t?: string; mention?: string }> {
  const norm = (text || '')
    .replace(/<span[^>]*data-type=["']mention["'][^>]*label=["']([^"']+)["'][^>]*>\s*<\/span>/g, '@$1')
    .replace(/<span[^>]*data-type=["']mention["'][^>]*>([^<]*)<\/span>/g, '@$1')
  if (!norm.includes('@')) return [{ t: norm }]
  const parts: Array<{ t?: string; mention?: string }> = []
  let last = 0, m: RegExpExecArray | null
  MENTION_RE.lastIndex = 0
  while ((m = MENTION_RE.exec(norm))) {
    if (m.index > last) parts.push({ t: norm.slice(last, m.index) })
    let label = m[1]
    if (label.startsWith('"')) label = label.slice(1, -1)
    parts.push({ mention: label })
    last = MENTION_RE.lastIndex
  }
  if (last < norm.length) parts.push({ t: norm.slice(last) })
  return parts
}

// Interleave every suggestion's hunks (server-positioned by char offset) onto the
// live text. On overlap, the newest suggestion wins (build_number rank).
const segments = computed(() => {
  const cur = mainText.value || ''
  const all: any[] = []
  for (const s of suggestions.value) {
    for (const h of s.hunks) {
      all.push({ ...h, build_id: s.build_id, source: s.source, created_by: s.created_by, evidence: s.evidence, rank: s.build_number ?? 0 })
    }
  }
  const claimed: [number, number][] = []
  const kept: any[] = []
  for (const h of [...all].sort((a, b) => (b.rank - a.rank) || (a.start - b.start))) {
    const s = h.start, e = Math.max(h.end, h.start), point = e === s
    const clash = claimed.some(([cs, ce]) => point ? (s > cs && s < ce) : (s < ce && e > cs))
    if (clash) continue
    claimed.push([s, e]); kept.push(h)
  }
  kept.sort((a, b) => a.start - b.start || 0)
  const segs: any[] = []
  let cursor = 0
  for (const h of kept) {
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

// `silent` swaps the hunk data in place WITHOUT flipping `loading` (which would
// blank the pane to a "Loading…" placeholder). Used after accept/reject so the
// view updates the resolved hunk away smoothly instead of flickering.
async function load(opts: { silent?: boolean } = {}) {
  if (!props.instructionId) return
  if (!opts.silent) loading.value = true
  try {
    const { data } = await useMyFetch<any>(`/api/instructions/${props.instructionId}/review-hunks`, { method: 'GET' })
    const d = data.value || {}
    mainText.value = d.main_text || ''
    mainVersionId.value = d.main_version_id || null
    suggestions.value = d.suggestions || []
  } finally { if (!opts.silent) loading.value = false }
  if (!totalHunks.value) emit('empty')
}
defineExpose({ reload: () => load() })

async function _resolve(seg: any, action: 'accept' | 'reject') {
  const top = scrollEl.value?.scrollTop ?? 0
  resolving.value = seg.key
  try {
    const url = `/api/instructions/${props.instructionId}/hunks/${action}`
    const body: any = action === 'accept'
      ? { build_id: seg.build_id, hunk_key: seg.key, against_main_version_id: mainVersionId.value }
      : { build_id: seg.build_id, hunk_key: seg.key }
    const { error } = await useMyFetch(url, { method: 'POST', body })
    if (error.value) throw new Error((error.value as any)?.data?.detail || 'Failed')
    await load({ silent: true })
    emit('changed')
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
    // One server-side pass (one build for accept-all) — no per-hunk reload churn.
    const url = `/api/instructions/${props.instructionId}/hunks/${mode}-all`
    const body = mode === 'accept' ? { against_main_version_id: mainVersionId.value } : {}
    const { error } = await useMyFetch(url, { method: 'POST', body })
    if (error.value) throw new Error((error.value as any)?.data?.detail || 'Failed')
    await load({ silent: true })
    emit('changed')
    await nextTick()
    if (scrollEl.value) scrollEl.value.scrollTop = top
  } catch (e: any) {
    useToast?.().add?.({ title: `Couldn’t ${mode} all`, description: e?.message, color: 'red' })
  } finally { busy.value = false }
}

// Switching to a different instruction shows the loading state; resolves reload
// silently (see `load`). Wrap so the watcher's args aren't passed as `opts`.
watch(() => props.instructionId, () => load())
onMounted(() => load())
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
