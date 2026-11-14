<template>
  <li :lang="props.locale || resolvedLocale" :data-response-locale="props.locale || resolvedLocale" :data-message-dir="responseDir">
    <slot :t="translate" :dir="responseDir" />
  </li>
</template>

<script setup lang="ts">
import { computed, provide, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { decideMessageDir, localeTextDir, type TextDir } from '~/utils/textDirection'
import { responseLocaleKey } from '~/composables/useResponseI18n'

interface AnswerBlock {
  content?: string | null
  status?: string | null
  phase?: string | null
  plan_decision?: { final_answer?: string | null; assistant?: string | null } | null
  tool_execution?: { tool_name?: string | null } | null
}

const props = defineProps<{
  locale?: string | null
  /** Direction of the question this message answers. */
  questionDir?: TextDir | null
  /** The message's completion blocks: their answer text votes on its direction. */
  blocks?: AnswerBlock[] | null
}>()
const global = useI18n({ useScope: 'global' })
const { $loadLocaleMessages } = useNuxtApp()
const loadedLocale = ref<string | null>(null)
const resolvedLocale = computed(() => loadedLocale.value || global.locale.value)

// The answer text the reader sees, block by block (reasoning excluded): the
// same blocks the transcript renders as markdown.
const answerTexts = computed(() => (props.blocks || [])
  .filter(b => b && b.phase !== 'knowledge_harness' && b.status !== 'error' && b.tool_execution?.tool_name !== 'clarify')
  .map(b => b.content || b.plan_decision?.final_answer || b.plan_decision?.assistant || ''))

// One direction for the whole message: its header row, every markdown block,
// table and list, and its tool cards (see decideMessageDir). Synchronous, so
// catalog loading never moves the header.
const responseDir = computed<TextDir>(() => decideMessageDir({
  texts: answerTexts.value,
  saved: props.locale ? localeTextDir(props.locale) : null,
  question: props.questionDir ?? null,
  fallback: localeTextDir(global.locale.value),
}))
provide(responseLocaleKey, resolvedLocale)
let request = 0
watch(() => props.locale, async locale => {
  const current = ++request
  loadedLocale.value = null
  if (locale && await $loadLocaleMessages(locale) && current === request) {
    loadedLocale.value = locale
  }
}, { immediate: true })
const translate = ((key: string, values?: any) =>
  global.t(key, values || {}, { locale: resolvedLocale.value })) as typeof global.t
</script>
