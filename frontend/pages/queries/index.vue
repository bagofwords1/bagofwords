<template>
  <!-- The queries catalog moved into the /agents knowledge tree, where each
       query hangs under the agents it reads. Kept as a redirect so old links,
       bookmarks and muscle memory still land somewhere useful. -->
  <div />
</template>

<script setup lang="ts">
definePageMeta({ auth: true })

onMounted(() => navigateTo('/agents', { replace: true }))
</script>
