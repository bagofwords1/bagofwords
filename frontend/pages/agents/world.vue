<template><WorldView /></template>
<script setup lang="ts">
import WorldView from '~/components/world/WorldView.vue'
definePageMeta({ auth: true, layout: 'default', middleware: async () => {
  const { data } = await useMyFetch<any>('/api/organization/settings')
  if (data.value?.config?.general?.show_world !== true) return navigateTo('/agents', { replace: true })
} })
</script>
