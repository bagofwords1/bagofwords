<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-5xl' }">
    <UCard :ui="{ body: { padding: '' }, header: { padding: 'px-4 pt-3 pb-0' }, footer: { padding: 'px-4 py-2.5' } }">
      <template #header>
        <div class="flex items-baseline gap-3 flex-wrap">
          <h3 class="text-base font-semibold text-gray-900 dark:text-white">{{ $t('allInstructions.title') }}</h3>
          <span class="text-xs text-gray-500 dark:text-gray-400">{{ subtitle }}</span>
          <div class="flex-1"></div>
          <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" size="xs" @click="close" />
        </div>
        <nav class="flex gap-5 mt-3 -mb-px" role="tablist">
          <button
            v-for="t in tabs" :key="t.key" role="tab"
            :aria-selected="tab === t.key"
            @click="selectTab(t.key)"
            :class="[
              'whitespace-nowrap border-b-2 pb-2 text-[13px] flex items-center gap-1.5',
              tab === t.key
                ? 'border-indigo-500 text-gray-900 dark:text-white font-medium'
                : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200'
            ]"
          >
            {{ t.label }}
            <span v-if="t.badge" class="text-[11px] text-gray-400 dark:text-gray-500 tabular-nums">{{ t.badge }}</span>
          </button>
        </nav>
      </template>

      <!-- ============ Instructions ============ -->
      <div v-if="tab === 'list'" class="flex flex-col" style="height: 62vh; min-height: 380px;">
        <div class="px-4 pt-3 flex gap-2 flex-wrap flex-shrink-0 items-center">
          <UInput
            v-model="search" size="xs" class="flex-1 min-w-[180px]"
            icon="i-heroicons-magnifying-glass-20-solid"
            :placeholder="$t('allInstructions.searchInstructions')"
          />
          <MultiSelectFilter
            v-if="agentOptions.length > 1"
            v-model="agentFilter" :options="agentOptions"
            :empty-label="$t('allInstructions.allAgents')" :noun="$t('allInstructions.nounAgents')"
          />
        </div>
        <ActiveFilterChips class="px-4 pt-2 flex-shrink-0" :groups="listFilterChips" @remove="removeListFilter" @clear="clearListFilters" />

        <div class="px-4 pt-3 pb-2.5 flex gap-1.5 flex-wrap flex-shrink-0">
          <button
            v-for="s in stateChips" :key="s.key"
            @click="stateFilter = s.key"
            :aria-pressed="stateFilter === s.key"
            :class="[
              'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg border text-xs',
              stateFilter === s.key
                ? 'bg-indigo-50 dark:bg-indigo-950/40 border-transparent text-gray-900 dark:text-white'
                : 'border-gray-200 dark:border-gray-800 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50'
            ]"
          >
            <span v-if="s.dot" class="w-[7px] h-[7px] rounded-full" :class="s.dot"></span>
            {{ s.label }}
            <span class="font-mono tabular-nums font-semibold text-gray-900 dark:text-white">{{ s.count ?? '–' }}</span>
          </button>
        </div>

        <!-- Only while instructions exist that the live build isn't carrying. -->
        <div
          v-if="notLiveCount > 0 && stateFilter === 'not_live'"
          class="mx-4 mb-2.5 px-3 py-2.5 rounded-lg border border-amber-500 bg-amber-50 dark:bg-amber-950/30 flex items-center gap-3 flex-wrap flex-shrink-0"
        >
          <p class="text-[13px] text-gray-800 dark:text-gray-100 flex-1 min-w-[260px] m-0">
            {{ $t('allInstructions.notLiveExplainer', { n: notLiveCount }) }}
          </p>
        </div>

        <div class="flex-1 min-h-0 overflow-auto border-t border-gray-200 dark:border-gray-800">
          <div v-if="listLoading" class="flex items-center justify-center py-14">
            <Spinner class="w-5 h-5 text-gray-400 animate-spin" />
          </div>
          <div v-else-if="listError" class="py-12 text-center text-xs text-gray-500">{{ listError }}</div>
          <div v-else-if="visibleRows.length === 0" class="py-12 text-center text-xs text-gray-400 italic">
            {{ $t('allInstructions.noneMatch') }}
          </div>
          <table v-else class="w-full text-sm">
            <tbody>
              <tr
                v-for="row in visibleRows" :key="row.id"
                class="border-b border-gray-100 dark:border-gray-800/70 hover:bg-gray-50 dark:hover:bg-gray-800/40 cursor-pointer"
                @click="openInTree(row)"
              >
                <td class="px-4 py-2.5">
                  <div class="flex flex-col gap-0.5 min-w-0">
                    <span class="text-[13px] text-gray-800 dark:text-gray-200 font-medium truncate">
                      {{ rowLabel(row) }}
                    </span>
                    <span class="flex gap-1.5 flex-wrap items-center text-[11px] text-gray-400 dark:text-gray-500">
                      <span
                        v-for="ds in (row.data_sources || [])" :key="ds.id"
                        class="px-1.5 rounded bg-indigo-50 dark:bg-indigo-950/50 text-indigo-600 dark:text-indigo-300 font-medium"
                      >{{ ds.name }}</span>
                      <span v-if="!(row.data_sources || []).length" class="px-1.5 rounded bg-gray-100 dark:bg-gray-800">{{ $t('allInstructions.global') }}</span>
                      <span v-if="row.source_type && row.source_type !== 'user'" class="px-1.5 rounded bg-gray-100 dark:bg-gray-800">{{ row.source_type }}</span>
                    </span>
                  </div>
                </td>
                <td class="px-3 py-2.5 whitespace-nowrap">
                  <span class="inline-flex items-center gap-1.5 text-xs" :class="rowState(row).cls">
                    <span class="w-[7px] h-[7px] rounded-full" :class="rowState(row).dot"></span>{{ rowState(row).label }}
                  </span>
                </td>
                <td class="px-4 py-2.5 text-end whitespace-nowrap text-[12px] font-mono tabular-nums text-gray-400 dark:text-gray-500">
                  {{ row.load_mode }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- ============ Changelog ============ -->
      <div v-else class="flex flex-col" style="height: 62vh; min-height: 380px;">
        <div class="px-4 pt-3 pb-2 flex gap-2 flex-wrap flex-shrink-0 items-center">
          <MultiSelectFilter
            v-if="agentOptions.length > 1"
            v-model="logAgent" :options="agentOptions"
            :empty-label="$t('allInstructions.allAgents')" :noun="$t('allInstructions.nounAgents')"
          />
          <!-- A people filter with one person can't filter anything. -->
          <MultiSelectFilter
            v-if="peopleOptions.length > 1"
            v-model="logUser" :options="peopleOptions"
            :empty-label="$t('allInstructions.allPeople')" :noun="$t('allInstructions.nounPeople')"
          />
          <span
            v-if="agentOptions.length > 1 || peopleOptions.length > 1"
            class="w-px h-5 bg-gray-200 dark:bg-gray-800 mx-0.5"
          ></span>
          <!-- Four options that never grow: chips beat a menu. -->
          <FilterChipGroup v-model="logSource" :options="sourceOptions" :label="$t('allInstructions.madeBy')" />
        </div>
        <ActiveFilterChips class="px-4 pb-2 flex-shrink-0" :groups="logFilterChips" @remove="removeLogFilter" @clear="clearLogFilters" />

        <div class="flex-1 min-h-0 overflow-auto border-t border-gray-200 dark:border-gray-800">
          <div v-if="logLoading" class="flex items-center justify-center py-14">
            <Spinner class="w-5 h-5 text-gray-400 animate-spin" />
          </div>
          <div v-else-if="logError" class="py-12 text-center text-xs text-gray-500">{{ logError }}</div>
          <div v-else-if="entries.length === 0" class="py-12 text-center text-xs text-gray-400 italic">
            {{ $t('allInstructions.noChanges') }}
          </div>
          <template v-else>
            <template v-for="group in groupedEntries" :key="group.day">
              <div class="sticky top-0 z-10 px-4 py-1.5 bg-gray-50 dark:bg-gray-900/95 border-b border-gray-200 dark:border-gray-800 text-[10.5px] uppercase tracking-wider font-semibold text-gray-500 dark:text-gray-400">
                {{ group.day }}
              </div>
              <template v-for="e in group.items" :key="e.build_id">
                <div
                  class="grid gap-3 px-4 py-3 border-b border-gray-100 dark:border-gray-800/70 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800/40"
                  style="grid-template-columns: 26px 1fr auto;"
                  @click="toggleEntry(e)"
                >
                  <span
                    class="w-[26px] h-[26px] rounded-full grid place-items-center text-[10px] font-bold text-white"
                    :class="actorChip(e).cls"
                  >{{ actorChip(e).text }}</span>
                  <span class="flex flex-col gap-1 min-w-0">
                    <span class="text-[13px] text-gray-800 dark:text-gray-200 leading-snug">{{ entryLine(e) }}</span>
                    <span class="text-[11.5px] text-gray-400 dark:text-gray-500 flex gap-2 flex-wrap items-center">
                      <span>{{ $t('allInstructions.buildN', { n: e.build_number }) }}</span>
                      <span v-if="e.is_live" class="text-emerald-600 dark:text-emerald-400">{{ $t('allInstructions.liveNow') }}</span>
                      <span v-else-if="e.status === 'pending_approval'" class="text-amber-600 dark:text-amber-400">{{ $t('allInstructions.awaitingReview') }}</span>
                      <span v-if="e.commit_sha" class="font-mono">{{ e.commit_sha.slice(0, 7) }}</span>
                    </span>
                  </span>
                  <span class="flex items-center gap-3 whitespace-nowrap">
                    <!-- dir=ltr: the sign is a bidi-neutral character, so in an
                         RTL locale "+1" is reordered to "1+" and "−5" to "5−",
                         inverting the one signal this feed is read for. Same for
                         a clock time. -->
                    <span dir="ltr" class="font-mono tabular-nums text-[12.5px] flex gap-1.5">
                      <span v-if="e.added" class="text-emerald-600 dark:text-emerald-400">+{{ e.added }}</span>
                      <span v-if="e.modified" class="text-gray-400 dark:text-gray-500">~{{ e.modified }}</span>
                      <span v-if="e.removed" class="text-amber-700 dark:text-amber-400 font-semibold">−{{ e.removed }}</span>
                    </span>
                    <span dir="ltr" class="font-mono tabular-nums text-[11.5px] text-gray-400 dark:text-gray-500">{{ timeOf(e) }}</span>
                  </span>
                </div>

                <div v-if="expandedId === e.build_id" class="px-4 pb-3.5 ps-[56px] bg-indigo-50/50 dark:bg-indigo-950/20 border-b border-gray-100 dark:border-gray-800/70">
                  <div v-if="detailLoading" class="py-3"><Spinner class="w-4 h-4 text-gray-400 animate-spin" /></div>
                  <template v-else-if="detail">
                    <ul class="flex flex-col gap-1 py-2 m-0 list-none p-0">
                      <li
                        v-for="c in detailRows" :key="c.op + c.id"
                        class="text-[12.5px] text-gray-600 dark:text-gray-300 flex gap-2 items-baseline"
                      >
                        <span dir="ltr" class="font-mono font-bold w-3 flex-shrink-0" :class="c.op === '−' ? 'text-amber-700 dark:text-amber-400' : (c.op === '+' ? 'text-emerald-600 dark:text-emerald-400' : 'text-gray-400')">{{ c.op }}</span>
                        <span class="truncate">{{ c.label }}</span>
                      </li>
                      <li v-if="detailRemainder > 0" class="text-[12.5px] text-gray-400 dark:text-gray-500 ps-5">
                        {{ $t('allInstructions.andMore', { n: detailRemainder }) }}
                      </li>
                    </ul>
                  </template>
                </div>
              </template>
            </template>
          </template>
        </div>
      </div>

      <template #footer>
        <div class="flex items-center gap-2 flex-wrap text-xs text-gray-500 dark:text-gray-400">
          <span v-if="tab === 'list'">{{ $t('allInstructions.showingN', { n: visibleRows.length }) }}</span>
          <span v-else>{{ $t('allInstructions.changelogHint') }}</span>
          <div class="flex-1"></div>
          <UButton
            v-if="tab === 'log' && entries.length < logTotal"
            size="2xs" color="gray" variant="soft" :loading="logLoadingMore" @click="loadMoreLog"
          >{{ $t('allInstructions.loadMore') }}</UButton>
        </div>
      </template>
    </UCard>
  </UModal>
</template>

<script setup lang="ts">
// Explicit imports: components under components/instructions/ auto-import with a
// path prefix (InstructionsMultiSelectFilter), so the bare tag resolves to
// nothing and the control silently renders empty.
import MultiSelectFilter from '~/components/instructions/MultiSelectFilter.vue'
import FilterChipGroup from '~/components/instructions/FilterChipGroup.vue'
import ActiveFilterChips from '~/components/instructions/ActiveFilterChips.vue'

const props = defineProps<{
  modelValue: boolean
  agents?: any[]
  initialTab?: string
  initialState?: string
}>()
const emit = defineEmits(['update:modelValue', 'open-instruction'])
const { t } = useI18n()

const isOpen = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})
const close = () => { isOpen.value = false }

