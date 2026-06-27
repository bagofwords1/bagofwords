<template>
  <UPopover :popper="popper">
    <UTooltip :text="selectedLabel" :popper="{ strategy: 'fixed', placement: 'top' }">
      <button
        type="button"
        class="text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white hover:bg-gray-50 dark:hover:bg-gray-800/50 rounded-md px-2 py-1 text-xs flex items-center max-w-[200px] border border-gray-200 dark:border-gray-700"
      >
        <Icon name="heroicons-cpu-chip" class="w-4 h-4 flex-shrink-0" />
        <span class="ms-1 truncate">{{ selectedLabel }}</span>
      </button>
    </UTooltip>
    <template #panel="{ close }">
      <div class="p-2 text-xs max-h-64 overflow-y-auto w-[220px]">
        <!-- Default (let the system pick) -->
        <div
          class="px-2 py-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800/70 cursor-pointer flex items-center"
          @click="() => { select(null); close(); }"
        >
          <div class="me-2"><Icon name="heroicons-sparkles" class="w-4 h-4 text-gray-400" /></div>
          <div class="flex-1 text-start">{{ $t('prompts.modelDefault') }}</div>
          <Icon v-if="!modelValue" name="heroicons-check" class="w-4 h-4 text-blue-500 ms-2 flex-shrink-0" />
        </div>
        <div class="my-1 border-t border-gray-100 dark:border-gray-800" />
        <div
          v-for="m in models"
          :key="m.id"
          class="px-2 py-1 rounded hover:bg-gray-100 dark:hover:bg-gray-800/70 cursor-pointer flex items-center"
          @click="() => { select(m.id); close(); }"
        >
          <div class="me-2">
            <LLMProviderIcon :provider="m.provider?.provider_type || 'default'" :icon="true" class="w-4 h-4" />
          </div>
          <div class="flex flex-col flex-1 text-start min-w-0">
            <span class="font-medium truncate" :title="m.name">{{ m.name }}</span>
            <span class="text-gray-500 dark:text-gray-400 text-[10px] truncate">{{ m.provider?.name }}</span>
          </div>
          <Icon v-if="modelValue === m.id" name="heroicons-check" class="w-4 h-4 text-blue-500 ms-2 flex-shrink-0" />
        </div>
      </div>
    </template>
  </UPopover>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import LLMProviderIcon from '@/components/LLMProviderIcon.vue'

const props = defineProps<{
  modelValue: string | null
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', v: string | null): void
}>()

const { t } = useI18n()

const popper = { strategy: 'absolute' as const, placement: 'bottom-start' as const, offset: [0, 8] }

// Same LLM list endpoint PromptBoxV2 uses.
const models = ref<any[]>([])
async function loadModels() {
  try {
    const { data } = await useMyFetch('/api/llm/models?is_enabled=true')
    if (Array.isArray(data.value)) models.value = data.value as any[]
  } catch {}
}
onMounted(loadModels)

const selectedLabel = computed(() => {
  if (!props.modelValue) return t('prompts.modelDefault')
  const m = models.value.find(x => x.id === props.modelValue)
  return m?.name || t('prompts.modelDefault')
})

function select(id: string | null) {
  emit('update:modelValue', id)
}
</script>
