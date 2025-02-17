<template>
    <div>
        <UNotifications />
        <slot />
    </div>
</template>

<script setup lang="ts">
  const { signIn, signOut, token, data: currentUser, status, lastRefreshedAt, getSession } = useAuth()
  const { organization } = useOrganization()

</script>