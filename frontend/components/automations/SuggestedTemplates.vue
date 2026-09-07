<template>
  <div>
    <!-- Cards (variant "cards"): the templates not set up yet, inside the
         empty state. Once a template is enabled it lives in the task list
         only, under its "Template" badge — never in two places. -->
    <div v-if="variant === 'cards' && available.length" data-testid="suggested-templates">
      <div class="text-xs font-medium text-gray-700 dark:text-gray-300 mb-2 text-center">{{ $t('scheduledTemplates.startWith') }}</div>
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 text-start">
        <TemplateCard v-for="entry in available" :key="entry.key" :entry="entry" />
      </div>
    </div>

    <!-- Drawer: "From template" in the toolbar. A quiet side panel — the
         templates not set up yet as a gallery, each with its schedule and
         one-click Enable; clicking a card opens the New task form prefilled
         from the template, every field editable. -->
    <USlideover v-model="showDrawer" :ui="{ width: 'max-w-md' }">
      <!-- The onboarding/license banner is fixed above every overlay (z-1000);
           the drawer is full-height, so its header would sit under it. -->
      <div class="h-full flex flex-col bg-white dark:bg-gray-900" :style="showTopBanner ? { paddingTop: bannerHeight } : undefined" data-testid="template-drawer">
        <div class="relative px-5 pt-5 pb-4 border-b border-gray-100 dark:border-gray-800 overflow-hidden">
          <div class="absolute inset-0 bg-gradient-to-br from-blue-50/80 via-transparent to-transparent dark:from-blue-500/10 pointer-events-none" />
          <div class="relative flex items-start justify-between gap-3">
            <div>
              <div class="flex items-center gap-2">
                <span class="inline-flex items-center justify-center w-7 h-7 rounded-lg bg-blue-50 dark:bg-blue-500/15 text-blue-500">
                  <UIcon name="heroicons-sparkles" class="w-4 h-4" />
                </span>
                <h3 class="text-sm font-semibold text-gray-900 dark:text-white">{{ $t('scheduledTemplates.drawerTitle') }}</h3>
              </div>
              <p class="mt-2 text-xs leading-relaxed text-gray-500 dark:text-gray-400 max-w-xs">{{ $t('scheduledTemplates.drawerSubtitle') }}</p>
            </div>
            <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" size="xs" @click="showDrawer = false" />
          </div>
        </div>

        <div class="flex-1 overflow-y-auto px-5 py-4 space-y-3">
          <div
            v-for="(entry, i) in available"
            :key="entry.key"
            class="tpl-enter group rounded-xl border border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-900 p-4 hover:border-blue-200 dark:hover:border-blue-500/40 hover:shadow-[0_6px_24px_-12px_rgba(37,99,235,0.35)] transition-all cursor-pointer"
            :style="{ animationDelay: `${i * 60}ms` }"
            :data-testid="`template-drawer-card-${entry.key}`"
            @click="customize(entry)"
          >
            <div class="flex items-start gap-3">
              <span class="shrink-0 inline-flex items-center justify-center w-9 h-9 rounded-lg bg-blue-50 dark:bg-blue-500/15 text-blue-500 group-hover:bg-blue-100 dark:group-hover:bg-blue-500/25 transition-colors">
                <UIcon :name="entry.icon || 'heroicons-sparkles'" class="w-4.5 h-4.5" />
              </span>
              <div class="min-w-0 flex-1">
                <div class="text-sm font-medium text-gray-900 dark:text-white">{{ entryTitle(entry) }}</div>
                <p class="mt-1 text-xs leading-relaxed text-gray-500 dark:text-gray-400">{{ entryDescription(entry) }}</p>
              </div>
            </div>
            <div class="mt-3 flex items-center justify-between gap-2">
              <span class="inline-flex items-center gap-1 text-[11px] text-gray-500 dark:text-gray-400 bg-gray-50 dark:bg-gray-800 rounded-full px-2 py-0.5">
                <UIcon name="heroicons-clock" class="w-3 h-3" />
                {{ getCronLabel(entry.cron_schedule || entry.default_cron) }}
              </span>
              <div class="flex items-center gap-1">
                <button
                  type="button"
                  class="h-7 px-3 rounded-md text-xs font-medium bg-blue-50 dark:bg-blue-500/15 text-blue-600 dark:text-blue-300 hover:bg-blue-100 dark:hover:bg-blue-500/25 transition-colors disabled:opacity-50"
                  :disabled="busyKey === entry.key"
                  :data-testid="`template-drawer-enable-${entry.key}`"
                  @click.stop="enableFromDrawer(entry)"
                >{{ busyKey === entry.key ? $t('scheduled.creating') : $t('scheduledTemplates.enable') }}</button>
              </div>
            </div>
          </div>

          <p v-if="!available.length" class="py-10 text-center text-xs text-gray-400 dark:text-gray-500">{{ $t('scheduledTemplates.allEnabled') }}</p>
        </div>
      </div>
    </USlideover>

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
import { computed, defineComponent, h, resolveComponent, type PropType } from 'vue'
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

