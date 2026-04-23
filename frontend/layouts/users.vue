<template>
  <div class="bg-gray-50">
    <UNotifications />
    <slot />
  </div>
</template>

<script setup lang="ts">
const { signIn, signOut, token, data: currentUser, status, lastRefreshedAt, getSession } = useAuth()
const { $intercom } = useNuxtApp()
const { environment, intercom } = useRuntimeConfig().public
const route = useRoute()
const { locale: i18nLocale } = useI18n({ useScope: 'global' })
const RTL_LOCALES = new Set(['he', 'ar', 'fa', 'ur'])
const intercomAlignment = computed<'left' | 'right'>(() =>
  RTL_LOCALES.has(i18nLocale.value) ? 'left' : 'right'
)

if (environment === 'production' && intercom) {
  $intercom.boot({ alignment: intercomAlignment.value })
  watch(intercomAlignment, (alignment) => {
    $intercom.update({ alignment })
  })
}

onMounted(async () => {
  // If redirected with an access_token in query, let the target page set the token first
  if (route.query.access_token) {
    return
  }
  await getSession({ force: true })
})

</script>