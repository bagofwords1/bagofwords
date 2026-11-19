<template>
  <div class="flex flex-col h-full min-h-0">
    <!-- Header -->
    <div class="shrink-0 px-6 pt-4 pb-2">
      <div class="flex items-center justify-between gap-3">
        <h2 class="min-w-0 text-base font-semibold text-gray-900 dark:text-white flex items-center gap-2">
          <span class="truncate">{{ $t('reviewFeed.title') }}</span>
          <span v-if="openCount" class="shrink-0 inline-flex items-center justify-center min-w-[20px] h-5 px-1.5 rounded-full bg-gray-900 text-white dark:bg-gray-100 dark:text-gray-900 text-2xs font-semibold tabular-nums">{{ openCount }}</span>
        </h2>
        <div class="flex items-center gap-0.5 shrink-0">
          <UButton size="2xs" color="gray" variant="ghost" icon="i-heroicons-arrow-path" :loading="loading" :title="$t('reviewFeed.refresh')" :aria-label="$t('reviewFeed.refresh')" @click="refresh" />
          <UButton size="2xs" color="gray" variant="ghost" icon="i-heroicons-x-mark" :title="$t('reviewFeed.close')" :aria-label="$t('reviewFeed.close')" @click="$emit('close')" />
        </div>
      </div>
      <p class="mt-0.5 text-xs text-gray-500 dark:text-gray-400">{{ $t('reviewFeed.subtitle') }}</p>

      <!-- Search · agent · resolved -->
      <div class="mt-3 flex items-center gap-1.5">
        <div class="relative flex-1 min-w-0">
          <UIcon name="i-heroicons-magnifying-glass" class="absolute start-2 top-1/2 -translate-y-1/2 w-3 h-3 text-ink-muted" />
          <input v-model="search" type="text" :placeholder="$t('reviewFeed.search')" class="h-7 w-full ps-7 pe-2 text-xs bg-gray-50 border border-gray-200 rounded-md outline-none focus:border-gray-400 focus:bg-white placeholder:text-ink-subtle dark:bg-gray-800 dark:border-gray-700 dark:text-gray-100 dark:placeholder-gray-500" />
        </div>
        <UPopover :popper="{ placement: 'bottom-start' }" :ui="{ ring: '', shadow: 'shadow-md' }">
          <button type="button" class="inline-flex items-center gap-1 h-7 px-2 max-w-[12rem] rounded-md border border-gray-200 dark:border-gray-800 text-xs text-gray-600 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800/50">
            <DataSourceIcon v-if="agentFilter && !agentFilter.startsWith('folder:')" :icon-token="agentIconTokenOf(agentFilter)" class="w-3 h-3 shrink-0" />
            <UIcon v-else name="i-heroicons-cube" class="w-3 h-3 shrink-0 text-ink-muted" />
            <span class="truncate" v-text-dir>{{ agentFilter?.startsWith('folder:') ? folderLabel : agentFilter ? agentNameOf(agentFilter) : $t('reviewFeed.allAgents') }}</span>
            <UIcon name="i-heroicons-chevron-down" class="w-2.5 h-2.5 shrink-0 opacity-60" />
          </button>
          <template #panel="{ close }">
            <div class="p-1 w-56 max-h-72 overflow-auto">
              <button class="w-full flex items-center gap-2 px-2 py-1.5 rounded hover:bg-gray-50 dark:hover:bg-gray-800/50 text-start text-sm" @click="agentFilter = null; close()"><UIcon name="i-heroicons-cube" class="w-4 h-4 text-ink-muted" />{{ $t('reviewFeed.allAgents') }}<UIcon v-if="!agentFilter" name="i-heroicons-check" class="w-3.5 h-3.5 ms-auto text-gray-900 dark:text-white" /></button>
              <AgentFolderChoices :agents="agents" @select="(_, f) => { agentFilter = 'folder:' + f.id; folderLabel = f.path; close() }" :selected-folders="agentFilter?.startsWith('folder:') ? [agentFilter.slice(7)] : []">
                <template #agent="{agent: a, parentPath}">
                  <button class="w-full flex items-center gap-2 px-2 py-1.5 rounded hover:bg-gray-50 dark:hover:bg-gray-800/50 text-start text-sm" @click="agentFilter = a.id; close()"><DataSourceIcon :type="a.type" :icon-token="a.icon_token" :icon="a.icon" class="w-4 h-4" /><span class="truncate">{{ a.name }}<small v-if="parentPath" class="block truncate text-2xs text-ink-muted">{{ parentPath }}</small></span><UIcon v-if="agentFilter === a.id" name="i-heroicons-check" class="w-3.5 h-3.5 ms-auto text-gray-900 dark:text-white shrink-0" /></button>
                </template>
              </AgentFolderChoices>
            </div>
          </template>
        </UPopover>
        <button class="shrink-0 inline-flex items-center gap-1 h-7 px-2 rounded-md text-2xs font-medium whitespace-nowrap transition-colors" :class="showResolved ? 'text-gray-700 dark:text-gray-300 bg-gray-100 dark:bg-gray-800' : 'text-ink-muted hover:bg-gray-50 dark:hover:bg-gray-800/50'" :aria-pressed="showResolved" @click="showResolved = !showResolved">
          <UIcon name="i-heroicons-check-circle" class="w-3 h-3" />{{ $t('reviewFeed.resolvedFilter') }}
        </button>
      </div>

    </div>
    <!-- Type filter: soft pill buttons with counts. Quiet by default; the
         selected one gets a light fill instead of a heavy dark pill. -->
    <div class="shrink-0 flex items-center gap-1 px-6 pt-1 pb-3 border-b border-gray-100 dark:border-gray-800 flex-wrap" role="tablist" data-testid="review-type-filter">
      <button v-for="tab in typeTiles" :key="tab.value || 'all'" role="tab" :aria-selected="typeFilter === tab.value"
              class="inline-flex items-center gap-1.5 h-6 px-2.5 rounded-full text-xs whitespace-nowrap transition-colors outline-none focus-visible:ring-2 focus-visible:ring-blue-500/40"
              :class="typeFilter === tab.value
                ? 'bg-gray-100 text-gray-900 font-medium dark:bg-gray-800 dark:text-white'
                : 'text-gray-500 hover:text-gray-800 hover:bg-gray-50 dark:text-gray-400 dark:hover:text-gray-200 dark:hover:bg-gray-800/60'"
              @click="typeFilter = tab.value">
        {{ tab.label }}<span class="text-2xs tabular-nums text-gray-400 dark:text-gray-500">{{ tab.count }}</span>
      </button>
    </div>

    <!-- Opened from a notification for an item that is no longer open. -->
    <div v-if="focusNotice" class="shrink-0 mx-6 mt-3 px-3 py-2 rounded-md bg-gray-50 dark:bg-gray-800/60 text-xs text-gray-600 dark:text-gray-300 flex items-center gap-2" data-testid="review-focus-notice">
      <UIcon name="i-heroicons-information-circle" class="w-4 h-4 shrink-0 text-ink-muted" />{{ focusNotice }}
    </div>

    <!-- List, grouped by agent -->
    <div ref="listEl" class="flex-1 min-h-0 overflow-y-auto" @scroll.passive="openMenuKey = null" @pointerdown="clearHighlight">
      <div v-if="loading && !items.length" class="flex items-center justify-center py-20 text-ink-muted"><UIcon name="i-heroicons-arrow-path" class="w-5 h-5 animate-spin" /></div>
      <div v-else-if="!filtered.length" class="flex-1 flex items-center justify-center px-6">
        <div class="relative w-full max-w-md h-64 overflow-hidden">
          <img src="/assets/empty-states/review-empty.png" alt="" class="absolute inset-x-0 bottom-6 w-full opacity-80 select-none pointer-events-none" />
          <div class="absolute inset-x-0 bottom-0 flex flex-col items-center justify-center text-center px-6 pb-2">
            <div class="w-12 h-12 flex items-center justify-center rounded-xl bg-white/70 dark:bg-gray-900/70 backdrop-blur-sm ring-1 ring-gray-200/70 dark:ring-gray-700 shadow-sm"><UIcon name="i-heroicons-check-circle" class="w-5 h-5 text-ink-muted" /></div>
            <h3 class="mt-3 text-base font-medium text-gray-900 dark:text-white">{{ $t('reviewFeed.emptyTitle') }}</h3>
            <p class="mt-1.5 max-w-xs text-sm leading-relaxed text-gray-500 dark:text-gray-400">{{ agentFilter ? $t('reviewFeed.emptyBodyAgent') : $t('reviewFeed.emptyBody') }}</p>
          </div>
        </div>
      </div>
      <template v-else>
        <section v-for="g in groups" :key="g.key" data-testid="review-group">
          <button type="button" class="sticky top-0 z-[1] w-full flex items-center gap-2 px-6 py-1.5 bg-gray-50/95 dark:bg-gray-900/95 backdrop-blur border-b border-gray-100 dark:border-gray-800 text-start"
                  :aria-expanded="!collapsed.has(g.key)" @click="toggleGroup(g.key)">
            <UIcon name="i-heroicons-chevron-down-20-solid" class="w-3.5 h-3.5 text-ink-muted transition-transform" :class="collapsed.has(g.key) ? '-rotate-90 rtl:rotate-90' : ''" />
            <DataSourceIcon v-if="g.agentId" :icon-token="agentIconTokenOf(g.agentId)" class="w-3.5 h-3.5 shrink-0" />
            <UIcon v-else name="i-heroicons-globe-alt" class="w-3.5 h-3.5 shrink-0 text-ink-muted" />
            <span class="text-xs font-semibold text-gray-700 dark:text-gray-200 truncate" v-text-dir>{{ g.label }}</span>
            <span class="ms-auto text-2xs text-ink-muted tabular-nums shrink-0">{{ $t('reviewFeed.openN', { n: g.open }) }}</span>
          </button>
          <ul v-show="!collapsed.has(g.key)" class="divide-y divide-gray-100 dark:divide-gray-800">
            <li v-for="row in g.rows" :key="row.key"
                :data-review-key="row.key"
                :data-focused="isFocused(row) ? 'true' : undefined"
                class="group px-6 py-3 transition-[background-color,box-shadow] duration-500"
                :class="[isExpanded(row) ? 'bg-gray-50/80 dark:bg-gray-800/40' : 'hover:bg-gray-50/60 dark:hover:bg-gray-800/30', isFocused(row) ? 'ring-2 ring-inset ring-blue-400/60' : 'ring-0', isHandled(row) ? 'opacity-70' : '']">
              <div class="flex items-start gap-3">
                <div class="mt-0.5 w-7 h-7 rounded-lg flex items-center justify-center shrink-0" :class="iconWrapClass(row)">
                  <UIcon :name="typeMeta(row.type).icon" class="w-4 h-4" />
                </div>
                <!-- Summary: click to expand -->
                <div class="min-w-0 flex-1 cursor-pointer outline-none rounded focus-visible:ring-2 focus-visible:ring-blue-500/50" role="button" tabindex="0"
                     :aria-expanded="isExpanded(row)" @click="toggleExpand(row)" @keydown.enter.prevent="toggleExpand(row)" @keydown.space.prevent="toggleExpand(row)">
                  <div class="flex items-center gap-2 min-w-0">
                    <span v-text-dir class="text-sm font-medium text-gray-900 dark:text-white truncate">{{ rowTitle(row) }}</span>
                    <span v-if="countLabel(row)" class="text-2xs font-medium px-1.5 rounded-full bg-gray-100 dark:bg-gray-800 text-gray-500 dark:text-gray-400 shrink-0 tabular-nums">{{ countLabel(row) }}</span>
                    <span v-if="row.status === 'in_progress'" class="inline-flex items-center gap-1 text-2xs text-blue-600 dark:text-blue-400 shrink-0"><UIcon name="i-heroicons-arrow-path" class="w-3 h-3 animate-spin" />{{ $t('reviewFeed.running') }}</span>
                    <span v-else-if="isHandled(row)" class="inline-flex items-center gap-0.5 text-2xs font-medium px-1.5 rounded bg-green-50 text-green-700 dark:bg-green-500/10 dark:text-green-300 shrink-0" data-testid="review-handled-badge"><UIcon name="i-heroicons-check" class="w-3 h-3" />{{ $t('reviewFeed.resolved') }}</span>
                  </div>
                  <p v-text-dir.align v-if="rowWhy(row)" class="mt-0.5 text-xs text-gray-500 dark:text-gray-400" :class="isExpanded(row) ? 'whitespace-pre-line' : 'line-clamp-1'">{{ rowWhy(row) }}</p>
                  <div class="mt-1 flex items-center gap-1.5 text-2xs text-ink-muted">
                    <span v-if="g.mixed" class="inline-flex items-center gap-1" v-text-dir>{{ row.agentLabel }}</span>
                    <span v-if="g.mixed">·</span>
                    <span>{{ typeMeta(row.type).label }}</span>
                    <span>·</span>
                    <span>{{ fmtDate(row.last) }}</span>
                    <template v-if="isHandled(row) && handledLine(row)">
                      <span>·</span>
                      <span class="text-green-700 dark:text-green-400" v-text-dir>{{ handledLine(row) }}</span>
                    </template>
                  </div>
                </div>
                <!-- One clear action + the rest under ⋯ -->
                <div class="flex items-center gap-0.5 shrink-0" @click.stop>
                  <UButton v-if="isOpen(row) && primaryAction(row)" size="2xs" color="blue" variant="soft"
                           :loading="busy === row.key" :data-testid="`review-primary-${row.type}`"
                           @click="runAction(row, primaryAction(row))">{{ actionLabel(row, primaryAction(row)) }}</UButton>
                  <UDropdown :items="moreItems(row)" :popper="{ placement: 'bottom-end' }" :ui="{ width: 'w-max min-w-48 max-w-xs' }"
                             :open="openMenuKey === row.key" @update:open="(v: boolean) => onMenuOpen(row.key, v)">
                    <UButton size="2xs" color="gray" variant="ghost" icon="i-heroicons-ellipsis-horizontal" :aria-label="$t('reviewFeed.more')" />
                  </UDropdown>
                </div>
              </div>
              <!-- Expanded detail: the same diff view as the inbox + where to go. -->
              <div v-if="isExpanded(row)" class="mt-2.5 ms-10 flex flex-col gap-2.5" data-testid="review-detail">
                <SchemaDiffView v-if="row.rep?.subject?.diff" :diff="row.rep.subject.diff" />
                <div v-if="navLinks(row).length" class="flex flex-wrap items-center gap-1">
                  <UButton v-for="a in navLinks(row)" :key="a.key" size="2xs" color="gray" variant="ghost" :icon="a.icon || 'i-heroicons-arrow-top-right-on-square'" @click="goTo(a.to)">{{ $t(a.labelKey) }}</UButton>
                </div>
              </div>
            </li>
          </ul>
        </section>
      </template>
    </div>

  </div>
