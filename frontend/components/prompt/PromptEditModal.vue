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
        </section>

        <!-- Section 2: Parameters -->
        <section>
          <div class="flex items-center justify-between mb-2">
            <div class="text-[11px] font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500">
              {{ $t('prompts.sectionParameters') }}
            </div>
            <button
              type="button"
              class="inline-flex items-center gap-1 text-[11px] text-blue-500 hover:text-blue-600"
              @click="addParameter()"
            >
              <UIcon name="heroicons-plus" class="w-3 h-3" />
              {{ $t('prompts.addParam') }}
            </button>
          </div>

          <div v-if="!form.parameters.length" class="text-[11px] text-gray-400 dark:text-gray-500">
            {{ $t('prompts.noParamsYet') }}
          </div>

          <div
            v-for="(param, idx) in form.parameters"
            :key="idx"
            class="border border-gray-100 dark:border-gray-800 rounded-md p-2.5 mb-2 bg-gray-50/50 dark:bg-gray-800/30"
          >
            <div class="grid grid-cols-12 gap-2 items-center">
              <input
                v-model="param.name"
                type="text"
                :placeholder="$t('prompts.paramName')"
                class="col-span-4 text-xs border border-gray-200 dark:border-gray-700 rounded px-2 py-1.5 dark:bg-gray-900 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-500"
              />
              <input
                v-model="param.label"
                type="text"
                :placeholder="$t('prompts.paramLabel')"
                class="col-span-4 text-xs border border-gray-200 dark:border-gray-700 rounded px-2 py-1.5 dark:bg-gray-900 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-500"
              />
              <select
                v-model="param.type"
                class="col-span-3 text-xs border border-gray-200 dark:border-gray-700 rounded px-1.5 py-1.5 dark:bg-gray-900 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-500"
              >
                <option value="text">{{ $t('prompts.typeText') }}</option>
                <option value="number">{{ $t('prompts.typeNumber') }}</option>
                <option value="enum">{{ $t('prompts.typeEnum') }}</option>
                <option value="date">{{ $t('prompts.typeDate') }}</option>
                <option value="date_range">{{ $t('prompts.typeDateRange') }}</option>
              </select>
              <button
                type="button"
                class="col-span-1 flex items-center justify-center p-1 rounded text-gray-400 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20"
                :title="$t('prompts.removeParam')"
                @click="removeParameter(idx)"
              >
                <UIcon name="heroicons-x-mark" class="w-4 h-4" />
              </button>
            </div>

            <div class="flex items-center gap-3 mt-2">
              <label class="flex items-center gap-1.5 text-[11px] text-gray-500 dark:text-gray-400 cursor-pointer">
                <input v-model="param.required" type="checkbox" class="rounded border-gray-300" />
                {{ $t('prompts.paramRequired') }}
              </label>
              <input
                v-if="param.type === 'enum'"
                :value="(param.options || []).join(', ')"
                type="text"
                :placeholder="$t('prompts.paramOptions')"
                class="flex-1 text-[11px] border border-gray-200 dark:border-gray-700 rounded px-2 py-1 dark:bg-gray-900 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-500"
                @input="onOptionsInput(param, ($event.target as HTMLInputElement).value)"
              />
            </div>
          </div>
        </section>

        <!-- Section 3: Audience -->
        <section>
          <div class="text-[11px] font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500 mb-2">
            {{ $t('prompts.sectionAudience') }}
          </div>

          <div class="flex gap-1 p-0.5 bg-gray-100 dark:bg-gray-800 rounded w-fit mb-3">
            <button
              v-for="opt in scopeOptions"
              :key="opt.value"
              type="button"
              class="inline-flex items-center gap-1 px-2.5 py-1 text-[11px] rounded transition-colors"
              :class="form.scope === opt.value ? 'bg-white dark:bg-gray-900 text-gray-900 dark:text-white shadow-sm font-medium' : 'text-gray-500 hover:text-gray-700'"
              @click="form.scope = opt.value"
            >
              <UIcon :name="opt.icon" class="w-3.5 h-3.5" />
              {{ opt.label }}
            </button>
          </div>

          <!-- Agent multiselect -->
          <div v-if="form.scope === 'agent'">
            <div class="text-[11px] text-gray-500 dark:text-gray-400 mb-1.5">{{ $t('prompts.selectAgents') }}</div>
            <div v-if="!manageableAgents.length" class="text-[11px] text-gray-400">{{ $t('prompts.noManageableAgents') }}</div>
            <div v-else class="flex flex-wrap gap-1.5">
              <button
                v-for="a in manageableAgents"
                :key="a.id"
                type="button"
                class="inline-flex items-center gap-1 text-[11px] px-2 py-1 rounded border transition-colors"
                :class="form.data_source_ids.includes(a.id)
                  ? 'border-violet-300 dark:border-violet-700 bg-violet-50 dark:bg-violet-900/30 text-violet-700 dark:text-violet-300'
                  : 'border-gray-200 dark:border-gray-700 text-gray-600 dark:text-gray-300 hover:border-gray-300'"
                @click="toggleAgent(a.id)"
              >
                <UIcon name="heroicons-cube" class="w-3 h-3" />
                {{ a.name }}
              </button>
            </div>
          </div>

          <p v-if="form.scope === 'global'" class="text-[11px] text-gray-400 dark:text-gray-500">{{ $t('prompts.globalHint') }}</p>
          <p v-if="form.scope === 'private'" class="text-[11px] text-gray-400 dark:text-gray-500">{{ $t('prompts.privateHint') }}</p>
        </section>

        <!-- Section 4: Advanced (collapsed) -->
        <section>
          <button
            type="button"
            class="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-gray-400 dark:text-gray-500 mb-2"
            @click="showAdvanced = !showAdvanced"
          >
            <UIcon :name="showAdvanced ? 'heroicons-chevron-down' : 'heroicons-chevron-right'" class="w-3.5 h-3.5" />
            {{ $t('prompts.sectionAdvanced') }}
          </button>

          <div v-if="showAdvanced" class="space-y-3">
            <div>
              <div class="text-[11px] text-gray-500 dark:text-gray-400 mb-1">{{ $t('prompts.mode') }}</div>
              <select
                v-model="form.mode"
                class="text-xs border border-gray-200 dark:border-gray-700 rounded px-2 py-1.5 dark:bg-gray-900 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-500"
              >
                <option value="chat">{{ $t('prompts.modeChat') }}</option>
                <option value="deep">{{ $t('prompts.modeDeep') }}</option>
                <option value="training">{{ $t('prompts.modeTraining') }}</option>
              </select>
            </div>
            <div>
              <div class="text-[11px] text-gray-500 dark:text-gray-400 mb-1">{{ $t('prompts.model') }}</div>
              <select
                v-model="form.model_id"
                class="text-xs border border-gray-200 dark:border-gray-700 rounded px-2 py-1.5 dark:bg-gray-900 dark:text-gray-100 focus:outline-none focus:ring-1 focus:ring-blue-500 min-w-[180px]"
              >
                <option :value="null">{{ $t('prompts.modelDefault') }}</option>
                <option v-for="m in models" :key="m.id" :value="m.id">{{ m.name || m.model_id || m.id }}</option>
              </select>
            </div>
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
import { usePrompts } from '~/composables/usePrompts'
import type { Prompt, PromptScope } from '~/composables/usePrompts'
import type { PromptParameter } from '~/composables/usePromptFill'

