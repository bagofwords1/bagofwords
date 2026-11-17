<template>
  <div />
</template>

<script setup lang="ts">
// Scheduled tasks moved into the Automations page (Scheduled tab).
// Keep the old URL working via redirect.
definePageMeta({ titleKey: 'nav.automations', auth: true })
const router = useRouter()
onMounted(() => {
  router.replace({ path: '/automations', query: { tab: 'scheduled' } })
})
</script>
