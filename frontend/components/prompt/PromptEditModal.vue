<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-2xl' }" :prevent-close="isSaving">
    <UCard :ui="{ body: { padding: 'px-5 py-4 sm:p-5' }, header: { padding: 'px-5 py-3 sm:px-5 sm:py-3' }, footer: { padding: 'px-5 py-3 sm:px-5 sm:py-3' } }">
      <template #header>
        <div class="flex items-center justify-between">
          <h3 class="text-sm font-semibold text-gray-900 dark:text-white">
            {{ isEditing ? $t('prompts.editTitle') : $t('prompts.newTitle') }}
          </h3>
          <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" size="xs" @click="isOpen = false" />
        </div>
      </template>

      <div class="space-y-5 max-h-[68vh] overflow-auto pe-1">
        <!-- Section 1: Prompt -->
        <section>
          <div class="text-[11px] font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500 mb-2">
            {{ $t('prompts.sectionPrompt') }}
          </div>
          <input
            v-model="form.title"
            type="text"
            :placeholder="$t('prompts.titlePlaceholder')"
            class="w-full text-sm border border-gray-200 dark:border-gray-700 rounded-md px-3 py-2 mb-2 dark:bg-gray-800 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-500 focus:border-blue-500"
          />
          <div class="border border-gray-200 dark:border-gray-700 rounded-md p-1 focus-within:ring-1 focus-within:ring-blue-500 focus-within:border-blue-500">
            <MentionInput
              v-model="form.text"
              :placeholder="$t('prompts.textPlaceholder')"
              :rows="4"
              @update:mentionsGroups="onMentionsGroups"
            />
          </div>
          <div class="flex items-center justify-between mt-1.5">
            <span class="text-[11px] text-gray-400 dark:text-gray-500">{{ $t('prompts.insertParamHint') }}</span>
            <button
              type="button"
              class="inline-flex items-center gap-1 text-[11px] text-blue-500 hover:text-blue-600"
              @click="insertParameterPrompt"
            >
              <UIcon name="heroicons-plus" class="w-3 h-3" />
              {{ $t('prompts.insertParam') }}
            </button>
          </div>
          <!-- Detected parameters (read-only, derived from {{name}} placeholders) -->
          <div v-if="detectedParams.length" class="flex flex-wrap items-center gap-1.5 mt-2">
            <span class="text-[11px] text-gray-400 dark:text-gray-500">{{ $t('prompts.detectedParams') }}</span>
            <code
              v-for="n in detectedParams"
              :key="n"
              class="text-[11px] text-indigo-500 dark:text-indigo-400 bg-indigo-50 dark:bg-indigo-900/20 rounded px-1 py-0.5"
            >{{ placeholder(n) }}</code>
          </div>
        </section>

        <!-- Section 2: Audience -->
        <section>
          <div class="text-[11px] font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500 mb-2">
            {{ $t('prompts.sectionAudience') }}
          </div>

          <div class="flex gap-1 p-0.5 bg-gray-100 dark:bg-gray-800 rounded w-fit mb-3">
            <button
              v-for="opt in audienceOptions"
              :key="opt.value"
              type="button"
              class="inline-flex items-center gap-1 px-2.5 py-1 text-[11px] rounded transition-colors"
              :class="audience === opt.value ? 'bg-white dark:bg-gray-900 text-gray-900 dark:text-white shadow-sm font-medium' : 'text-gray-500 hover:text-gray-700'"
              @click="audience = opt.value"
            >
              <UIcon :name="opt.icon" class="w-3.5 h-3.5" />
              {{ opt.label }}
            </button>
          </div>

          <!-- Agents: explicit multiselect — always shows the agent name(s) by
               name (DataSourceIcon + name). Never collapses to "Auto". -->
          <div v-if="audience === 'agent'">
            <div class="text-[11px] text-gray-500 dark:text-gray-400 mb-1.5">{{ $t('prompts.selectAgents') }}</div>
            <div v-if="!manageableAgents.length" class="text-[11px] text-gray-400">{{ $t('prompts.noManageableAgents') }}</div>
            <template v-else>
              <USelectMenu
                v-model="agentIds"
                :options="agentOptions"
                option-attribute="name"
                value-attribute="id"
                multiple
                size="xs"
                class="text-xs w-72"
                :placeholder="$t('prompts.selectAgentsPlaceholder')"
                :ui="{ option: { base: 'text-xs py-1.5' } }"
              >
                <template #label>
                  <span v-if="!agentIds.length" class="text-gray-400">{{ $t('prompts.selectAgentsPlaceholder') }}</span>
                  <span v-else class="text-xs truncate">{{ $t('prompts.nAgentsSelected', { n: agentIds.length }) }}</span>
                </template>
                <template #option="{ option }">
                  <DataSourceIcon v-if="option.type || option.icon" :type="option.type" :icon="option.icon" class="h-3.5 w-auto flex-shrink-0" />
                  <span class="text-xs truncate">{{ option.name }}</span>
                </template>
              </USelectMenu>

              <!-- Selected agents shown explicitly as named chips -->
              <div v-if="selectedAgentChips.length" class="flex flex-wrap gap-1.5 mt-2">
                <span
                  v-for="a in selectedAgentChips"
                  :key="a.id"
                  class="inline-flex items-center gap-1 text-[11px] px-2 py-1 rounded border border-violet-200 dark:border-violet-900 text-violet-700 dark:text-violet-300 bg-violet-50 dark:bg-violet-900/20"
                >
                  <DataSourceIcon :type="a.type" :icon="a.icon" class="h-3 w-auto" />
                  {{ a.name }}
                  <button type="button" class="hover:text-red-500" @click="removeAgent(a.id)">
                    <UIcon name="heroicons-x-mark" class="w-2.5 h-2.5" />
                  </button>
                </span>
              </div>
            </template>
          </div>

          <p v-if="audience === 'global'" class="text-[11px] text-gray-400 dark:text-gray-500">{{ $t('prompts.globalHint') }}</p>
          <p v-if="audience === 'private'" class="text-[11px] text-gray-400 dark:text-gray-500">{{ $t('prompts.privateHint') }}</p>
        </section>

        <!-- Section 3: Advanced (collapsed) — mode + model selectors -->
        <section>
          <button
            type="button"
            class="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500 mb-2"
            @click="showAdvanced = !showAdvanced"
          >
            <UIcon :name="showAdvanced ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3.5 h-3.5" />
            {{ $t('prompts.sectionAdvanced') }}
          </button>

          <div v-if="showAdvanced" class="flex items-center gap-2">
            <ModeSelector v-model="form.mode" :dataSourceIds="agentIds" />
            <ModelSelector v-model="form.model_id" />
          </div>
        </section>
      </div>

      <template #footer>
        <div class="flex items-center justify-between gap-2">
          <span v-if="errorMsg" class="text-[11px] text-red-500">{{ errorMsg }}</span>
          <span v-else />
          <div class="flex justify-end gap-2">
            <UButton color="gray" variant="ghost" size="xs" @click="isOpen = false">{{ $t('prompts.cancel') }}</UButton>
            <UButton color="blue" size="xs" :loading="isSaving" :disabled="!canSave" @click="save">
              {{ isEditing ? $t('prompts.update') : $t('prompts.create') }}
            </UButton>
          </div>
        </div>
      </template>
    </UCard>
  </UModal>
