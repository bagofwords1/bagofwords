<template>
  <div class="flex flex-col h-full min-h-0">
    <!-- Header -->
    <div class="shrink-0 px-6 pt-4 pb-3 border-b border-gray-100">
      <div class="flex items-start justify-between gap-3">
        <div class="min-w-0">
          <h2 class="text-base font-semibold text-gray-900 flex items-center gap-2">
            Review
            <span v-if="unread" class="inline-flex items-center justify-center min-w-[18px] h-[18px] px-1 rounded-full bg-gray-900 text-white text-[10px] font-semibold tabular-nums">{{ unread }}</span>
          </h2>
          <p class="mt-0.5 text-sm text-gray-500">Suggestions, schema changes and quality signals that need a decision.</p>
        </div>
        <div class="flex items-center gap-1.5 shrink-0">
          <button v-if="unread" class="h-7 px-2.5 rounded-md text-xs font-medium text-gray-500 hover:bg-gray-100" @click="markAllRead">Mark all read</button>
          <button class="h-7 w-7 rounded-md flex items-center justify-center text-gray-400 hover:bg-gray-100" title="Refresh" @click="refresh"><UIcon name="i-heroicons-arrow-path" :class="['w-4 h-4', { 'animate-spin': loading }]" /></button>
          <button class="h-7 w-7 rounded-md flex items-center justify-center text-gray-400 hover:bg-gray-100" title="Close" @click="$emit('close')"><UIcon name="i-heroicons-x-mark" class="w-4 h-4" /></button>
        </div>
      </div>
      <!-- Filters -->
      <div class="mt-3 flex items-center gap-2 flex-wrap">
        <div class="relative">
          <UIcon name="i-heroicons-magnifying-glass" class="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-400" />
          <input v-model="search" type="text" placeholder="Search…" class="h-8 w-48 pl-8 pr-2 text-[13px] bg-gray-50 border border-gray-200 rounded-md outline-none focus:border-gray-400 focus:bg-white placeholder:text-gray-400" />
        </div>
        <!-- Agent filter -->
        <UPopover :popper="{ placement: 'bottom-start' }" :ui="{ ring: '', shadow: 'shadow-md' }">
          <button type="button" class="inline-flex items-center gap-1.5 h-8 px-2.5 rounded-md border border-gray-200 text-[13px] text-gray-700 hover:bg-gray-50">
            <DataSourceIcon v-if="agentFilter" :type="agentTypeOf(agentFilter)" class="w-3.5 h-3.5" />
            <UIcon v-else name="i-heroicons-cube" class="w-3.5 h-3.5 text-gray-400" />
            {{ agentFilter ? agentNameOf(agentFilter) : 'All agents' }}
            <UIcon name="i-heroicons-chevron-down" class="w-3 h-3 opacity-60" />
          </button>
          <template #panel="{ close }">
            <div class="p-1 w-56 max-h-72 overflow-auto">
              <button class="w-full flex items-center gap-2 px-2 py-1.5 rounded hover:bg-gray-50 text-left text-[13px]" @click="agentFilter = null; close()"><UIcon name="i-heroicons-cube" class="w-4 h-4 text-gray-400" />All agents<UIcon v-if="!agentFilter" name="i-heroicons-check" class="w-3.5 h-3.5 ml-auto text-gray-900" /></button>
              <button v-for="a in agents" :key="a.id" class="w-full flex items-center gap-2 px-2 py-1.5 rounded hover:bg-gray-50 text-left text-[13px]" @click="agentFilter = a.id; close()"><DataSourceIcon :type="a.type" class="w-4 h-4" /><span class="truncate">{{ a.name }}</span><UIcon v-if="agentFilter === a.id" name="i-heroicons-check" class="w-3.5 h-3.5 ml-auto text-gray-900 shrink-0" /></button>
            </div>
          </template>
        </UPopover>
        <!-- Type filter chips -->
        <button v-for="t in typeChips" :key="t.value" class="inline-flex items-center gap-1 h-8 px-2.5 rounded-md border text-[12px] font-medium transition-colors"
                :class="typeFilter === t.value ? 'border-gray-900 bg-gray-900 text-white' : 'border-gray-200 text-gray-600 hover:bg-gray-50'"
                @click="typeFilter = (typeFilter === t.value ? null : t.value)">
          <UIcon :name="t.icon" class="w-3.5 h-3.5" />{{ t.label }}
        </button>
        <span class="flex-1"></span>
        <button class="inline-flex items-center gap-1 h-8 px-2.5 rounded-md text-[12px] font-medium transition-colors" :class="showResolved ? 'text-gray-700 bg-gray-100' : 'text-gray-400 hover:bg-gray-50'" @click="showResolved = !showResolved">
          <UIcon name="i-heroicons-check-circle" class="w-3.5 h-3.5" />Resolved
        </button>
      </div>
    </div>

    <!-- List -->
    <div class="flex-1 min-h-0 overflow-y-auto">
      <div v-if="loading && !items.length" class="flex items-center justify-center py-20 text-gray-400"><UIcon name="i-heroicons-arrow-path" class="w-5 h-5 animate-spin" /></div>
      <div v-else-if="!filtered.length" class="flex flex-col items-center justify-center py-24 text-center px-6">
        <div class="w-12 h-12 rounded-xl bg-gray-50 ring-1 ring-gray-200/70 flex items-center justify-center"><UIcon name="i-heroicons-check-circle" class="w-6 h-6 text-gray-300" /></div>
        <p class="mt-3 text-sm font-medium text-gray-700">You're all caught up</p>
        <p class="mt-1 text-xs text-gray-400 max-w-xs">Nothing needs review{{ agentFilter ? ' for this agent' : '' }}. New suggestions, schema changes and quality signals will land here.</p>
      </div>
      <ul v-else class="divide-y divide-gray-100">
        <li v-for="it in filtered" :key="it.id"
            class="group relative px-6 py-3.5 transition-colors hover:bg-gray-50/70"
            :class="!it.read && it.status !== 'resolved' ? 'bg-white' : ''">
          <!-- severity accent -->
          <span class="absolute left-0 top-0 bottom-0 w-0.5" :class="accentClass(it)"></span>
          <div class="flex items-start gap-3">
            <div class="mt-0.5 w-7 h-7 rounded-lg flex items-center justify-center shrink-0" :class="iconWrapClass(it)">
              <UIcon :name="typeMeta(it.type).icon" class="w-4 h-4" />
            </div>
            <div class="min-w-0 flex-1">
              <div class="flex items-center gap-2 min-w-0">
                <span v-if="!it.read && it.status !== 'resolved'" class="w-1.5 h-1.5 rounded-full bg-blue-500 shrink-0"></span>
                <span class="text-[13px] font-medium text-gray-900 truncate">{{ it.title }}</span>
                <span v-if="it.group_count > 1" class="text-[10px] font-semibold px-1.5 rounded-full bg-gray-100 text-gray-600 shrink-0 tabular-nums">×{{ it.group_count }}</span>
                <span v-if="it.status === 'in_progress'" class="inline-flex items-center gap-1 text-[10px] text-blue-600 shrink-0"><UIcon name="i-heroicons-arrow-path" class="w-3 h-3 animate-spin" />Running</span>
                <span v-else-if="it.status === 'resolved'" class="inline-flex items-center gap-0.5 text-[10px] text-green-600 shrink-0"><UIcon name="i-heroicons-check" class="w-3 h-3" />Resolved</span>
              </div>
              <p v-if="it.why" class="mt-0.5 text-[12px] text-gray-500 line-clamp-2">{{ it.why }}</p>
              <div class="mt-1.5 flex items-center gap-2 text-[11px] text-gray-400">
                <span class="inline-flex items-center gap-1">
                  <DataSourceIcon v-if="it.agent_id" :type="agentTypeOf(it.agent_id)" class="w-3 h-3" />
                  <UIcon v-else name="i-heroicons-globe-alt" class="w-3 h-3" />
                  {{ it.agent_id ? agentNameOf(it.agent_id) : 'All agents' }}
                </span>
                <span>·</span>
                <span>{{ typeMeta(it.type).label }}</span>
                <span>·</span>
                <span>{{ fmtDate(it.last_seen_at || it.created_at) }}</span>
              </div>
            </div>
            <!-- actions -->
            <div class="flex items-center gap-1 shrink-0 self-center" @click.stop>
              <template v-if="it.status === 'open' || it.status === 'snoozed'">
                <button v-for="a in primaryActions(it)" :key="a.id"
                        class="h-7 px-2.5 rounded-md text-[12px] font-medium inline-flex items-center gap-1 transition-colors disabled:opacity-40"
                        :class="a.primary ? 'bg-gray-50 hover:bg-gray-100 border border-gray-150 text-gray-700' : 'text-gray-500 hover:bg-gray-100'"
                        :disabled="busy === it.id"
                        @click="runAction(it, a)">
                  <UIcon v-if="busy === it.id && a.primary" name="i-heroicons-arrow-path" class="w-3.5 h-3.5 animate-spin" />
                  <UIcon v-else-if="a.id === 'run_eval'" name="i-heroicons-beaker" class="w-3.5 h-3.5 text-gray-400" />
                  <UIcon v-else-if="a.id === 'run_training'" name="i-heroicons-academic-cap" class="w-3.5 h-3.5 text-gray-400" />
                  <UIcon v-else-if="a.id === 'review'" name="i-heroicons-arrow-right" class="w-3.5 h-3.5 text-gray-400" />
                  {{ a.label }}
                </button>
              </template>
              <button v-if="it.status !== 'resolved' && it.status !== 'dismissed'" class="h-7 w-7 rounded-md flex items-center justify-center text-gray-300 hover:text-gray-600 hover:bg-gray-100 opacity-0 group-hover:opacity-100" :title="it.read ? 'Mark unread' : 'Mark read'" @click="toggleRead(it)"><UIcon :name="it.read ? 'i-heroicons-envelope' : 'i-heroicons-envelope-open'" class="w-3.5 h-3.5" /></button>
              <button v-if="it.status === 'open' || it.status === 'snoozed'" class="h-7 w-7 rounded-md flex items-center justify-center text-gray-300 hover:text-red-500 hover:bg-gray-100 opacity-0 group-hover:opacity-100" title="Dismiss" @click="dismiss(it)"><UIcon name="i-heroicons-x-mark" class="w-4 h-4" /></button>
            </div>
          </div>
        </li>
      </ul>
    </div>
  </div>
