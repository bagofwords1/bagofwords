<template>
  <div class="min-h-screen bg-white dark:bg-gray-950">
    <UNotifications />
    <!-- Product mark, out of the content flow so every auth page shares one
         anchor and the form stays optically centered. -->
    <NuxtLink to="/" class="fixed top-5 start-6 z-10 flex items-center gap-2">
      <img src="/assets/logo-128.png" alt="Bag of words" class="h-7 w-7" />
    </NuxtLink>
    <slot />
  </div>
</template>
<script setup lang="ts">
const { signIn, signOut, token, data: currentUser, status, lastRefreshedAt, getSession } = useAuth()
const { $intercom } = useNuxtApp()
const { environment, intercom } = useRuntimeConfig().public
const { isMobile } = useMobile()
const route = useRoute()
const { locale: i18nLocale } = useI18n({ useScope: 'global' })
const RTL_LOCALES = new Set(['he', 'ar', 'fa', 'ur'])
const intercomAlignment = computed<'left' | 'right'>(() =>
  RTL_LOCALES.has(i18nLocale.value) ? 'left' : 'right'
)

if (environment === 'production' && intercom) {
  $intercom.boot({
    hide_default_launcher: isMobile.value,
    alignment: intercomAlignment.value
  })
  watch(intercomAlignment, (alignment) => {
    $intercom.update({ alignment })
  })
  watch(isMobile, (hide) => {
    $intercom.update({ hide_default_launcher: hide })
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