const props = defineProps<{
  modelValue: boolean
  prompt?: Prompt | null
  agents?: { id: string; name: string }[]
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: boolean): void
  (e: 'saved', p: Prompt): void
}>()

const { t } = useI18n()
const toast = useToast()
const { createPrompt, updatePrompt } = usePrompts()

const isOpen = computed({
  get: () => props.modelValue,
  set: (v: boolean) => emit('update:modelValue', v),
})

const isEditing = computed(() => !!props.prompt?.id)
const isSaving = ref(false)
const showAdvanced = ref(false)
const errorMsg = ref('')
const mentionGroups = ref<any[]>([])

interface FormParam {
  name: string
  label: string
  type: PromptParameter['type']
  required: boolean
  options?: string[]
}

const form = reactive({
  title: '' as string,
  text: '' as string,
  mode: 'chat' as string,
  model_id: null as string | null,
  scope: 'private' as PromptScope,
  data_source_ids: [] as string[],
  parameters: [] as FormParam[],
})

// ── Permissions: which agents the user can manage, and full-admin gate ──
const fullAdmin = computed(() => useCan('full_admin_access'))

const manageableAgents = computed(() =>
  (props.agents || []).filter(a => useCan('update_data_source', { type: 'data_source', id: a.id })),
)

const scopeOptions = computed(() => {
  const opts: { value: PromptScope; label: string; icon: string }[] = [
    { value: 'private', label: t('prompts.scopePrivate'), icon: 'heroicons-lock-closed' },
  ]
  if (manageableAgents.value.length) {
    opts.push({ value: 'agent', label: t('prompts.scopeAgent'), icon: 'heroicons-cube' })
  }
  if (fullAdmin.value) {
    opts.push({ value: 'global', label: t('prompts.scopeGlobal'), icon: 'heroicons-globe-alt' })
  }
  return opts
})

