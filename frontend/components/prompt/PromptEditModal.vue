<template>
  <UModal v-model="isOpen" :ui="{ width: 'sm:max-w-2xl' }">
    <UCard :ui="{ body: { padding: 'px-5 py-4 sm:p-5' }, header: { padding: 'px-5 py-3 sm:px-5 sm:py-3' }, footer: { padding: 'px-5 py-3 sm:px-5 sm:py-3' } }">
      <template #header>
        <div class="flex items-center justify-between">
          <h3 class="text-sm font-semibold text-gray-900">{{ isEditing ? 'Edit prompt' : 'New prompt' }}</h3>
          <UButton color="gray" variant="ghost" icon="i-heroicons-x-mark-20-solid" size="xs" @click="isOpen = false" />
        </div>
      </template>

      <div class="space-y-4 max-h-[70vh] overflow-y-auto pr-1">
        <!-- Title -->
        <div>
          <div class="text-xs text-gray-500 mb-1.5">Title</div>
          <input v-model="form.title" type="text" placeholder="Weekly revenue summary" class="w-full rounded border border-gray-200 px-2 py-1.5 text-sm" />
        </div>

        <!-- Text -->
        <div>
          <div class="text-xs text-gray-500 mb-1.5">Prompt text</div>
          <textarea v-model="form.text" rows="4" placeholder="Summarize this week's revenue by region…" class="w-full rounded border border-gray-200 px-2 py-1.5 text-sm resize-y" />
        </div>

        <div class="grid grid-cols-2 gap-4">
          <!-- Mode -->
          <div>
            <div class="text-xs text-gray-500 mb-1.5">Mode</div>
            <select v-model="form.mode" class="w-full rounded border border-gray-200 px-2 py-1.5 text-sm">
              <option value="chat">Chat</option>
              <option value="deep">Deep</option>
              <option value="training">Training</option>
            </select>
          </div>
          <!-- Model -->
          <div>
            <div class="text-xs text-gray-500 mb-1.5">Model</div>
            <select v-model="form.model_id" class="w-full rounded border border-gray-200 px-2 py-1.5 text-sm">
              <option :value="null">Default</option>
              <option v-for="m in models" :key="m.id" :value="m.id">{{ m.name }}</option>
            </select>
          </div>
        </div>

        <!-- Agents / data sources multi-select -->
        <div>
          <div class="text-xs text-gray-500 mb-1.5">Agents (data sources)</div>
          <div class="flex flex-wrap gap-1.5 border border-gray-200 rounded px-2 py-2 min-h-[40px]">
            <button
              v-for="ds in dataSources"
              :key="ds.id"
              type="button"
              class="inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded-full border transition-colors"
              :class="form.data_source_ids.includes(ds.id) ? 'bg-blue-50 border-blue-300 text-blue-700' : 'bg-white border-gray-200 text-gray-600 hover:border-gray-300'"
              @click="toggleDataSource(ds.id)"
            >
              <DataSourceIcon v-if="ds.type" :type="ds.type" class="w-3 h-3" />
              {{ ds.name }}
            </button>
            <span v-if="!dataSources.length" class="text-[11px] text-gray-400">No accessible data sources.</span>
          </div>
        </div>

        <div class="grid grid-cols-2 gap-4">
          <!-- Scope -->
          <div>
            <div class="text-xs text-gray-500 mb-1.5">Scope</div>
            <select v-model="form.scope" class="w-full rounded border border-gray-200 px-2 py-1.5 text-sm">
              <option value="private">Private</option>
              <option value="agent">Agent</option>
            </select>
          </div>
          <!-- Status -->
          <div>
            <div class="text-xs text-gray-500 mb-1.5">Status</div>
            <select v-model="form.status" class="w-full rounded border border-gray-200 px-2 py-1.5 text-sm">
              <option value="draft">Draft</option>
              <option value="published">Published</option>
            </select>
          </div>
        </div>

        <div class="grid grid-cols-2 gap-4">
          <!-- Category -->
          <div>
            <div class="text-xs text-gray-500 mb-1.5">Category</div>
            <input v-model="form.category" type="text" placeholder="Finance" class="w-full rounded border border-gray-200 px-2 py-1.5 text-sm" />
          </div>
          <!-- Tags -->
          <div>
            <div class="text-xs text-gray-500 mb-1.5">Tags (comma separated)</div>
            <input v-model="tagsInput" type="text" placeholder="revenue, weekly" class="w-full rounded border border-gray-200 px-2 py-1.5 text-sm" />
          </div>
        </div>

        <!-- Defaults -->
        <PromptSchedulePicker v-model="defaultCron" />
        <p class="text-[11px] text-gray-400 -mt-2">Default schedule offered when users subscribe.</p>

        <div>
          <div class="text-xs text-gray-500 mb-1.5">Default channel</div>
          <select v-model="form.default_channel" class="w-full rounded border border-gray-200 px-2 py-1.5 text-sm">
            <option :value="null">None</option>
            <option v-for="c in PROMPT_CHANNELS" :key="c.value" :value="c.value">{{ c.label }}</option>
          </select>
        </div>

        <!-- Is starter -->
        <label class="flex items-center gap-2 cursor-pointer select-none">
          <UCheckbox v-model="form.is_starter" />
          <span class="text-xs text-gray-600">Show as a conversation starter</span>
        </label>
      </div>

      <template #footer>
        <div class="flex justify-end gap-2">
          <UButton color="gray" variant="ghost" size="xs" @click="isOpen = false">Cancel</UButton>
          <UButton color="blue" size="xs" :loading="isSaving" :disabled="!form.title || !form.text" @click="submit">{{ isEditing ? 'Save' : 'Create' }}</UButton>
        </div>
      </template>
    </UCard>
  </UModal>
