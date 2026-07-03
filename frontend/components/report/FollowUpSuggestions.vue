<template>
  <div v-if="items.length > 0" class="mt-4 max-w-2xl" :dir="contentDir">
    <!-- Header -->
    <div class="px-1 pb-1 text-sm font-medium text-gray-700 dark:text-gray-300">
      {{ heading }}
    </div>

    <!-- Suggestions list (hairline dividers between rows, OpenWebUI-style) -->
    <ul class="divide-y divide-gray-100 dark:divide-gray-800/70">
      <li v-for="(q, idx) in items" :key="idx" class="group">
        <button
          type="button"
          dir="auto"
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
import { useI18n } from 'vue-i18n'

const props = defineProps<{
  suggestions?: string[] | null
  disabled?: boolean
}>()

const emit = defineEmits<{
  select: [question: string]
}>()

const { t } = useI18n()

const items = computed(() =>
  (props.suggestions || []).map((s) => String(s ?? '').trim()).filter(Boolean)
)

/**
 * Follow-ups are generated in the conversation's language, which can differ
 * from the UI locale (English UI, Hebrew conversation). Like the answer text
 * above (dir="auto"), the section must follow the CONTENT: detect the script
 * of the first strong character across the suggestions, set the section's
 * `dir` from it (the existing `[dir="rtl"]` CSS then also flips the arrow
 * icon), and translate the heading when the script maps unambiguously to a
 * supported locale. Latin can't be told apart (en/es/fr/…), so it keeps the
 * UI locale; no strong character keeps the document direction.
 */
const SCRIPTS: Array<{ re: RegExp; dir: 'rtl' | 'ltr'; locale?: string }> = [
  { re: /[\u0590-\u05FF\uFB1D-\uFB4F]/, dir: 'rtl', locale: 'he' }, // Hebrew
  { re: /[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFC]/, dir: 'rtl', locale: 'ar' }, // Arabic
  { re: /[\u0400-\u04FF]/, dir: 'ltr', locale: 'ru' }, // Cyrillic
  { re: /[A-Za-z\u00C0-\u024F]/, dir: 'ltr' }, // Latin
]

const contentScript = computed(() => {
  for (const ch of items.value.join(' ')) {
    const script = SCRIPTS.find((s) => s.re.test(ch))
    if (script) return script
  }
  return null
})

const contentDir = computed(() => contentScript.value?.dir)

const heading = computed(() => {
  const locale = contentScript.value?.locale
  return locale ? t('reportView.followUp', {}, { locale }) : t('reportView.followUp')
})

function select(q: string) {
  if (props.disabled) return
  emit('select', q)
}
</script>
