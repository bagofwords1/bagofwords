<template>
  <div data-testid="audit-streams">
    <div class="flex items-start justify-between gap-4 mb-3">
      <div class="min-w-0">
        <h3 class="text-sm font-medium text-gray-900 dark:text-white">{{ $t('settings.audit.streams.title') }}</h3>
        <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5 max-w-2xl">{{ $t('settings.audit.streams.subtitle') }}</p>
      </div>
      <UButton v-if="canManage" size="xs" icon="i-heroicons-plus" data-testid="stream-add" @click="openNew">
        {{ $t('settings.audit.streams.add') }}
      </UButton>
    </div>

    <p v-if="!canManage" class="text-[11px] text-gray-400 mb-2">{{ $t('settings.audit.streams.manageRequired') }}</p>

    <div v-if="loading && !streams.length" class="py-8 text-center">
      <div class="inline-block w-4 h-4 border-2 border-gray-200 dark:border-gray-700 border-t-gray-500 rounded-full animate-spin"></div>
    </div>
    <div v-else-if="error" class="py-6 text-center text-xs text-red-500">{{ error }}</div>

    <div v-else-if="!streams.length" class="rounded border border-dashed border-gray-200 dark:border-gray-700 py-10 px-6 text-center">
      <UIcon name="i-heroicons-arrow-up-tray" class="w-6 h-6 text-gray-300 mx-auto mb-2" />
      <p class="text-sm text-gray-700 dark:text-gray-200">{{ $t('settings.audit.streams.empty') }}</p>
      <p class="text-xs text-gray-400 mt-1 max-w-md mx-auto">{{ $t('settings.audit.streams.emptyHint') }}</p>
    </div>

    <div v-else class="border border-gray-200 dark:border-gray-700 rounded divide-y divide-gray-100 dark:divide-gray-800">
      <div v-for="s in streams" :key="s.id" data-testid="stream-row" class="px-3 py-2.5 text-xs flex items-center gap-3">
        <AuditStreamIcon :destination="s.destination" size="md" />
        <div class="flex-1 min-w-0">
          <div class="flex items-center gap-2 min-w-0">
            <span class="text-sm text-gray-900 dark:text-white truncate">{{ s.name }}</span>
            <span data-testid="stream-state" class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium shrink-0" :class="stateClass(s.state)">
              <span class="w-1.5 h-1.5 rounded-full" :class="dotClass(s.state)"></span>
              {{ $t(`settings.audit.streams.state.${s.state}`) }}
            </span>
          </div>
          <div class="flex flex-wrap items-center gap-x-3 gap-y-0.5 mt-0.5 text-gray-500 dark:text-gray-400">
            <span>{{ $t(`settings.audit.streams.destinations.${s.destination}`) }}</span>
            <span>{{ $t('settings.audit.streams.delivered', { n: s.delivered_count.toLocaleString() }) }}</span>
            <span v-if="s.status.pending" :class="s.state === 'active' ? '' : 'text-amber-600'">
              {{ $t('settings.audit.streams.pending', { n: s.status.pending.toLocaleString() }) }}
              <template v-if="s.status.lag_seconds > 60"> · {{ $t('settings.audit.streams.lag', { time: duration(s.status.lag_seconds) }) }}</template>
            </span>
            <span>{{ $t('settings.audit.streams.lastDelivered', { time: s.last_delivered_at ? relative(s.last_delivered_at) : $t('settings.audit.streams.never') }) }}</span>
            <span v-if="s.state === 'active' && s.next_attempt_at && s.consecutive_failures" class="text-amber-600">
              {{ $t('settings.audit.streams.nextAttempt', { time: relative(s.next_attempt_at) }) }}
            </span>
          </div>
          <div v-if="s.last_error" class="mt-1 text-red-600 dark:text-red-400 truncate" :title="s.last_error" data-testid="stream-last-error">
            {{ $t('settings.audit.streams.lastError') }}: {{ s.last_error }}
          </div>
        </div>
        <div v-if="canManage" class="flex items-center gap-1 shrink-0">
          <UButton
            v-if="s.state === 'active'"
            size="2xs" color="gray" variant="ghost" icon="i-heroicons-pause"
            data-testid="stream-pause" :loading="busy === s.id" @click="setState(s, 'inactive')"
          >{{ $t('settings.audit.streams.pause') }}</UButton>
          <UButton
            v-else
            size="2xs" color="primary" variant="soft" icon="i-heroicons-play"
            data-testid="stream-resume" :loading="busy === s.id" @click="setState(s, 'active')"
          >{{ $t('settings.audit.streams.resume') }}</UButton>
          <UButton size="2xs" color="gray" variant="ghost" icon="i-heroicons-pencil-square" data-testid="stream-edit" :aria-label="$t('settings.audit.streams.edit')" @click="openEdit(s)" />
          <UButton size="2xs" color="red" variant="ghost" icon="i-heroicons-trash" data-testid="stream-delete" :aria-label="$t('settings.audit.streams.delete')" @click="remove(s)" />
        </div>
      </div>
    </div>

    <AuditStreamForm v-model="formOpen" :stream="editing" :specs="destinations" @saved="onSaved" />
  </div>