</template>

<script setup lang="ts">
import DataSourceIcon from '@/components/DataSourceIcon.vue'
import PromptSchedulePicker from '@/components/prompt/PromptSchedulePicker.vue'
import { PROMPT_CHANNELS, type PromptChannel, type PromptMode, type PromptScope, type PromptStatus, type PromptResponse, type PromptUpsertPayload } from '@/composables/usePrompts'

const props = defineProps<{ prompt?: PromptResponse | null }>()
const emit = defineEmits(['saved'])

const isOpen = defineModel<boolean>({ default: false })
const toast = useToast()
const { createPrompt, updatePrompt } = usePrompts()

const isEditing = computed(() => !!props.prompt?.id)
const isSaving = ref(false)

const models = ref<{ id: string; name: string; is_default?: boolean }[]>([])
const dataSources = ref<{ id: string; name: string; type?: string }[]>([])

const form = reactive<{
  title: string
  text: string
  mode: PromptMode
  model_id: string | null
  scope: PromptScope
  is_starter: boolean
  status: PromptStatus
  default_channel: PromptChannel | null
  category: string
  data_source_ids: string[]
}>({
  title: '',
  text: '',
  mode: 'chat',
  model_id: null,
  scope: 'private',
  is_starter: false,
  status: 'draft',
  default_channel: null,
  category: '',
  data_source_ids: [],
})

const tagsInput = ref('')
const defaultCron = ref('0 8 * * *')

function resetFromProp() {
  const p = props.prompt
  form.title = p?.title || ''
  form.text = p?.text || ''
  form.mode = p?.mode || 'chat'
  form.model_id = p?.model_id ?? null
  form.scope = p?.scope || 'private'
  form.is_starter = p?.is_starter ?? false
  form.status = p?.status || 'draft'
  form.default_channel = (p?.default_channel as PromptChannel) ?? null
  form.category = p?.category || ''
  form.data_source_ids = [...(p?.data_source_ids || [])]
  tagsInput.value = (p?.tags || []).join(', ')
  defaultCron.value = p?.default_cron || '0 8 * * *'
}

watch(isOpen, (open) => {
  if (open) {
    resetFromProp()
    loadOptions()
  }
})

function toggleDataSource(id: string) {
  const idx = form.data_source_ids.indexOf(id)
  if (idx === -1) form.data_source_ids.push(id)
  else form.data_source_ids.splice(idx, 1)
}

async function loadOptions() {
  try {
    const mRes = await useMyFetch('/api/llm/models?is_enabled=true')
    if (mRes.data.value && Array.isArray(mRes.data.value)) models.value = mRes.data.value as any[]
  } catch {}
  try {
    const dRes = await useMyFetch('/data_sources/active', { query: { include_unconnected: true } })
    if (dRes.data.value && Array.isArray(dRes.data.value)) {
      dataSources.value = (dRes.data.value as any[]).map((d) => ({ id: d.id, name: d.name, type: d.type }))
    }
  } catch {}
}

async function submit() {
  isSaving.value = true
  try {
    const tags = tagsInput.value.split(',').map((t) => t.trim()).filter(Boolean)
    const body: PromptUpsertPayload = {
      title: form.title,
      text: form.text,
      mode: form.mode,
      model_id: form.model_id,
      scope: form.scope,
      is_starter: form.is_starter,
      status: form.status,
      default_cron: defaultCron.value,
      default_channel: form.default_channel,
      category: form.category || null,
      tags,
      data_source_ids: form.data_source_ids,
    }
    if (isEditing.value && props.prompt) {
      await updatePrompt(props.prompt.id, body)
    } else {
      await createPrompt(body)
    }
    toast.add({ title: isEditing.value ? 'Prompt updated' : 'Prompt created', color: 'green' })
    isOpen.value = false
    emit('saved')
  } catch (e: any) {
    toast.add({ title: 'Failed to save prompt', description: e?.data?.detail || e?.message, color: 'red' })
  } finally {
    isSaving.value = false
  }
}
</script>
