<template>
  <div>
    <!-- Empty state -->
    <div v-if="!isLoading && triggers.length === 0" class="flex flex-col items-center justify-center text-center py-20 px-4">
      <UIcon name="heroicons-bolt" class="w-10 h-10 text-amber-400" />
      <h3 class="mt-3 text-sm font-medium text-gray-900 dark:text-white">{{ $t('triggers.empty') }}</h3>
      <p class="mt-1 max-w-sm text-xs leading-relaxed text-gray-500 dark:text-gray-400">{{ $t('triggers.emptyDescription') }}</p>
      <button
        @click="openNew"
        class="mt-5 inline-flex items-center gap-1.5 h-8 px-3 rounded-md bg-blue-600 text-white text-xs font-medium hover:bg-blue-700 transition-colors"
        data-testid="new-trigger-empty"
      >
        <UIcon name="heroicons-plus" class="w-3.5 h-3.5" />
        {{ $t('triggers.newTrigger') }}
      </button>
    </div>

    <template v-else>
      <div class="mb-5 flex items-center justify-between">
        <div class="text-xs text-gray-500 dark:text-gray-400">{{ $t('triggers.description') }}</div>
        <button
          @click="openNew"
          class="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-blue-500 hover:bg-blue-600 rounded-md transition-colors"
          data-testid="new-trigger"
        >
          <UIcon name="heroicons-plus" class="w-3.5 h-3.5" />
          {{ $t('triggers.newTrigger') }}
        </button>
      </div>

      <!-- Loading -->
      <div v-if="isLoading" class="text-xs text-gray-500 dark:text-gray-400 inline-flex items-center">
        <Spinner class="me-1" /> {{ $t('triggers.loading') }}
      </div>

      <!-- Trigger cards -->
      <div v-else class="space-y-3">
        <div
          v-for="trig in triggers"
          :key="trig.id"
          class="border border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-900 rounded-lg p-4 hover:shadow-md hover:border-gray-200 dark:hover:border-gray-700 transition-all"
          :data-testid="`trigger-card-${trig.name}`"
        >
          <div class="group flex items-start justify-between gap-3">
            <div class="min-w-0 flex-1 cursor-pointer" @click="openEdit(trig)">
              <div class="flex items-center gap-2 mb-1">
                <UIcon name="heroicons-bolt-solid" class="w-3.5 h-3.5 text-amber-500" />
                <span class="text-sm font-medium text-gray-900 dark:text-white">{{ trig.name }}</span>
                <span
                  class="text-[10px] px-1.5 py-0.5 rounded border"
                  :class="trig.is_active
                    ? 'text-green-700 border-green-200 bg-green-50'
                    : 'text-gray-700 dark:text-gray-300 border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800'"
                >{{ trig.is_active ? $t('triggers.active') : $t('triggers.paused') }}</span>
                <span v-if="trig.classify_enabled" class="text-[10px] px-1.5 py-0.5 rounded border text-purple-700 border-purple-200 bg-purple-50">AI {{ $t('triggers.filter') }}</span>
              </div>
              <div class="text-xs text-gray-500 dark:text-gray-400 line-clamp-1 mb-1.5">{{ trig.task_template || $t('triggers.noTask') }}</div>
              <div class="flex items-center gap-3 text-[11px] text-gray-400 dark:text-gray-500 flex-wrap">
                <span class="inline-flex items-center gap-1">
                  <UIcon name="heroicons-cube" class="w-3 h-3" />
                  {{ trig.data_sources.length ? trig.data_sources.map((d: any) => d.name).join(', ') : $t('triggers.noAgents') }}
                </span>
                <span class="inline-flex items-center gap-1">
                  <UIcon name="heroicons-cpu-chip" class="w-3 h-3" />
                  {{ modelName(trig.model_id) }}
                </span>
                <span>{{ trig.mode === 'deep' ? $t('triggers.modeDeep') : $t('triggers.modeChat') }}</span>
                <span v-if="trig.last_delivery_at">&middot; {{ $t('triggers.lastDelivery', { time: formatRelativeTime(trig.last_delivery_at) }) }}</span>
              </div>
            </div>
            <div class="shrink-0 flex items-center gap-1">
              <button
                @click.stop="toggleRuns(trig)"
                class="inline-flex items-center gap-1 text-[11px] px-2 py-1 rounded border border-gray-200 dark:border-gray-700 text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-gray-800/60"
                :data-testid="`trigger-runs-${trig.name}`"
              >
                <UIcon name="heroicons-clock" class="w-3 h-3" />
                {{ $t('triggers.runs', { n: trig.run_count }) }}
                <UIcon :name="expandedId === trig.id ? 'heroicons-chevron-up' : 'heroicons-chevron-down'" class="w-3 h-3" />
              </button>
              <UTooltip :text="$t('triggers.rotate')">
                <button @click.stop="rotate(trig)" class="p-1 rounded text-gray-300 dark:text-gray-600 hover:text-gray-600">
                  <UIcon name="heroicons-arrow-path" class="w-3.5 h-3.5" />
                </button>
              </UTooltip>
              <UTooltip :text="$t('triggers.delete')">
                <button @click.stop="removeTrigger(trig)" class="p-1 rounded text-gray-300 dark:text-gray-600 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-950/40">
                  <UIcon name="heroicons-trash" class="w-3.5 h-3.5" />
                </button>
              </UTooltip>
            </div>
          </div>

          <!-- Run history (expandable) -->
          <div v-if="expandedId === trig.id" class="mt-3 pt-3 border-t border-gray-100 dark:border-gray-800">
            <div v-if="runsLoading" class="text-[11px] text-gray-400 inline-flex items-center"><Spinner class="me-1 w-3 h-3" /> {{ $t('triggers.loading') }}</div>
            <div v-else-if="runs.length === 0" class="text-[11px] text-gray-400 py-2">{{ $t('triggers.noRuns') }}</div>
            <div v-else class="space-y-1">
              <NuxtLink
                v-for="run in runs"
                :key="run.report_id"
                :to="`/reports/${run.report_id}`"
                class="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-gray-50 dark:hover:bg-gray-800/60 text-xs"
              >
                <span class="w-1.5 h-1.5 rounded-full shrink-0"
                  :class="run.status === 'success' ? 'bg-green-500' : run.status === 'error' ? 'bg-red-500' : 'bg-amber-400'" />
                <span class="flex-1 min-w-0 truncate text-gray-700 dark:text-gray-300">{{ run.event_summary || run.title }}</span>
                <span class="text-[10px] text-gray-400 shrink-0">{{ formatRelativeTime(run.created_at) }}</span>
                <UIcon name="heroicons-arrow-top-right-on-square" class="w-3 h-3 text-gray-300 shrink-0" />
              </NuxtLink>
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- Create / edit modal -->
    <UModal v-model="showModal" :ui="{ width: 'sm:max-w-2xl' }">
      <UCard :ui="{ body: { padding: 'px-5 py-4 sm:p-5' }, header: { padding: 'px-5 py-3 sm:px-5 sm:py-3' } }">
        <template #header>
          <div class="flex items-start justify-between">
            <div>
              <h3 class="text-sm font-semibold text-gray-900 dark:text-white">
                {{ editing ? $t('triggers.editTitle') : $t('triggers.newTitle') }}
              </h3>
              <p class="text-xs text-gray-400 mt-0.5">{{ $t('triggers.modalSubtitle') }}</p>
            </div>
            <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" size="xs" @click="showModal = false" />
          </div>
        </template>

        <!-- Secret reveal (shown once after create / rotate) -->
        <section v-if="reveal" class="mb-5" data-testid="trigger-reveal">
          <h4 class="text-xs font-medium text-green-600 uppercase tracking-wide mb-2">{{ $t('triggers.copyOnce') }}</h4>
          <div class="space-y-2">
            <div class="flex items-center gap-2 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 px-3 py-2">
              <span class="text-[10px] font-medium uppercase tracking-wide text-gray-400 w-10">URL</span>
              <code class="flex-1 text-xs text-gray-700 dark:text-gray-300 truncate" data-testid="reveal-url">{{ reveal.delivery_url }}</code>
              <button class="text-gray-400 hover:text-gray-700 dark:hover:text-gray-300" @click="copy(reveal.delivery_url)"><UIcon name="heroicons-clipboard-document" class="w-4 h-4" /></button>
            </div>
            <div class="flex items-center gap-2 rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 px-3 py-2">
              <span class="text-[10px] font-medium uppercase tracking-wide text-gray-400 w-10">{{ $t('triggers.secret') }}</span>
              <code class="flex-1 text-xs text-gray-700 dark:text-gray-300 truncate" data-testid="reveal-secret">{{ reveal.secret }}</code>
              <button class="text-gray-400 hover:text-gray-700 dark:hover:text-gray-300" @click="copy(reveal.secret)"><UIcon name="heroicons-clipboard-document" class="w-4 h-4" /></button>
            </div>
          </div>
        </section>

        <!-- Task + run spec: the standard prompt box (agents, mode, model) -->
        <div data-testid="trigger-task-box">
          <PromptBoxV2
            v-if="showModal"
            ref="promptBoxRef"
            :key="editing?.id || 'new'"
            :initialSelectedDataSources="initialDataSources"
            :initialMode="editing?.mode || 'chat'"
            :initialModel="editing?.model_id || ''"
            :textareaContent="editing?.task_template || ''"
            :hideScheduleButton="true"
            :hideSubmitButton="true"
          />
          <p class="mt-1 text-[11px] text-gray-400">{{ $t('triggers.taskHint') }}</p>
        </div>

        <div class="space-y-4 mt-4">
          <!-- Receiving -->
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="block text-xs text-gray-500 dark:text-gray-400 mb-1.5">{{ $t('triggers.name') }}</label>
              <input v-model="form.name" type="text" :placeholder="$t('triggers.namePlaceholder')" data-testid="trigger-name"
                class="w-full rounded-lg border border-gray-200 dark:border-gray-700 px-3 py-2 text-sm text-gray-800 dark:text-gray-200 dark:bg-gray-900 focus:outline-none focus:ring-2 focus:ring-blue-100 focus:border-blue-300" />
            </div>
            <div>
              <label class="block text-xs text-gray-500 dark:text-gray-400 mb-1.5">{{ $t('triggers.auth') }}</label>
              <select v-model="form.auth_mode"
                class="w-full rounded-lg border border-gray-200 dark:border-gray-700 px-3 py-2 text-sm text-gray-800 dark:text-gray-200 bg-white dark:bg-gray-900 focus:outline-none focus:ring-2 focus:ring-blue-100 focus:border-blue-300">
                <option value="token">{{ $t('triggers.authToken') }}</option>
                <option value="hmac">{{ $t('triggers.authHmac') }}</option>
                <option value="url_token">{{ $t('triggers.authUrlToken') }}</option>
              </select>
            </div>
          </div>

          <!-- Classifier gate -->
          <label class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 cursor-pointer">
            <input v-model="form.classify_enabled" type="checkbox" class="rounded border-gray-300 dark:border-gray-600 text-blue-600 focus:ring-blue-200" />
            {{ $t('triggers.classifierToggle') }}
          </label>
          <div v-if="form.classify_enabled">
            <label class="block text-xs text-gray-500 dark:text-gray-400 mb-1.5">{{ $t('triggers.classifierGuidance') }}</label>
            <textarea v-model="form.classifier_prompt" rows="2" :placeholder="$t('triggers.classifierPlaceholder')"
              class="w-full rounded-lg border border-gray-200 dark:border-gray-700 px-3 py-2 text-sm text-gray-800 dark:text-gray-200 dark:bg-gray-900 focus:outline-none focus:ring-2 focus:ring-blue-100 focus:border-blue-300"></textarea>
          </div>

          <!-- Active toggle (edit only) -->
          <label v-if="editing" class="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300 cursor-pointer">
            <input v-model="form.is_active" type="checkbox" class="rounded border-gray-300 dark:border-gray-600 text-blue-600 focus:ring-blue-200" />
            {{ $t('triggers.activeToggle') }}
          </label>

          <div class="flex justify-end pt-1">
            <button
              :disabled="saving"
              class="inline-flex items-center gap-2 rounded-lg bg-gray-900 px-4 py-2 text-sm font-medium text-white hover:bg-gray-800 disabled:opacity-60 disabled:cursor-not-allowed transition-colors"
              data-testid="trigger-save"
              @click="save"
            >
              <Spinner v-if="saving" class="w-4 h-4" />
              {{ editing ? $t('triggers.save') : $t('triggers.create') }}
            </button>
          </div>
        </div>
      </UCard>
    </UModal>
  </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import PromptBoxV2 from '~/components/prompt/PromptBoxV2.vue'