</template>

<script setup lang="ts">
import DataSourceIcon from '~/components/DataSourceIcon.vue'
import SchemaDiffView from '~/components/notifications/SchemaDiffView.vue'
import { displayTitle, displayBody, actionsFor, handledText, trainingPrompt, type NotificationAction } from '~/utils/notifications'

// `focusItemId` comes from a notification deep link (/agents/[<id>/]review/<item>):
// the feed scrolls to that item, highlights it and shows its full detail.
const props = defineProps<{ agents: any[]; initialAgentId?: string | null; focusItemId?: string | null }>()
const { t, te } = useI18n()
const emit = defineEmits<{ (e: 'close'): void; (e: 'open-instruction', p: { instructionId: string; buildId?: string }): void; (e: 'count', n: number): void }>()

const toast = useToast()
const items = ref<any[]>([])
const loading = ref(false)
const busy = ref<string | null>(null)
const search = ref('')
const folderLabel = ref('')
const agentFilter = ref<string | null>(props.initialAgentId || null)
const typeFilter = ref<string | null>(null)
const showResolved = ref(false)

const TYPE_TILES = [
  { value: 'low_confidence', labelKey: 'reviewFeed.chipLowConfidence' },
  { value: 'schema_changed', labelKey: 'reviewFeed.chipSchema' },
  { value: 'instruction_suggestion', labelKey: 'reviewFeed.chipSuggestions' },
  { value: 'slow_query', labelKey: 'reviewFeed.chipSlow' },
  { value: 'query_error', labelKey: 'reviewFeed.typeQueryError' },
]
const TYPE_META: Record<string, { labelKey: string; icon: string; tint: string }> = {
  instruction_suggestion: { labelKey: 'reviewFeed.typeSuggestion', icon: 'i-heroicons-document-text', tint: 'blue' },
  schema_changed: { labelKey: 'reviewFeed.typeSchema', icon: 'i-heroicons-table-cells', tint: 'amber' },
  slow_query: { labelKey: 'reviewFeed.typeSlow', icon: 'i-heroicons-clock', tint: 'amber' },
  low_confidence: { labelKey: 'reviewFeed.typeLowConfidence', icon: 'i-heroicons-exclamation-triangle', tint: 'orange' },
  query_error: { labelKey: 'reviewFeed.typeQueryError', icon: 'i-heroicons-x-circle', tint: 'red' },
}
const typeMeta = (type: string) => {
  const m = TYPE_META[type]
  return m ? { label: t(m.labelKey), icon: m.icon, tint: m.tint } : { label: type, icon: 'i-heroicons-bell', tint: 'gray' }
}

