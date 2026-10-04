<template>
  <div ref="root" class="relative">
    <button
      type="button"
      :data-testid="testid"
      class="flex items-center gap-1.5 px-2 py-1 text-xs border rounded bg-white dark:bg-gray-900 max-w-[220px]"
      :class="hasValue ? 'border-blue-300 dark:border-blue-800 text-blue-700 dark:text-blue-300' : 'border-gray-200 dark:border-gray-700 text-gray-600 dark:text-gray-400 hover:border-gray-300'"
      :aria-expanded="open"
      @click="toggle"
    >
      <UIcon v-if="icon" :name="icon" class="w-3.5 h-3.5 shrink-0 opacity-70" />
      <span class="truncate">{{ buttonLabel }}</span>
      <UIcon name="i-heroicons-chevron-down" class="w-3 h-3 shrink-0 opacity-60" />
    </button>
    <div
      v-if="open"
      class="absolute top-full start-0 mt-1 w-64 bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700 rounded shadow-md z-20"
      @keydown.esc.stop="open = false"
    >
      <div v-if="searchable" class="p-1.5 border-b border-gray-100 dark:border-gray-800">
        <input
          ref="searchInput"
          v-model="query"
          type="text"
          :placeholder="searchPlaceholder"
          class="w-full px-2 py-1 text-xs border border-gray-200 dark:border-gray-700 rounded focus:outline-none focus:border-gray-400 bg-white dark:bg-gray-900"
        />
      </div>
      <div class="max-h-72 overflow-y-auto py-1">
        <button
          v-if="!multiple && allLabel"
          type="button"
          class="w-full text-start px-2.5 py-1.5 text-xs hover:bg-gray-50 dark:hover:bg-gray-800"
          :class="!hasValue ? 'font-medium text-gray-900 dark:text-white' : 'text-gray-600 dark:text-gray-400'"
          @click="pick(null)"
        >{{ allLabel }}</button>
        <template v-for="group in grouped" :key="group.name">
          <div v-if="group.name" class="px-2.5 pt-2 pb-0.5 text-[10px] uppercase tracking-wide text-gray-400">{{ group.name }}</div>
          <label
            v-for="opt in group.options"
            :key="opt.value"
            :data-testid="`${testid}-option`"
            class="flex items-center gap-2 px-2.5 py-1.5 text-xs cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800"
            @click.prevent="pick(opt.value)"
          >
            <input
              v-if="multiple"
              type="checkbox"
              :checked="isSelected(opt.value)"
              class="w-3 h-3 rounded border-gray-300 text-gray-900 focus:ring-0 pointer-events-none"
            />
            <span class="flex-1 min-w-0">
              <span class="block truncate" :class="isSelected(opt.value) ? 'font-medium text-gray-900 dark:text-white' : 'text-gray-700 dark:text-gray-300'">{{ opt.label }}</span>
              <span v-if="opt.sub" class="block truncate text-[10px] text-gray-400">{{ opt.sub }}</span>
            </span>
            <UIcon v-if="!multiple && isSelected(opt.value)" name="i-heroicons-check" class="w-3.5 h-3.5 text-blue-600" />
          </label>
        </template>
        <div v-if="filtered.length === 0" class="px-2.5 py-2 text-xs text-gray-400">{{ emptyLabel }}</div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import type { FilterOption } from '~/utils/auditActionFormat'

const props = withDefaults(defineProps<{
  modelValue: string | string[] | null
  options: FilterOption[]
  label: string
  allLabel?: string
  multiple?: boolean
  searchable?: boolean
  searchPlaceholder?: string
  emptyLabel?: string
  icon?: string
  testid?: string
}>(), { multiple: false, searchable: true, testid: 'filter' })

const emit = defineEmits<{ (e: 'update:modelValue', v: string | string[] | null): void }>()

const open = ref(false)
const query = ref('')
const root = ref<HTMLElement | null>(null)
const searchInput = ref<HTMLInputElement | null>(null)

const selected = computed<string[]>(() =>
  Array.isArray(props.modelValue) ? props.modelValue : props.modelValue ? [props.modelValue] : [],
)
const hasValue = computed(() => selected.value.length > 0)
const isSelected = (v: string) => selected.value.includes(v)

const buttonLabel = computed(() => {
  if (!hasValue.value) return props.label
  if (selected.value.length === 1) {
    return props.options.find((o) => o.value === selected.value[0])?.label || selected.value[0]
  }
  return `${props.label} · ${selected.value.length}`
})

const filtered = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return props.options
  return props.options.filter((o) => o.label.toLowerCase().includes(q) || o.value.toLowerCase().includes(q) || (o.sub || '').toLowerCase().includes(q))
})

const grouped = computed(() => {
  const groups = new Map<string, FilterOption[]>()
  for (const o of filtered.value) {
    const g = o.group || ''
    if (!groups.has(g)) groups.set(g, [])
    groups.get(g)!.push(o)
  }
  return [...groups.entries()].map(([name, options]) => ({ name, options }))
})

function pick(v: string | null) {
  if (props.multiple) {
    if (v === null) return emit('update:modelValue', [])
    const next = isSelected(v) ? selected.value.filter((x) => x !== v) : [...selected.value, v]
    emit('update:modelValue', next)
  } else {
    emit('update:modelValue', v)
    open.value = false
  }
}

async function toggle() {
  open.value = !open.value
  if (open.value) {
    query.value = ''
    await nextTick()
    searchInput.value?.focus()
  }
}

const onDocClick = (e: MouseEvent) => {
  if (root.value && !root.value.contains(e.target as Node)) open.value = false
}
const onKey = (e: KeyboardEvent) => {
  if (e.key === 'Escape') open.value = false
}
onMounted(() => {
  document.addEventListener('click', onDocClick)
  document.addEventListener('keydown', onKey)
})
onUnmounted(() => {
  document.removeEventListener('click', onDocClick)
  document.removeEventListener('keydown', onKey)
})
</script>