const toast = useToast()
const { t } = useI18n()
const { getErrorMessage } = useErrorMessage()

const triggers = ref<any[]>([])
const models = ref<any[]>([])
const isLoading = ref(true)
const saving = ref(false)
const showModal = ref(false)
const editing = ref<any | null>(null)
const reveal = ref<any>(null)
const expandedId = ref<string | null>(null)
const runs = ref<any[]>([])
const runsLoading = ref(false)
const promptBoxRef = ref<InstanceType<typeof PromptBoxV2> | null>(null)

// Receiving/gate config kept as a plain form; the run spec (task text,
// agents, mode, model) lives in the embedded PromptBoxV2 (standalone mode —
// no report_id) and is read back via its exposed getters on save.
const defaultForm = () => ({
  name: '',
  source: 'generic',
  auth_mode: 'token',
  classify_enabled: false,
  classifier_prompt: '',
  is_active: true,
})
const form = ref(defaultForm())

// Agents pre-selected in the prompt box when editing an existing trigger.
const initialDataSources = computed(() => (editing.value?.data_sources || []).map((d: any) => ({ ...d })))

function modelName(id: string | null): string {
  if (!id) return t('triggers.defaultModel')
  const m = models.value.find((m: any) => m.id === id)
  return m?.name || t('triggers.defaultModel')
}