// Item copy is stored in English; re-render it in the viewer's locale from the
// same catalogue the inbox uses (subject.i18n carries the params).
const rowTitle = (row: any) => displayTitle({ type: row.type, title: row.title, subject: row.rep?.subject }, t, te)
const rowWhy = (row: any) => displayBody({ type: row.type, title: row.title, body: row.why, subject: row.rep?.subject }, t, te)
const ACTION_KEYS: Record<string, string> = { review: 'reviewFeed.actionReview', run_eval: 'reviewFeed.actionRunEval', train_in_chat: 'reviewFeed.actionTrainInChat' }
const actionLabel = (row: any, a: any) => ACTION_KEYS[a.id] ? t(ACTION_KEYS[a.id]) : a.label

const agentNameOf = (id: string) => props.agents.find(a => a.id === id)?.name || t('reviewFeed.agent')
const agentIconTokenOf = (id: string) => props.agents.find(a => a.id === id)?.icon_token

const iconWrapClass = (it: any) => {
  const t = typeMeta(it.type).tint
  return ({
    blue: 'bg-blue-50 text-blue-600 dark:bg-blue-500/10 dark:text-blue-400', amber: 'bg-amber-50 text-amber-600 dark:bg-amber-500/10 dark:text-amber-400',
    orange: 'bg-orange-50 text-orange-600 dark:bg-orange-500/10 dark:text-orange-400', red: 'bg-red-50 text-red-600 dark:bg-red-500/10 dark:text-red-400', gray: 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400',
  } as any)[t] || 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400'
}
// Search applies first; the type tabs count what the search left, so each
// tab says how many it would show.
const searched = computed(() => {
  if (!search.value.trim()) return items.value
  const q = search.value.toLowerCase()
  return items.value.filter(i => (i.title || '').toLowerCase().includes(q) || (i.why || '').toLowerCase().includes(q))
})
const filtered = computed(() => typeFilter.value ? searched.value.filter(i => i.type === typeFilter.value) : searched.value)
const typeTiles = computed(() => {
  const counts: Record<string, number> = {}
  for (const i of searched.value) counts[i.type] = (counts[i.type] || 0) + 1
  return [
    { value: null as string | null, label: t('reviewFeed.all'), count: searched.value.length },
    // A type with nothing in it stays hidden unless it's the active filter.
    ...TYPE_TILES.filter(x => counts[x.value] || typeFilter.value === x.value)
      .map(x => ({ value: x.value as string | null, label: t(x.labelKey), count: counts[x.value] || 0 })),
  ]
})