</template>

<script setup lang="ts">
import AuditStreamForm from '~/components/audit/AuditStreamForm.vue'
import AuditStreamIcon from '~/components/audit/AuditStreamIcon.vue'
import { useAuditStreams, type AuditStream, type StreamState } from '~/ee/composables/useAuditStreams'

const { t, locale } = useI18n({ useScope: 'global' })
const { streams, destinations, loading, error, fetchStreams, fetchDestinations, updateStream, deleteStream, getErrorMessage } = useAuditStreams()
const toast = useToast()
const canManage = computed(() => useCan('manage_settings'))

const formOpen = ref(false)
const editing = ref<AuditStream | null>(null)
const busy = ref<string | null>(null)
let timer: ReturnType<typeof setInterval> | null = null

onMounted(async () => {
  await Promise.all([fetchDestinations(), fetchStreams()])
  // Delivery runs in the background; keep counters and states current.
  timer = setInterval(() => { if (!formOpen.value) fetchStreams() }, 10000)
})
onUnmounted(() => { if (timer) clearInterval(timer) })

function openNew() {
  editing.value = null
  formOpen.value = true
}
function openEdit(s: AuditStream) {
  editing.value = s
  formOpen.value = true
}
async function onSaved() {
  await fetchStreams()
}
async function setState(s: AuditStream, state: 'active' | 'inactive') {
  busy.value = s.id
  try {
    await updateStream(s.id, { state })
    await fetchStreams()
  } catch (e) {
    toast.add({ title: getErrorMessage(e), color: 'red' })
  } finally {
    busy.value = null
  }
}
async function remove(s: AuditStream) {
  if (!confirm(t('settings.audit.streams.deleteConfirm', { name: s.name }))) return
  try {
    await deleteStream(s.id)
    await fetchStreams()
  } catch (e) {
    toast.add({ title: getErrorMessage(e), color: 'red' })
  }
}

const stateClass = (s: StreamState) => ({
  active: 'bg-green-50 text-green-700 dark:bg-green-950 dark:text-green-300',
  inactive: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-300',
  error: 'bg-red-50 text-red-700 dark:bg-red-950 dark:text-red-300',
  invalid: 'bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300',
}[s])
const dotClass = (s: StreamState) => ({ active: 'bg-green-500', inactive: 'bg-gray-400', error: 'bg-red-500', invalid: 'bg-amber-500' }[s])

const toDate = (s: string) => new Date(/[zZ]|[+-]\d\d:\d\d$/.test(s) ? s : s + 'Z')
function relative(iso: string) {
  const diff = (toDate(iso).getTime() - Date.now()) / 1000
  const rtf = new Intl.RelativeTimeFormat(String(locale.value), { numeric: 'auto' })
  const abs = Math.abs(diff)
  if (abs < 60) return rtf.format(Math.round(diff), 'second')
  if (abs < 3600) return rtf.format(Math.round(diff / 60), 'minute')
  if (abs < 86400) return rtf.format(Math.round(diff / 3600), 'hour')
  return rtf.format(Math.round(diff / 86400), 'day')
}
function duration(sec: number) {
  const nf = (n: number, unit: string) => new Intl.NumberFormat(String(locale.value), { style: 'unit', unit, unitDisplay: 'narrow' }).format(n)
  if (sec < 3600) return nf(Math.round(sec / 60), 'minute')
  if (sec < 86400) return nf(Math.round(sec / 3600), 'hour')
  return nf(Math.round(sec / 86400), 'day')
}
</script>
