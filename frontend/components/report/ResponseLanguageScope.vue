<template>
  <li :lang="props.locale || resolvedLocale" :data-response-locale="props.locale || resolvedLocale">
    <slot :t="translate" :dir="responseDir" />
  </li>
</template>

<script setup lang="ts">
import { computed, provide, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { localeTextDir, type TextDir } from '~/utils/textDirection'
import { responseLocaleKey } from '~/composables/useResponseI18n'

const props = defineProps<{ locale?: string | null; fallbackDir?: TextDir | null }>()
const global = useI18n({ useScope: 'global' })
const { $loadLocaleMessages } = useNuxtApp()
const loadedLocale = ref<string | null>(null)
const resolvedLocale = computed(() => loadedLocale.value || global.locale.value)
// Direction is synchronous; catalog loading must never move the header.
const responseDir = computed(() => props.locale
  ? localeTextDir(props.locale)
  : props.fallbackDir || localeTextDir(global.locale.value))
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
