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
    // Visible to anyone with `manage` on at least one data source. The page's
    // data is filtered to those data sources on the backend.
    resourcePermissionAny: { permission: 'manage', resourceType: 'data_source' }
})
</script>

