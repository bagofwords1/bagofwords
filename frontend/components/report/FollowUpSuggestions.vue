<template>
  <div v-if="items.length > 0" class="mt-3 max-w-2xl">
    <!-- Header -->
    <div class="flex items-center gap-1.5 px-1 pb-1 text-gray-400 dark:text-gray-500">
      <Icon name="heroicons-sparkles" class="w-3.5 h-3.5" />
      <span class="text-xs font-medium tracking-wide">{{ $t('reportView.followUp') }}</span>
    </div>

    <!-- Suggestions list (hairline dividers, OpenWebUI-style) -->
    <ul class="border-t border-gray-100 dark:border-gray-800">
      <li
        v-for="(q, idx) in items"
        :key="idx"
        class="group border-b border-gray-100 dark:border-gray-800"
      >
        <button
          type="button"
          :disabled="disabled"
          @click="select(q)"
          class="w-full flex items-center justify-between gap-2 py-2 px-1 text-start text-sm text-gray-500 dark:text-gray-400 transition-colors duration-150 hover:text-gray-900 dark:hover:text-gray-100 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <span class="truncate">{{ q }}</span>
          <Icon
            name="heroicons-plus"
            class="w-4 h-4 flex-shrink-0 text-gray-300 dark:text-gray-600 opacity-0 group-hover:opacity-100 transition-opacity duration-150"
          />
        </button>
      </li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{
  suggestions?: string[] | null
  disabled?: boolean
}>()

const emit = defineEmits<{
  select: [question: string]
}>()

const items = computed(() =>
  (props.suggestions || []).map((s) => String(s ?? '').trim()).filter(Boolean)
)

function select(q: string) {
  if (props.disabled) return
  emit('select', q)
}
</script>
