<template>
  <!-- "Since you were here": what was prepared or checked while the user was
       away. Pull-only — nothing here ever notifies. Hidden when empty. -->
  <div
    v-if="items.length"
    class="w-full md:w-4/5 mx-auto mt-4 text-start rounded-lg border border-gray-200 dark:border-gray-800 bg-white/90 dark:bg-gray-900/90 backdrop-blur-sm relative z-10"
    data-testid="briefing-card"
  >
    <div class="flex items-center justify-between px-3 pt-2.5 pb-1">
      <div class="flex items-center gap-1.5 text-xs font-medium text-gray-500 dark:text-gray-400">
        <UIcon name="i-heroicons-moon" class="w-3.5 h-3.5 text-indigo-400" />
        {{ $t('briefing.title') }}
      </div>
      <button
        type="button"
        class="text-[11px] text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
        data-testid="briefing-dismiss"
        @click="dismissAll"
      >{{ $t('briefing.gotIt') }}</button>
    </div>

    <ul class="px-1.5 pb-1.5">
      <li
        v-for="item in items"
        :key="`${item.kind}:${item.id}`"
        class="group flex items-start gap-2 rounded-md px-1.5 py-1.5 hover:bg-gray-50 dark:hover:bg-gray-800/60"
        :data-testid="`briefing-item-${item.kind}`"
      >
        <UIcon :name="iconFor(item)" class="w-4 h-4 mt-0.5 shrink-0" :class="iconColor(item)" />
        <div class="min-w-0 flex-1">
          <div class="text-sm text-gray-800 dark:text-gray-200 leading-snug">
            <template v-if="item.kind === 'habit'">
              {{ $t('briefing.habit', { intent: item.text, when: habitWhen(item) }) }}
            </template>
            <template v-else-if="item.kind === 'event'">
              <span class="font-medium">{{ item.text }}</span>
              <span class="text-gray-500 dark:text-gray-400"> · {{ fmtDay(item.when) }}</span>
              <span v-if="item.prepared" class="text-gray-500 dark:text-gray-400">
                — {{ $t('briefing.eventPrepared', { report: item.prepared.report_title }) }}
              </span>
            </template>
            <template v-else-if="item.kind === 'checkin'">
              <span v-if="item.status === 'sent' && item.text">{{ item.text }}</span>
              <span v-else>{{ $t('briefing.checkinQuiet') }}</span>
            </template>
            <template v-else>{{ item.text }}</template>
          </div>
          <div class="flex items-center gap-2 mt-0.5 text-[11px] text-gray-400 min-w-0">
            <NuxtLink
              v-if="item.report_id && item.report_title"
              :to="`/reports/${item.report_id}`"
              class="truncate max-w-[240px] hover:text-blue-600 dark:hover:text-blue-400"
              @click="markSeen"
            >{{ item.report_title }}</NuxtLink>
            <span v-if="item.why" class="truncate" :title="item.why">· {{ item.kind === 'thread' ? $t('briefing.waitingOn', { what: item.why }) : $t('briefing.why', { why: item.why }) }}</span>
          </div>
          <div v-if="item.kind === 'habit'" class="flex items-center gap-2 mt-1.5">
            <UButton size="2xs" color="blue" :loading="busy === item.id" data-testid="briefing-habit-accept" @click="acceptHabit(item)">
              {{ $t('briefing.habitYes') }}
            </UButton>
            <UButton size="2xs" color="gray" variant="ghost" :disabled="busy === item.id" data-testid="briefing-habit-decline" @click="declineHabit(item)">
              {{ $t('briefing.habitNo') }}
            </UButton>
          </div>
        </div>
        <UTooltip v-if="item.kind !== 'habit'" :text="$t('briefing.notUseful')">
          <button
            type="button"
            class="opacity-0 group-hover:opacity-100 focus:opacity-100 text-gray-300 hover:text-gray-500 dark:hover:text-gray-300"
            :aria-label="$t('briefing.notUseful')"
            data-testid="briefing-not-useful"
            @click="notUseful(item)"
          >
            <UIcon name="i-heroicons-x-mark" class="w-3.5 h-3.5" />
          </button>
        </UTooltip>
      </li>
    </ul>
  </div>
