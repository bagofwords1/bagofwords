<template>
  <div class="w-[320px] text-xs" data-testid="model-picker" @keydown="onKeydown">
    <!-- Search -->
    <div class="p-2 border-b border-gray-100 dark:border-gray-800">
      <div class="flex items-center gap-1.5 px-2 py-1.5 rounded-md bg-gray-50 dark:bg-gray-800/60">
        <Icon name="heroicons-magnifying-glass" class="w-3.5 h-3.5 text-gray-400 flex-shrink-0" />
        <input
          ref="searchRef"
          v-model="query"
          type="text"
          data-testid="model-picker-search"
          class="flex-1 min-w-0 bg-transparent outline-none text-xs text-gray-800 dark:text-gray-100 placeholder-gray-400"
          :placeholder="$t('prompt.effort.searchModels')"
          @input="highlighted = 0"
        />
      </div>
    </div>

    <!-- Model list, grouped by provider -->
    <div ref="listRef" class="max-h-72 overflow-y-auto p-1">
      <template v-if="showAuto && !query">
        <button type="button" class="picker-row" :class="rowClass(false, props.modelValue === AUTO)" @click="pick(AUTO)">
          <Icon name="heroicons-sparkles" class="w-4 h-4 text-gray-400 me-2 flex-shrink-0" />
          <span class="flex flex-col flex-1 text-start min-w-0">
            <span class="font-medium">{{ $t('prompt.modelAuto') }}</span>
            <span class="text-gray-500 dark:text-gray-400 text-[10px] truncate">{{ $t('prompt.modelAutoHint') }}</span>
          </span>
          <Icon v-if="props.modelValue === AUTO" name="heroicons-check" class="w-4 h-4 text-blue-500 ms-2 flex-shrink-0" />
        </button>
        <div class="my-1 border-t border-gray-100 dark:border-gray-800" />
      </template>
      <template v-if="showDefault && !query">
        <button type="button" class="picker-row" :class="rowClass(false, !props.modelValue)" @click="pick(null)">
          <Icon name="heroicons-sparkles" class="w-4 h-4 text-gray-400 me-2 flex-shrink-0" />
          <span class="flex flex-col flex-1 text-start min-w-0">
            <span class="font-medium">{{ defaultLabel || $t('prompts.modelDefault') }}</span>
            <span v-if="defaultHint" class="text-gray-500 dark:text-gray-400 text-[10px] truncate">{{ defaultHint }}</span>
          </span>
          <Icon v-if="!props.modelValue" name="heroicons-check" class="w-4 h-4 text-blue-500 ms-2 flex-shrink-0" />
        </button>
        <div class="my-1 border-t border-gray-100 dark:border-gray-800" />
      </template>

      <div v-if="!flat.length" class="px-3 py-4 text-center text-gray-400">{{ $t('prompt.effort.noMatches') }}</div>

      <template v-for="group in groups" :key="group.name">
        <div class="px-2 pt-2 pb-1 text-[10px] font-semibold uppercase tracking-wide text-gray-400">{{ group.name }}</div>
        <div
          v-for="m in group.models"
          :key="m.id"
          :data-picker-index="indexOf(m)"
          role="option"
          :aria-selected="props.modelValue === m.id"
          class="picker-row group"
          :class="rowClass(indexOf(m) === highlighted, props.modelValue === m.id)"
          :data-testid="`model-option-${m.model_id}`"
          @mouseenter="highlighted = indexOf(m)"
          @click="pick(m.id)"
        >
          <LLMProviderIcon :provider="m.provider?.provider_type || 'default'" :model="`${m.name || ''} ${m.model_id || ''}`" :icon="true" class="w-4 h-4 me-2 flex-shrink-0" />
          <span class="flex-1 min-w-0 truncate font-medium text-start" :title="m.model_id">{{ m.name }}</span>
          <!-- One-click model + effort. Visible for the hovered and the selected row. -->
          <span
            v-if="m.reasoning?.supported"
            class="ms-2 flex items-center gap-0.5 flex-shrink-0"
            :class="props.modelValue === m.id || indexOf(m) === highlighted ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'"
          >
            <button
              v-for="lvl in LEVELS"
              :key="lvl"
              type="button"
              class="px-1 py-0.5 rounded text-[10px] leading-none border transition-colors"
              :class="props.modelValue === m.id && props.effort === lvl
                ? 'bg-blue-500 border-blue-500 text-white'
                : 'border-gray-200 dark:border-gray-700 text-gray-500 dark:text-gray-400 hover:border-blue-400 hover:text-blue-600'"
              :title="effortTitle(m, lvl)"
              :data-testid="`model-effort-${m.model_id}-${lvl}`"
              @click.stop="pick(m.id, lvl)"
            >{{ $t(`prompt.effort.short.${lvl}`) }}</button>
          </span>
          <Icon v-if="props.modelValue === m.id" name="heroicons-check" class="w-4 h-4 text-blue-500 ms-1.5 flex-shrink-0" />
        </div>
      </template>
    </div>

    <!-- Effort for the selected model -->
    <div class="border-t border-gray-100 dark:border-gray-800 p-2" data-testid="effort-bar">
      <div class="flex items-center justify-between mb-1.5">
        <span class="font-medium text-gray-700 dark:text-gray-200">{{ $t('prompt.effort.title') }}</span>
        <span class="text-[10px] text-gray-400 truncate ms-2">{{ effortHint }}</span>
      </div>
      <div class="flex gap-0.5 p-0.5 rounded-md bg-gray-100 dark:bg-gray-800" :class="{ 'opacity-50': effortDisabled }">
        <button
          v-for="opt in effortOptions"
          :key="opt.value || 'default'"
          type="button"
          :disabled="effortDisabled && !!opt.value"
          class="flex-auto px-1.5 py-1 rounded text-[11px] whitespace-nowrap transition-colors"
          :class="(props.effort || null) === opt.value
            ? 'bg-white dark:bg-gray-900 shadow-sm text-gray-900 dark:text-white font-medium'
            : 'text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-white disabled:cursor-not-allowed'"
          :data-testid="`effort-${opt.value || 'default'}`"
          @click="setEffort(opt.value)"
        >{{ opt.label }}</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, nextTick, watch } from 'vue'
