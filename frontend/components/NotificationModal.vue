<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-2xl', container: 'items-start', margin: 'sm:mt-[10vh]' }">
    <div class="bg-white dark:bg-gray-900 rounded-lg overflow-hidden flex flex-col max-h-[78vh]">
      <!-- Header -->
      <div class="shrink-0 flex items-center justify-between px-4 py-3 border-b border-gray-100 dark:border-gray-800">
        <div class="flex items-center gap-2">
          <h2 class="text-sm font-semibold text-gray-900 dark:text-gray-100">{{ $t('notifications.title') }}</h2>
          <span
            v-if="unread"
            class="min-w-[18px] h-[18px] px-1 rounded-full bg-blue-600 text-white text-2xs font-semibold leading-none flex items-center justify-center"
          >{{ unread > 99 ? '99+' : unread }}</span>
        </div>
        <div class="flex items-center gap-1">
          <!-- Clear handled: removes every handled row from MY inbox at once
               (personal, like the per-row ✕). -->
          <button
            v-if="hasHandled"
            @click="dismissResolved()"
            data-testid="notifications-clear-handled"
            class="text-xs text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200 px-2 py-1 rounded-md hover:bg-gray-100 dark:hover:bg-gray-800/70 transition-colors"
          >{{ $t('notifications.clearHandled') }}</button>
          <button
            v-if="unread"
            @click="markAllRead()"
            class="text-xs text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200 px-2 py-1 rounded-md hover:bg-gray-100 dark:hover:bg-gray-800/70 transition-colors"
          >{{ $t('notifications.markAllRead') }}</button>
          <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" size="xs" @click="isOpen = false" />
        </div>
      </div>

      <!-- Filters: read state + where it came from, as underline tabs. The last
           choice is remembered. shrink-0: overflow-x makes a flex child
           shrinkable, and in a max-height modal a long list would squeeze it. -->
      <div class="shrink-0 flex items-end gap-5 px-4 border-b border-gray-100 dark:border-gray-800 overflow-x-auto" role="tablist">
        <button
          v-for="f in visibleFilters"
          :key="f.value"
          role="tab"
          :aria-selected="filter === f.value"
          :data-testid="`notifications-filter-${f.value}`"
          @click="setFilter(f.value)"
          :class="[
            '-mb-px inline-flex items-center gap-1.5 pt-2.5 pb-2 text-xs whitespace-nowrap border-b-2 transition-colors outline-none focus-visible:text-gray-900 dark:focus-visible:text-white',
            filter === f.value
              ? 'border-gray-900 dark:border-gray-100 text-gray-900 dark:text-white font-semibold'
              : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200'
          ]"
        >
          {{ $t(f.labelKey) }}
          <span
            v-if="f.value === 'unread' && unread"
            class="min-w-[16px] h-4 px-1 rounded-full bg-blue-50 text-blue-600 dark:bg-blue-500/15 dark:text-blue-300 text-2xs font-semibold leading-none inline-flex items-center justify-center tabular-nums"
          >{{ unread > 99 ? '99+' : unread }}</span>
        </button>
      </div>

      <!-- List -->
      <div class="flex-1 overflow-y-auto min-h-[220px]">
        <div v-if="loading" class="py-14 flex justify-center">
          <Spinner class="w-5 h-5 text-ink-muted animate-spin" />
        </div>

        <div v-else-if="error" class="py-16 flex flex-col items-center text-center gap-2 px-6">
          <div class="flex items-center justify-center w-12 h-12 rounded-full bg-red-50 dark:bg-red-500/10">
            <UIcon name="i-heroicons-exclamation-triangle" class="w-6 h-6 text-red-400 dark:text-red-500" />
          </div>
          <p class="text-sm font-medium text-gray-700 dark:text-gray-300">{{ $t('notifications.loadErrorTitle') }}</p>
          <p class="text-xs text-ink-muted">{{ $t('notifications.loadErrorBody') }}</p>
          <button
            @click="fetchItems()"
            class="mt-1 text-xs text-blue-600 dark:text-blue-400 hover:underline px-2 py-1 rounded-md"
          >{{ $t('notifications.tryAgain') }}</button>
        </div>

        <div v-else-if="!visibleItems.length" class="py-16 flex flex-col items-center text-center gap-2 px-6">
          <div class="flex items-center justify-center w-12 h-12 rounded-full bg-gray-50 dark:bg-gray-800">
            <UIcon name="i-heroicons-bell-slash" class="w-6 h-6 text-gray-300 dark:text-gray-600" />
          </div>
          <template v-if="filter === 'unread' && items.length">
            <p class="text-sm font-medium text-gray-700 dark:text-gray-300">{{ $t('notifications.noUnreadTitle') }}</p>
            <p class="text-xs text-ink-muted">{{ $t('notifications.noUnreadBody') }}</p>
          </template>
          <template v-else>
            <p class="text-sm font-medium text-gray-700 dark:text-gray-300">{{ $t('notifications.emptyTitle') }}</p>
            <p class="text-xs text-ink-muted">{{ $t('notifications.emptyBody') }}</p>
          </template>
        </div>

        <ul v-else class="divide-y divide-gray-100 dark:divide-gray-800">
          <li
            v-for="n in visibleItems"
            :key="n.id"
            :data-testid="`notification-row-${n.type}`"
            :class="[
              'group relative flex gap-3 px-4 py-3 transition-colors',
              !n.read && !n.resolved ? 'bg-blue-50/40 dark:bg-blue-500/[0.06]' : '',
              expandedId === n.id ? 'bg-gray-50 dark:bg-gray-800/40' : 'hover:bg-gray-50 dark:hover:bg-gray-800/50',
            ]"
          >
            <div
              v-if="isCheckin(n)"
              class="mt-0.5 flex items-center justify-center w-7 h-7 rounded-full shrink-0 bg-gradient-to-br from-blue-500 to-indigo-500 text-white shadow-sm"
              data-testid="checkin-notification-avatar"
            >
              <UIcon name="i-heroicons-sparkles" class="w-4 h-4" />
            </div>
            <div v-else :class="['mt-0.5 flex items-center justify-center w-7 h-7 rounded-full shrink-0', n.resolved ? 'bg-gray-100 dark:bg-gray-800' : sevBg(n)]">
              <UIcon :name="iconFor(n)" :class="['w-4 h-4', n.resolved ? 'text-ink-muted' : sevText(n)]" />
            </div>

            <div class="min-w-0 flex-1 pe-5" :data-testid="isCheckin(n) ? 'checkin-notification' : undefined">
              <!-- The clickable summary: opens details (review rows) or goes to the target. -->
              <div
                role="button"
                tabindex="0"
                :aria-expanded="isReview(n) ? expandedId === n.id : undefined"
                :class="['outline-none rounded focus-visible:ring-2 focus-visible:ring-blue-500/50', (isReview(n) || linkFor(n)) ? 'cursor-pointer' : '']"
                @click="onRowClick(n)"
                @keydown.enter.prevent="onRowClick(n)"
                @keydown.space.prevent="onRowClick(n)"
              >
                <!-- Agent follow-up: reads like a message from the assistant -->
                <div v-if="isCheckin(n)" class="flex items-center gap-1.5 text-2xs text-gray-500 dark:text-gray-400 mb-0.5">
                  <span class="font-medium text-gray-700 dark:text-gray-300" dir="auto">{{ assistantName }}</span>
                  <span class="text-ink-muted">&middot;</span>
                  <span class="inline-flex items-center gap-0.5 px-1.5 py-px rounded-full bg-blue-50 dark:bg-blue-500/10 text-blue-600 dark:text-blue-300">
                    <UIcon name="i-heroicons-arrow-path-rounded-square" class="w-3 h-3" />
                    {{ $t('notifications.checkin.badge') }}
                  </span>
                </div>
                <div class="flex items-center gap-2 min-w-0">
                  <span v-text-dir
                    :class="[
                      'text-sm truncate',
                      n.resolved ? 'text-gray-500 dark:text-gray-400 font-medium'
                        : !n.read ? 'font-semibold text-gray-900 dark:text-gray-100' : 'font-medium text-gray-700 dark:text-gray-300'
                    ]"
                  >{{ title(n) }}</span>
                  <span v-if="n.resolved" data-testid="notification-resolved-badge" class="shrink-0 inline-flex items-center gap-0.5 text-2xs font-medium px-1.5 py-px rounded bg-green-50 text-green-700 dark:bg-green-500/10 dark:text-green-300">
                    <UIcon name="i-heroicons-check" class="w-3 h-3" />
                    {{ $t('notifications.detail.resolved') }}
                  </span>
                  <span v-else-if="!n.read" class="w-1.5 h-1.5 rounded-full bg-blue-500 shrink-0"></span>
                </div>
                <p
                  v-if="body(n)"
                  :class="['text-xs mt-0.5', n.resolved ? 'text-gray-400 dark:text-gray-500' : 'text-gray-500 dark:text-gray-400',
                           expandedId === n.id ? 'whitespace-pre-line' : (isCheckin(n) ? 'line-clamp-3 whitespace-pre-line' : 'line-clamp-2')]"
                  dir="auto"
                >{{ body(n) }}</p>
                <span v-if="isCheckin(n) && n.link" class="inline-flex items-center gap-0.5 mt-1 text-2xs font-medium text-blue-600 dark:text-blue-400">
                  {{ $t('notifications.checkin.openReport') }}
                  <UIcon name="i-heroicons-arrow-right-20-solid" class="w-3 h-3 rtl-flip" />
                </span>
                <div class="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 mt-1 text-2xs text-ink-muted">
                  <span v-if="n.subject?.agent_name" class="inline-flex items-center gap-0.5" v-text-dir>
                    <UIcon name="i-heroicons-cube" class="w-3 h-3" />{{ n.subject.agent_name }}
                  </span>
                  <span v-if="n.subject?.agent_name" class="text-ink-muted">&middot;</span>
                  <span v-if="n.actor?.name">{{ $t('notifications.detail.by', { name: n.actor.name }) }}</span>
                  <span v-if="n.actor?.name" class="text-ink-muted">&middot;</span>
                  <span>{{ relativeTime(n.updated_at || n.created_at) }}</span>
                  <template v-if="n.resolved && handledText(n.subject?.handled, t)">
                    <span class="text-ink-muted">&middot;</span>
                    <span class="text-green-700 dark:text-green-400" v-text-dir>{{ handledText(n.subject?.handled, t) }}</span>
                  </template>
                  <span v-if="isReview(n)" class="inline-flex items-center gap-0.5 ms-1 text-ink-muted">
                    <UIcon :name="expandedId === n.id ? 'i-heroicons-chevron-up-20-solid' : 'i-heroicons-chevron-down-20-solid'" class="w-3.5 h-3.5" />
                    {{ expandedId === n.id ? $t('notifications.detail.hideDetails') : $t('notifications.detail.showDetails') }}
                  </span>
                </div>
              </div>

              <!-- Details (review rows) -->
              <div v-if="expandedId === n.id" class="mt-2.5 flex flex-col gap-2.5 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 p-3" data-testid="notification-detail">
                <div v-if="n.subject?.connection?.name" class="flex items-center gap-1 text-2xs text-gray-500 dark:text-gray-400">
                  <UIcon name="i-heroicons-circle-stack" class="w-3 h-3" />
                  <span>{{ $t('notifications.detail.connection') }}:</span>
                  <span class="font-medium text-gray-700 dark:text-gray-300" v-text-dir>{{ n.subject.connection.name }}</span>
                </div>
                <SchemaDiffView v-if="n.subject?.diff" :diff="n.subject.diff" />
                <p v-if="n.subject?.reliability_check?.started_at" class="text-2xs text-gray-500 dark:text-gray-400 flex items-start gap-1">
                  <UIcon name="i-heroicons-beaker" class="w-3 h-3 mt-0.5 shrink-0" />
                  {{ $t('notifications.detail.reliabilityStarted', { time: relativeTime(n.subject.reliability_check.started_at) }) }}
                </p>
                <div v-if="n.subject?.history?.length" class="flex flex-col gap-0.5">
                  <span class="text-2xs font-semibold uppercase tracking-wide text-ink-muted">{{ $t('notifications.detail.history') }}</span>
                  <ul class="flex flex-col gap-0.5 text-2xs text-gray-500 dark:text-gray-400">
                    <li v-for="(h, i) in n.subject.history" :key="i" class="flex gap-1.5">
                      <span class="tabular-nums shrink-0">{{ h.at ? relativeTime(h.at) : '' }}</span>
                      <span>&middot;</span>
                      <span v-if="h.counts">{{ $t('notifications.detail.historyEntry', { added: h.counts.added || 0, removed: h.counts.removed || 0, modified: h.counts.modified || 0 }) }}</span>
                      <span v-else dir="auto">{{ h.summary }}</span>
                    </li>
                  </ul>
                </div>
                <!-- Actions: compact, on their own row under a hairline. One soft
                     blue primary (where to act), quiet ghost secondaries with icons. -->
                <div v-if="actionsFor(n).length" class="-mx-3 -mb-3 mt-0.5 px-2 py-1.5 border-t border-gray-100 dark:border-gray-800 flex flex-wrap items-center gap-1">
                  <UButton
                    v-for="a in actionsFor(n)"
                    :key="a.key"
                    :data-testid="`notification-action-${a.key}`"
                    size="2xs"
                    :color="a.primary ? 'blue' : 'gray'"
                    :variant="a.primary ? 'soft' : 'ghost'"
                    :icon="a.primary ? undefined : a.icon"
                    @click.stop="go(n, a.to)"
                  >
                    {{ $t(a.labelKey) }}
                    <template v-if="a.primary" #trailing>
                      <UIcon name="i-heroicons-arrow-right-20-solid" class="w-3 h-3 rtl-flip" />
                    </template>
                  </UButton>
                </div>
              </div>
            </div>

            <button
              @click.stop="dismiss(n.id)"
              class="absolute top-2.5 end-2.5 opacity-0 group-hover:opacity-100 focus-visible:opacity-100 text-ink-subtle hover:text-gray-600 dark:hover:text-gray-300 p-0.5 rounded transition-opacity"
              :title="$t('notifications.dismiss')"
              :aria-label="$t('notifications.dismiss')"
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
import SchemaDiffView from '~/components/notifications/SchemaDiffView.vue'
import type { BowNotification } from '~/composables/useNotifications'
import {
  NOTIFICATION_FILTERS, type NotificationFilter,
  iconFor, isReview, linkFor, actionsFor, displayTitle, displayBody, handledText,
} from '~/utils/notifications'

