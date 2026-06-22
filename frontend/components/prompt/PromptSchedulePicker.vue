<template>
  <div>
    <div class="text-xs text-gray-500 mb-1.5">Schedule</div>

    <!-- Presets -->
    <div class="flex gap-0.5 p-0.5 bg-gray-100 rounded w-fit mb-2">
      <button
        v-for="p in presets"
        :key="p.value"
        type="button"
        class="px-2 py-0.5 text-[11px] rounded transition-colors"
        :class="activePreset === p.value ? 'bg-white text-gray-900 shadow-sm font-medium' : 'text-gray-400 hover:text-gray-600'"
        @click="selectPreset(p.value)"
      >
        {{ p.label }}
      </button>
    </div>

    <!-- Cron string input -->
    <div class="flex items-center gap-2">
      <input
        v-model="cronModel"
        type="text"
        spellcheck="false"
        placeholder="0 8 * * *"
        class="flex-1 rounded border border-gray-200 px-2 py-1.5 text-xs font-mono"
        @input="activePreset = 'custom'"
      />
    </div>
    <p class="mt-1 text-[11px] text-gray-400">
      {{ cronLabel || 'Standard 5-field cron expression.' }}
    </p>
  </div>
</template>

<script setup lang="ts">
const cronModel = defineModel<string>({ default: '0 8 * * *' })

const { getCronLabel } = useCronLabel()

const presets = [
  { value: 'daily', label: 'Daily', cron: '0 8 * * *' },
  { value: 'weekly', label: 'Weekly', cron: '0 8 * * 1' },
  { value: 'weekdays', label: 'Weekdays', cron: '0 8 * * 1-5' },
  { value: 'monthly', label: 'Monthly', cron: '0 8 1 * *' },
  { value: 'custom', label: 'Custom', cron: '' },
] as const

type PresetValue = typeof presets[number]['value']

const activePreset = ref<PresetValue>('custom')

const cronLabel = computed(() => getCronLabel(cronModel.value))

function selectPreset(value: PresetValue) {
  activePreset.value = value
  const preset = presets.find((p) => p.value === value)
  if (preset && preset.cron) cronModel.value = preset.cron
}

// On first mount, detect if the incoming cron matches a known preset.
onMounted(() => {
  const match = presets.find((p) => p.cron && p.cron === cronModel.value)
  activePreset.value = match ? match.value : 'custom'
})
</script>