// Group instruction_suggestion items per instruction → one row "TITLE · N changes"
// (the same instruction fanned across agents collapses to one row). Other types
// render one row each.
const displayRows = computed(() => {
  const rows: any[] = []
  const byInstr = new Map<string, any>()
  for (const it of filtered.value) {
    const iid = it.type === 'instruction_suggestion' ? it.subject?.instruction_id : null
    if (iid) {
      let g = byInstr.get(iid)
      if (!g) {
        g = { key: 'instr:' + iid, kind: 'instruction', type: it.type, severity: it.severity,
              title: it.title, why: it.why, count: 0, agentIds: [] as any[], read: true,
              status: 'resolved', items: [] as any[], rep: it, last: it.last_seen_at || it.created_at }
        byInstr.set(iid, g); rows.push(g)
      }
      g.items.push(it)
      g.count = Math.max(g.count, it.group_count || 1)
      if (!g.agentIds.includes(it.agent_id)) g.agentIds.push(it.agent_id)
      if (!it.read && it.status !== 'resolved') g.read = false
      // Surface the most "active" status for the group.
      const rank: any = { open: 0, in_progress: 1, snoozed: 2, resolved: 3, dismissed: 4 }
      if (rank[it.status] < rank[g.status]) { g.status = it.status; g.rep = it }
    } else {
      rows.push({ key: it.id, kind: 'single', type: it.type, severity: it.severity,
                  title: it.title, why: it.why, count: it.group_count || 1, agentIds: [it.agent_id],
                  read: it.read, status: it.status, items: [it], rep: it, last: it.last_seen_at || it.created_at })
    }
  }
  // Derive an agent label + single-agent icon per row.
  for (const r of rows) {
    const ids = r.agentIds.filter((x: any) => x)
    const hasGlobal = r.agentIds.some((x: any) => !x)
    r.singleAgentId = (!hasGlobal && ids.length === 1) ? ids[0] : null
    r.agentLabel = hasGlobal ? t('reviewFeed.allAgents') : (ids.length === 1 ? agentNameOf(ids[0]) : t('reviewFeed.nAgents', { n: ids.length }))
  }
  return rows
})

