<template>
  <div class="space-y-4" data-testid="memory-panel">
    <div>
      <h3 class="text-base font-semibold text-gray-900 dark:text-gray-100">{{ $t('profile.memory.title') }}</h3>
      <p class="text-xs text-gray-500 dark:text-gray-400 mt-0.5">{{ $t('profile.memory.subtitle') }}</p>
      <p class="text-[11px] text-gray-400 dark:text-gray-500 mt-1 flex items-center gap-1 flex-wrap">
        <UIcon name="i-heroicons-information-circle" class="w-3.5 h-3.5 shrink-0" />
        <span>{{ $t('profile.memory.vsInstructions') }}</span>
        <NuxtLink to="/instructions" class="text-blue-600 dark:text-blue-400 hover:underline" data-testid="memory-instructions-link">
          {{ $t('profile.memory.instructionsLink') }}
        </NuxtLink>
      </p>
    </div>

    <div v-if="loading" class="py-6 flex justify-center">
      <Spinner class="w-5 h-5 text-gray-400" />
    </div>

    <template v-else>
      <!-- Toolbar: tag filter + count + forget everything -->
      <div class="flex items-center gap-2 flex-wrap">
        <div v-if="tags.length" class="flex items-center gap-1 flex-wrap" data-testid="memory-tag-filter">
          <UIcon name="i-heroicons-funnel" class="w-3.5 h-3.5 text-gray-400" />
          <button
            v-for="tg in tags.slice(0, 12)"
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
            #{{ tg.tag }} <span class="text-gray-400">{{ tg.count }}</span>
          </button>
          <button v-if="activeTag" type="button" class="text-[11px] text-gray-400 hover:text-gray-600 ms-1" @click="activeTag = null">
            {{ $t('profile.memory.clearFilter') }}
          </button>
        </div>
        <div class="ms-auto flex items-center gap-3">
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
      </div>

      <p v-if="total === 0 && !hasExpired" class="text-xs text-gray-400 dark:text-gray-500 italic" data-testid="memory-empty">
        {{ $t('profile.memory.empty') }}
      </p>

      <!-- Sections -->
      <div class="space-y-2.5">
        <section
          v-for="sec in SECTION_ORDER"
          :key="sec"
          class="rounded-md border border-gray-200 dark:border-gray-800"
          :data-testid="`memory-section-${sec}`"
        >
          <header
            class="flex items-center gap-2 px-3 py-2 bg-gray-50/60 dark:bg-gray-950/60 rounded-t-md"
            :class="{ 'border-b border-gray-100 dark:border-gray-800': visible(sec).length || (form && form.section === sec) || activeTag, 'rounded-b-md': !visible(sec).length && !(form && form.section === sec) && !activeTag }"
          >
            <UIcon :name="SECTION_ICONS[sec]" class="w-4 h-4 text-gray-500 dark:text-gray-400" />
            <div class="min-w-0">
              <div class="text-[13px] font-medium text-gray-800 dark:text-gray-200">
                {{ $t(`profile.memory.sections.${sec}.title`) }}
                <span class="text-[11px] font-normal text-gray-400">{{ visible(sec).length || '' }}</span>
              </div>
              <div class="text-[11px] text-gray-400 dark:text-gray-500 truncate">{{ $t(`profile.memory.sections.${sec}.hint`) }}</div>
            </div>
            <UButton
              class="ms-auto"
              size="2xs"
              color="gray"
              variant="ghost"
              icon="i-heroicons-plus"
              :data-testid="`memory-add-${sec}`"
              @click="startAdd(sec)"
            >
              {{ $t('profile.memory.add') }}
            </UButton>
          </header>

          <ul v-if="visible(sec).length || (form && form.section === sec) || activeTag" class="divide-y divide-gray-100 dark:divide-gray-800">
            <!-- Add form -->
            <li v-if="form && form.mode === 'add' && form.section === sec" class="px-3 py-2">
              <MemoryEntryForm :form="form" :saving="saving" @save="saveForm" @cancel="form = null" />
            </li>

            <li
              v-for="e in visible(sec)"
              :key="e.id"
              class="group px-3 py-2"
              :class="{ 'opacity-60': e.expired }"
              :data-testid="`memory-entry`"
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
                <UTooltip :text="sourceLabel(e.source)">
                  <UIcon :name="sourceIcon(e.source)" class="w-3.5 h-3.5 mt-0.5 shrink-0" :class="e.source === 'user' ? 'text-gray-500' : 'text-blue-500'" />
                </UTooltip>
                <div class="flex-1 min-w-0">
                  <div class="text-[13px] text-gray-800 dark:text-gray-200 break-words" dir="auto" data-testid="memory-entry-text">
                    <span v-if="sec === 'events' && e.event_start" class="font-medium text-gray-900 dark:text-gray-100 me-1" dir="ltr">{{ formatEventDates(e) }}</span>{{ e.text }}
                  </div>
                  <div class="flex items-center gap-1.5 flex-wrap mt-1">
                    <span v-if="e.expired" class="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-500">{{ $t('profile.memory.past') }}</span>
                    <span v-for="tg in displayTags(e)" :key="tg" class="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 dark:bg-gray-800 text-gray-600 dark:text-gray-400" dir="ltr">#{{ tg }}</span>
                    <span v-for="a in e.aliases" :key="a" class="text-[10px] px-1.5 py-0.5 rounded border border-dashed border-gray-200 dark:border-gray-700 text-gray-500" dir="auto">“{{ a }}”</span>
                    <NuxtLink
                      v-if="e.evidence?.report_link"
                      :to="e.evidence.report_link"
                      class="text-[11px] text-gray-400 hover:text-blue-600 dark:hover:text-blue-400 inline-flex items-center gap-0.5 min-w-0"
                      :title="e.evidence.quote ? `“${e.evidence.quote}”` : undefined"
                      data-testid="memory-entry-source"
                    >
                      <UIcon name="i-heroicons-link" class="w-3 h-3 shrink-0" />
                      <span class="truncate max-w-[220px]" dir="auto">{{ $t('profile.memory.fromReport', { title: e.evidence.report_title || $t('profile.memory.untitledReport') }) }}</span>
                    </NuxtLink>
                  </div>
                </div>
                <div class="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
                  <UButton size="2xs" color="gray" variant="ghost" icon="i-heroicons-pencil-square" :title="$t('common.edit')" data-testid="memory-edit" @click="startEdit(e)" />
                  <UButton size="2xs" color="red" variant="ghost" icon="i-heroicons-trash" :title="$t('common.delete')" data-testid="memory-delete" @click="forget(e)" />
                </div>
              </div>
            </li>
            <li v-if="activeTag && !visible(sec).length && !(form && form.mode === 'add' && form.section === sec)" class="px-3 py-2 text-[11px] text-gray-400 dark:text-gray-500 italic">
              {{ $t('profile.memory.noneForTag') }}
            </li>
          </ul>
        </section>
      </div>

      <!-- What the agent sees -->
      <details class="rounded-md border border-gray-200 dark:border-gray-800" data-testid="memory-preview">
        <summary class="px-3 py-2 text-[12px] text-gray-600 dark:text-gray-400 cursor-pointer select-none">
          {{ $t('profile.memory.previewTitle') }}
          <span class="text-[11px] text-gray-400">· {{ $t('profile.memory.previewChars', { count: previewChars }) }}</span>
        </summary>
        <div class="px-3 pb-3">
          <p class="text-[11px] text-gray-400 dark:text-gray-500 mb-2">{{ $t('profile.memory.previewHint') }}</p>
          <pre class="text-[11px] leading-relaxed text-gray-700 dark:text-gray-300 bg-gray-50 dark:bg-gray-950 rounded p-2 whitespace-pre-wrap break-words" dir="auto">{{ preview || $t('profile.memory.previewEmpty') }}</pre>
        </div>
      </details>
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

