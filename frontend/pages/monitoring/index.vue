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
    // Keep in step with useCanAccessMonitoring() — the sidebar entry and the tab
    // strip use that predicate, and a page that guards on less would let a user
    // click an entry that bounces them straight back to '/'.
    anyOf: ['manage_settings', 'manage_connections', { permission: 'manage', resourceType: 'data_source' }]
})
</script>