function formatRelativeTime(dateStr: string): string {
  if (!dateStr) return ''
  const date = new Date(dateStr)
  const diff = Math.max(0, Date.now() - date.getTime())
  const mins = Math.floor(diff / 60000)
  if (mins < 60) return t('queries.timeMinutesAgo', { n: mins })
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return t('queries.timeHoursAgo', { n: hrs })
  const days = Math.floor(hrs / 24)
  return t('queries.timeDaysAgo', { n: days })
}

async function fetchTriggers() {
  isLoading.value = true
  try {
    const { data } = await useMyFetch('/triggers')
    triggers.value = (data.value as any[]) || []
  } catch { triggers.value = [] } finally { isLoading.value = false }
}

async function fetchModels() {
  try {
    const { data } = await useMyFetch('/llm/models?is_enabled=true')
    models.value = ((data.value as any[]) || []).filter((m: any) => m.is_enabled !== false)
  } catch { models.value = [] }
}

function openNew() {
  editing.value = null
  reveal.value = null
  form.value = defaultForm()
  showModal.value = true
}

function openEdit(trig: any) {
  editing.value = trig
  reveal.value = null
  form.value = {
    name: trig.name,
    source: trig.source,
    auth_mode: trig.auth_mode,
    classify_enabled: trig.classify_enabled,
    classifier_prompt: trig.classifier_prompt || '',
    is_active: trig.is_active,
  }
  showModal.value = true
}