const tabs = computed(() => [
  { key: 'list', label: t('allInstructions.tabInstructions'), badge: rows.value.length ? String(totalCount.value) : '' },
  { key: 'log', label: t('allInstructions.tabChangelog'), badge: '' },
])
const tab = ref(props.initialTab === 'log' ? 'log' : 'list')

/* ---------------- instructions tab ---------------- */
const rows = ref<any[]>([])
const notLiveRows = ref<any[]>([])
const listLoading = ref(false)
const listError = ref<string | null>(null)
const search = ref('')
const agentFilter = ref<string[]>([])
const stateFilter = ref(props.initialState || 'all')

const totalCount = computed(() => rows.value.length + notLiveRows.value.length)
const notLiveCount = computed(() => notLiveRows.value.length)

const countBy = (pred: (r: any) => boolean) => rows.value.filter(pred).length
const isPending = (r: any) => !!r.current_build_id && r.current_build_status !== 'approved'

const stateChips = computed(() => [
  { key: 'all', label: t('allInstructions.stateAll'), count: totalCount.value, dot: '' },
  { key: 'active', label: t('allInstructions.stateActive'), count: countBy(r => r.status === 'published' && !isPending(r)), dot: 'bg-emerald-500' },
  { key: 'not_live', label: t('allInstructions.stateNotLive'), count: notLiveCount.value, dot: 'bg-amber-500' },
  { key: 'pending', label: t('allInstructions.statePending'), count: countBy(isPending), dot: 'bg-amber-500' },
  { key: 'draft', label: t('allInstructions.stateDraft'), count: countBy(r => r.status === 'draft'), dot: 'bg-gray-400' },
  { key: 'archived', label: t('allInstructions.stateArchived'), count: countBy(r => r.status === 'archived'), dot: 'bg-gray-300 dark:bg-gray-600' },
])

