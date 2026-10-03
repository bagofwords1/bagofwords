<template><div v-if="canManage" data-testid="connection-schedule">      <!-- Auto-reindex schedule (enterprise `scheduled_reindex`). Admin-only.
           Periodically re-indexes the shared catalog so tables stay fresh
           without a manual reindex. -->
      <details class="py-3 border-t border-gray-100 dark:border-gray-800" data-testid="auto-reindex-settings">
        <summary class="flex items-center gap-2 text-[13px] text-gray-700 dark:text-gray-300 cursor-pointer list-none"><UIcon name="heroicons-chevron-right" class="schedule-chevron w-3.5 h-3.5 shrink-0 text-gray-400" /><span>{{ $t('data.autoReindex') }}</span><span class="ms-auto text-xs text-gray-500 text-end">{{ scheduleSummary }}</span></summary>
        <div class="pt-3">
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-1.5">
            <span class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('data.autoReindex') }}</span>
            <UIcon v-if="!autoReindexLicensed" name="heroicons-lock-closed" class="w-3 h-3 text-gray-400 dark:text-gray-500" />
          </div>
          <UToggle
            :model-value="autoReindexEnabled"
            :disabled="!autoReindexLicensed || savingAutoReindex"
            size="sm"
            @update:model-value="onToggleAutoReindex"
          />
        </div>
        <p class="text-[11px] text-gray-400 dark:text-gray-500 mt-1">
          {{ autoReindexLicensed ? $t('data.autoReindexHint') : $t('data.autoReindexEnterprise') }}
        </p>

        <!-- Schedule picker — either a recurring interval OR a fixed daily time.
             Only when enabled & licensed. -->
        <div v-if="autoReindexLicensed && autoReindexEnabled" class="mt-2 space-y-2">
          <!-- Mode toggle -->
          <div class="flex items-center justify-between">
            <span class="text-xs text-gray-500 dark:text-gray-400">{{ $t('data.autoReindexSchedule') }}</span>
            <div class="inline-flex rounded-md border border-gray-200 dark:border-gray-800 overflow-hidden text-xs">
              <button
                type="button"
                :disabled="savingAutoReindex"
                :class="reindexMode === 'interval' ? 'bg-blue-50 text-blue-700' : 'bg-white dark:bg-gray-800 text-gray-600 dark:text-gray-400'"
                class="px-2 py-1 disabled:opacity-50"
                @click="setReindexMode('interval')"
              >{{ $t('data.autoReindexModeInterval') }}</button>
              <button
                type="button"
                :disabled="savingAutoReindex"
                :class="reindexMode === 'time' ? 'bg-blue-50 text-blue-700' : 'bg-white dark:bg-gray-800 text-gray-600 dark:text-gray-400'"
                class="px-2 py-1 border-l border-gray-200 dark:border-gray-800 disabled:opacity-50"
                @click="setReindexMode('time')"
              >{{ $t('data.autoReindexModeTime') }}</button>
            </div>
          </div>

          <!-- Interval: number + unit (1 minute minimum) -->
          <div v-if="reindexMode === 'interval'" class="flex items-center justify-between">
            <span class="text-xs text-gray-500 dark:text-gray-400">{{ $t('data.autoReindexEvery') }}</span>
            <div class="flex items-center gap-1">
              <input
                type="number"
                min="1"
                v-model.number="intervalValue"
                :disabled="savingAutoReindex"
                class="w-16 text-xs border border-gray-200 dark:border-gray-700 rounded-md px-2 py-1 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-300 disabled:opacity-50"
                @change="onScheduleChange"
              />
              <select
                v-model="intervalUnit"
                :disabled="savingAutoReindex"
                class="text-xs border border-gray-200 dark:border-gray-700 rounded-md px-2 py-1 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-300 disabled:opacity-50"
                @change="onScheduleChange"
              >
                <option value="minutes">{{ $t('data.unitMinutes') }}</option>
                <option value="hours">{{ $t('data.unitHours') }}</option>
              </select>
            </div>
          </div>

          <!-- Fixed daily time (interpreted in the org timezone) -->
          <div v-else class="flex items-center justify-between">
            <span class="text-xs text-gray-500 dark:text-gray-400">{{ $t('data.autoReindexAt') }}</span>
            <input
              type="time"
              v-model="reindexAtTime"
              :disabled="savingAutoReindex"
              class="text-xs border border-gray-200 dark:border-gray-700 rounded-md px-2 py-1 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-300 disabled:opacity-50"
              @change="onScheduleChange"
            />
          </div>

          <p v-if="reindexScheduleError" class="text-[11px] text-amber-600">{{ reindexScheduleError }}</p>
          <p v-else-if="reindexMode === 'time'" class="text-[11px] text-gray-400 dark:text-gray-500">{{ $t('data.autoReindexTimeHint') }}</p>
        </div>

      </div>
      </details>
        <!-- Last background failure, if any. -->
        <p v-if="autoReindexError && !sameConnectionDiagnostic(autoReindexError, displayedError)" class="text-[11px] text-red-500 mt-1.5 truncate" :title="autoReindexError">
          {{ $t('data.autoReindexLastError') }}: {{ autoReindexError }}
        </p>


