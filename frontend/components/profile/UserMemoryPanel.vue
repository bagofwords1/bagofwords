<template>
  <div class="space-y-3" data-testid="memory-panel">
    <div>
      <h3 class="text-base font-semibold text-gray-900 dark:text-gray-100">{{ $t('profile.memory.title') }}</h3>
      <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('profile.memory.subtitle') }}</p>
      <p class="text-[11px] text-gray-400 dark:text-gray-500 mt-1 flex items-center gap-1 flex-wrap">
        <UIcon name="i-heroicons-information-circle" class="w-3.5 h-3.5 shrink-0" />
        <span>{{ $t('profile.memory.vsInstructions') }}</span>
        <button type="button" class="text-blue-600 dark:text-blue-400 hover:underline" data-testid="memory-open-instructions" @click="emit('open-instructions')">
          {{ $t('profile.memory.instructionsLink') }}
        </button>
      </p>
    </div>

    <div v-if="loading" class="py-6 flex justify-center">
      <Spinner class="w-5 h-5 text-gray-400" />
    </div>

    <template v-else>
      <!-- Search + add -->
      <div class="flex items-center gap-2">
        <UInput
          v-model="query"
          class="flex-1"
          size="sm"
          icon="i-heroicons-magnifying-glass"
          :placeholder="$t('profile.memory.searchPlaceholder')"
          data-testid="memory-search"
        />
        <UButton size="sm" color="gray" variant="solid" icon="i-heroicons-plus" data-testid="memory-add" @click="startAdd">
          {{ $t('profile.memory.add') }}
        </UButton>
      </div>

      <!-- Tag chips -->
      <div v-if="tags.length" class="flex items-center gap-1 flex-wrap" data-testid="memory-tag-filter">
        <button
          v-for="tg in tags.slice(0, 16)"
          :key="tg.tag"
          type="button"
          :class="[
            'px-1.5 py-0.5 rounded text-[11px] border transition-colors',
            activeTag === tg.tag
              ? 'bg-blue-50 border-blue-200 text-blue-700 dark:bg-blue-950 dark:border-blue-800 dark:text-blue-300'
              : 'bg-white border-gray-200 text-gray-600 hover:bg-gray-50 dark:bg-gray-900 dark:border-gray-700 dark:text-gray-400'
          ]"
          @click="activeTag = activeTag === tg.tag ? null : tg.tag"
        >
          <bdi dir="ltr">#{{ tg.tag }}</bdi> <span class="text-gray-400">{{ tg.count }}</span>
        </button>
      </div>

      <!-- Add form -->
      <div v-if="form && form.mode === 'add'" class="rounded-md border border-gray-200 dark:border-gray-800 px-3 py-2">
        <MemoryEntryForm :form="form" :saving="saving" @save="saveForm" @cancel="form = null" />
      </div>

      <!-- The list -->
      <ul class="rounded-md border border-gray-200 dark:border-gray-800 divide-y divide-gray-100 dark:divide-gray-800" data-testid="memory-list">
        <li
          v-for="e in visible"
          :key="e.id"
          class="group px-3 py-2"
          :class="{ 'opacity-60': e.expired }"
          data-testid="memory-entry"
          :data-handle="e.handle"
        >
          <MemoryEntryForm
            v-if="form && form.mode === 'edit' && form.id === e.id"
            :form="form"
            :saving="saving"
            @save="saveForm"
            @cancel="form = null"
          />
          <div v-else class="flex items-start gap-2">
            <div class="flex-1 min-w-0">
              <div class="text-[13px] text-gray-800 dark:text-gray-200 break-words" data-testid="memory-entry-text">
                <bdi>{{ e.text }}</bdi>
              </div>
              <div class="flex items-center gap-1.5 flex-wrap mt-1">
                <span v-if="e.date" class="text-[11px] text-gray-600 dark:text-gray-300 inline-flex items-center gap-1" data-testid="memory-entry-date">
                  <UIcon name="i-heroicons-calendar" class="w-3 h-3" />
                  <bdi>{{ formatDates(e) }}</bdi>
                </span>
                <span v-if="e.expired" class="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500">{{ $t('profile.memory.past') }}</span>
                <button
                  v-for="tg in displayTags(e)"
                  :key="tg"
                  type="button"
                  class="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400 hover:text-blue-600"
                  @click="activeTag = tg"
                ><bdi dir="ltr">#{{ tg }}</bdi></button>
                <!-- Where it came from: only on hover, to keep the list quiet. -->
                <span class="hidden group-hover:inline-flex items-center gap-1 text-[11px] text-gray-400 min-w-0" data-testid="memory-entry-source">
                  <UIcon :name="e.source === 'user' ? 'i-heroicons-user' : e.source === 'dream' ? 'i-heroicons-moon' : 'i-heroicons-sparkles'" class="w-3 h-3 shrink-0" />
                  <NuxtLink
                    v-if="e.evidence?.report_link"
                    :to="e.evidence.report_link"
                    class="hover:text-blue-600 dark:hover:text-blue-400 truncate max-w-[220px]"
                    :title="e.evidence.quote ? `“${e.evidence.quote}”` : undefined"
                  ><bdi>{{ $t('profile.memory.fromReport', { title: e.evidence.report_title || $t('profile.memory.untitledReport') }) }}</bdi></NuxtLink>
                  <span v-else>{{ sourceLabel(e.source) }}</span>
                </span>
              </div>
            </div>
            <div class="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
              <UButton size="2xs" color="gray" variant="ghost" icon="i-heroicons-pencil-square" :title="$t('common.edit')" data-testid="memory-edit" @click="startEdit(e)" />
              <UButton size="2xs" color="red" variant="ghost" icon="i-heroicons-trash" :title="$t('common.delete')" data-testid="memory-delete" @click="forget(e)" />
            </div>
          </div>
        </li>
        <li v-if="!visible.length" class="px-3 py-3 text-xs text-gray-400 dark:text-gray-500 italic" data-testid="memory-empty">
          {{ entries.length ? $t('profile.memory.noMatches') : $t('profile.memory.empty') }}
        </li>
      </ul>

      <div class="flex items-center justify-between">
        <span class="text-[11px] text-gray-400 dark:text-gray-500">{{ $t('profile.memory.count', { count: total, cap }) }}</span>
        <UButton
          v-if="total > 0"
          size="2xs"
          color="red"
          variant="ghost"
          icon="i-heroicons-trash"
          data-testid="memory-forget-all"
          @click="confirmForgetAll = true"
        >
          {{ $t('profile.memory.forgetAll') }}
        </UButton>
      </div>
    </template>

    <!-- Forget everything confirmation -->
    <UModal v-model="confirmForgetAll">
      <div class="p-5 space-y-3" data-testid="memory-forget-all-confirm">
        <h4 class="text-sm font-semibold text-gray-900 dark:text-gray-100">{{ $t('profile.memory.forgetAllTitle') }}</h4>
        <p class="text-xs text-gray-500 dark:text-gray-400">{{ $t('profile.memory.forgetAllBody', { count: total }) }}</p>
        <div class="flex justify-end gap-2">
          <UButton size="sm" color="gray" variant="ghost" @click="confirmForgetAll = false">{{ $t('common.cancel') }}</UButton>
          <UButton size="sm" color="red" :loading="saving" data-testid="memory-forget-all-confirm-btn" @click="forgetAll">{{ $t('profile.memory.forgetAllConfirm') }}</UButton>
        </div>
      </div>
    </UModal>
  </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import MemoryEntryForm, { type MemoryForm } from '~/components/profile/MemoryEntryForm.vue'