const router = useRouter()
const { t, te } = useI18n()
const { isOpen, items, unread, loading, error, fetchItems, markRead, markAllRead, dismiss, dismissResolved } = useNotifications()
const hasHandled = computed(() => items.value.some(n => n.resolved))

// ── Filters (client-side; the list is already fully loaded) ──────────────────
// Source tabs only show when the inbox actually has something from that
// source, so a user who only ever gets shares isn't offered empty tabs.
const FILTER_KEY = 'bow.notifications.filter'
const filter = ref<NotificationFilter>('all')
function readStoredFilter(): NotificationFilter {
  try {
    const v = localStorage.getItem(FILTER_KEY)
    if (v && NOTIFICATION_FILTERS.some(f => f.value === v)) return v as NotificationFilter
  } catch { /* storage blocked */ }
  return 'all'
}
function setFilter(v: NotificationFilter) {
  filter.value = v
  try { localStorage.setItem(FILTER_KEY, v) } catch { /* storage blocked */ }
}
const visibleFilters = computed(() => NOTIFICATION_FILTERS.filter(f =>
  !f.sources || f.value === filter.value || items.value.some(n => (f.sources as readonly string[]).includes(n.source)),
))
const visibleItems = computed(() => {
  const f = NOTIFICATION_FILTERS.find(x => x.value === filter.value)
  if (filter.value === 'unread') return items.value.filter(n => !n.read)
  if (f?.sources) return items.value.filter(n => (f.sources as readonly string[]).includes(n.source))
  return items.value
})

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