</template>

<script setup lang="ts">
import DataSourceIcon from '~/components/DataSourceIcon.vue'

const props = defineProps<{ agents: any[]; initialAgentId?: string | null }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'open-instruction', p: { instructionId: string; buildId?: string }): void; (e: 'count', n: number): void }>()

const toast = useToast()
const items = ref<any[]>([])
const loading = ref(false)
const busy = ref<string | null>(null)
const unread = ref(0)
const search = ref('')
const agentFilter = ref<string | null>(props.initialAgentId || null)
const typeFilter = ref<string | null>(null)
const showResolved = ref(false)

const typeChips = [
  { value: 'instruction_suggestion', label: 'Suggestions', icon: 'i-heroicons-document-text' },
  { value: 'schema_changed', label: 'Schema', icon: 'i-heroicons-table-cells' },
  { value: 'slow_query', label: 'Slow', icon: 'i-heroicons-clock' },
  { value: 'low_confidence', label: 'Low confidence', icon: 'i-heroicons-exclamation-triangle' },
]
const TYPE_META: Record<string, { label: string; icon: string; tint: string }> = {
  instruction_suggestion: { label: 'Suggestion', icon: 'i-heroicons-document-text', tint: 'blue' },
  schema_changed: { label: 'Schema change', icon: 'i-heroicons-table-cells', tint: 'amber' },
  slow_query: { label: 'Slow query', icon: 'i-heroicons-clock', tint: 'amber' },
  low_confidence: { label: 'Low confidence', icon: 'i-heroicons-exclamation-triangle', tint: 'orange' },
  query_error: { label: 'Query error', icon: 'i-heroicons-x-circle', tint: 'red' },
}
const typeMeta = (t: string) => TYPE_META[t] || { label: t, icon: 'i-heroicons-bell', tint: 'gray' }