// ── Grouping by agent ───────────────────────────────────────────────────────
// One section per agent, in the list's own order (severity, then recency).
// Items for every agent (global) or a suggestion spanning several agents get a
// section of their own and keep the agent label on each row.
const isOpen = (row: any) => row.status === 'open' || row.status === 'snoozed' || row.status === 'in_progress'
const groups = computed(() => {
  const out: any[] = []
  const byKey = new Map<string, any>()
  for (const row of displayRows.value) {
    const hasGlobal = row.agentIds.some((x: any) => !x)
    const key = row.singleAgentId || (hasGlobal ? 'global' : 'multi')
    let g = byKey.get(key)
    if (!g) {
      g = { key, agentId: row.singleAgentId, rows: [] as any[], open: 0, mixed: !row.singleAgentId,
            label: row.singleAgentId ? agentNameOf(row.singleAgentId) : hasGlobal ? t('reviewFeed.allAgents') : t('reviewFeed.multipleAgents') }
      byKey.set(key, g); out.push(g)
    }
    g.rows.push(row)
    if (isOpen(row)) g.open++
  }
  return out
})
const collapsed = ref(new Set<string>())
const toggleGroup = (key: string) => {
  const next = new Set(collapsed.value)
  next.has(key) ? next.delete(key) : next.add(key)
  collapsed.value = next
}