const { relativeTime } = useRelativeTime()

// Agent check-in follow-ups are presented as a message from the assistant
// (the org's AI analyst name), not as a system alert.
const { data: currentUser } = useAuth()
const { organization } = useOrganization()
function isCheckin(n: BowNotification): boolean {
  return n.source === 'checkin'
}
const assistantName = computed(() => {
  const orgs = (currentUser.value as any)?.organizations || []
  const org = orgs.find((o: any) => o.id === organization.value?.id) || orgs[0]
  return org?.ai_analyst_name || t('notifications.checkin.assistantFallback')
})

const title = (n: BowNotification) => displayTitle(n, t, te)
const body = (n: BowNotification) => displayBody(n, t, te)

// Review rows (schema change, suggestion, quality signal) open their details
// in place; the buttons there go to where the admin acts on it. Everything
// else (shares, runs, connections) goes straight to its target as before.
const expandedId = ref<string | null>(null)
function onRowClick(n: BowNotification) {
  if (!n.read) markRead(n.id, true)
  if (isReview(n)) {
    expandedId.value = expandedId.value === n.id ? null : n.id
    return
  }
  const to = linkFor(n)
  if (to) go(n, to)
}
function go(n: BowNotification, to: string) {
  if (!n.read) markRead(n.id, true)
  isOpen.value = false
  router.push(to)
}

// Refetch the list each time the modal opens (badge count stays live via polling).
watch(isOpen, (open) => {
  if (open) {
    filter.value = readStoredFilter()
    expandedId.value = null
    fetchItems()
  }
})
</script>