interface MemoryEntry {
  id: string
  handle: string
  text: string
  tags: string[]
  aliases: string[]
  date?: string | null
  end_date?: string | null
  expired: boolean
  source: 'user' | 'agent'
  evidence?: { report_id?: string; report_title?: string; report_link?: string; quote?: string } | null
}

const emit = defineEmits<{ (e: 'open-instructions'): void }>()
const { t, locale } = useI18n()
const toast = useToast()
const { getErrorMessage } = useErrorMessage()

const loading = ref(false)
const saving = ref(false)
const entries = ref<MemoryEntry[]>([])
const tags = ref<{ tag: string; count: number }[]>([])
const total = ref(0)
const cap = ref(200)
const query = ref('')
const activeTag = ref<string | null>(null)
const form = ref<MemoryForm | null>(null)
const confirmForgetAll = ref(false)

// Search matches the text, the tags and the user's own words (aliases).
const visible = computed(() => {
  const q = query.value.trim().toLowerCase()
  return entries.value.filter(e => {
    if (activeTag.value && !e.tags.includes(activeTag.value)) return false
    if (!q) return true
    return [e.text, ...(e.tags || []), ...(e.aliases || [])].some(s => (s || '').toLowerCase().includes(q))
  })
})

function displayTags(e: MemoryEntry): string[] {
  // Object tags (agent:<id>) are machine links, not something to read.
  return (e.tags || []).filter(tg => !/^(agent|data_source|report):/.test(tg))
}

