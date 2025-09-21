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

if (environment === 'production' && intercom) {
  $intercom.boot()
}

onMounted(async () => {
  // If redirected with an access_token in query, let the target page set the token first
  if (route.query.access_token) {
    return
  }
  await getSession({ force: true })
})

</script>