<template>
  <UPopover :popper="{ placement: 'bottom-start' }" :ui="{ ring: '', shadow: 'shadow-lg' }">
    <button
      type="button"
      :class="[
        'inline-flex items-center gap-1.5 h-8 ps-2.5 pe-2 rounded-lg border text-xs whitespace-nowrap transition-colors',
        selected.length
          ? 'border-indigo-200 dark:border-indigo-500/40 bg-indigo-50 dark:bg-indigo-950/40 text-gray-900 dark:text-white'
          : 'border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800/50'
      ]"
    >
      <span>{{ triggerLabel }}</span>
      <!-- Clearing is the most common correction, so it's one click from the
           trigger rather than buried in the panel. -->
      <span
        v-if="selected.length"
        role="button"
        :aria-label="$t('allInstructions.clearFilter')"
        class="ms-0.5 -me-0.5 w-4 h-4 rounded grid place-items-center hover:bg-indigo-100 dark:hover:bg-indigo-900/60"
        @click.stop="clear"
      >
        <UIcon name="i-heroicons-x-mark-20-solid" class="w-3 h-3" />
      </span>
      <UIcon v-else name="i-heroicons-chevron-down-20-solid" class="w-3 h-3 opacity-60" />
    </button>

    <template #panel>
      <div class="w-64 p-1.5">
        <!-- Search appears only when scanning the list is real work. -->
        <div v-if="options.length > SEARCH_THRESHOLD" class="px-1 pb-1.5">
          <UInput
            v-model="query" size="xs" autofocus
            icon="i-heroicons-magnifying-glass-20-solid"
            :placeholder="$t('allInstructions.filterSearch')"
          />
        </div>

        <div class="max-h-64 overflow-auto">
          <button
            v-for="o in filtered" :key="o.value"
            type="button"
            class="w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-start text-xs hover:bg-gray-50 dark:hover:bg-gray-800/60"
            @click="toggle(o.value)"
          >
            <span
              :class="[
                'w-3.5 h-3.5 rounded border flex items-center justify-center flex-shrink-0',
                selected.includes(o.value)
                  ? 'bg-indigo-600 border-indigo-600'
                  : 'border-gray-300 dark:border-gray-600'
              ]"
            >
              <UIcon v-if="selected.includes(o.value)" name="i-heroicons-check-20-solid" class="w-3 h-3 text-white" />
            </span>
            <span class="truncate text-gray-700 dark:text-gray-200">{{ o.label }}</span>
          </button>
          <div v-if="filtered.length === 0" class="px-2 py-3 text-center text-[11px] text-gray-400 italic">
            {{ $t('allInstructions.filterNoMatch') }}
          </div>
        </div>

        <div
          v-if="selected.length"
          class="flex items-center justify-between pt-1.5 mt-1 border-t border-gray-100 dark:border-gray-800 px-1"
        >
          <span class="text-[11px] text-gray-400 dark:text-gray-500 tabular-nums">
            {{ $t('allInstructions.nSelected', { n: selected.length }) }}
          </span>
          <button
            class="text-[11px] text-gray-500 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200"
            @click="clear"
          >{{ $t('allInstructions.clear') }}</button>
        </div>
      </div>
    </template>
  </UPopover>
</template>

<script setup lang="ts">
/**
 * A filter that can hold several values at once.
 *
 * The trigger can't show every selection once there is more than one, so it
 * summarises ("3 agents") and the caller renders the actual selections as
 * removable chips beneath the bar. A multi-select whose state you can only
 * read by opening it is a control people stop trusting.
 */
const props = defineProps<{
  modelValue: string[]
  options: { value: string; label: string }[]
  /** Shown when nothing is selected, e.g. "All agents". */
  emptyLabel: string
  /** Pluralised noun for the summary, e.g. "agents". */
  noun: string
}>()
const emit = defineEmits(['update:modelValue'])

const SEARCH_THRESHOLD = 8
const query = ref('')

const selected = computed(() => props.modelValue || [])

const filtered = computed(() => {
  const q = query.value.toLowerCase().trim()
  if (!q) return props.options
  return props.options.filter(o => o.label.toLowerCase().includes(q))
})

const triggerLabel = computed(() => {
  if (selected.value.length === 0) return props.emptyLabel
  if (selected.value.length === 1) {
    const one = props.options.find(o => o.value === selected.value[0])
    return one?.label || props.emptyLabel
  }
  return `${selected.value.length} ${props.noun}`
})

const toggle = (v: string) => {
  const cur = [...selected.value]
  const i = cur.indexOf(v)
  if (i >= 0) cur.splice(i, 1)
  else cur.push(v)
  emit('update:modelValue', cur)
}
const clear = () => emit('update:modelValue', [])
</script>