import LLMProviderIcon from '@/components/LLMProviderIcon.vue'

// The model list with search, provider groups and effort. Consumers own the
// trigger (each surface styles its own button) and render this in the panel.
const props = withDefaults(defineProps<{
  models: any[]
  modelValue: string | null
  effort: string | null
  showAuto?: boolean
  showDefault?: boolean
  defaultLabel?: string
  defaultHint?: string
}>(), { showAuto: false, showDefault: false, defaultLabel: '', defaultHint: '' })

const emit = defineEmits<{
  (e: 'update:modelValue', v: string | null): void
  (e: 'update:effort', v: string | null): void
  (e: 'close'): void
}>()

const { t } = useI18n()
const AUTO = 'auto'
const LEVELS = ['low', 'medium', 'high', 'max'] as const

const query = ref('')
const highlighted = ref(0)
const searchRef = ref<HTMLInputElement | null>(null)
const listRef = ref<HTMLElement | null>(null)

onMounted(() => {
  nextTick(() => searchRef.value?.focus())
  const i = flat.value.findIndex(m => m.id === props.modelValue)
  if (i >= 0) {
    highlighted.value = i
    nextTick(() => scrollToHighlighted())
  }
})

const filtered = computed(() => {
  const q = query.value.trim().toLowerCase()
  const list = props.models || []
  if (!q) return list
  return list.filter(m =>
    [m.name, m.model_id, m.provider?.name, m.provider?.provider_type]
      .some(v => String(v || '').toLowerCase().includes(q))
  )
})

// Providers sorted by name; models keep the API order inside a provider.
const groups = computed(() => {
  const by = new Map<string, any[]>()
  for (const m of filtered.value) {
    const key = m.provider?.name || t('prompt.effort.otherProvider')
    if (!by.has(key)) by.set(key, [])
    by.get(key)!.push(m)
  }
  return [...by.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([name, models]) => ({ name, models }))
})
const flat = computed(() => groups.value.flatMap(g => g.models))
function indexOf(m: any) { return flat.value.indexOf(m) }

watch(query, () => { highlighted.value = 0 })

function rowClass(isHighlighted: boolean, isSelected: boolean) {
  return [
    isHighlighted ? 'bg-gray-100 dark:bg-gray-800/70' : '',
    isSelected ? 'text-gray-900 dark:text-white' : 'text-gray-700 dark:text-gray-300',
  ]
}

const selectedModel = computed(() => (props.models || []).find(m => m.id === props.modelValue) || null)
// Effort applies to whichever model runs when the pick is Auto/Default.
const effortDisabled = computed(() => !!selectedModel.value && !selectedModel.value.reasoning?.supported)

const effortOptions = computed(() => [
  { value: null, label: t('prompt.effort.default') },
  ...LEVELS.map(l => ({ value: l, label: t(`prompt.effort.levels.${l}`) })),
])

const effortHint = computed(() => {
  const m = selectedModel.value
  if (!m) return t('prompt.effort.anyModel')
  if (!m.reasoning?.supported) return t('prompt.effort.notSupported')
  if (!props.effort) {
    const d = m.reasoning?.default
    return d ? t('prompt.effort.modelDefault', { level: t(`prompt.effort.levels.${d}`) }) : t('prompt.effort.providerDefault')
  }
  const runsAs = m.reasoning?.levels?.[props.effort]
  if (runsAs && runsAs !== props.effort) return t('prompt.effort.runsAs', { level: runsAs })
  return ''
})

function effortTitle(m: any, lvl: string) {
  const runsAs = m.reasoning?.levels?.[lvl]
  const label = t(`prompt.effort.levels.${lvl}`)
  return runsAs && runsAs !== lvl ? `${label} — ${t('prompt.effort.runsAs', { level: runsAs })}` : label
}

function pick(id: string | null, lvl?: string) {
  emit('update:modelValue', id)
  if (lvl !== undefined) emit('update:effort', lvl)
  emit('close')
}

function setEffort(v: string | null) {
  if (effortDisabled.value && v) return
  emit('update:effort', v)
}

function scrollToHighlighted() {
  const el = listRef.value?.querySelector(`[data-picker-index="${highlighted.value}"]`) as HTMLElement | null
  el?.scrollIntoView({ block: 'nearest' })
}

function onKeydown(e: KeyboardEvent) {
  const n = flat.value.length
  if (!n) return
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    highlighted.value = (highlighted.value + 1) % n
    scrollToHighlighted()
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    highlighted.value = (highlighted.value - 1 + n) % n
    scrollToHighlighted()
  } else if (e.key === 'Enter') {
    const m = flat.value[highlighted.value]
    if (m) {
      e.preventDefault()
      pick(m.id)
    }
  }
}
</script>

<style scoped>
.picker-row {
  @apply w-full px-2 py-1.5 rounded cursor-pointer flex items-center;
}
</style>
