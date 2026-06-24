<template>
  <div class="min-h-screen flex items-center justify-center bg-gray-50 dark:bg-gray-900 p-8">
    <div class="max-w-md w-full bg-white dark:bg-gray-900 rounded-lg shadow p-8 text-center space-y-4">
      <h1 class="text-4xl font-bold" data-test="smoke-hello">{{ $t('smoke.hello') }}</h1>
      <p class="text-gray-600 dark:text-gray-400" data-test="smoke-subtitle">{{ $t('smoke.subtitle') }}</p>
      <div class="pt-4 border-t border-gray-200 dark:border-gray-700">
        <p class="text-sm text-gray-500 dark:text-gray-400 mb-2">locale: <code data-test="smoke-locale">{{ $i18n.locale }}</code></p>
        <div class="flex gap-2 justify-center" data-test="smoke-buttons">
          <button
            v-for="code in ['en', 'es', 'he', 'fr', 'sv', 'ar', 'ru', 'de', 'pt', 'it']"
            :key="code"
            class="px-3 py-1 rounded border border-gray-300 dark:border-gray-600 hover:bg-gray-100 dark:hover:bg-gray-700"
            :class="{ 'bg-blue-500 text-white border-blue-500': $i18n.locale === code }"
            @click="switchLocale(code)"
          >
            {{ code }}
          </button>
        </div>
      </div>
      <div class="pt-4 border-t border-gray-200 dark:border-gray-700 text-left text-sm text-gray-700 dark:text-gray-300 space-y-1">
        <p>{{ $t('common.loading') }}</p>
        <p>{{ $t('common.save') }} · {{ $t('common.cancel') }} · {{ $t('common.delete') }}</p>
        <p>{{ $t('nav.reports') }} · {{ $t('nav.settings') }}</p>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
const { $setLocale } = useNuxtApp()
function switchLocale(code: string) {
  ;($setLocale as (c: string) => void)(code)
}
definePageMeta({ auth: false, layout: false })
</script>
