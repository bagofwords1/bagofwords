<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-2xl', container: 'items-start', margin: 'sm:mt-[10vh]' }">
    <div class="bg-white dark:bg-gray-900 rounded-lg overflow-hidden flex flex-col max-h-[78vh]">
      <!-- Header -->
      <div class="flex items-center justify-between px-4 py-3 border-b border-gray-100 dark:border-gray-800">
        <div class="flex items-center gap-2">
          <h2 class="text-sm font-semibold text-gray-900 dark:text-gray-100">Notifications</h2>
          <span
            v-if="unread"
            class="min-w-[18px] h-[18px] px-1 rounded-full bg-blue-500 text-white text-[10px] font-semibold leading-none flex items-center justify-center"
          >{{ unread > 99 ? '99+' : unread }}</span>
        </div>
        <div class="flex items-center gap-1">
          <button
            v-if="unread"
            @click="markAllRead()"
            class="text-[12px] text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200 px-2 py-1 rounded-md hover:bg-gray-100 dark:hover:bg-gray-800/70 transition-colors"
          >Mark all read</button>
          <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" size="xs" @click="isOpen = false" />
        </div>
      </div>

      <!-- Filters -->
      <div class="flex items-center gap-1 px-4 py-2 border-b border-gray-100 dark:border-gray-800">
        <button
          v-for="f in FILTERS"
          :key="f.value"
          @click="filter = f.value"
          :class="[
            'text-[12px] px-2.5 py-1 rounded-full transition-colors',
            filter === f.value
              ? 'bg-gray-900 text-white dark:bg-gray-100 dark:text-gray-900 font-medium'
              : 'text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800/70'
          ]"
        >
          {{ f.label }}<span v-if="f.value === 'unread' && unread" class="ms-1 opacity-70">{{ unread > 99 ? '99+' : unread }}</span>
        </button>
      </div>

      <!-- List -->
      <div class="flex-1 overflow-y-auto min-h-[220px]">
        <div v-if="loading" class="py-14 flex justify-center">
          <Spinner class="w-5 h-5 text-gray-400 animate-spin" />
        </div>

        <div v-else-if="error" class="py-16 flex flex-col items-center text-center gap-2 px-6">
          <div class="flex items-center justify-center w-12 h-12 rounded-full bg-red-50 dark:bg-red-500/10">
            <UIcon name="i-heroicons-exclamation-triangle" class="w-6 h-6 text-red-400 dark:text-red-500" />
          </div>
          <p class="text-sm font-medium text-gray-700 dark:text-gray-300">Couldn't load notifications</p>
          <p class="text-xs text-gray-400 dark:text-gray-500">Something went wrong fetching your inbox.</p>
          <button
            @click="fetchItems()"
            class="mt-1 text-[12px] text-blue-600 dark:text-blue-400 hover:underline px-2 py-1 rounded-md"
          >Try again</button>
        </div>

        <div v-else-if="!visibleItems.length" class="py-16 flex flex-col items-center text-center gap-2 px-6">
          <div class="flex items-center justify-center w-12 h-12 rounded-full bg-gray-50 dark:bg-gray-800">
            <UIcon name="i-heroicons-bell-slash" class="w-6 h-6 text-gray-300 dark:text-gray-600" />
          </div>
          <template v-if="filter === 'unread' && items.length">
            <p class="text-sm font-medium text-gray-700 dark:text-gray-300">No unread notifications</p>
            <p class="text-xs text-gray-400 dark:text-gray-500">You've read everything in your inbox.</p>
          </template>
          <template v-else>
            <p class="text-sm font-medium text-gray-700 dark:text-gray-300">You're all caught up</p>
            <p class="text-xs text-gray-400 dark:text-gray-500">New notifications will show up here.</p>
          </template>
        </div>

        <ul v-else class="divide-y divide-gray-100 dark:divide-gray-800">
          <li
            v-for="n in visibleItems"
            :key="n.id"
            @click="onRowClick(n)"
            :class="[
              'group relative flex gap-3 px-4 py-3 transition-colors',
              n.link ? 'cursor-pointer' : '',
              'hover:bg-gray-50 dark:hover:bg-gray-800/50',
              !n.read ? 'bg-blue-50/40 dark:bg-blue-500/[0.06]' : ''
            ]"
          >
            <div :class="['mt-0.5 flex items-center justify-center w-7 h-7 rounded-full shrink-0', sevBg(n)]">
              <UIcon :name="iconFor(n)" :class="['w-4 h-4', sevText(n)]" />
            </div>

            <div class="min-w-0 flex-1 pe-5">
              <div class="flex items-center gap-2">
                <span
                  :class="[
                    'text-[13px] truncate',
                    !n.read ? 'font-semibold text-gray-900 dark:text-gray-100' : 'font-medium text-gray-700 dark:text-gray-300'
                  ]"
                >{{ n.title }}</span>
                <span v-if="!n.read" class="w-1.5 h-1.5 rounded-full bg-blue-500 shrink-0"></span>
              </div>
              <p v-if="n.body" class="text-[12px] text-gray-500 dark:text-gray-400 mt-0.5 line-clamp-2">{{ n.body }}</p>
              <span class="text-[11px] text-gray-400 dark:text-gray-500 mt-1 block">{{ relativeTime(n.created_at) }}</span>
            </div>

            <button
              @click.stop="dismiss(n.id)"
              class="absolute top-2.5 end-2.5 opacity-0 group-hover:opacity-100 text-gray-300 hover:text-gray-600 dark:text-gray-600 dark:hover:text-gray-300 p-0.5 rounded transition-opacity"
              title="Dismiss"
            >
              <UIcon name="i-heroicons-x-mark-20-solid" class="w-4 h-4" />
            </button>
          </li>
        </ul>
      </div>
    </div>
  </UModal>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import type { BowNotification } from '~/composables/useNotifications'