<details class="border-t border-gray-100 dark:border-gray-800 pt-3"><summary class="flex items-center gap-2 text-[13px] text-gray-700 dark:text-gray-300 cursor-pointer list-none"><UIcon name="heroicons-chevron-right" class="schedule-chevron w-3.5 h-3.5 shrink-0 text-gray-400" /><span>{{ $t('data.rateLimit') }}</span><span class="ms-auto text-xs text-gray-500">{{ $t(rateLimitEnabled ? 'scheduledPrompt.scheduleOn' : 'scheduledPrompt.scheduleOff') }}</span></summary>      <!-- Per-connection request rate limit (enterprise `connection_rate_limit`).
           Admin-only. Hard-blocks agent queries once a fixed per-window
           threshold is crossed; the budget is shared across all users. -->
      <div v-if="canManage" class="py-3 border-t border-gray-100 dark:border-gray-800">
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-1.5">
            <span class="text-xs font-medium text-gray-700 dark:text-gray-300">{{ $t('data.rateLimit') }}</span>
            <UIcon v-if="!rateLimitLicensed" name="heroicons-lock-closed" class="w-3 h-3 text-gray-400 dark:text-gray-500" />
          </div>
          <UToggle
            :model-value="rateLimitEnabled"
            :disabled="!rateLimitLicensed || savingRateLimit"
            size="sm"
            @update:model-value="onToggleRateLimit"
          />
        </div>
        <p class="text-[11px] text-gray-400 dark:text-gray-500 mt-1">
          {{ rateLimitLicensed ? $t('data.rateLimitHint') : $t('data.rateLimitEnterprise') }}
        </p>

        <!-- Per-window caps. Blank / 0 means "no limit" for that window. -->
        <div v-if="rateLimitLicensed && rateLimitEnabled" class="mt-2 space-y-2">
          <div
            v-for="w in rateLimitWindows"
            :key="w.key"
            class="flex items-center justify-between"
          >
            <span class="text-xs text-gray-500 dark:text-gray-400">{{ w.label }}</span>
            <div class="flex items-center gap-1">
              <input
                type="number"
                min="0"
                v-model.number="w.model.value"
                :disabled="savingRateLimit"
                :placeholder="$t('data.rateLimitNoLimit')"
                class="w-24 text-xs border border-gray-200 dark:border-gray-700 rounded-md px-2 py-1 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-300 disabled:opacity-50"
                @change="onRateLimitChange"
              />
              <span class="text-[11px] text-gray-400 dark:text-gray-500 w-14">{{ w.unit }}</span>
            </div>
          </div>
          <p v-if="rateLimitError" class="text-[11px] text-red-500">{{ rateLimitError }}</p>
        </div>
      </div>

</details></div></template>
<script setup lang="ts">
import { sameConnectionDiagnostic } from '~/utils/connectionDiagnostics'
import { useEnterprise } from '~/ee/composables/useEnterprise'
import { useCan } from '~/composables/usePermissions'
const props = defineProps<{ connection: any; displayedError?: string | null }>()
const { t, locale } = useI18n()
const toast = useToast()
const canManage = computed(() => useCan('manage_connection', { type: 'connection', id: props.connection?.id }))
// ── Auto-reindex schedule (enterprise `scheduled_reindex`) ──────────────────
const { hasFeature } = useEnterprise()
const autoReindexLicensed = computed(() => hasFeature('scheduled_reindex'))
const autoReindexEnabled = ref(true)
const autoReindexError = ref<string | null>(null)
const savingAutoReindex = ref(false)

