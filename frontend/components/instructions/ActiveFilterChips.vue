<template>
  <div v-if="chips.length" class="flex items-center gap-1.5 flex-wrap">
    <span
      v-for="c in chips" :key="c.group + c.value"
      class="inline-flex items-center gap-1 h-6 ps-2 pe-1 rounded-md bg-gray-100 dark:bg-gray-800 text-[11px] text-gray-700 dark:text-gray-200"
    >
      <span class="text-gray-400 dark:text-gray-500">{{ c.groupLabel }}</span>
      <span class="font-medium truncate max-w-[160px]">{{ c.label }}</span>
      <button
        type="button"
        class="w-4 h-4 rounded grid place-items-center hover:bg-gray-200 dark:hover:bg-gray-700"
        :aria-label="$t('allInstructions.removeFilter', { name: c.label })"
        @click="$emit('remove', { group: c.group, value: c.value })"
      >
        <UIcon name="i-heroicons-x-mark-20-solid" class="w-3 h-3" />
      </button>
    </span>
    <button
      v-if="chips.length > 1"
      type="button"
      class="text-[11px] text-gray-500 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200 ms-0.5"
      @click="$emit('clear')"
    >{{ $t('allInstructions.clearAll') }}</button>
  </div>
</template>

<script setup lang="ts">
/**
 * What's currently filtered, as removable chips.
 *
 * A multi-select trigger collapses to "3 agents" once there's more than one
 * selection, so without this row the only way to know what's applied is to open
 * each menu and read the checkmarks. These chips are the readable half of the
 * control.
 */
const props = defineProps<{
  groups: { group: string; groupLabel: string; items: { value: string; label: string }[] }[]
}>()
defineEmits(['remove', 'clear'])

const chips = computed(() =>
  (props.groups || []).flatMap(g =>
    g.items.map(i => ({ group: g.group, groupLabel: g.groupLabel, value: i.value, label: i.label }))
  )
)
</script>
