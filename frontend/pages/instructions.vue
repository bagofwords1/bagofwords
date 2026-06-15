<template>
    <div />
</template>

<script setup lang="ts">
// The instructions page has moved to the Agents explorer at /agents.
// Keep this route as a redirect so existing links/bookmarks still work.
definePageMeta({
    auth: true,
    layout: 'default'
})

await navigateTo('/agents', { replace: true })
</script>