// Schedule: either a recurring interval (value + unit) OR a fixed daily time.
const MIN_INTERVAL_MINUTES = 1
const reindexMode = ref<'interval' | 'time'>('interval')
const intervalValue = ref<number>(12)
const intervalUnit = ref<'minutes' | 'hours'>('hours')
const reindexAtTime = ref<string>('02:00')
const reindexScheduleError = ref<string | null>(null)
const scheduleSummary = computed(() => {
  if (!autoReindexEnabled.value) return t('scheduledPrompt.scheduleOff')
  if (reindexMode.value === 'time') return reindexAtTime.value
  if (intervalUnit.value === 'hours') return t('data.everyNHours', { n: intervalValue.value })
  return new Intl.NumberFormat(locale.value, { style: 'unit', unit: 'minute', unitDisplay: 'short' }).format(intervalValue.value)
})


// Resolve the interval inputs to minutes, enforcing the minimum-interval floor.
function resolvedIntervalMinutes(): number {
  const raw = Number(intervalValue.value) || 0
  const mins = intervalUnit.value === 'hours' ? raw * 60 : raw
  return Math.max(MIN_INTERVAL_MINUTES, Math.round(mins))
}

function applyAutoReindexConfig(d: any) {
  // Schedule fields only exist on the admin detail payload.
  if (!d || !canManage.value) return
  autoReindexEnabled.value = d.auto_reindex_enabled !== false
  autoReindexError.value = d.last_reindex_error || null
  reindexMode.value = d.reindex_schedule_mode === 'time' ? 'time' : 'interval'
  reindexAtTime.value = d.reindex_at_time || '02:00'
  // Prefer the minutes column; fall back to the legacy hours field. Present
  // whole-hour intervals in hours, otherwise minutes.
  const mins = d.reindex_interval_minutes
    ?? (d.reindex_interval_hours ? d.reindex_interval_hours * 60 : null)
    ?? (12 * 60)
  if (mins % 60 === 0) {
    intervalUnit.value = 'hours'
    intervalValue.value = mins / 60
  } else {
    intervalUnit.value = 'minutes'
    intervalValue.value = mins
  }
}

async function saveAutoReindex() {
  if (!props.connection?.id || savingAutoReindex.value) return
  savingAutoReindex.value = true
  try {
    const body: Record<string, any> = {
      auto_reindex_enabled: autoReindexEnabled.value,
      reindex_schedule_mode: reindexMode.value,
    }
    if (reindexMode.value === 'time') {
      body.reindex_at_time = reindexAtTime.value
    } else {
      body.reindex_interval_minutes = resolvedIntervalMinutes()
    }
    const { error } = await useMyFetch(`/connections/${props.connection.id}`, {
      method: 'PUT',
      body,
    })
    if (error.value) {
      toast.add({
        title: t('data.autoReindexSaveFailed'),
        description: (error.value as any)?.data?.detail || (error.value as any)?.message,
        color: 'red',
      })
    }
  } finally {
    savingAutoReindex.value = false
  }
}

function onToggleAutoReindex(val: boolean) {
  autoReindexEnabled.value = val
  saveAutoReindex()
}

function setReindexMode(mode: 'interval' | 'time') {
  if (reindexMode.value === mode) return
  reindexMode.value = mode
  onScheduleChange()
}

function onScheduleChange() {
  reindexScheduleError.value = null
  if (reindexMode.value === 'interval') {
    const mins = resolvedIntervalMinutes()
    // Reflect the enforced floor back into the inputs so the UI is honest.
    if (mins === MIN_INTERVAL_MINUTES && resolvedRawMinutes() < MIN_INTERVAL_MINUTES) {
      reindexScheduleError.value = t('data.autoReindexMinInterval', { n: MIN_INTERVAL_MINUTES })
      intervalUnit.value = 'minutes'
      intervalValue.value = MIN_INTERVAL_MINUTES
    }
  } else if (!reindexAtTime.value) {
    reindexAtTime.value = '02:00'
  }
  saveAutoReindex()
}

