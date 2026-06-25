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
    // Admin-only: matches the `manage_settings` gate on the /console/* endpoints
    // this page calls.
    permissions: ['manage_settings']
})
</script>