function readRunSpec() {
  const box = promptBoxRef.value as any
  const fallback = editing.value || {}
  return {
    task_template: box?.getText?.() ?? fallback.task_template ?? '',
    mode: box?.getMode?.() || fallback.mode || 'chat',
    model_id: box?.getModel?.() || fallback.model_id || null,
    data_source_ids: ((box?.getDataSources?.() as any[]) || fallback.data_sources || [])
      .map((d: any) => d?.id).filter(Boolean),
  }
}

async function save() {
  saving.value = true
  try {
    const body = { ...form.value, ...readRunSpec() }
    if (editing.value) {
      const { data, error } = await useMyFetch(`/triggers/${editing.value.id}`, {
        method: 'PUT', body,
      })
      if (error.value) throw error.value
      toast.add({ title: t('triggers.toastSaved'), color: 'green' })
      showModal.value = false
    } else {
      const { data, error } = await useMyFetch('/triggers', {
        method: 'POST', body,
      })
      if (error.value) throw error.value
      reveal.value = data.value
      toast.add({ title: t('triggers.toastCreated'), color: 'green' })
      // Keep the modal open so the user can copy the URL + secret (shown once)
    }
    await fetchTriggers()
  } catch (e) {
    console.error('save trigger failed', e)
    toast.add({ title: t('common.error'), description: t('triggers.saveFailed'), color: 'red' })
  } finally { saving.value = false }
}

async function rotate(trig: any) {
  const { data } = await useMyFetch(`/triggers/${trig.id}/rotate`, { method: 'POST' })
  if (data.value) {
    editing.value = trig
    reveal.value = data.value
    openEdit(trig)
    reveal.value = data.value
  }
}

async function removeTrigger(trig: any) {
  if (!confirm(t('triggers.deleteConfirm'))) return
  try {
    await useMyFetchStrict(`/triggers/${trig.id}`, { method: 'DELETE' })
    triggers.value = triggers.value.filter((x: any) => x.id !== trig.id)
    toast.add({ title: t('triggers.toastDeleted'), color: 'green' })
  } catch (e) {
    console.error('delete trigger failed', e)
    toast.add({ title: t('common.error'), description: getErrorMessage(e), color: 'red' })
  }
}

async function toggleRuns(trig: any) {
  if (expandedId.value === trig.id) { expandedId.value = null; return }
  expandedId.value = trig.id
  runsLoading.value = true
  runs.value = []
  try {
    const { data } = await useMyFetch(`/triggers/${trig.id}/runs`)
    runs.value = (data.value as any)?.runs || []
  } catch { runs.value = [] } finally { runsLoading.value = false }
}

function copy(text: string) {
  if (text) navigator.clipboard.writeText(text)
}

onMounted(async () => {
  await Promise.all([fetchTriggers(), fetchModels()])
})
</script>

<style scoped>
.line-clamp-1 {
  display: -webkit-box;
  -webkit-line-clamp: 1;
  line-clamp: 1;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