function resolvedRawMinutes(): number {
  const raw = Number(intervalValue.value) || 0
  return intervalUnit.value === 'hours' ? raw * 60 : raw
}

// ── Per-connection request rate limit (enterprise `connection_rate_limit`) ───
const rateLimitLicensed = computed(() => hasFeature('connection_rate_limit'))
const rateLimitEnabled = ref(false)
const rateLimitPerMinute = ref<number | null>(null)
const rateLimitPerHour = ref<number | null>(null)
const rateLimitPerDay = ref<number | null>(null)
const rateLimitError = ref<string | null>(null)
const savingRateLimit = ref(false)

// Rendered rows; each binds to one window ref.
const rateLimitWindows = computed(() => [
  { key: 'minute', label: t('data.rateLimitPerMinute'), unit: t('data.rateLimitReqMin'), model: rateLimitPerMinute },
  { key: 'hour', label: t('data.rateLimitPerHour'), unit: t('data.rateLimitReqHour'), model: rateLimitPerHour },
  { key: 'day', label: t('data.rateLimitPerDay'), unit: t('data.rateLimitReqDay'), model: rateLimitPerDay },
])

// Normalize an input value to a non-negative int or null (blank / 0 = no limit).
function normalizeRateLimit(v: number | null): number | null {
  const n = Number(v)
  if (!Number.isFinite(n) || n <= 0) return null
  return Math.floor(n)
}

function applyRateLimitConfig(d: any) {
  if (!d || !canManage.value) return
  rateLimitEnabled.value = d.rate_limit_enabled === true
  rateLimitPerMinute.value = d.rate_limit_per_minute ?? null
  rateLimitPerHour.value = d.rate_limit_per_hour ?? null
  rateLimitPerDay.value = d.rate_limit_per_day ?? null
}

async function saveRateLimit() {
  if (!props.connection?.id || savingRateLimit.value) return
  savingRateLimit.value = true
  rateLimitError.value = null
  try {
    const body: Record<string, any> = {
      rate_limit_enabled: rateLimitEnabled.value,
      // Send 0 for "no limit" so a cleared field persists (the API treats
      // 0/null identically as unlimited).
      rate_limit_per_minute: normalizeRateLimit(rateLimitPerMinute.value) ?? 0,
      rate_limit_per_hour: normalizeRateLimit(rateLimitPerHour.value) ?? 0,
      rate_limit_per_day: normalizeRateLimit(rateLimitPerDay.value) ?? 0,
    }
    const { error } = await useMyFetch(`/connections/${props.connection.id}`, {
      method: 'PUT',
      body,
    })
    if (error.value) {
      rateLimitError.value = (error.value as any)?.data?.detail || (error.value as any)?.message || t('data.rateLimitSaveFailed')
      toast.add({
        title: t('data.rateLimitSaveFailed'),
        description: rateLimitError.value,
        color: 'red',
      })
    }
  } finally {
    savingRateLimit.value = false
  }
}

function onToggleRateLimit(val: boolean) {
  rateLimitEnabled.value = val
  saveRateLimit()
}

function onRateLimitChange() {
  rateLimitError.value = null
  // Reflect the normalization back into the inputs so the UI is honest.
  rateLimitPerMinute.value = normalizeRateLimit(rateLimitPerMinute.value)
  rateLimitPerHour.value = normalizeRateLimit(rateLimitPerHour.value)
  rateLimitPerDay.value = normalizeRateLimit(rateLimitPerDay.value)
  saveRateLimit()
}


watch(() => [props.connection?.id, canManage.value], async ([id]) => {
  if (!id || !canManage.value) return
  const { data } = await useMyFetch(`/connections/${id}`, { method: 'GET' })
  if (props.connection?.id !== id || !data.value) return
  applyAutoReindexConfig(data.value)
  applyRateLimitConfig(data.value)
}, { immediate: true })
</script>
<style scoped>
summary::-webkit-details-marker { display: none; }
details[open] > summary .schedule-chevron { transform: rotate(90deg); }
</style>
