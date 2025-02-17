<template>
    <UNotifications />
    <slot />
</template>

<script setup lang="ts">
const { signIn, signOut, token, data: currentUser, status, lastRefreshedAt, getSession } = useAuth()
const { $intercom } = useNuxtApp()
const { environment, intercom } = useRuntimeConfig().public

if (environment === 'production' && intercom) {
  $intercom.boot()
}

onMounted(async () => {
  await getSession({ force: true })
})

</script>