const agentNameOf = (id: string) => props.agents.find(a => a.id === id)?.name || 'Agent'
const agentTypeOf = (id: string) => props.agents.find(a => a.id === id)?.type

const accentClass = (it: any) => it.severity === 'error' ? 'bg-red-400' : it.severity === 'warning' ? 'bg-amber-400' : 'bg-transparent'
const iconWrapClass = (it: any) => {
  const t = typeMeta(it.type).tint
  return ({
    blue: 'bg-blue-50 text-blue-600', amber: 'bg-amber-50 text-amber-600',
    orange: 'bg-orange-50 text-orange-600', red: 'bg-red-50 text-red-600', gray: 'bg-gray-100 text-gray-500',
  } as any)[t] || 'bg-gray-100 text-gray-500'
}
const primaryActions = (it: any) => (it.actions || []).filter((a: any) => a.id !== 'dismiss')

const filtered = computed(() => {
  let out = items.value
  if (typeFilter.value) out = out.filter(i => i.type === typeFilter.value)
  if (search.value.trim()) {
    const q = search.value.toLowerCase()
    out = out.filter(i => (i.title || '').toLowerCase().includes(q) || (i.why || '').toLowerCase().includes(q))
  }
  return out
})

const fmtDate = (s: string) => {
  if (!s) return ''
  const d = new Date(s); const diff = (Date.now() - d.getTime()) / 1000
  if (diff < 60) return 'just now'
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  if (diff < 604800) return `${Math.floor(diff / 86400)}d ago`
  return d.toLocaleDateString()
}

