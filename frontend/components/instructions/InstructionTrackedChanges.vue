<!--
  Per-hunk tracked-changes review (immutable cherry-pick model).
  Server-authoritative: fetches GET /instructions/{id}/review-hunks and renders
  every pending suggestion's hunks interleaved on the live text, Google-Docs
  style. Accept = POST hunks/accept (cherry-pick onto main); Reject = hunks/reject.
  Reused by the Knowledge Explorer review and the Report agent instruction view.
-->
<template>
  <div class="flex-1 flex flex-col min-h-0">
    <div class="px-6 py-3 flex items-center justify-between border-b border-gray-100">
      <div class="flex items-center gap-2 min-w-0">
        <span class="w-1.5 h-1.5 rounded-full bg-amber-500 shrink-0"></span>
        <span class="text-xs font-medium text-gray-700">Pending review</span>
        <span class="text-[11px] text-gray-400 tabular-nums shrink-0">· {{ totalHunks }} change{{ totalHunks === 1 ? '' : 's' }} · {{ suggestions.length }} suggestion{{ suggestions.length === 1 ? '' : 's' }}</span>
      </div>
      <div v-if="canApprove && totalHunks" class="flex items-center gap-1.5 shrink-0">
        <button class="inline-flex items-center gap-1 h-7 px-2.5 rounded-md bg-white border border-gray-200 text-gray-700 text-[11px] font-medium hover:bg-gray-50 disabled:opacity-40 transition-colors" :disabled="busy" @click="resolveAll('reject')"><UIcon name="i-heroicons-x-mark" class="w-3.5 h-3.5 text-gray-400" />Reject all</button>
        <button class="inline-flex items-center gap-1 h-7 px-2.5 rounded-md bg-emerald-50 border border-emerald-200 text-emerald-700 text-[11px] font-medium hover:bg-emerald-100 disabled:opacity-40 transition-colors" :disabled="busy" @click="resolveAll('accept')"><UIcon :name="busy ? 'i-heroicons-arrow-path' : 'i-heroicons-check'" :class="['w-3.5 h-3.5', { 'animate-spin': busy }]" />Accept all</button>
      </div>
    </div>
    <div ref="scrollEl" class="flex-1 min-h-0 overflow-auto px-8 py-6 max-w-3xl">
      <div v-if="loading" class="text-center text-xs text-gray-400 py-10">Loading…</div>
      <div v-else-if="!totalHunks" class="text-center text-xs text-gray-400 py-10">No pending changes — all suggestions resolved.</div>
      <div v-else class="text-[13px] leading-[1.6] whitespace-pre-wrap break-words text-gray-800">
        <template v-for="(seg, si) in segments" :key="si">
          <span v-if="seg.kind === 'context'">{{ seg.text }}</span>
          <span v-else :id="`htc-${seg.key}`" class="group/h relative inline align-baseline rounded-[3px] transition-colors"
                :class="resolving === seg.key ? 'bg-amber-100' : 'hover:bg-amber-50'">
            <del v-if="seg.before" class="text-rose-500/70 line-through decoration-rose-300 decoration-1">{{ seg.before }}</del>
            <ins v-if="seg.after" class="text-emerald-700 underline decoration-dotted decoration-emerald-400/70 underline-offset-[3px] decoration-1">{{ seg.after }}</ins>
            <span v-if="resolving === seg.key" class="absolute inset-0 rounded bg-white/50 flex items-center justify-center"><UIcon name="i-heroicons-arrow-path" class="w-3.5 h-3.5 text-gray-500 animate-spin" /></span>
            <span v-if="canApprove" class="invisible opacity-0 group-hover/h:visible group-hover/h:opacity-100 transition-opacity absolute z-30 top-0 left-0 pt-[1.7em] cursor-default select-none whitespace-normal" @click.stop>
              <span class="block w-max max-w-xs rounded-lg bg-white shadow-md ring-1 ring-gray-200/70 p-2">
                <span class="flex items-center gap-1.5 mb-1.5">
                  <span class="w-1.5 h-1.5 rounded-full shrink-0" :class="seg.source === 'ai' ? 'bg-violet-500' : 'bg-blue-500'"></span>
                  <span class="text-[10px] text-gray-500 truncate">{{ seg.source === 'ai' ? 'AI suggestion' : 'Proposed' }}<template v-if="seg.created_by"> · {{ seg.created_by.name }}</template></span>
                </span>
                <span class="flex items-center gap-1.5">
                  <button class="inline-flex items-center gap-1 h-7 px-2.5 rounded-md bg-emerald-50 border border-emerald-200 text-emerald-700 text-[11px] font-medium hover:bg-emerald-100 disabled:opacity-40 transition-colors" :disabled="busy" @click.stop="accept(seg)"><UIcon name="i-heroicons-check" class="w-3.5 h-3.5" />Accept</button>
                  <button class="inline-flex items-center gap-1 h-7 px-2.5 rounded-md bg-white border border-gray-200 text-gray-700 text-[11px] font-medium hover:bg-gray-50 disabled:opacity-40 transition-colors" :disabled="busy" @click.stop="reject(seg)"><UIcon name="i-heroicons-x-mark" class="w-3.5 h-3.5 text-gray-400" />Reject</button>
                </span>
              </span>
            </span>
          </span>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'

const props = defineProps<{ instructionId: string; canApprove?: boolean }>()
const emit = defineEmits<{ (e: 'changed'): void; (e: 'empty'): void }>()

const loading = ref(false)
const busy = ref(false)
const resolving = ref<string | null>(null)
const mainText = ref('')
const mainVersionId = ref<string | null>(null)
const suggestions = ref<any[]>([])
const scrollEl = ref<HTMLElement | null>(null)

const totalHunks = computed(() => suggestions.value.reduce((n, s) => n + s.hunks.length, 0))

// Interleave every suggestion's hunks (server-positioned by char offset) onto the
// live text. On overlap, the newest suggestion wins (build_number rank).
const segments = computed(() => {
  const cur = mainText.value || ''
  const all: any[] = []
  for (const s of suggestions.value) {
    for (const h of s.hunks) {
      all.push({ ...h, build_id: s.build_id, source: s.source, created_by: s.created_by, rank: s.build_number ?? 0 })
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

async function load() {
  if (!props.instructionId) return
  loading.value = true
  try {
    const { data } = await useMyFetch<any>(`/api/instructions/${props.instructionId}/review-hunks`, { method: 'GET' })
    const d = data.value || {}
    mainText.value = d.main_text || ''
    mainVersionId.value = d.main_version_id || null
    suggestions.value = d.suggestions || []
  } finally { loading.value = false }
  if (!totalHunks.value) emit('empty')
}
defineExpose({ reload: load })

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
    await load()
    emit('changed')
    await nextTick()
    if (scrollEl.value) scrollEl.value.scrollTop = top
  } catch (e: any) {
    useToast?.().add?.({ title: 'Couldn’t apply change', description: e?.message, color: 'red' })
  } finally { resolving.value = null }
}
const accept = (seg: any) => _resolve(seg, 'accept')
const reject = (seg: any) => _resolve(seg, 'reject')

async function resolveAll(mode: 'accept' | 'reject') {
  if (busy.value) return
  busy.value = true
  try {
    let guard = 0
    while (totalHunks.value && guard++ < 2000) {
      const s = suggestions.value.find(x => x.hunks.length)
      if (!s) break
      await _resolve({ ...s.hunks[0], build_id: s.build_id }, mode)
    }
  } finally { busy.value = false }
}

watch(() => props.instructionId, load)
onMounted(load)
</script>
