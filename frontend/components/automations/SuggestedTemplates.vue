<template>
  <div v-if="templates.length > 0" class="mb-6">
    <button
      class="w-full flex items-center justify-between text-start group"
      @click="toggleCollapsed"
    >
      <div>
        <div class="text-xs font-semibold text-gray-700 dark:text-gray-300 uppercase tracking-wide">
          {{ $t('scheduledTemplates.suggested') }}
        </div>
        <div v-if="!collapsed" class="mt-0.5 text-[11px] text-gray-400 dark:text-gray-500">
          {{ $t('scheduledTemplates.subtitle') }}
        </div>
      </div>
      <UIcon
        name="heroicons-chevron-down"
        class="w-4 h-4 text-gray-400 transition-transform"
        :class="{ '-rotate-90': collapsed }"
      />
    </button>

    <div v-if="!collapsed" class="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-3">
      <div
        v-for="entry in templates"
        :key="entry.key"
        class="border border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-900 rounded-lg p-4 cursor-pointer hover:shadow-md hover:border-gray-200 dark:hover:border-gray-700 transition-all"
        :data-testid="`template-card-${entry.key}`"
        @click="openDetails(entry)"
      >
        <div class="flex items-start justify-between gap-3">
          <div class="min-w-0 flex-1">
            <div class="flex items-center gap-1.5">
              <UIcon v-if="entry.icon" :name="entry.icon" class="w-4 h-4 text-blue-500 shrink-0" />
              <span class="text-sm font-medium text-gray-900 dark:text-white truncate">
                {{ entryTitle(entry) }}
              </span>
            </div>
            <p class="mt-1 text-[11px] leading-relaxed text-gray-500 dark:text-gray-400 line-clamp-2">
              {{ entryDescription(entry) }}
            </p>
            <div class="mt-2 flex items-center gap-2 text-[11px] text-gray-400 dark:text-gray-500">
              <UIcon name="heroicons-clock" class="w-3 h-3 shrink-0" />
              {{ getCronLabel(entry.cron_schedule || entry.default_cron) }}
              <span v-if="entry.paused" class="text-amber-500">{{ $t('scheduledTemplates.pausedHint') }}</span>
            </div>
          </div>
          <UTooltip :text="entry.enabled ? $t('scheduledTemplates.disable') : $t('scheduledTemplates.enable')">
            <button
              @click.stop="toggle(entry)"
              :disabled="busyKey === entry.key"
              class="relative inline-flex h-4 w-7 items-center rounded-full transition-colors disabled:opacity-50 shrink-0 mt-0.5"
              :class="entry.enabled ? 'bg-blue-500' : 'bg-gray-300 dark:bg-gray-700'"
              :aria-pressed="entry.enabled"
              :data-testid="`template-toggle-${entry.key}`"
            >
              <span
                class="inline-block h-3 w-3 rounded-full bg-white transition-transform"
                :class="entry.enabled ? 'translate-x-3.5' : 'translate-x-0.5'"
              />
            </button>
          </UTooltip>
        </div>
      </div>
    </div>

    <!-- Details: full prompt + schedule, and (pre-enable) the agent scope -->
    <UModal v-model="showDetails">
      <UCard v-if="detailsEntry" :ui="{ body: { padding: 'px-5 py-4 sm:p-5' }, header: { padding: 'px-5 py-3 sm:px-5 sm:py-3' }, footer: { padding: 'px-5 py-3 sm:px-5 sm:py-3' } }">
        <template #header>
          <div class="flex items-center justify-between">
            <div class="flex items-center gap-1.5 min-w-0">
              <UIcon v-if="detailsEntry.icon" :name="detailsEntry.icon" class="w-4 h-4 text-blue-500 shrink-0" />
              <h3 class="text-sm font-semibold text-gray-900 dark:text-white truncate">{{ entryTitle(detailsEntry) }}</h3>
            </div>
            <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" size="xs" @click="showDetails = false" />
          </div>
        </template>

        <p class="text-xs text-gray-500 dark:text-gray-400 leading-relaxed">
          {{ entryDescription(detailsEntry) }}
        </p>

        <div class="mt-3">
          <div class="text-xs text-gray-500 dark:text-gray-400 mb-1.5">{{ $t('scheduledTemplates.promptLabel') }}</div>
          <div class="rounded-lg border border-gray-100 dark:border-gray-800 bg-gray-50/60 dark:bg-gray-900/40 px-3 py-2 text-xs text-gray-700 dark:text-gray-300 whitespace-pre-wrap max-h-40 overflow-y-auto" dir="auto" data-testid="template-details-prompt">
            {{ detailsEntry.prompt_content }}
          </div>
        </div>

        <div class="mt-3 flex items-center gap-2 text-xs text-gray-600 dark:text-gray-400">
          <UIcon name="heroicons-clock" class="w-3.5 h-3.5 shrink-0 text-gray-400" />
          {{ getCronLabel(detailsEntry.cron_schedule || detailsEntry.default_cron) }}
        </div>

        <!-- Agent scope: only meaningful before the task exists — afterwards
             the agents live on the task's report and are edited there. -->
        <div v-if="!hasInstance(detailsEntry)" class="mt-4 border-t border-gray-100 dark:border-gray-800 pt-3">
          <div class="text-xs text-gray-500 dark:text-gray-400 mb-1.5">{{ $t('scheduledPrompt.agents') }}</div>
          <div v-if="agentsLoading" class="text-[11px] text-gray-400 inline-flex items-center">
            <Spinner class="me-1 w-3 h-3" /> {{ $t('scheduled.loading') }}
          </div>
          <template v-else-if="agents.length">
            <div class="max-h-36 overflow-y-auto space-y-1" data-testid="template-agent-picker">
              <label
                v-for="agent in agents"
                :key="agent.id"
                class="flex items-center gap-2 text-xs text-gray-700 dark:text-gray-300 cursor-pointer select-none py-0.5"
              >
                <UCheckbox :model-value="selectedAgentIds.includes(agent.id)" @update:model-value="toggleAgent(agent.id)" />
                <span class="truncate">{{ agent.name }}</span>
              </label>
            </div>
            <p class="mt-1.5 text-[11px] text-gray-400">{{ $t('scheduledTemplates.agentsHint') }}</p>
          </template>
          <p v-else class="text-[11px] text-gray-400">{{ $t('scheduledTemplates.noAgents') }}</p>
        </div>

        <p v-else class="mt-4 border-t border-gray-100 dark:border-gray-800 pt-3 text-[11px] text-gray-400 leading-relaxed">
          {{ $t('scheduledTemplates.editHint') }}
        </p>

        <template #footer>
          <div class="flex justify-end gap-2">
            <UButton color="gray" variant="ghost" size="xs" @click="showDetails = false">{{ $t('scheduledPrompt.cancel') }}</UButton>
            <UButton
              v-if="!hasInstance(detailsEntry)"
              color="gray"
              variant="soft"
              size="xs"
              icon="i-heroicons-pencil-square"
              :loading="customizing"
              :disabled="busyKey === detailsEntry.key"
              data-testid="template-details-customize"
              @click="customizeFromDetails"
            >{{ $t('scheduledTemplates.customize') }}</UButton>
            <UButton
              v-if="!detailsEntry.enabled"
              color="blue"
              size="xs"
              :loading="busyKey === detailsEntry.key"
              :disabled="customizing"
              data-testid="template-details-enable"
              @click="enableFromDetails"
            >{{ $t('scheduledTemplates.enable') }}</UButton>
            <UButton
              v-else
              color="gray"
              variant="soft"
              size="xs"
              :loading="busyKey === detailsEntry.key"
              @click="disableFromDetails"
            >{{ $t('scheduledTemplates.disable') }}</UButton>
          </div>
        </template>
      </UCard>
    </UModal>

    <!-- Customize: the full new-automation form, prefilled from the template.
         Everything is editable before anything is enabled. -->
    <ScheduledPromptModal
      v-if="customizeReportId && customizeEntry"
      v-model="showCustomize"
      :report-id="customizeReportId"
      :initial-data-sources="customizeAgents"
      :draft-content="customizeEntry.prompt_content"
      :draft-title="entryTitle(customizeEntry)"
      :draft-cron="customizeEntry.default_cron"
      :draft-spawn-new-report="customizeEntry.spawn_new_report"
      :template-key="customizeEntry.key"
      @saved="onCustomizeSaved"
    />
  </div>