type Section = 'style' | 'role' | 'vocabulary' | 'events' | 'focus' | 'preferences'
interface MemoryEntry {
  id: string
  handle: string
  section: Section
  text: string
  tags: string[]
  aliases: string[]
  event_start?: string | null
  event_end?: string | null
  expired: boolean
  source: 'user' | 'agent' | 'migration'
  evidence?: { report_id?: string; report_title?: string; report_link?: string; quote?: string } | null
}

const SECTION_ORDER: Section[] = ['style', 'role', 'vocabulary', 'events', 'focus', 'preferences']
const SECTION_ICONS: Record<Section, string> = {
  style: 'i-heroicons-pencil',
  role: 'i-heroicons-user-circle',
  vocabulary: 'i-heroicons-chat-bubble-bottom-center-text',
  events: 'i-heroicons-calendar',
  focus: 'i-heroicons-viewfinder-circle',
  preferences: 'i-heroicons-adjustments-horizontal',
}

const { t, locale } = useI18n()
const toast = useToast()
const { getErrorMessage } = useErrorMessage()

const loading = ref(false)
const saving = ref(false)
const sections = ref<Record<string, MemoryEntry[]>>({})
const tags = ref<{ tag: string; count: number }[]>([])
const total = ref(0)
const cap = ref(200)
const preview = ref('')
const previewChars = ref(0)
const activeTag = ref<string | null>(null)
const form = ref<MemoryForm | null>(null)
const confirmForgetAll = ref(false)