</template>

<script setup lang="ts">
import { ref, reactive, computed, watch } from 'vue'
import MentionInput from '@/components/prompt/MentionInput.vue'
import ModeSelector from '@/components/prompt/ModeSelector.vue'
import ModelSelector from '@/components/prompt/ModelSelector.vue'
import { usePrompts } from '~/composables/usePrompts'
import { usePromptFill } from '~/composables/usePromptFill'
import type { Prompt, PromptScope } from '~/composables/usePrompts'

type Audience = 'private' | 'agent' | 'global'

const props = defineProps<{
  modelValue: boolean
  prompt?: Prompt | null
  agents?: { id: string; name: string; type?: string }[]
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
  (e: 'saved', p: Prompt): void
}>()

const { t } = useI18n()
const toast = useToast()
const { createPrompt, updatePrompt } = usePrompts()
const { extractParamNames } = usePromptFill()

const isOpen = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

const isEditing = computed(() => !!props.prompt?.id)
const isSaving = ref(false)
const showAdvanced = ref(false)
const errorMsg = ref('')
const mentionGroups = ref<any[]>([])

const form = reactive({
  title: '' as string,
  text: '' as string,
  mode: 'chat' as string,
  model_id: null as string | null,
})

// Audience is a single explicit choice. 'agent' uses an explicit multiselect.
const audience = ref<Audience>('private')
// Explicitly selected agent ids (never collapses to "all"/"auto").
const agentIds = ref<string[]>([])

// Parameters are derived from the prompt text — shown read-only as chips.
const detectedParams = computed(() => extractParamNames(form.text))
function placeholder(name: string): string { return '{{' + name + '}}' }