</template>

<script setup lang="ts">
import Spinner from '~/components/Spinner.vue'
import ScheduledPromptModal from '~/components/ScheduledPromptModal.vue'

interface TemplateEntry {
  key: string
  title: string
  description: string
  prompt_content: string
  default_cron: string
  spawn_new_report: boolean
  icon?: string | null
  enabled: boolean
  paused: boolean
  scheduled_prompt_id?: string | null
  report_id?: string | null
  cron_schedule?: string | null
  duplicate_count: number
}

const emit = defineEmits<{ (e: 'changed'): void }>()

const toast = useToast()
const { t, te } = useI18n()
const { getCronLabel } = useCronLabel()

const templates = ref<TemplateEntry[]>([])
// Server state drives the toggles: the switch flips only once enable/disable
// lands, so a failed request never shows a template as enabled.
const busyKey = ref<string | null>(null)

const COLLAPSE_KEY = 'bow:suggestedTemplatesCollapsed'
const collapsed = ref(false)
try { collapsed.value = localStorage.getItem(COLLAPSE_KEY) === '1' } catch { /* private mode */ }
const toggleCollapsed = () => {
  collapsed.value = !collapsed.value
  try { localStorage.setItem(COLLAPSE_KEY, collapsed.value ? '1' : '0') } catch { /* ignore */ }
}