const props = withDefaults(defineProps<{
  /** cards: grid inside the empty state · menu: modals only (the "From
   *  template" menu in ScheduledTab lists `available` and calls openDetails) */
  variant?: 'cards' | 'menu'
}>(), { variant: 'menu' })
const emit = defineEmits<{ (e: 'changed'): void }>()

const toast = useToast()
const { t, te } = useI18n()
const { getCronLabel } = useCronLabel()

const templates = ref<TemplateEntry[]>([])
// Server state drives the toggles: the switch flips only once enable/disable
// lands, so a failed request never shows a template as enabled.
const busyKey = ref<string | null>(null)

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
// Only templates not set up yet are offered; set-up ones are tasks in the list.
const available = computed(() => templates.value.filter((e) => !hasInstance(e)))

// One card in the empty state: click opens the New task form prefilled,
// the Enable button enables with template defaults. A single action per
// surface — no switch inside a clickable card.
const TemplateCard = defineComponent({
  props: { entry: { type: Object as PropType<TemplateEntry>, required: true } },
  setup(cardProps) {
    return () => {
      const entry = cardProps.entry
      return h('div', {
        class: 'border border-gray-100 dark:border-gray-800 bg-white dark:bg-gray-900 rounded-lg p-4 cursor-pointer hover:shadow-md hover:border-gray-200 dark:hover:border-gray-700 transition-all',
        'data-testid': `template-card-${entry.key}`,
        onClick: () => customize(entry),
      }, [
        h('div', { class: 'flex items-start justify-between gap-3' }, [
          h('div', { class: 'min-w-0 flex-1' }, [
            h('div', { class: 'flex items-center gap-1.5' }, [
              entry.icon ? h(resolveComponent('UIcon'), { name: entry.icon, class: 'w-4 h-4 text-blue-500 shrink-0' }) : null,
              h('span', { class: 'text-sm font-medium text-gray-900 dark:text-white truncate' }, entryTitle(entry)),
            ]),
            h('p', { class: 'mt-1 text-[11px] leading-relaxed text-gray-500 dark:text-gray-400 line-clamp-2' }, entryDescription(entry)),
            h('div', { class: 'mt-2 flex items-center gap-1.5 text-[11px] text-gray-400 dark:text-gray-500' }, [
              h(resolveComponent('UIcon'), { name: 'heroicons-clock', class: 'w-3 h-3 shrink-0' }),
              getCronLabel(entry.cron_schedule || entry.default_cron),
            ]),
          ]),
          h('button', {
            type: 'button',
            class: 'shrink-0 h-7 px-2.5 rounded-md border border-gray-200 dark:border-gray-700 text-xs font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50 disabled:opacity-50',
            disabled: busyKey.value === entry.key,
            'data-testid': `template-enable-${entry.key}`,
            onClick: (e: Event) => { e.stopPropagation(); toggle(entry) },
          }, busyKey.value === entry.key ? t('scheduled.creating') : t('scheduledTemplates.enable')),
        ]),
      ])
    }
  },
})

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

// ── Drawer ("From template") ───────────────────────────────────────────────
const showDrawer = ref(false)
const { showTopBanner, bannerHeight } = useTopBanner()
const openDrawer = () => { showDrawer.value = true }
const enableFromDrawer = async (entry: TemplateEntry) => {
  const ok = await callTemplateAction(entry, 'enable')
  // The last template enabled: nothing left to offer, so close.
  if (ok && !available.value.length) showDrawer.value = false
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
// create one first (scoped to the picked agents) and open the modal on it,
// prefilled from the template with every field editable.
const customize = async (entry: TemplateEntry, agentIds?: string[]) => {
  if (!entry || customizing.value) return
  customizing.value = true
  try {
    // A card click has no picker: default to every usable agent, the same
    // scope one-click Enable uses.
    if (!agentIds) {
      await fetchAgents()
      agentIds = agents.value.map((a) => a.id)
    }
    const response = await useMyFetch('/reports', {
      method: 'POST',
      body: JSON.stringify({
        title: entryTitle(entry),
        files: [],
        data_sources: agentIds,
      }),
    })
    if ((response as any).error?.value) throw new Error('Report creation failed')
    const report = (response as any).data?.value as any
    customizeEntry.value = entry
    customizeAgents.value = agents.value.filter((a) => agentIds!.includes(a.id))
    customizeReportId.value = report.id
    showDetails.value = false
    showDrawer.value = false
    showCustomize.value = true
  } catch (error) {
    console.error('Error starting template customization:', error)
    toast.add({ title: t('common.error'), description: t('scheduledTemplates.actionFailed'), color: 'red' })
  } finally {
    customizing.value = false
  }
}
const customizeFromDetails = () => {
  if (detailsEntry.value) customize(detailsEntry.value, selectedAgentIds.value)
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

defineExpose({ refresh: fetchTemplates, available, openDetails, openDrawer, customize })

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

<style scoped>
.tpl-enter { animation: tpl-in 320ms cubic-bezier(.2,.7,.2,1) both; }
@keyframes tpl-in {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: none; }
}
@media (prefers-reduced-motion: reduce) {
  .tpl-enter { animation: none; }
}
</style>