const router = useRouter()
const { isOpen, items, unread, loading, error, fetchItems, markRead, markAllRead, dismiss } = useNotifications()

// All / Unread filter (client-side; the list is already fully loaded).
const FILTERS = [
  { value: 'all', label: 'All' },
  { value: 'unread', label: 'Unread' },
] as const
const filter = ref<'all' | 'unread'>('all')
const visibleItems = computed(() =>
  filter.value === 'unread' ? items.value.filter(n => !n.read) : items.value
)

// Severity → accent (icon bubble).
function sevBg(n: BowNotification): string {
  if (n.severity === 'error') return 'bg-red-50 dark:bg-red-500/10'
  if (n.severity === 'warning') return 'bg-amber-50 dark:bg-amber-500/10'
  return 'bg-blue-50 dark:bg-blue-500/10'
}
function sevText(n: BowNotification): string {
  if (n.severity === 'error') return 'text-red-500'
  if (n.severity === 'warning') return 'text-amber-500'
  return 'text-blue-500'
}

// Icon by type, falling back to source.
const TYPE_ICONS: Record<string, string> = {
  low_confidence: 'i-heroicons-arrow-trending-down',
  schema_changed: 'i-heroicons-table-cells',
  slow_query: 'i-heroicons-clock',
  query_error: 'i-heroicons-exclamation-triangle',
  instruction_suggestion: 'i-heroicons-sparkles',
  share_conversation: 'i-heroicons-chat-bubble-left-right',
  share_artifact: 'i-heroicons-chart-bar-square',
  agent_access: 'i-heroicons-user-plus',
  scheduled_run: 'i-heroicons-clock',
  scheduled_run_failed: 'i-heroicons-exclamation-triangle',
}
const SOURCE_ICONS: Record<string, string> = {
  review: 'i-heroicons-bell-alert',
  share: 'i-heroicons-share',
  schedule: 'i-heroicons-clock',
  report_tool: 'i-heroicons-sparkles',
}
function iconFor(n: BowNotification): string {
  return TYPE_ICONS[n.type] || SOURCE_ICONS[n.source] || 'i-heroicons-bell'
}

const { format: formatTs, toDate } = useFormatDate()

function relativeTime(iso?: string): string {
  if (!iso) return ''
  // toDate treats marker-less timestamps as UTC, so the "ago" math isn't thrown
  // off by the viewer's offset; the >7d fallback renders in the org timezone.
  const then = toDate(iso).getTime()
  if (Number.isNaN(then)) return ''
  const s = Math.max(0, Math.floor((Date.now() - then) / 1000))
  if (s < 60) return 'just now'
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  const d = Math.floor(h / 24)
  if (d < 7) return `${d}d ago`
  return formatTs(iso, { month: 'short', day: 'numeric' })
}

function onRowClick(n: BowNotification) {
  if (!n.read) markRead(n.id, true)
  if (n.link) {
    isOpen.value = false
    router.push(n.link)
  }
}

// Refetch the list each time the modal opens (badge count stays live via polling).
watch(isOpen, (open) => {
  if (open) {
    filter.value = 'all'
    fetchItems()
  }
})
</script>