const rowLabel = (r: any) => instructionRowLabel(r, 90)

const rowState = (r: any) => {
  if (r.__not_live) return { label: t('allInstructions.stateNotLive'), dot: 'bg-amber-500', cls: 'text-amber-700 dark:text-amber-400' }
  if (isPending(r)) return { label: t('allInstructions.statePending'), dot: 'bg-amber-500', cls: 'text-amber-700 dark:text-amber-400' }
  if (r.status === 'archived') return { label: t('allInstructions.stateArchived'), dot: 'bg-gray-300 dark:bg-gray-600', cls: 'text-gray-500 dark:text-gray-400' }
  if (r.status === 'draft') return { label: t('allInstructions.stateDraft'), dot: 'bg-gray-400', cls: 'text-gray-500 dark:text-gray-400' }
  return { label: t('allInstructions.stateActive'), dot: 'bg-emerald-500', cls: 'text-emerald-700 dark:text-emerald-400' }
}

const visibleRows = computed(() => {
  let list: any[]
  if (stateFilter.value === 'not_live') list = notLiveRows.value
  else if (stateFilter.value === 'all') list = [...rows.value, ...notLiveRows.value]
  else if (stateFilter.value === 'active') list = rows.value.filter(r => r.status === 'published' && !isPending(r))
  else if (stateFilter.value === 'pending') list = rows.value.filter(isPending)
  else list = rows.value.filter(r => r.status === stateFilter.value)

  const q = search.value.toLowerCase().trim()
  if (q) list = list.filter(r => instructionSearchText(r).includes(q))
  if (agentFilter.value.length) {
    list = list.filter(r => (r.data_sources || []).some((d: any) => agentFilter.value.includes(d.id)))
  }
  return list
})