// ── Expand a row for its detail ─────────────────────────────────────────────
const expandedKey = ref<string | null>(null)
const isExpanded = (row: any) => expandedKey.value === row.key
const toggleExpand = (row: any) => {
  expandedKey.value = isExpanded(row) ? null : row.key
}
// Where to go from an item: the same per-type targets the inbox offers,
// minus "Open in Review" (we are already here).
const navLinks = (row: any): NotificationAction[] => {
  const it = row.rep || {}
  return actionsFor({
    source: 'review', type: row.type, title: row.title,
    subject: { ...(it.subject || {}), review_type: row.type, data_source_id: it.agent_id, review_item_id: it.id },
  }).filter(a => a.key !== 'review').map(a => ({ ...a, primary: false }))
}
const goTo = (to: string) => navigateTo(to)

// The ⋯ menu floats outside the scrolling list, so it would stay put while the
// rows scroll under it: track which one is open and close it on scroll.
const openMenuKey = ref<string | null>(null)
const onMenuOpen = (key: string, open: boolean) => {
  if (open) openMenuKey.value = key
  else if (openMenuKey.value === key) openMenuKey.value = null
}

// ── Actions: one primary per row, the rest under ⋯ ──────────────────────────
const primaryAction = (row: any) => {
  if (row.kind === 'instruction') return { id: 'review' }
  // Only an explicitly primary action (e.g. "Review" on a suggestion) gets a
  // visible button; everything else, training included, lives under ⋯.
  const p = rowActions(row).find((a: any) => a.primary)
  return p && !actionUnavailable(row, p) ? p : null
}
const ACTION_ICONS: Record<string, string> = { run_eval: 'i-heroicons-beaker', train_in_chat: 'i-heroicons-academic-cap', review: 'i-heroicons-arrow-right' }
const moreItems = (row: any) => {
  const primary = primaryAction(row)
  const secondary = isOpen(row)
    ? rowActions(row).filter((a: any) => a.id !== primary?.id).map((a: any) => ({
        label: actionLabel(row, a), icon: ACTION_ICONS[a.id], disabled: !!actionUnavailable(row, a), click: () => runAction(row, a),
      }))
    : []
  const close = isOpen(row) ? [{ label: t('reviewFeed.dismiss'), icon: 'i-heroicons-check', click: () => dismiss(row) }] : []
  return [secondary, close].filter(g => g.length)
}

