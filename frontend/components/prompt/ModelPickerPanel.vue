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

    <!-- Model list (sorted by provider) -->
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

      <div
        v-for="m in flat"
        :key="m.id"
        :data-picker-index="indexOf(m)"
        role="option"
        :aria-selected="props.modelValue === m.id"
        class="picker-row"
        :class="rowClass(indexOf(m) === highlighted, props.modelValue === m.id)"
        :data-testid="`model-option-${m.model_id}`"
        @mouseenter="highlighted = indexOf(m)"
        @click="pick(m.id)"
      >
        <LLMProviderIcon :provider="m.provider?.provider_type || 'default'" :model="`${m.name || ''} ${m.model_id || ''}`" :icon="true" class="w-4 h-4 me-2 flex-shrink-0" />
        <span class="flex flex-col flex-1 min-w-0 text-start">
          <span class="font-medium truncate" :title="m.model_id">{{ m.name }}</span>
          <span class="text-gray-500 dark:text-gray-400 text-[10px] truncate">{{ m.provider?.name }}</span>
        </span>
        <Icon v-if="props.modelValue === m.id" name="heroicons-check" class="w-4 h-4 text-blue-500 ms-2 flex-shrink-0" />
      </div>
    </div>

    <!-- Effort for the selected model: a Faster ↔ Smarter slider -->
    <div class="border-t border-gray-100 dark:border-gray-800 px-3 pt-2.5 pb-2" data-testid="effort-bar">
      <div class="flex items-baseline justify-between gap-2">
        <span class="flex items-baseline gap-1.5 min-w-0">
          <span class="text-gray-500 dark:text-gray-400">{{ $t('prompt.effort.title') }}</span>
          <span class="font-medium text-gray-900 dark:text-white" data-testid="effort-current">{{ currentLabel }}</span>
        </span>
        <span v-if="effortHint" class="text-[10px] text-gray-400 truncate" data-testid="effort-hint">{{ effortHint }}</span>
      </div>
      <div class="flex justify-between mt-2 mb-1 text-[10px] text-gray-400 dark:text-gray-500">
        <span>{{ $t('prompt.effort.faster') }}</span>
        <span>{{ $t('prompt.effort.smarter') }}</span>
      </div>
      <div
        ref="trackRef"
        role="slider"
        :tabindex="effortDisabled ? -1 : 0"
        :aria-label="$t('prompt.effort.title')"
        aria-valuemin="0"
        :aria-valuemax="STOPS.length - 1"
        :aria-valuenow="currentIndex"
        :aria-valuetext="currentLabel"
        :aria-disabled="effortDisabled"
        class="effort-track relative h-6 rounded-full bg-gray-100 dark:bg-gray-800 select-none touch-none outline-none focus-visible:ring-2 focus-visible:ring-blue-500/40"
        :class="effortDisabled ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'"
        data-testid="effort-slider"
        @pointerdown="onPointerDown"
        @keydown="onSliderKey"
      >
        <div class="effort-fill absolute inset-y-0 start-0 rounded-full bg-gray-300/70 dark:bg-gray-700" :style="{ width: fillWidth }" />
        <span
          v-for="(stop, i) in STOPS"
          :key="stop"
          class="absolute top-1/2 w-1 h-1 -mt-0.5 -ms-0.5 rounded-full"
          :class="i <= currentIndex ? 'bg-gray-500 dark:bg-gray-400' : 'bg-gray-300 dark:bg-gray-600'"
          :style="{ insetInlineStart: stopPos(i) }"
          :data-testid="`effort-${stop}`"
        />
        <span
          class="effort-thumb absolute top-0.5 w-5 h-5 -ms-2.5 rounded-full bg-white dark:bg-gray-200 shadow ring-1 ring-black/5"
          :style="{ insetInlineStart: stopPos(currentIndex) }"
        />
      </div>
      <div class="relative h-4 mt-1 text-[10px] text-gray-400 dark:text-gray-500">
        <span class="absolute top-0 w-0 flex" :class="recommendedAlign" :style="recommendedStyle">
          <span class="whitespace-nowrap" data-testid="effort-recommended">{{ $t('prompt.effort.recommended') }}</span>
        </span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, nextTick, watch } from 'vue'
import LLMProviderIcon from '@/components/LLMProviderIcon.vue'

// The model list with search and an effort bar. Consumers own the
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

// Sorted by provider name; models keep the API order inside a provider.
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