const loadInstructions = async () => {
  listLoading.value = true
  listError.value = null
  try {
    // Two passes: what the live build carries, and what it doesn't. The second
    // is the only way instructions that stopped reaching the agent are
    // reachable at all.
    const base = { include_own: true, include_drafts: true, include_archived: true }
    const [liveRes, notLiveRes] = await Promise.all([
      fetchAllInstructions({ ...base, live: true }),
      fetchAllInstructions({ ...base, live: false }),
    ])
    rows.value = liveRes.items
    notLiveRows.value = notLiveRes.items.map((i: any) => ({ ...i, __not_live: true }))
  } catch (e) {
    listError.value = t('allInstructions.loadFailed')
  } finally {
    listLoading.value = false
  }
}

const openInTree = (row: any) => {
  emit('open-instruction', row)
  close()
}

/* ---------------- changelog tab ---------------- */
const entries = ref<any[]>([])
const logTotal = ref(0)
const logLoading = ref(false)
const logLoadingMore = ref(false)
const logError = ref<string | null>(null)
const logAgent = ref<string[]>([])
const logUser = ref<string[]>([])
const logSource = ref<string[]>([])
const PAGE = 25

const loadLog = async (append = false) => {
  if (append) logLoadingMore.value = true
  else { logLoading.value = true; logError.value = null }
  try {
    const query: Record<string, any> = { skip: append ? entries.value.length : 0, limit: PAGE }
    if (logAgent.value.length) query.agent_ids = logAgent.value.join(',')
    if (logUser.value.length) query.user_ids = logUser.value.join(',')
    if (logSource.value.length) query.sources = logSource.value.join(',')
    const { data, error } = await useMyFetch<any>('/api/instructions/activity', { method: 'GET', query })
    if (error?.value) throw error.value
    const payload: any = (data as any)?.value
    const items = payload?.items || []
    entries.value = append ? [...entries.value, ...items] : items
    logTotal.value = Number(payload?.total ?? items.length)
  } catch (e) {
    if (!append) { logError.value = t('allInstructions.loadFailed'); entries.value = [] }
  } finally {
    logLoading.value = false
    logLoadingMore.value = false
  }
}
const loadMoreLog = () => loadLog(true)