// The badge counts occurrences, so its word depends on the type: schema
// changes and instruction edits are "changes", slow queries are "times". For
// low confidence the body already says "N answers…", so no badge.
const countLabel = (row: any): string | null => {
  if (!(row.count > 1)) return null
  if (row.kind === 'instruction' || row.type === 'schema_changed' || row.type === 'instruction_suggestion') return t('reviewFeed.changes', { n: row.count })
  if (row.type === 'slow_query' || row.type === 'query_error') return t('reviewFeed.times', { n: row.count })
  return null
}

// One closed state for the team: "Handled", with how and by whom.
const isHandled = (row: any) => row.status === 'resolved' || row.status === 'dismissed'
const handledLine = (row: any) => handledText(row.rep?.handled, t)

// Training mode is an org feature; with it off the "Improve in training mode"
// action is hidden rather than shown disabled.
const { isTrainingModeEnabled } = useOrgSettings()
const rowActions = (row: any) => {
  if (row.kind === 'instruction') return [{ id: 'review', label: t('reviewFeed.actionReview') }]
  return (row.rep.actions || []).filter((a: any) => a.id !== 'dismiss'
    && !(a.id === 'train_in_chat' && (!isTrainingModeEnabled.value || !row.rep?.agent_id)))
}

// run_eval needs the agent to have at least one active eval; the backend
// reports this per item. Returns a reason string when unavailable.
const actionUnavailable = (row: any, a: any): string | null => {
  if (a.id === 'run_eval' && row.status === 'in_progress') return t('reviewFeed.running')
  if (a.id === 'run_eval' && row.rep?.agent_has_evals === false)
    return t('reviewFeed.noEvals')
  return null
}

const { relativeTime: fmtDate } = useRelativeTime()

const fetchItems = async () => {
  loading.value = true
  try {
    const q: any = {}
    if (agentFilter.value?.startsWith('folder:')) q.folder_id = agentFilter.value.slice(7)
    else if (agentFilter.value) q.agent_id = agentFilter.value
    // "Handled" = resolved (fixed through an action) or dismissed (marked as handled).
    if (showResolved.value) q.status = 'open,in_progress,snoozed,resolved,dismissed'
    const { data } = await useMyFetch<any>('/api/review', { method: 'GET', query: q })
    items.value = data.value?.items || []
  } catch (e) { /* noop */ } finally { loading.value = false }
  await focusItem()
}

// ── Deep-link focus ─────────────────────────────────────────────────────────
// The focused item may already be closed: retry once with resolved items
// included; if it still isn't listed it was dismissed (or isn't visible).
const listEl = ref<HTMLElement | null>(null)
const focusNotice = ref<string | null>(null)
let focusHandled = false
const holdsFocusItem = (row: any) => !!props.focusItemId && (row.items || []).some((it: any) => it.id === props.focusItemId)
// The ring only says "this is the item you came for": it marks the row on
// arrival and goes away on the first click in the list or after a few seconds.
const highlightKey = ref<string | null>(null)
let highlightTimer: ReturnType<typeof setTimeout> | null = null
const clearHighlight = () => {
  highlightKey.value = null
  if (highlightTimer) { clearTimeout(highlightTimer); highlightTimer = null }
}
onBeforeUnmount(clearHighlight)
const isFocused = (row: any) => highlightKey.value === row.key
async function focusItem() {
  if (!props.focusItemId || focusHandled) return
  const row = displayRows.value.find(holdsFocusItem)
  if (!row) {
    if (!showResolved.value) { showResolved.value = true; return } // watcher refetches
    focusHandled = true
    focusNotice.value = t('reviewFeed.focusGone')
    return
  }
  focusHandled = true
  expandedKey.value = row.key
  highlightKey.value = row.key
  highlightTimer = setTimeout(clearHighlight, 4000)
  if (isHandled(row)) focusNotice.value = t('reviewFeed.focusResolved')
  await nextTick()
  const el = listEl.value?.querySelector(`[data-review-key="${CSS.escape(row.key)}"]`) as HTMLElement | null
  el?.scrollIntoView({ block: 'center' })
}
const refresh = () => fetchItems()