// ── Permissions: which agents the user can manage, and full-admin gate ──
const fullAdmin = computed(() => useCan('full_admin_access'))
const manageableAgents = computed(() =>
  (props.agents || []).filter(a => useCan('update_data_source', { type: 'data_source', id: a.id })),
)

const audienceOptions = computed(() => {
  const opts: { value: Audience; label: string; icon: string }[] = [
    { value: 'private', label: t('prompts.scopePrivate'), icon: 'heroicons-lock-closed' },
  ]
  if (manageableAgents.value.length) {
    opts.push({ value: 'agent', label: t('prompts.scopeAgent'), icon: 'heroicons-cube' })
  }
  if (fullAdmin.value) {
    // 'global' is presented as "Public".
    opts.push({ value: 'global', label: t('prompts.scopePublic'), icon: 'heroicons-globe-alt' })
  }
  return opts
})

// Options for the agent multiselect (only agents the user can manage).
const agentOptions = computed(() =>
  manageableAgents.value.map(a => ({ id: a.id, name: a.name, type: a.type, icon: a.icon })),
)

// Selected agents resolved to { id, name, type, icon } for the named chips.
// Always explicit — never "Auto".
const selectedAgentChips = computed(() =>
  agentIds.value.map(id => {
    const a = (props.agents || []).find(x => x.id === id)
    return { id, name: a?.name || id, type: a?.type, icon: a?.icon }
  }),
)

function removeAgent(id: string) {
  agentIds.value = agentIds.value.filter(x => x !== id)
}

function seed() {
  const p = props.prompt
  form.title = p?.title || ''
  form.text = p?.text || ''
  form.mode = p?.mode || 'chat'
  form.model_id = p?.model_id || null
  mentionGroups.value = (p?.mentions as any[]) || []
  errorMsg.value = ''

  // Map scope → audience.
  const scope = (p?.scope as PromptScope) || 'private'
  audience.value = (scope === 'global' ? 'global' : scope === 'agent' ? 'agent' : 'private')
  // Seed explicitly selected agent ids from the prompt's data_source_ids.
  agentIds.value = [...(p?.data_source_ids || [])]

  // Fall back to an audience the user is allowed to set.
  if (!audienceOptions.value.some(o => o.value === audience.value)) {
    audience.value = 'private'
  }
}

watch(() => [props.modelValue, props.prompt?.id], ([open]) => {
  if (open) seed()
}, { immediate: true })

function onMentionsGroups(groups: any[]) {
  mentionGroups.value = groups || []
}

// Insert a {{name}} placeholder into the text. The parameter set is derived
// from the text, so there's nothing else to register.
function insertParameterPrompt() {
  const name = (window.prompt(t('prompts.insertParamPromptLabel'), 'param') || '').trim()
  if (!name) return
  const clean = name.replace(/[^\p{L}\p{N}_.-]/gu, '_')
  form.text = `${form.text || ''}{{${clean}}}`
}

// Agent ids to persist — always the explicit selection (no "auto == all").
const resolvedAgentIds = computed<string[]>(() =>
  audience.value === 'agent' ? [...agentIds.value] : [],
)

const canSave = computed(() => {
  if (!form.text.trim()) return false
  // scope=agent requires at least one explicitly selected agent (backend enforces too).
  if (audience.value === 'agent' && resolvedAgentIds.value.length === 0) return false
  return true
})

async function save() {
  if (!canSave.value || isSaving.value) return
  errorMsg.value = ''
  isSaving.value = true
  try {
    const scope: PromptScope = audience.value
    const body = {
      title: form.title.trim() || null,
      text: form.text,
      mode: form.mode,
      model_id: form.model_id || null,
      mentions: mentionGroups.value.length ? mentionGroups.value : null,
      // No parameter schema is sent — params are bare {{name}} placeholders
      // derived from the text at run time. The backend column stays dormant.
      scope,
      data_source_ids: scope === 'agent' ? resolvedAgentIds.value : [],
    }

    const saved = isEditing.value
      ? await updatePrompt(props.prompt!.id, body)
      : await createPrompt(body)

    if (saved) {
      toast.add({ title: isEditing.value ? t('prompts.toastUpdated') : t('prompts.toastCreated'), color: 'green' })
      emit('saved', saved)
      isOpen.value = false
    } else {
      errorMsg.value = t('prompts.toastSaveFailed')
    }
  } catch (e: any) {
    errorMsg.value = e?.data?.detail || t('prompts.toastSaveFailed')
  } finally {
    isSaving.value = false
  }
}
</script>