const fetchItems = async () => {
  loading.value = true
  try {
    const q: any = {}
    if (agentFilter.value) q.agent_id = agentFilter.value
    if (showResolved.value) q.status = 'open,in_progress,snoozed,resolved'
    const { data } = await useMyFetch<any>('/api/review', { method: 'GET', query: q })
    items.value = data.value?.items || []
    unread.value = data.value?.unread || 0
    emit('count', unread.value)
  } catch (e) { /* noop */ } finally { loading.value = false }
}
const refresh = () => fetchItems()

watch([agentFilter, showResolved], fetchItems)

const runAction = async (it: any, a: any) => {
  if (a.id === 'review') {
    const s = it.subject || {}
    if (s.instruction_id) emit('open-instruction', { instructionId: s.instruction_id, buildId: s.build_id })
    return
  }
  busy.value = it.id
  try {
    const { data, error } = await useMyFetch<any>(`/api/review/${it.id}/resolve`, { method: 'POST', body: { action_id: a.id } })
    if (error.value) throw new Error((error.value as any)?.data?.detail || 'Failed')
    const label = a.id === 'run_training' ? 'Training started' : a.id === 'run_eval' ? 'Eval started' : 'Done'
    toast.add({ title: label, color: 'blue' })
    await fetchItems()
  } catch (e: any) { toast.add({ title: 'Action failed', description: e?.message, color: 'red' }) } finally { busy.value = null }
}
const dismiss = async (it: any) => {
  try { await useMyFetch(`/api/review/${it.id}/dismiss`, { method: 'POST' }); await fetchItems() } catch {}
}
const toggleRead = async (it: any) => {
  try { await useMyFetch(`/api/review/${it.id}/read`, { method: 'POST', body: { read: !it.read } }); await fetchItems() } catch {}
}
const markAllRead = async () => {
  try { await useMyFetch('/api/review/read-all', { method: 'POST', body: { agent_id: agentFilter.value } }); await fetchItems() } catch {}
}

onMounted(fetchItems)
defineExpose({ refresh })
</script>
