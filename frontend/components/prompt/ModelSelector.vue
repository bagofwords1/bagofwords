<template>
  <UPopover :popper="popper">
    <UTooltip :text="selectedLabel" :popper="{ strategy: 'fixed', placement: 'top' }">
      <button
        type="button"
        class="text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white hover:bg-gray-50 dark:hover:bg-gray-800/50 rounded-md px-2 py-1 text-xs flex items-center max-w-[200px] border border-gray-200 dark:border-gray-700"
      >
        <Icon name="heroicons-cpu-chip" class="w-4 h-4 flex-shrink-0" />
        <span class="ms-1 truncate">{{ selectedLabel }}</span>
        <span v-if="effort" class="ms-1 px-1 rounded bg-blue-50 dark:bg-blue-900/30 text-blue-600 dark:text-blue-300 text-[10px] leading-4 flex-shrink-0">{{ $t(`prompt.effort.levels.${effort}`) }}</span>
      </button>
    </UTooltip>
    <template #panel="{ close }">
      <ModelPickerPanel
        :models="models"
        :model-value="modelValue"
        :effort="effort"
        :show-default="true"
        :default-label="$t('prompts.modelDefault')"
        :default-hint="routingOn ? $t('prompts.modelDefaultAuto') : ''"
        @update:model-value="select"
        @update:effort="(v) => emit('update:effort', v)"
        @close="close"
      />
    </template>
  </UPopover>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import ModelPickerPanel from '@/components/prompt/ModelPickerPanel.vue'

const props = withDefaults(defineProps<{
  modelValue: string | null
  // Reasoning level paired with the model (v-model:effort); null = Default.
  effort?: string | null
}>(), { effort: null })

const emit = defineEmits<{
  (e: 'update:modelValue', v: string | null): void
  (e: 'update:effort', v: string | null): void
}>()

const { t } = useI18n()

const popper = { strategy: 'absolute' as const, placement: 'bottom-start' as const, offset: [0, 8] }

// Same LLM list endpoint PromptBoxV2 uses.
const models = ref<any[]>([])
async function loadModels() {
  try {
    const { data } = await useMyFetch('/api/llm/models?is_enabled=true')
    // Exclude image-generation models (e.g. gpt-image-2.5-sunburst) — they aren't chat models.
    if (Array.isArray(data.value)) {
      models.value = (data.value as any[]).filter(m => !m?.supports_image_generation)
    }
  } catch {}
}

// When the org's Auto router is on, "Default" also routes by difficulty.
const routingOn = ref(false)
async function loadRouting() {
  try {
    const { data } = await useMyFetch('/api/organization/settings')
    routingOn.value = !!(data.value as any)?.config?.model_routing?.value
  } catch {}
}

onMounted(() => { loadModels(); loadRouting() })

const selectedLabel = computed(() => {
  if (!props.modelValue) return t('prompts.modelDefault')
  const m = models.value.find(x => x.id === props.modelValue)
  return m?.name || t('prompts.modelDefault')
})

function select(id: string | null) {
  emit('update:modelValue', id)
}
</script>