// Registry copy ships in English; translate by key when the locale has it.
const entryTitle = (entry: TemplateEntry) => {
  const key = `scheduledTemplates.items.${entry.key}.title`
  return te(key) ? t(key) : entry.title
}
const entryDescription = (entry: TemplateEntry) => {
  const key = `scheduledTemplates.items.${entry.key}.description`
  return te(key) ? t(key) : entry.description
}

const hasInstance = (entry: TemplateEntry) => entry.enabled || entry.paused

// ── Details modal + pre-enable agent scope ─────────────────────────────────
const showDetails = ref(false)
const detailsEntry = ref<TemplateEntry | null>(null)
const agents = ref<{ id: string; name: string }[]>([])
const agentsLoaded = ref(false)
const agentsLoading = ref(false)
const selectedAgentIds = ref<string[]>([])

const openDetails = async (entry: TemplateEntry) => {
  detailsEntry.value = entry
  showDetails.value = true
  if (!hasInstance(entry)) {
    await fetchAgents()
    // Default to everything usable — the quick toggle's behavior — so the
    // picker only narrows when the user unchecks.
    selectedAgentIds.value = agents.value.map((a) => a.id)
  }
}

const fetchAgents = async () => {
  if (agentsLoaded.value || agentsLoading.value) return
  agentsLoading.value = true
  try {
    const response = await useMyFetch('/data_sources', { method: 'GET' })
    if (response.status.value === 'success' && response.data.value) {
      agents.value = (response.data.value as any[]).map((d: any) => ({ id: d.id, name: d.name }))
      agentsLoaded.value = true
    }
  } catch (error) {
    console.error('Error fetching agents for template details:', error)
  } finally {
    agentsLoading.value = false
  }
}

const toggleAgent = (id: string) => {
  selectedAgentIds.value = selectedAgentIds.value.includes(id)
    ? selectedAgentIds.value.filter((a) => a !== id)
    : [...selectedAgentIds.value, id]
}

const applyResult = (updated: TemplateEntry) => {
  const idx = templates.value.findIndex((e) => e.key === updated.key)
  if (idx >= 0) templates.value.splice(idx, 1, updated)
  if (detailsEntry.value?.key === updated.key) detailsEntry.value = updated
}

