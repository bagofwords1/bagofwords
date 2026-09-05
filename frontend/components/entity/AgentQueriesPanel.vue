<!--
  One agent's saved queries, as a list in the explorer's right pane.

  The tree row for Queries opens this instead of expanding, the same way Evals
  opens its panel: a query is a document with a body, not a label, so a flat
  list with room for the description and shape reads better than a row of
  truncated titles in a 260px tree. Selecting one is emitted upward — the
  explorer owns the selection so it can put it in the URL — and the detail
  swaps in over this list with a back button (mirrors EvalRunDetail embedded).
-->
<template>
  <div class="px-6 py-4 text-sm">
    <div class="flex items-center gap-2 mb-3">
      <div class="relative flex-1 max-w-xs">
        <UIcon name="i-heroicons-magnifying-glass" class="absolute start-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-400 dark:text-gray-500" />
        <input
          v-model="q"
          type="text"
          :placeholder="$t('queries.searchPlaceholder')"
          data-testid="agent-queries-search"
          class="w-full h-8 ps-8 pe-2 text-xs bg-gray-50 dark:bg-gray-800 border border-gray-200 dark:border-gray-700 dark:text-gray-100 rounded-md outline-none focus:border-gray-400 focus:bg-white dark:focus:bg-gray-800 placeholder:text-gray-400 dark:placeholder:text-gray-500"
        />
      </div>
      <span class="text-xs text-gray-400 dark:text-gray-500 tabular-nums ms-auto">{{ visible.length }}</span>
    </div>

    <div v-if="loading" class="flex items-center gap-2 py-8 justify-center text-xs text-gray-400 dark:text-gray-500">
      <Spinner class="w-3.5 h-3.5" /><span>{{ $t('queries.loading') }}</span>
    </div>

    <div v-else-if="visible.length === 0" class="flex flex-col items-center justify-center text-center py-14 px-4">
      <div class="w-12 h-12 flex items-center justify-center rounded-xl bg-white dark:bg-gray-900 ring-1 ring-gray-200/70 dark:ring-gray-700/70 shadow-sm">
        <LibraryIcon class="w-5 h-5 text-gray-400 dark:text-gray-500" />
      </div>
      <h3 class="mt-3 text-sm font-medium text-gray-900 dark:text-white">
        {{ q ? $t('queries.noPublished') : $t('agentsPage.noQueries') }}
      </h3>
      <p class="mt-1.5 max-w-xs text-xs leading-relaxed text-gray-500 dark:text-gray-400">
        {{ $t('queries.publishedDescription') }}
      </p>
    </div>

    <div v-else class="space-y-2">
      <button
        v-for="e in visible"
        :key="e.id"
        type="button"
        data-testid="agent-query-row"
        class="w-full text-start border border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-900 rounded-lg px-3 py-2.5 hover:border-gray-200 dark:hover:border-gray-700 hover:shadow-sm transition-all"
        @click="$emit('select', e)"
      >
        <div class="flex items-center gap-2 min-w-0">
          <span
            class="shrink-0 text-[10px] px-1.5 py-0.5 rounded border"
            :class="e.type === 'metric' ? 'text-emerald-700 border-emerald-200 bg-emerald-50 dark:bg-emerald-500/10 dark:border-emerald-500/30 dark:text-emerald-400' : 'text-blue-700 border-blue-200 bg-blue-50 dark:bg-blue-500/10 dark:border-blue-500/30 dark:text-blue-400'"
          >{{ (e.type || 'model').toUpperCase() }}</span>
          <span class="min-w-0 truncate text-[13px] font-medium text-gray-900 dark:text-white">{{ e.title || e.slug }}</span>
          <span
            v-if="badgeOf(e)"
            class="shrink-0 inline-flex items-center px-1.5 h-5 rounded text-[10px] font-medium"
            :class="badgeOf(e)!.class"
          >{{ badgeOf(e)!.text }}</span>
          <UIcon v-if="!badgeOf(e)" name="i-heroicons-check-badge" class="shrink-0 w-4 h-4 text-green-600 dark:text-green-500" :title="$t('queries.detail.approvedTitle')" />
          <span class="ms-auto shrink-0 text-[11px] text-gray-400 dark:text-gray-500">{{ timeAgo(e.updated_at) }}</span>
        </div>
        <div class="mt-1 flex items-center gap-3 min-w-0">
          <span class="min-w-0 truncate text-[12px] text-gray-500 dark:text-gray-400">{{ e.description || $t('queries.noDescription') }}</span>
          <!-- Attached to more agents than this one: the m:n fact a per-agent
               list would otherwise hide. -->
          <span
            v-if="(e.data_sources?.length || 0) > 1"
            class="shrink-0 inline-flex items-center gap-1 text-[11px] text-gray-400 dark:text-gray-500"
            :title="(e.data_sources || []).map(d => d.name).join(', ')"
          >
            <UIcon name="i-heroicons-cube" class="w-3 h-3" />{{ e.data_sources!.length }}
          </span>
          <span v-if="e.data?.info?.total_rows !== undefined" class="shrink-0 text-[11px] text-gray-400 dark:text-gray-500" :title="$t('queries.rowsTitle')">
            {{ formatCount(e.data.info.total_rows) }}
          </span>
        </div>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import LibraryIcon from '~/components/icons/LibraryIcon.vue'

