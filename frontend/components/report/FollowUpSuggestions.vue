<template>
  <div v-if="items.length > 0" class="mt-4 max-w-chat">
    <!-- Header -->
    <div class="px-1 pb-1 text-sm font-medium text-gray-700 dark:text-gray-300">
      {{ t('reportView.followUp') }}
    </div>

    <!-- Suggestions list (hairline dividers between rows) -->
    <ul class="divide-y divide-gray-100 dark:divide-gray-800/70">
      <li v-for="(q, idx) in items" :key="idx" class="group">
        <button
          type="button"
          :disabled="disabled"
          @click="select(q)"
          class="w-full flex items-center justify-between gap-3 py-2.5 px-1 text-start text-sm leading-snug text-gray-500 dark:text-gray-400 transition-colors duration-150 hover:text-gray-900 dark:hover:text-gray-100 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <span class="truncate" dir="auto">{{ q }}</span>
          <!-- The hover slide lives on the wrapper: a translate on the icon
               itself would replace its RTL mirror transform. -->
          <span class="flex-shrink-0 inline-flex opacity-0 -translate-x-1 rtl:translate-x-1 group-hover:opacity-100 group-hover:translate-x-0 transition duration-150">
            <Icon
              name="heroicons-arrow-up-right"
              class="w-3.5 h-3.5 text-gray-300 dark:text-gray-600"
            />
          </span>
        </button>
      </li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

const props = defineProps<{
  suggestions?: string[] | null
  disabled?: boolean
}>()

const emit = defineEmits<{
  select: [question: string]
}>()

const { t } = useI18n()

// The section (heading, alignment, arrow) follows the UI locale like the rest
// of the chat chrome. Suggestions are generated in the conversation's language,
// so each one keeps its own text direction via dir="auto" on its label.
const items = computed(() =>
  (props.suggestions || []).map((s) => String(s ?? '').trim()).filter(Boolean)
)

function select(q: string) {
  if (props.disabled) return
  emit('select', q)
}
</script>
