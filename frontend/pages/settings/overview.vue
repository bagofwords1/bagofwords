<script setup lang="ts">
definePageMeta({
    permissions: ['manage_settings'],
})
</script>

<template>
    <div>
        <h2 class="text-xl font-semibold mb-4">Console Overview</h2>
        <ConsoleOverview />
    </div>
</template>