function sourceLabel(source: string) {
  return t(`profile.memory.source.${source === 'user' ? 'user' : source === 'dream' ? 'dream' : 'agent'}`)
}

function fmtDay(iso: string) {
  const d = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z')
  return new Intl.DateTimeFormat(locale.value, { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' }).format(d)
}
function formatDates(e: MemoryEntry) {
  if (!e.date) return ''
  const start = fmtDay(e.date)
  if (e.end_date && e.end_date.slice(0, 10) !== e.date.slice(0, 10)) return `${start} – ${fmtDay(e.end_date)}`
  return start
}

async function load() {
  loading.value = true
  try {
    const res = await useMyFetch('/users/me/memory')
    if (res.status.value !== 'success') throw res.error.value
    const body = res.data.value as any
    entries.value = body.entries || []
    tags.value = body.tags || []
    total.value = body.total || 0
    cap.value = body.cap || 200
    if (activeTag.value && !tags.value.some(tg => tg.tag === activeTag.value)) activeTag.value = null
  } catch (e) {
    toast.add({ title: getErrorMessage(e, t('profile.memory.loadFailed')), color: 'red' })
  } finally {
    loading.value = false
  }
}

function startAdd() {
  form.value = { mode: 'add', text: '', tags: activeTag.value || '', date: '', end_date: '' }
}
function startEdit(e: MemoryEntry) {
  form.value = {
    mode: 'edit', id: e.id, text: e.text,
    tags: (e.tags || []).join(', '),
    date: e.date ? e.date.slice(0, 10) : '',
    end_date: e.end_date ? e.end_date.slice(0, 10) : '',
  }
}

const splitList = (s: string) => s.split(',').map(x => x.trim()).filter(Boolean)
const jsonHeaders = { 'Content-Type': 'application/json' }

async function saveForm(f: MemoryForm) {
  saving.value = true
  try {
    const body = {
      text: f.text.trim(),
      tags: splitList(f.tags),
      date: f.date || null,
      end_date: f.date ? (f.end_date || null) : null,
    }
    const res = f.mode === 'add'
      ? await useMyFetch('/users/me/memory', { method: 'POST', headers: jsonHeaders, body: JSON.stringify(body) })
      : await useMyFetch(`/users/me/memory/${f.id}`, { method: 'PATCH', headers: jsonHeaders, body: JSON.stringify(body) })
    if (res.status.value !== 'success') throw res.error.value
    form.value = null
    toast.add({ title: t('profile.memory.saved'), color: 'green' })
    await load()
  } catch (e) {
    toast.add({ title: getErrorMessage(e, t('profile.memory.saveFailed')), color: 'red' })
  } finally {
    saving.value = false
  }
}

async function forget(e: MemoryEntry) {
  try {
    const res = await useMyFetch(`/users/me/memory/${e.id}`, { method: 'DELETE' })
    if (res.status.value !== 'success') throw res.error.value
    toast.add({ title: t('profile.memory.forgotten'), color: 'green' })
    await load()
  } catch (err) {
    toast.add({ title: getErrorMessage(err, t('profile.memory.saveFailed')), color: 'red' })
  }
}

async function forgetAll() {
  saving.value = true
  try {
    const res = await useMyFetch('/users/me/memory', { method: 'DELETE' })
    if (res.status.value !== 'success') throw res.error.value
    confirmForgetAll.value = false
    toast.add({ title: t('profile.memory.forgotAll'), color: 'green' })
    await load()
  } catch (err) {
    toast.add({ title: getErrorMessage(err, t('profile.memory.saveFailed')), color: 'red' })
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>