// Weakest to strongest. "off" is Fast: no reasoning where the model can skip
// it, its lightest level where it cannot (the backend decides which).
const STOPS = ['off', 'medium', 'high', 'max'] as const
type Stop = typeof STOPS[number]
// Saved efforts outside the stops (older "low" picks) sit at the nearest one.
const NEAREST: Record<string, Stop> = { none: 'off', minimal: 'off', low: 'off', xhigh: 'max' }
function stopIndex(effort: string | null | undefined) {
  if (!effort) return -1
  const s = (STOPS as readonly string[]).includes(effort) ? effort : NEAREST[effort]
  return s ? STOPS.indexOf(s as Stop) : -1
}

// No explicit pick runs the admin's default for the model, else Fast.
const recommendedIndex = computed(() => {
  const i = stopIndex(selectedModel.value?.reasoning?.default)
  return i >= 0 ? i : 0
})
const currentIndex = computed(() => {
  const i = stopIndex(props.effort)
  return i >= 0 ? i : recommendedIndex.value
})
const currentLabel = computed(() => {
  const e = props.effort && !STOPS.includes(props.effort as Stop) ? props.effort : STOPS[currentIndex.value]
  return t(`prompt.effort.levels.${e}`)
})

// Stop centres sit a half-thumb (12px) in from each end of the track.
function stopPos(i: number) {
  return `calc(12px + (100% - 24px) * ${i / (STOPS.length - 1)})`
}
const fillWidth = computed(() => `calc(24px + (100% - 24px) * ${currentIndex.value / (STOPS.length - 1)})`)
// The label centres under its stop; at either end it hugs the edge instead.
const recommendedStyle = computed(() => {
  const i = recommendedIndex.value
  if (i === 0) return { insetInlineStart: '0' }
  if (i === STOPS.length - 1) return { insetInlineEnd: '0' }
  return { insetInlineStart: stopPos(i) }
})
const recommendedAlign = computed(() => {
  const i = recommendedIndex.value
  return i === 0 ? 'justify-start' : i === STOPS.length - 1 ? 'justify-end' : 'justify-center'
})

const effortHint = computed(() => {
  const m = selectedModel.value
  if (!m) return t('prompt.effort.anyModel')
  if (!m.reasoning?.supported) return t('prompt.effort.notSupported')
  const level = STOPS[currentIndex.value]
  const runsAs = level === 'off' ? null : m.reasoning?.levels?.[level]
  if (runsAs && runsAs !== level) return t('prompt.effort.runsAs', { level: runsAs })
  return ''
})

function setStop(i: number) {
  if (effortDisabled.value) return
  const clamped = Math.max(0, Math.min(STOPS.length - 1, i))
  // The recommended stop means "no explicit choice", so the admin default
  // keeps applying and the trigger shows no badge.
  emit('update:effort', clamped === recommendedIndex.value ? null : STOPS[clamped])
}

const trackRef = ref<HTMLElement | null>(null)
function indexAt(clientX: number) {
  const el = trackRef.value
  if (!el) return currentIndex.value
  const r = el.getBoundingClientRect()
  const rtl = getComputedStyle(el).direction === 'rtl'
  const x = rtl ? r.right - clientX : clientX - r.left
  const f = (x - 12) / Math.max(1, r.width - 24)
  return Math.round(Math.max(0, Math.min(1, f)) * (STOPS.length - 1))
}
function onPointerDown(e: PointerEvent) {
  if (effortDisabled.value) return
  const el = trackRef.value
  el?.setPointerCapture?.(e.pointerId)
  setStop(indexAt(e.clientX))
  const move = (ev: PointerEvent) => {
    const i = indexAt(ev.clientX)
    if (i !== currentIndex.value) setStop(i)
  }
  const up = () => {
    el?.removeEventListener('pointermove', move)
    el?.removeEventListener('pointerup', up)
    el?.removeEventListener('pointercancel', up)
  }
  el?.addEventListener('pointermove', move)
  el?.addEventListener('pointerup', up)
  el?.addEventListener('pointercancel', up)
}
function onSliderKey(e: KeyboardEvent) {
  const rtl = trackRef.value ? getComputedStyle(trackRef.value).direction === 'rtl' : false
  const step = { ArrowRight: rtl ? -1 : 1, ArrowLeft: rtl ? 1 : -1, ArrowUp: 1, ArrowDown: -1 } as Record<string, number>
  if (e.key in step) setStop(currentIndex.value + step[e.key])
  else if (e.key === 'Home') setStop(0)
  else if (e.key === 'End') setStop(STOPS.length - 1)
  else return
  e.preventDefault()
  e.stopPropagation()
}

function pick(id: string | null) {
  emit('update:modelValue', id)
  emit('close')
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
.effort-thumb,
.effort-fill {
  transition: inset-inline-start 180ms cubic-bezier(.2, .8, .2, 1), width 180ms cubic-bezier(.2, .8, .2, 1);
}
@media (prefers-reduced-motion: reduce) {
  .effort-thumb,
  .effort-fill { transition: none; }
}
</style>
