<template>
  <div class="p-3 bg-white">
    <!-- Instructions -->
    <div class="mb-2">
      <button
        class="text-gray-500 hover:text-gray-900 hover:bg-gray-50 rounded-md p-1 text-xs flex items-center"
        @click="openInstructions"
      >
        <Icon name="heroicons-cube" class="w-4 h-4 mr-1" />
        Instructions
      </button>
    </div>

    <!-- Prompt -->
    <div class="border border-gray-200 rounded-xl bg-white focus-within:border-gray-300 transition-colors">
      <div class="p-3">
        <MentionInput
          v-model="text"
          @update:mentions="handleMentionsUpdate"
          :placeholder="placeholder"
          :rows="6"
          :selectedDataSourceIds="selectedDataSources.map(ds => ds.id)"
        />
      </div>
    </div>

    <!-- Controls (verbose: each on its own line) -->
    <div class="mt-1 space-y-0.5">
      <!-- Data source selector -->
      <div class="flex items-center gap-2">
        <div class="text-[11px] text-gray-500 flex-none w-28 whitespace-nowrap">Data sources</div>
        <div class="flex-1 min-w-[460px] flex items-center min-h-[32px]">
          <DataSourceSelector v-model:selectedDataSources="selectedDataSources" :reportId="report_id" />
        </div>
      </div>

      <!-- Model selector -->
      <div class="flex items-center gap-2">
        <div class="text-[11px] text-gray-500 flex-none w-28 whitespace-nowrap">LLM</div>
        <div class="flex-1">
          <UPopover :popper="popperLegacy">
            <UTooltip :text="selectedModelLabel" :popper="{ strategy: 'fixed', placement: 'bottom-start' }">
              <button class="text-gray-600 hover:text-gray-900 hover:bg-gray-50 rounded-md px-2 text-xs flex items-center border border-gray-200 h-8">
                <Icon name="heroicons-cpu-chip" class="w-4 h-4" />
                <span class="ml-1 truncate max-w-[260px] text-left">{{ selectedModelLabel }}</span>
              </button>
            </UTooltip>
            <template #panel="{ close }">
              <div class="p-2 text-xs max-h-64 overflow-y-auto w-[260px]">
                <div
                  v-for="m in models"
                  :key="m.id || m.model_id"
                  class="px-2 py-1 rounded hover:bg-gray-100 cursor-pointer flex items-center"
                  @click="() => { selectModel(m); close(); }"
                >
                  <div class="mr-2">
                    <LLMProviderIcon :provider="m.provider?.provider_type || 'default'" :icon="true" class="w-4 h-4" />
                  </div>
                  <div class="flex flex-col flex-1 text-left min-w-0">
                    <span class="font-medium truncate">{{ m.name || m.model_id }}</span>
                    <span class="text-gray-500 text-[10px] truncate">{{ m.provider?.name || m.provider_name || '' }}</span>
                  </div>
                  <Icon v-if="selectedModelId === (m.id || m.model_id)" name="heroicons-check" class="w-4 h-4 text-blue-500 ml-2 flex-shrink-0" />
                </div>
              </div>
            </template>
          </UPopover>
        </div>
      </div>

      <!-- File upload -->
      <div class="flex items-center gap-2">
        <div class="text-[11px] text-gray-500 flex-none w-28 whitespace-nowrap">Files</div>
        <div class="flex-1 flex items-center min-h-[32px]">
          <FileUploadComponent :report_id="report_id" @update:uploadedFiles="onFilesUploaded" />
        </div>
      </div>
    </div>

    <!-- Modals -->
    <InstructionsListModalComponent ref="instructionsListModalRef" />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from 'vue'
import MentionInput from '@/components/prompt/MentionInput.vue'
import DataSourceSelector from '@/components/prompt/DataSourceSelector.vue'
import InstructionsListModalComponent from '@/components/InstructionsListModalComponent.vue'
import FileUploadComponent from '@/components/FileUploadComponent.vue'
import LLMProviderIcon from '@/components/LLMProviderIcon.vue'

const props = defineProps({
  report_id: { type: String, default: '' },
  textareaContent: { type: String, default: '' },
  // Allow parent to control initial/ongoing selection for edit flows
  selectedDataSources: { type: Array, default: () => [] },
})

const emit = defineEmits([
  'update:modelValue',
  'update:selectedDataSources',
  'update:selectedModelId',
  'update:uploadedFiles',
  'update:mentions'
])

const placeholder = 'Ask for data, dashboard or a deep analysis'
const text = ref<string>('')
const selectedDataSources = ref<any[]>([])
const uploadedFiles = ref<any[]>([])
const models = ref<any[]>([])
const selectedModelId = ref<string>('')

// Legacy popper used across app for consistent placement
const popperLegacy = computed(() => ({ strategy: 'absolute' as const, placement: 'bottom-start' as const, offset: [ 0, 8 ] }))

const selectedModelLabel = computed(() => {
  const m = models.value.find(x => (x.id || x.model_id) === selectedModelId.value)
  return m?.name || m?.model_id || 'Select Model'
})

function handleMentionsUpdate(mentions: any[]) {
  emit('update:mentions', mentions)
}

function openInstructions() {
  instructionsListModalRef.value?.openModal?.()
}
const instructionsListModalRef = ref<any | null>(null)

function onFilesUploaded(files: any[]) {
  uploadedFiles.value = files || []
  emit('update:uploadedFiles', uploadedFiles.value)
}

function selectModel(m: any) {
  selectedModelId.value = m?.id || m?.model_id || ''
  emit('update:selectedModelId', selectedModelId.value)
}

async function loadModels() {
  try {
    const { data } = await useMyFetch('/api/llm/models?is_enabled=true')
    const list = (data as any)?.value || []
    models.value = list
    // Prefer regular default, then small default, then first
    const regular = list.find((m: any) => m.is_default)
    const small = list.find((m: any) => m.is_small_default)
    const pick = regular || small || list[0]
    if (pick) selectModel(pick)
  } catch {
    models.value = []
  }
}

onMounted(async () => {
  await loadModels()
  if (typeof props.textareaContent === 'string') {
    text.value = props.textareaContent
  }
  // Initialize selection from parent if provided (edit flow)
  if (Array.isArray(props.selectedDataSources) && props.selectedDataSources.length) {
    selectedDataSources.value = props.selectedDataSources as any[]
  }
})

watch(() => props.textareaContent, (v) => {
  if (typeof v === 'string' && v !== text.value) text.value = v
})

// Keep internal data source selection synced with parent during edit
watch(() => props.selectedDataSources, (v: any[]) => {
  if (!Array.isArray(v)) return
  // Avoid unnecessary churn if identical by ids
  const currIds = new Set((selectedDataSources.value || []).map((x: any) => x.id))
  const nextIds = new Set((v || []).map((x: any) => x.id))
  const sameSize = currIds.size === nextIds.size
  const same = sameSize && [...currIds].every(id => nextIds.has(id))
  if (!same) selectedDataSources.value = v as any[]
}, { deep: true })

watch(text, (v) => emit('update:modelValue', v))
watch(selectedDataSources, (v) => emit('update:selectedDataSources', v), { deep: true })
</script>

<style scoped>
</style>


