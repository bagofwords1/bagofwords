<template>
    <div>
        <ConsoleOverview />
    </div>
</template>

<script setup lang="ts">
// Redirect to overview tab
definePageMeta({
    auth: true,
    layout: 'monitoring',
    permissions: ['view_console']
})
</script>