type QueryEntity = {
  id: string
  title?: string
  slug?: string
  type?: string
  description?: string | null
  status?: string
  private_status?: string | null
  global_status?: string | null
  data_sources?: { id: string; name?: string }[]
  data?: any
  updated_at?: string
}

const props = defineProps<{ dsId: string }>()
defineEmits<{ (e: 'select', entity: QueryEntity): void }>()

const { t } = useI18n()
const items = ref<QueryEntity[]>([])
const loading = ref(true)
const q = ref('')

// Archived rows are hidden here and excluded from the server's badge count, so
// the list length and the tree badge always agree.
const isArchived = (e: QueryEntity) => e.status === 'archived' || e.private_status === 'archived'

const visible = computed(() => {
  const needle = q.value.trim().toLowerCase()
  return items.value
    .filter(e => !isArchived(e))
    .filter(e => !needle
      || (e.title || '').toLowerCase().includes(needle)
      || (e.slug || '').toLowerCase().includes(needle)
      || (e.description || '').toLowerCase().includes(needle))
})

// Lifecycle badge, same vocabulary the standalone page's tabs used: a published
// catalog row gets the approved check instead, everything else says what it is.
const badgeOf = (e: QueryEntity): { text: string; class: string } | null => {
  if (isArchived(e)) return { text: t('queries.archivedBadge'), class: 'bg-red-50 text-red-700 dark:bg-red-500/10 dark:text-red-400' }
  if (e.private_status && e.global_status === 'suggested') return { text: t('queries.suggestedBadge'), class: 'bg-amber-100 text-amber-800 dark:bg-amber-500/10 dark:text-amber-400' }
  if (e.private_status) return { text: t('queries.draftBadge'), class: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400' }
  if (e.status === 'draft') return { text: t('queries.draftBadge'), class: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400' }
  return null
}

async function load() {
  loading.value = true
  try {
    const { data } = await useMyFetch<any>(
      `/api/entities?data_source_ids=${encodeURIComponent(props.dsId)}&limit=1000`, { method: 'GET' })
    items.value = (data.value || []) as QueryEntity[]
  } catch (e) {
    console.error('Failed to load queries', e)
    items.value = []
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(() => props.dsId, () => { items.value = []; q.value = ''; load() })
defineExpose({ reload: load })

function timeAgo(iso?: string) {
  if (!iso) return '—'
  const hasTZ = /Z|[+-]\d{2}:?\d{2}$/.test(iso)
  const d = new Date(hasTZ ? iso : `${iso}Z`)
  const mins = Math.floor(Math.max(0, Date.now() - d.getTime()) / 60000)
  if (mins < 60) return t('queries.timeMinutesAgo', { n: mins })
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return t('queries.timeHoursAgo', { n: hrs })
  return t('queries.timeDaysAgo', { n: Math.floor(hrs / 24) })
}

function formatCount(n?: number): string {
  if (n === undefined || n === null) return '—'
  if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`
  return String(n)
}
</script>