// "To review" count = items still open for the team (not handled), matching
// /api/review/count's `open`.
const openCount = computed(() => items.value.filter((i: any) => i.status !== 'resolved' && i.status !== 'dismissed').length)
watch(openCount, (n) => emit('count', n))

watch([agentFilter, showResolved], fetchItems)

const runAction = async (row: any, a: any) => {
  if (a.id === 'review') {
    const s = row.rep?.subject || {}
    if (s.instruction_id) emit('open-instruction', { instructionId: s.instruction_id, buildId: s.build_id })
    return
  }
  if (a.id === 'train_in_chat') return openTraining(row)
  const reason = actionUnavailable(row, a)
  if (reason) { toast.add({ title: t('reviewFeed.nothingToRun'), description: reason, color: 'amber' }); return }
  busy.value = row.key
  try {
    // Single-item (non-instruction) actions resolve the representative item.
    const { data, error } = await useMyFetch<any>(`/api/review/${row.rep.id}/resolve`, { method: 'POST', body: { action_id: a.id } })
    if (error.value) throw new Error((error.value as any)?.data?.detail || t('reviewFeed.actionFailed'))
    const resp = data.value
    // The backend may decline (e.g. no evals) with a 200 + { ok: false }.
    if (resp && resp.ok === false) {
      const msg = resp.error === 'no_evals'
        ? t('reviewFeed.noEvals')
        : (resp.message || t('reviewFeed.actionUnavailable'))
      toast.add({ title: t('reviewFeed.nothingToRun'), description: msg, color: 'amber' })
      await fetchItems()
      return
    }
    const label = a.id === 'run_eval' ? t('reviewFeed.evalStarted') : t('reviewFeed.done')
    toast.add({ title: label, color: 'blue' })
    await fetchItems()
  } catch (e: any) { toast.add({ title: t('reviewFeed.actionFailed'), description: e?.message, color: 'red' }) } finally { busy.value = null }
}
// Open a new training-mode conversation on the item's agent with a prompt
// built from the item (schema diff, or the examples behind it). The prompt is
// prefilled only; the admin reads, edits and sends it. The item stays open
// until someone marks it as handled.
const openTraining = async (row: any) => {
  const it = row.rep || {}
  if (!it.agent_id) return
  busy.value = row.key
  let examples: any[] = []
  try {
    if (row.type !== 'schema_changed') {
      const { data } = await useMyFetch<any>(`/api/review/${it.id}/training-context`, { method: 'GET' })
      examples = data.value?.examples || []
    }
  } catch { /* fall back to the generic brief */ }
  busy.value = null
  const prompt = trainingPrompt({ type: row.type, subject: it.subject, groupCount: row.count, examples }, t)
  return navigateTo({ path: '/reports/new', query: { agents: it.agent_id, mode: 'training', prompt } })
}
const dismiss = async (row: any) => {
  const ids = new Set((row.items || []).map((it: any) => it.id))
  const prev = items.value
  // Optimistic: drop the row now, reconcile (and roll back) from the server.
  items.value = items.value.filter((it: any) => !ids.has(it.id))
  try {
    const res = await Promise.all((row.items || []).map((it: any) => useMyFetch(`/api/review/${it.id}/dismiss`, { method: 'POST' })))
    if (res.some((r: any) => r?.error?.value)) throw new Error(t('reviewFeed.actionFailed'))
    await fetchItems()
  } catch (e: any) {
    items.value = prev
    toast.add({ title: t('reviewFeed.dismissFailed'), description: e?.message, color: 'red' })
  }
}

onMounted(fetchItems)
defineExpose({ refresh })
</script>