</template>

<script setup lang="ts">
type BriefingItem = {
  kind: 'checkin' | 'thread' | 'event' | 'habit'
  id: string
  report_id?: string
  report_title?: string
  text?: string | null
  why?: string | null
  when?: string | null
  status?: string
  cadence?: string
  time?: string
  prepared?: { report_id: string; report_title: string; due_at: string } | null
}

const { t, locale } = useI18n()
const toast = useToast()
const items = ref<BriefingItem[]>([])
const busy = ref<string | null>(null)

async function load() {
  try {
    const res = await useMyFetch('/users/me/briefing')
    if (res.status.value === 'success' && res.data.value) items.value = ((res.data.value as any).items || []) as BriefingItem[]
  } catch {
    // non-fatal: the card simply doesn't show
  }
}

function iconFor(item: BriefingItem) {
  return {
    checkin: item.status === 'sent' ? 'i-heroicons-bell-alert' : 'i-heroicons-check-circle',
    thread: 'i-heroicons-arrow-uturn-right',
    event: 'i-heroicons-calendar-days',
    habit: 'i-heroicons-arrow-path',
  }[item.kind]
}

function iconColor(item: BriefingItem) {
  if (item.kind === 'checkin' && item.status === 'sent') return 'text-blue-500'
  if (item.kind === 'event') return 'text-amber-500'
  return 'text-gray-400'
}

function fmtDay(iso?: string | null) {
  if (!iso) return ''
  const d = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z')
  return d.toLocaleDateString(locale.value, { weekday: 'short', month: 'short', day: 'numeric' })
}

const WEEKDAY_INDEX: Record<string, number> = { sun: 0, mon: 1, tue: 2, wed: 3, thu: 4, fri: 5, sat: 6 }

function habitWhen(item: BriefingItem) {
  const time = item.time || '09:00'
  if (!item.cadence || item.cadence === 'daily') return t('briefing.everyDay', { time })
  const idx = WEEKDAY_INDEX[item.cadence.split(':')[1] || 'mon'] ?? 1
  // 2023-01-01 was a Sunday: a fixed date gives a localized weekday name.
  const day = new Date(Date.UTC(2023, 0, 1 + idx)).toLocaleDateString(locale.value, { weekday: 'long', timeZone: 'UTC' })
  return t('briefing.everyWeekday', { day, time })
}

function drop(item: BriefingItem) {
  items.value = items.value.filter(i => !(i.kind === item.kind && i.id === item.id))
}

async function markSeen() {
  try { await useMyFetch('/users/me/briefing/seen', { method: 'POST' }) } catch { /* non-fatal */ }
}

async function dismissAll() {
  items.value = []
  await markSeen()
}

async function notUseful(item: BriefingItem) {
  drop(item)
  try {
    await useMyFetch(`/users/me/briefing/items/${item.kind}/${item.id}/feedback`, {
      method: 'POST', body: { useful: false },
    })
  } catch { /* non-fatal */ }
}

async function acceptHabit(item: BriefingItem) {
  busy.value = item.id
  try {
    const res = await useMyFetch(`/users/me/habit_offers/${item.id}/accept`, { method: 'POST' })
    if (res.status.value !== 'success') throw new Error()
    drop(item)
    toast.add({ title: t('briefing.habitScheduled'), color: 'green' })
  } catch {
    toast.add({ title: t('briefing.habitFailed'), color: 'red' })
  } finally {
    busy.value = null
  }
}

async function declineHabit(item: BriefingItem) {
  busy.value = item.id
  try {
    await useMyFetch(`/users/me/habit_offers/${item.id}/decline`, { method: 'POST' })
    drop(item)
  } catch { /* non-fatal */ } finally {
    busy.value = null
  }
}

onMounted(load)
</script>