// The people filter is built from the changelog's own actors rather than the
// org member list: it needs no extra permission, and it only offers people who
// actually changed something — filtering by someone with no changes is a dead
// end by construction.
const members = computed(() => {
  const seen = new Map<string, any>()
  for (const e of entries.value) {
    if (e.actor?.id && !seen.has(e.actor.id)) seen.set(e.actor.id, e.actor)
  }
  return Array.from(seen.values())
})

const dayLabel = (iso: string) => {
  const d = new Date(iso)
  const today = new Date()
  const same = (a: Date, b: Date) => a.toDateString() === b.toDateString()
  if (same(d, today)) return t('allInstructions.today')
  const y = new Date(today); y.setDate(y.getDate() - 1)
  if (same(d, y)) return t('allInstructions.yesterday')
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}
const timeOf = (e: any) =>
  new Date(e.created_at).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })

const groupedEntries = computed(() => {
  const out: { day: string; items: any[] }[] = []
  for (const e of entries.value) {
    const day = dayLabel(e.created_at)
    const last = out[out.length - 1]
    if (last && last.day === day) last.items.push(e)
    else out.push({ day, items: [e] })
  }
  return out
})

const actorChip = (e: any) => {
  if (e.source === 'git') return { text: 'GIT', cls: 'bg-gray-600' }
  if (e.source === 'ai') return { text: 'AI', cls: 'bg-emerald-600' }
  if (e.source === 'rollback') return { text: '↺', cls: 'bg-amber-600' }
  const name = e.actor?.name || e.actor?.email || '?'
  const initials = name.split(/[\s@.]+/).filter(Boolean).slice(0, 2).map((p: string) => p[0]).join('').toUpperCase()
  return { text: initials || '?', cls: 'bg-indigo-600' }
}

const entryLine = (e: any) => {
  const who = e.source === 'git' ? t('allInstructions.whoGit')
    : e.source === 'ai' ? t('allInstructions.whoAgent')
    : e.source === 'rollback' ? t('allInstructions.whoRollback')
    : (e.actor?.name || e.actor?.email || t('allInstructions.whoSomeone'))
  const parts: string[] = []
  if (e.added) parts.push(t('allInstructions.deltaAdded', { n: e.added }))
  if (e.modified) parts.push(t('allInstructions.deltaModified', { n: e.modified }))
  if (e.removed) parts.push(t('allInstructions.deltaRemoved', { n: e.removed }))
  return `${who} — ${parts.join(', ')}`
}

