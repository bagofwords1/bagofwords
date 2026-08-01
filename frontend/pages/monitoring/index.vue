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
    // Mirrors the /console/* gate: org admins see the org-wide console, agent
    // managers see it scoped to the agents they manage.
    anyOf: ['manage_settings', { permission: 'manage', resourceType: 'data_source' }]
})
</script>