// ── Models for the Advanced section ──
const models = ref<any[]>([])
async function loadModels() {
  if (models.value.length) return
  try {
    const { data } = await useMyFetch('/api/llm/models?is_enabled=true')
    if (Array.isArray(data.value)) models.value = data.value as any[]
  } catch {}
}

function seed() {
  const p = props.prompt
  form.title = p?.title || ''
  form.text = p?.text || ''
  form.mode = p?.mode || 'chat'
  form.model_id = p?.model_id || null
  form.scope = (p?.scope as PromptScope) || 'private'
  form.data_source_ids = [...(p?.data_source_ids || [])]
  form.parameters = (p?.parameters || []).map(pp => ({
    name: pp.name || '',
    label: pp.label || '',
    type: pp.type || 'text',
    required: !!pp.required,
    options: pp.options ? [...pp.options] : [],
  }))
  mentionGroups.value = (p?.mentions as any[]) || []
  errorMsg.value = ''
  // Fall back to a scope the user is allowed to set.
  if (!scopeOptions.value.some(o => o.value === form.scope)) {
    form.scope = 'private'
  }
}

watch(() => [props.modelValue, props.prompt?.id], ([open]) => {
  if (open) {
    seed()
    loadModels()
  }
}, { immediate: true })

function onMentionsGroups(groups: any[]) {
  mentionGroups.value = groups || []
}

// ── Parameters management ──
function addParameter() {
  form.parameters.push({ name: '', label: '', type: 'text', required: false, options: [] })
}
function removeParameter(idx: number) {
  form.parameters.splice(idx, 1)
}
function onOptionsInput(param: FormParam, raw: string) {
  param.options = raw.split(',').map(s => s.trim()).filter(Boolean)
}

// Insert a {{name}} placeholder into the text and register it as a parameter.
function insertParameterPrompt() {
  const name = (window.prompt(t('prompts.insertParamPromptLabel'), 'param') || '').trim()
  if (!name) return
  const clean = name.replace(/[^\w.-]/g, '_')
  form.text = `${form.text || ''}{{${clean}}}`
  if (!form.parameters.some(p => p.name === clean)) {
    form.parameters.push({ name: clean, label: '', type: 'text', required: false, options: [] })
  }
}

function toggleAgent(id: string) {
  const i = form.data_source_ids.indexOf(id)
  if (i === -1) form.data_source_ids.push(id)
  else form.data_source_ids.splice(i, 1)
}

const canSave = computed(() => {
  if (!form.text.trim()) return false
  if (form.scope === 'agent' && form.data_source_ids.length === 0) return false
  return true
})

async function save() {
  if (!canSave.value || isSaving.value) return
  errorMsg.value = ''
  isSaving.value = true
  try {
    const body = {
      title: form.title.trim() || null,
      text: form.text,
      mode: form.mode,
      model_id: form.model_id || null,
      mentions: mentionGroups.value.length ? mentionGroups.value : null,
      parameters: form.parameters
        .filter(p => p.name.trim())
        .map(p => ({
          name: p.name.trim(),
          label: p.label.trim() || undefined,
          type: p.type,
          required: p.required,
          options: p.type === 'enum' ? (p.options || []) : undefined,
        })),
      scope: form.scope,
      data_source_ids: form.scope === 'agent' ? form.data_source_ids : [],
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