const expandedId = ref<string | null>(null)
const detail = ref<any>(null)
const detailLoading = ref(false)

const toggleEntry = async (e: any) => {
  if (expandedId.value === e.build_id) { expandedId.value = null; return }
  expandedId.value = e.build_id
  detail.value = null
  detailLoading.value = true
  try {
    const { data } = await useMyFetch<any>(`/api/instructions/activity/${e.build_id}`, { method: 'GET' })
    detail.value = (data as any)?.value || null
  } catch { detail.value = null }
  finally { detailLoading.value = false }
}

const detailRows = computed(() => {
  if (!detail.value) return []
  return [
    ...(detail.value.removed || []).map((c: any) => ({ ...c, op: '−' })),
    ...(detail.value.added || []).map((c: any) => ({ ...c, op: '+' })),
    ...(detail.value.modified || []).map((c: any) => ({ ...c, op: '~' })),
  ]
})
const detailRemainder = computed(() => {
  if (!detail.value) return 0
  const shown = detailRows.value.length
  const total = (detail.value.added_total || 0) + (detail.value.modified_total || 0) + (detail.value.removed_total || 0)
  return Math.max(0, total - shown)
})

/* ---------------- filter options + active chips ---------------- */
const agentOptions = computed(() =>
  (props.agents || []).map((a: any) => ({ value: a.id, label: a.name })))

const peopleOptions = computed(() =>
  members.value.map((m: any) => ({ value: m.id, label: m.name || m.email })))

const sourceOptions = computed(() => [
  { value: 'user', label: t('allInstructions.madeByPerson') },
  { value: 'ai', label: t('allInstructions.madeByAgent') },
  { value: 'git', label: t('allInstructions.madeByGit') },
  { value: 'rollback', label: t('allInstructions.madeByRollback') },
])

const labelIn = (opts: { value: string; label: string }[], v: string) =>
  opts.find(o => o.value === v)?.label || v

const listFilterChips = computed(() => [
  {
    group: 'agent',
    groupLabel: t('allInstructions.chipAgent'),
    items: agentFilter.value.map(v => ({ value: v, label: labelIn(agentOptions.value, v) })),
  },
].filter(g => g.items.length))

const logFilterChips = computed(() => [
  {
    group: 'agent',
    groupLabel: t('allInstructions.chipAgent'),
    items: logAgent.value.map(v => ({ value: v, label: labelIn(agentOptions.value, v) })),
  },
  {
    group: 'person',
    groupLabel: t('allInstructions.chipPerson'),
    items: logUser.value.map(v => ({ value: v, label: labelIn(peopleOptions.value, v) })),
  },
  {
    group: 'source',
    groupLabel: t('allInstructions.chipMadeBy'),
    items: logSource.value.map(v => ({ value: v, label: labelIn(sourceOptions.value, v) })),
  },
].filter(g => g.items.length))

const drop = (arr: Ref<string[]>, v: string) => { arr.value = arr.value.filter(x => x !== v) }
const removeListFilter = ({ value }: { group: string; value: string }) => drop(agentFilter, value)
const clearListFilters = () => { agentFilter.value = [] }
const removeLogFilter = ({ group, value }: { group: string; value: string }) => {
  if (group === 'agent') drop(logAgent, value)
  else if (group === 'person') drop(logUser, value)
  else drop(logSource, value)
}
const clearLogFilters = () => { logAgent.value = []; logUser.value = []; logSource.value = [] }

/* ---------------- lifecycle ---------------- */
const subtitle = computed(() =>
  t('allInstructions.subtitle', { n: totalCount.value, a: (props.agents || []).length }))

const selectTab = (key: string) => {
  tab.value = key
  if (key === 'log' && entries.value.length === 0) loadLog()
}

watch([logAgent, logUser, logSource], () => { if (tab.value === 'log') loadLog() })

watch(isOpen, (open) => {
  if (!open) return
  if (rows.value.length === 0 && notLiveRows.value.length === 0) loadInstructions()
  if (tab.value === 'log' && entries.value.length === 0) loadLog()
})

defineExpose({ reload: loadInstructions })
</script>