const hasExpired = computed(() => Object.values(sections.value).some(list => list.some(e => e.expired)))

function visible(sec: Section): MemoryEntry[] {
  const list = sections.value[sec] || []
  return activeTag.value ? list.filter(e => e.tags.includes(activeTag.value as string)) : list
}

function displayTags(e: MemoryEntry): string[] {
  // Object tags (agent:<id>) are machine links, not something to read.
  return (e.tags || []).filter(tg => !/^(agent|data_source|report):/.test(tg))
}

function sourceIcon(source: string) {
  if (source === 'user') return 'i-heroicons-user'
  if (source === 'migration') return 'i-heroicons-archive-box'
  return 'i-heroicons-sparkles'
}
function sourceLabel(source: string) {
  return t(`profile.memory.source.${source === 'user' || source === 'migration' ? source : 'agent'}`)
}

function fmtDay(iso: string) {
  const d = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z')
  return new Intl.DateTimeFormat(locale.value, { weekday: 'short', year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC' }).format(d)
}
function formatEventDates(e: MemoryEntry) {
  if (!e.event_start) return ''
  const start = fmtDay(e.event_start)
  if (e.event_end && e.event_end.slice(0, 10) !== e.event_start.slice(0, 10)) return `${start} → ${fmtDay(e.event_end)} ·`
  return `${start} ·`
}

async function load() {
  loading.value = true
  try {
    const res = await useMyFetch('/users/me/memory')
    if (res.status.value !== 'success') throw res.error.value
    const body = res.data.value as any
    sections.value = body.sections || {}
    tags.value = body.tags || []
    total.value = body.total || 0
    cap.value = body.cap || 200
    preview.value = body.preview || ''
    previewChars.value = body.preview_chars || 0
    if (activeTag.value && !tags.value.some(tg => tg.tag === activeTag.value)) activeTag.value = null
  } catch (e) {
    toast.add({ title: getErrorMessage(e, t('profile.memory.loadFailed')), color: 'red' })
  } finally {
    loading.value = false
  }
}

function startAdd(sec: Section) {
  form.value = { mode: 'add', section: sec, text: '', tags: '', aliases: '', event_start: '', event_end: '' }
}
function startEdit(e: MemoryEntry) {
  form.value = {
    mode: 'edit', id: e.id, section: e.section, text: e.text,
    tags: (e.tags || []).join(', '), aliases: (e.aliases || []).join(', '),
    event_start: e.event_start ? e.event_start.slice(0, 10) : '',
    event_end: e.event_end ? e.event_end.slice(0, 10) : '',
  }
}

const splitList = (s: string) => s.split(',').map(x => x.trim()).filter(Boolean)

async function saveForm(f: MemoryForm) {
  saving.value = true
  try {
    const body: Record<string, any> = {
      text: f.text.trim(),
      section: f.section,
      tags: splitList(f.tags),
      aliases: splitList(f.aliases),
    }
    if (f.section === 'events') {
      body.event_start = f.event_start || null
      body.event_end = f.event_end || null
    }
    const res = f.mode === 'add'
      ? await useMyFetch('/users/me/memory', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      : await useMyFetch(`/users/me/memory/${f.id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
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
