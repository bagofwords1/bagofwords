<template>
  <div v-if="items.length > 0" class="mt-4 max-w-2xl">
    <!-- Header -->
    <div class="px-1 pb-0.5 text-[11px] font-medium uppercase tracking-wider text-gray-400 dark:text-gray-500">
      {{ $t('reportView.followUp') }}
    </div>

    <!-- Suggestions list (hairline dividers between rows, OpenWebUI-style) -->
    <ul class="divide-y divide-gray-100 dark:divide-gray-800/70">
      <li v-for="(q, idx) in items" :key="idx" class="group">
        <button
          type="button"
          :disabled="disabled"
          @click="select(q)"
          class="w-full flex items-center justify-between gap-3 py-2.5 px-1 text-start text-[13px] leading-snug text-gray-500 dark:text-gray-400 transition-colors duration-150 hover:text-gray-900 dark:hover:text-gray-100 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <span class="truncate">{{ q }}</span>
          <Icon
            name="heroicons-arrow-up-right"
            class="w-3.5 h-3.5 flex-shrink-0 text-gray-300 dark:text-gray-600 opacity-0 -translate-x-1 group-hover:opacity-100 group-hover:translate-x-0 transition-all duration-150"
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