const callTemplateAction = async (entry: TemplateEntry, action: 'enable' | 'disable', body?: any) => {
  if (busyKey.value) return false
  busyKey.value = entry.key
  try {
    const response = await useMyFetch(`/scheduled-prompt-templates/${entry.key}/${action}`, {
      method: 'POST',
      ...(body ? { body } : {}),
    })
    if ((response as any).error?.value) throw new Error(`${action} failed`)
    applyResult((response as any).data?.value as TemplateEntry)
    toast.add({
      title: action === 'enable'
        ? t('scheduledTemplates.enabledToast', { name: entryTitle(entry) })
        : t('scheduledTemplates.disabledToast', { name: entryTitle(entry) }),
      color: 'green',
    })
    emit('changed')
    return true
  } catch (error) {
    console.error('Error toggling scheduled-task template:', error)
    toast.add({ title: t('common.error'), description: t('scheduledTemplates.actionFailed'), color: 'red' })
    return false
  } finally {
    busyKey.value = null
  }
}

// Quick toggle on the card: template defaults (all usable agents).
const toggle = (entry: TemplateEntry) => {
  callTemplateAction(entry, entry.enabled ? 'disable' : 'enable')
}

// Enable from the details modal: honor the agent picker. Only sent when a
// fresh instance is being created — resuming a paused one ignores scope.
const enableFromDetails = async () => {
  const entry = detailsEntry.value
  if (!entry) return
  const body = !hasInstance(entry) && agentsLoaded.value
    ? { data_source_ids: selectedAgentIds.value }
    : undefined
  const ok = await callTemplateAction(entry, 'enable', body)
  if (ok) showDetails.value = false
}

const disableFromDetails = async () => {
  const entry = detailsEntry.value
  if (!entry) return
  const ok = await callTemplateAction(entry, 'disable')
  if (ok) showDetails.value = false
}

// ── Customize: the full new-automation form, seeded from the template ──────
const showCustomize = ref(false)
const customizing = ref(false)
const customizeReportId = ref<string | null>(null)
const customizeEntry = ref<TemplateEntry | null>(null)
const customizeAgents = ref<{ id: string; name: string }[]>([])

// Same shape as ScheduledTab.openNewTask: a task needs a host report, so
// create one first (scoped to the picked agents) and open the modal on it.
const customizeFromDetails = async () => {
  const entry = detailsEntry.value
  if (!entry || customizing.value) return
  customizing.value = true
  try {
    const response = await useMyFetch('/reports', {
      method: 'POST',
      body: JSON.stringify({
        title: entryTitle(entry),
        files: [],
        data_sources: selectedAgentIds.value,
      }),
    })
    if ((response as any).error?.value) throw new Error('Report creation failed')
    const report = (response as any).data?.value as any
    customizeEntry.value = entry
    customizeAgents.value = agents.value.filter((a) => selectedAgentIds.value.includes(a.id))
    customizeReportId.value = report.id
    showDetails.value = false
    showCustomize.value = true
  } catch (error) {
    console.error('Error starting template customization:', error)
    toast.add({ title: t('common.error'), description: t('scheduledTemplates.actionFailed'), color: 'red' })
  } finally {
    customizing.value = false
  }
}

const onCustomizeSaved = () => {
  showCustomize.value = false
  fetchTemplates()
  emit('changed')
}

const fetchTemplates = async () => {
  try {
    const response = await useMyFetch('/scheduled-prompt-templates', { method: 'GET' })
    if (response.status.value === 'success' && response.data.value) {
      templates.value = response.data.value as TemplateEntry[]
    }
  } catch (error) {
    // The catalog is a convenience — the tab stays usable without it.
    console.error('Error fetching scheduled-task templates:', error)
  }
}

defineExpose({ refresh: fetchTemplates })

onMounted(fetchTemplates)
</script>

<style scoped>
.line-clamp-2 {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
</style>
